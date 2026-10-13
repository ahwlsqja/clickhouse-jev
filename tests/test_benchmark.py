"""Worker benchmark contracts, using synthetic records and offline transports."""

import contextlib
import copy
from dataclasses import replace
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from benchmarks import worker_batching as bench
from clickhouse_jev.transport import Config


def config():
    return Config(endpoint="https://example.invalid/judge", model="mock/request",
                  expected_model="mock/result")


class BenchmarkTests(unittest.TestCase):
    def test_generation_is_deterministic_and_fixture_labels_are_valid(self):
        dataset = bench.generate_dataset(23, 17)
        self.assertEqual(dataset, bench.generate_dataset(23, 17))
        self.assertNotEqual(dataset, bench.generate_dataset(23, 18))
        self.assertEqual(len({record["id"] for record in dataset["records"]}), 23)
        self.assertTrue(all(record["expected"] in dataset["options"]
                            for record in dataset["records"]))
        fixture = bench.load_dataset(Path(bench.__file__).with_name("synthetic.json"))
        self.assertGreater(len(fixture["records"]), 0)

    def test_reverse_preserves_membership_including_partial_last_batch(self):
        dataset = bench.generate_dataset(23, 17)
        plans = bench.plan_batches(dataset)
        baseline = plans["baseline_b20"]
        reverse = plans["reverse_b20"]
        self.assertEqual([len(batch) for batch in baseline], [20, 3])
        self.assertEqual(reverse, [list(reversed(batch)) for batch in baseline])
        self.assertEqual(plans["repeat_b20"], baseline)

    def test_mock_reordered_answers_and_duplicates_keep_identity(self):
        dataset = bench.generate_dataset(23, 17)
        dataset["records"][1]["row"] = copy.deepcopy(dataset["records"][0]["row"])
        dataset["records"][1]["expected"] = dataset["records"][0]["expected"]
        seen_rows = []

        def transport(body, timeout):
            rows = json.loads(body["state"])["rows"]
            seen_rows.extend(rows)
            response = bench.mock_response(body, "mock/result")
            response["answers"] = dict(reversed(list(response["answers"].items())))
            return response

        result = bench.run_benchmark(dataset, config(), transport)
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(result["actual_requests"], 34)
        self.assertTrue(all("id" not in row for row in seen_rows))
        self.assertTrue(all(item["flip_rate"] == 0 for item in result["comparisons"]))
        self.assertTrue(all(item["shared_validated_rows"] == 23
                            for item in result["comparisons"]))
        self.assertTrue(all(arm["accuracy"]["rate"] == 1.0
                            for arm in result["arms"].values()))
        self.assertIn("source_sha256", result["reproducibility"])
        self.assertTrue(all(len(arm["plan_sha256"]) == 64
                            for arm in result["arms"].values()))

    def test_missing_and_invalid_results_discard_only_the_failed_batch(self):
        dataset = bench.generate_dataset(23, 17)
        for fault, returned in (("missing", 22), ("invalid", 23), ("exception", 20)):
            with self.subTest(fault=fault):
                def transport(body, timeout):
                    response = bench.mock_response(body, "mock/result")
                    if len(body["questions"]) == 3:
                        if fault == "exception":
                            raise RuntimeError("private-answer-and-key")
                        if fault == "missing":
                            response["answers"].pop("r0")
                        else:
                            response["answers"]["r0"]["choice"] = "private-answer"
                    return response

                result = bench.run_benchmark(dataset, config(), transport)
                arm = result["arms"]["baseline_b20"]
                self.assertEqual(arm["attempted_requests"], 2)
                self.assertEqual(arm["validated_requests"], 1)
                self.assertEqual(arm["received_answer_keys"], returned)
                self.assertEqual(arm["returned_rows"], 20)
                self.assertEqual(arm["validated_rows"], 20)
                self.assertEqual(arm["missing_rows"], 3)
                self.assertEqual(arm["accuracy"]["validated_labeled_rows"], 20)
                self.assertEqual(arm["accuracy"]["labeled_rows"], 23)
                self.assertEqual(arm["accuracy"]["missing_labeled_rows"], 3)
                self.assertNotIn("private", json.dumps(result))

    def test_flip_and_accuracy_denominators_use_only_shared_validated_ids(self):
        dataset = bench.generate_dataset(3, 17)
        dataset["records"][0].pop("expected")

        def transport(body, timeout):
            response = bench.mock_response(body, "mock/result")
            if len(body["questions"]) == 1:
                for answer in response["answers"].values():
                    choice = next(option for option in dataset["options"]
                                  if option != answer["choice"])
                    answer.update(choice=choice, probabilities={
                        option: float(option == choice) for option in dataset["options"]})
            return response

        result = bench.run_benchmark(dataset, config(), transport)
        comparison = next(item for item in result["comparisons"]
                          if item["arm"] == "b1")
        self.assertEqual(comparison["shared_validated_rows"], 3)
        self.assertEqual(comparison["flipped_rows"], 3)
        self.assertEqual(comparison["flip_rate"], 1.0)
        self.assertEqual(result["arms"]["b1"]["accuracy"], {
            "labeled_rows": 2, "validated_labeled_rows": 2,
            "missing_labeled_rows": 0, "correct_rows": 0, "rate": 0.0})

    def test_no_shared_success_is_null_not_zero(self):
        def transport(body, timeout):
            raise RuntimeError("private-response")

        result = bench.run_benchmark(bench.generate_dataset(3), config(), transport)
        self.assertEqual(result["actual_requests"], result["planned_requests"])
        self.assertTrue(all(item["shared_validated_rows"] == 0
                            and item["flip_rate"] is None for item in result["comparisons"]))
        self.assertTrue(all(arm["accuracy"]["rate"] is None
                            for arm in result["arms"].values()))

    def test_cap_and_all_input_validation_precede_any_dispatch(self):
        transport = mock.Mock()
        with self.assertRaisesRegex(bench.BenchmarkError, "benchmark_request_cap"):
            bench.run_benchmark(bench.generate_dataset(23), config(), transport, max_requests=33)
        transport.assert_not_called()
        dataset = bench.generate_dataset(2)
        dataset["records"][-1]["row"] = {"private": "x" * 10001}
        with self.assertRaises(bench.BenchmarkError):
            bench.run_benchmark(dataset, config(), transport)
        transport.assert_not_called()
        with self.assertRaises(bench.BenchmarkError):
            bench.run_benchmark(bench.generate_dataset(2), config(), transport, concurrency=5)
        transport.assert_not_called()

    def test_concurrency_is_bounded_at_four(self):
        lock = threading.Lock()
        active = peak = 0

        def transport(body, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.002)
                return bench.mock_response(body, "mock/result")
            finally:
                with lock:
                    active -= 1

        bench.run_benchmark(bench.generate_dataset(23), config(), transport)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)

    def test_timed_out_transports_are_drained_before_report_and_late_outputs_stay_missing(self):
        lock = threading.Lock()
        active = completed = peak = 0

        def transport(body, timeout):
            nonlocal active, completed, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.05)
                return bench.mock_response(body, "mock/result")
            finally:
                with lock:
                    active -= 1
                    completed += 1

        result = bench.run_benchmark(bench.generate_dataset(1),
                                    replace(config(), deadline_seconds=.01), transport)
        with lock:
            self.assertEqual(active, 0)
            self.assertEqual(completed, result["actual_requests"])
            self.assertLessEqual(peak, 4)
        self.assertEqual(sum(arm["received_responses"] for arm in result["arms"].values()), completed)
        self.assertGreater(completed, 0)
        self.assertTrue(all(arm["validated_requests"] == 0 and arm["returned_rows"] == 0
                            and arm["missing_rows"] == 1 for arm in result["arms"].values()))

    def test_private_dataset_stays_local_and_report_contains_only_aggregates(self):
        dataset = bench.generate_dataset(3)
        dataset["question"] = "private-question"
        for record in dataset["records"]:
            record["id"] = "private-id-" + record["id"]
            record["row"]["description"] = "private-row"
            record["row"]["color"] = "private-" + record["row"]["color"]
            record["expected"] = record["row"]["color"]
        dataset["options"] = ["private-" + option for option in dataset["options"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-input.json"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            loaded = bench.load_dataset(path)
            result = bench.run_benchmark(loaded, config(),
                                        lambda body, timeout: bench.mock_response(body, "mock/result"),
                                        mode="offline_replay")
        rendered = json.dumps(result)
        for value in ("private", "example.invalid", "mock/result", "mock/request"):
            self.assertNotIn(value, rendered)
        self.assertNotIn("rows", result)
        self.assertNotIn("answers", result)

    def test_cli_live_requires_explicit_settings_and_never_echoes_secrets(self):
        cases = [["--input", "/private/missing.json"],
                 ["--live", "--endpoint", "https://private-endpoint.invalid"],
                 ["--private-argument", "private-value"],
                 ["--rows", "private-number"]]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(bench.main(args), 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(json.loads(stderr.getvalue()), {"error": "benchmark_failed"})

        environment = {"PRIVATE_KEY_NAME": "private-key-value"}
        args = ["--live", "--endpoint", "https://private-endpoint.invalid",
                "--model", "private-request-model", "--expected-model", "private-result-model",
                "--api-key-env", "PRIVATE_KEY_NAME", "--rows", "3"]
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(bench, "HttpTransport") as factory, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            factory.return_value.side_effect = RuntimeError("private-response")
            self.assertEqual(bench.main(args), 1)
            self.assertNotIn("PRIVATE_KEY_NAME", os.environ)
            self.assertNotIn("private", stdout.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["mode"], "live_model")
            self.assertEqual(factory.call_args.args[1], "private-key-value")

    def test_cli_partial_failure_keeps_report_and_successful_measurements_exit_zero(self):
        real_run = bench.run_benchmark

        def with_fault(dataset, cfg, transport, **kwargs):
            def partly_broken(body, timeout):
                if len(body["questions"]) == 1:
                    raise RuntimeError("private-response")
                return transport(body, timeout)
            return real_run(dataset, cfg, partly_broken, **kwargs)

        with mock.patch.object(bench, "run_benchmark", side_effect=with_fault), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(bench.main(["--rows", "3"]), 1)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["arms"]["baseline_b20"]["validated_rows"], 3)
            self.assertEqual(report["arms"]["b1"]["missing_rows"], 3)
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(bench.main(["--rows", "3"]), 0)
            self.assertEqual(json.loads(stdout.getvalue())["actual_requests"], 7)

    def test_cli_cap_prevents_transport_creation_and_credential_consumption(self):
        args = ["--live", "--endpoint", "https://example.invalid/judge", "--model", "m",
                "--expected-model", "m", "--api-key-env", "TEST_KEY", "--rows", "1000",
                "--max-requests", "1"]
        with mock.patch.dict(os.environ, {"TEST_KEY": "test-key"}, clear=True), \
                mock.patch.object(bench, "HttpTransport") as factory, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench.main(args), 1)
            factory.assert_not_called()
            self.assertIn("TEST_KEY", os.environ)

    def test_dataset_rejects_duplicate_ids_unknown_labels_and_nonfinite_values(self):
        for fault in ("duplicate", "label", "nonfinite"):
            dataset = bench.generate_dataset(2)
            if fault == "duplicate":
                dataset["records"][1]["id"] = dataset["records"][0]["id"]
            elif fault == "label":
                dataset["records"][0]["expected"] = "private-label"
            else:
                dataset["records"][0]["row"]["value"] = float("nan")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "private-input.json"
                path.write_text(json.dumps(dataset), encoding="utf-8")
                with self.assertRaisesRegex(bench.BenchmarkError, "benchmark_dataset_invalid"):
                    bench.load_dataset(path)


if __name__ == "__main__":
    unittest.main()
