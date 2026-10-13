"""Compare controlled worker input batches without publishing records or answers."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import threading
import time

from clickhouse_jev.protocol import build_request, parse_input
from clickhouse_jev.transport import Config, HttpTransport
from clickhouse_jev.worker import evaluate_block

ARMS = {"baseline_b20": 20, "b1": 1, "b5": 5,
        "reverse_b20": 20, "repeat_b20": 20}


class BenchmarkError(ValueError):
    """An error containing a fixed public code, never input or remote text."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_dataset(dataset):
    try:
        if not isinstance(dataset, dict) or set(dataset) != {"question", "options", "records"}:
            raise ValueError()
        question, options, records = (dataset[key] for key in ("question", "options", "records"))
        if not isinstance(question, str) or not question.strip():
            raise ValueError()
        if (not isinstance(options, list) or len(options) < 2
                or any(not isinstance(option, str) or not option.strip() for option in options)
                or len(set(options)) != len(options)):
            raise ValueError()
        if not isinstance(records, list) or not records:
            raise ValueError()
        seen = set()
        for record in records:
            if (not isinstance(record, dict) or not {"id", "row"} <= set(record)
                    or set(record) - {"id", "row", "expected"}
                    or not isinstance(record["id"], str) or not record["id"]
                    or record["id"] in seen or not isinstance(record["row"], dict)
                    or ("expected" in record and record["expected"] not in options)):
                raise ValueError()
            seen.add(record["id"])
        _canonical(dataset)
    except Exception:
        raise BenchmarkError("benchmark_dataset_invalid") from None
    return dataset


def load_dataset(path):
    """Read an explicitly supplied JSON file; no copying, logging, or publishing."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        with Path(path).open(encoding="utf-8") as stream:
            dataset = json.load(stream, object_pairs_hook=unique_object)
        return _validate_dataset(dataset)
    except Exception:
        raise BenchmarkError("benchmark_dataset_invalid") from None


def generate_dataset(count=1000, seed=1729):
    """Create repeatable synthetic records, including duplicate content with unique IDs."""
    if type(count) is not int or count < 1 or type(seed) is not int:
        raise BenchmarkError("benchmark_generation_invalid")
    fixture = load_dataset(Path(__file__).with_name("synthetic.json"))
    rng = random.Random(seed)
    records = []
    for index in range(count):
        template = rng.choice(fixture["records"])
        records.append({"id": "synthetic-%06d" % index,
                        "row": dict(template["row"]), "expected": template["expected"]})
    return {"question": fixture["question"], "options": fixture["options"], "records": records}


def plan_batches(dataset):
    """Preserve baseline batch membership when reversing, including the last short batch."""
    _validate_dataset(dataset)
    records = dataset["records"]
    return {name: [list(reversed(records[start:start + size])) if name == "reverse_b20"
                   else records[start:start + size] for start in range(0, len(records), size)]
            for name, size in ARMS.items()}


def _prepare(dataset, config, max_requests, concurrency):
    if (type(max_requests) is not int or max_requests < 1
            or type(concurrency) is not int or not 1 <= concurrency <= 4):
        raise BenchmarkError("benchmark_limits_invalid")
    plans = plan_batches(dataset)
    if sum(len(batches) for batches in plans.values()) > max_requests:
        raise BenchmarkError("benchmark_request_cap")
    try:
        payloads = {record["id"]: {
            "row_json": _canonical(record["row"]).decode("utf-8"),
            "question": dataset["question"], "kind": "choice",
            "options_json": _canonical(dataset["options"]).decode("utf-8")}
            for record in dataset["records"]}
        digests = {}
        for name, batches in plans.items():
            manifest = []
            for batch in batches:
                if len(batch) > config.max_block_rows:
                    raise ValueError()
                rows = [parse_input(payloads[record["id"]], index, config.max_row_chars)
                        for index, record in enumerate(batch)]
                body = build_request(config.model, rows, config.state_format)
                if len(_canonical(body)) > config.max_request_bytes:
                    raise ValueError()
                manifest.append({"ids": [record["id"] for record in batch], "request": body})
            digests[name] = _digest(manifest)
    except Exception:
        raise BenchmarkError("benchmark_plan_invalid") from None
    return plans, payloads, digests


def mock_response(body, expected_model="mock/result"):
    """An offline color-field oracle. Its stability says nothing about a real model."""
    state = body["state"]
    if isinstance(state, str):
        state = json.loads(state)
    answers = {}
    for index, row in enumerate(state["rows"]):
        key = "r%d" % index
        options = list(body["questions"][key]["criteria"])
        selected = row.get("color")
        if selected not in options:
            raise BenchmarkError("benchmark_mock_requires_color")
        answers[key] = {"type": "choice", "choice": selected, "confidence": 1.0,
                        "probabilities": {option: float(option == selected) for option in options}}
    return {"model": expected_model, "answers": answers}


def _run_arm(batches, payloads, config, transport, concurrency, semaphore):
    counters = {"attempted_requests": 0, "received_responses": 0,
                "attempted_rows": 0, "received_answer_keys": 0}
    lock = threading.Lock()
    condition = threading.Condition()
    callbacks = 0
    accepting = True

    def counted_transport(body, timeout):
        nonlocal callbacks
        with condition:
            if not accepting:
                raise BenchmarkError("benchmark_arm_finished")
            callbacks += 1
        acquired = False
        try:
            deadline = time.monotonic() + timeout
            acquired = semaphore.acquire(timeout=timeout)
            if not acquired:
                raise BenchmarkError("benchmark_transport_deadline")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BenchmarkError("benchmark_transport_deadline")
            with lock:
                counters["attempted_requests"] += 1
                counters["attempted_rows"] += len(body["questions"])
            response = transport(body, remaining)
            answers = response.get("answers") if isinstance(response, dict) else None
            returned = sum(key in answers for key in body["questions"]) if isinstance(answers, dict) else 0
            with lock:
                counters["received_responses"] += 1
                counters["received_answer_keys"] += returned
            return response
        finally:
            if acquired:
                semaphore.release()
            with condition:
                callbacks -= 1
                condition.notify_all()

    def evaluate(batch):
        try:
            return evaluate_block([payloads[record["id"]] for record in batch],
                                  config, counted_transport)
        except Exception:
            return None

    choices = {}
    validated_requests = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(evaluate, batch): batch for batch in batches}
        for future in as_completed(futures):
            answers = future.result()
            if answers is not None:
                validated_requests += 1
                choices.update((record["id"], answer["choice"])
                               for record, answer in zip(futures[future], answers))
    # Worker deadlines may finish before the underlying transport tears down.
    # Close admission atomically, then drain callbacks already admitted.
    with condition:
        accepting = False
        while callbacks:
            condition.wait()
    with lock:
        metrics = dict(counters)
    metrics.update(planned_requests=len(batches), validated_requests=validated_requests,
                   failed_batches=len(batches) - validated_requests,
                   returned_rows=len(choices), validated_rows=len(choices))
    return metrics, choices


def _source_digest():
    root = Path(__file__).resolve().parents[1]
    source = hashlib.sha256()
    for name in ("benchmarks/worker_batching.py", "clickhouse_jev/protocol.py",
                 "clickhouse_jev/transport.py", "clickhouse_jev/worker.py"):
        source.update(name.encode("utf-8") + b"\0")
        source.update((root / name).read_bytes())
    return source.hexdigest()


def run_benchmark(dataset, config, transport, *, max_requests=1500, concurrency=4, mode="mock"):
    """Run isolated one-request worker blocks; return aggregate metrics and hashes only."""
    if mode not in ("mock", "live_model", "offline_replay"):
        raise BenchmarkError("benchmark_mode_invalid")
    plans, payloads, digests = _prepare(dataset, config, max_requests, concurrency)
    semaphore = threading.BoundedSemaphore(concurrency)
    arms, choices = {}, {}
    labeled = {record["id"]: record["expected"] for record in dataset["records"] if "expected" in record}
    for name, batches in plans.items():
        arm_config = replace(config, batch_size=ARMS[name], concurrency=1)
        metrics, selected = _run_arm(batches, payloads, arm_config, transport, concurrency, semaphore)
        shared_labels = labeled.keys() & selected.keys()
        correct = sum(labeled[key] == selected[key] for key in shared_labels)
        metrics.update(batch_size=ARMS[name], plan_sha256=digests[name],
                       missing_rows=len(dataset["records"]) - len(selected),
                       accuracy={"labeled_rows": len(labeled),
                                 "validated_labeled_rows": len(shared_labels),
                                 "missing_labeled_rows": len(labeled) - len(shared_labels),
                                 "correct_rows": correct,
                                 "rate": correct / len(shared_labels) if shared_labels else None})
        arms[name], choices[name] = metrics, selected
    baseline = choices["baseline_b20"]
    comparisons = []
    for name, selected in choices.items():
        if name == "baseline_b20":
            continue
        shared = baseline.keys() & selected.keys()
        flips = sum(baseline[key] != selected[key] for key in shared)
        comparisons.append({"baseline": "baseline_b20", "arm": name,
                            "shared_validated_rows": len(shared), "flipped_rows": flips,
                            "flip_rate": flips / len(shared) if shared else None})
    return {"schema_version": 1, "mode": mode, "scope": "worker_input_batching",
            "interpretation": {
                "mock": "Mock validates benchmark plumbing, not model stability.",
                "offline_replay": "Offline replay validates supplied responses, not new model inference.",
                "live_model": "Observed paired choices; not a proof of general model stability."}[mode],
            "samples": len(dataset["records"]), "concurrency": concurrency,
            "planned_requests": sum(len(batches) for batches in plans.values()),
            "actual_requests": sum(arm["attempted_requests"] for arm in arms.values()),
            "reproducibility": {"dataset_sha256": _digest(dataset), "source_sha256": _source_digest(),
                                "python_version": sys.version.split()[0],
                                "request_model_sha256": _digest(config.model),
                                "expected_model_sha256": _digest(config.expected_model),
                                "state_format": config.state_format, "max_requests": max_requests,
                                "max_row_chars": config.max_row_chars,
                                "max_request_bytes": config.max_request_bytes,
                                "deadline_seconds": config.deadline_seconds},
            "arms": arms, "comparisons": comparisons}


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise BenchmarkError("benchmark_invalid_arguments")


def main(argv=None):
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--input", help="Explicit local dataset JSON; never copied into the report.")
    parser.add_argument("--rows", type=int, help="Synthetic record count (default: 1000).")
    parser.add_argument("--seed", type=int, help="Synthetic generation seed (default: 1729).")
    parser.add_argument("--max-requests", type=int, default=1500)
    parser.add_argument("--concurrency", type=int, default=4, help="Between 1 and 4.")
    parser.add_argument("--live", action="store_true", help="Explicitly enable remote model requests.")
    parser.add_argument("--endpoint", help="Live HTTPS endpoint; omitted from all output.")
    parser.add_argument("--model", help="Live request model identifier.")
    parser.add_argument("--expected-model", help="Required live response model identifier.")
    parser.add_argument("--api-key-env", help="Explicit environment variable name holding the live API key.")
    try:
        args = parser.parse_args(argv)
        live_settings = (args.endpoint, args.model, args.expected_model, args.api_key_env)
        if (args.live and not all(live_settings)) or (not args.live and any(live_settings)):
            raise BenchmarkError("benchmark_live_settings_invalid")
        if args.input and (args.rows is not None or args.seed is not None):
            raise BenchmarkError("benchmark_input_settings_invalid")
        count, seed = args.rows if args.rows is not None else 1000, args.seed if args.seed is not None else 1729
        dataset = load_dataset(args.input) if args.input else generate_dataset(count, seed)
        config = Config(endpoint=args.endpoint or "https://example.invalid/judge",
                        model=args.model or "mock/request", expected_model=args.expected_model or "mock/result")
        # Preflight before credential consumption or transport construction.
        _prepare(dataset, config, args.max_requests, args.concurrency)
        if args.live:
            transport = HttpTransport(config, os.environ.pop(args.api_key_env))
        else:
            transport = lambda body, timeout: mock_response(body, config.expected_model)
        report = run_benchmark(dataset, config, transport, max_requests=args.max_requests,
                               concurrency=args.concurrency, mode="live_model" if args.live else "mock")
        report["dataset_source"] = "external_input" if args.input else "synthetic"
        if not args.input:
            report["reproducibility"]["synthetic_seed"] = seed
        sys.stdout.write(json.dumps(report, sort_keys=True, allow_nan=False) + "\n")
        return 1 if any(arm["failed_batches"] for arm in report["arms"].values()) else 0
    except Exception:
        sys.stderr.write('{"error":"benchmark_failed"}\n')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
