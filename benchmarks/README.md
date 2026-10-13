# Worker batch variation benchmark

This benchmark controls the inputs to `evaluate_block`, the executable UDF's Python worker. It measures changes in validated choice results across batch size, order, and an exact repeated request plan. It does **not** measure ClickHouse optimizer behavior, ClickHouse server throughput, or end-to-end SQL performance.

Run from the repository root with Python 3.10 or newer. The default requires no credential, server, dependency, or network access:

```sh
python3 -m benchmarks.worker_batching
```

The default generates 1,000 synthetic records from [synthetic.json](synthetic.json), using seed `1729`. The deterministic mock reads each record's `color` field. Its expected zero flip rate verifies the benchmark plumbing; it is not evidence of model stability. Change the synthetic inputs with `--rows 23 --seed 17`, or run the small public fixture directly with `--input benchmarks/synthetic.json`.

## Request plan

Every arm uses the same records, one fixed choice question, and one fixed ordered options list. Original unique IDs remain outside the request payload. Records with identical content may have different IDs.

| Arm | Batch size | Record order |
| --- | --- | --- |
| `baseline_b20` | 20 | Original input order |
| `b1` | 1 | Original input order |
| `b5` | 5 | Original input order |
| `reverse_b20` | 20 | Reverse each original baseline batch, including its final short batch |
| `repeat_b20` | 20 | Exact baseline batch membership, order, and request content |

The request count is `N + ceil(N/5) + 3*ceil(N/20)`: 1,350 requests for 1,000 records. The entire plan is checked against `--max-requests` (default `1500`) before any credential consumption, transport construction, or dispatch. Inputs and request-size limits are also validated before dispatch. There are no retries. `--concurrency` must be from 1 to 4; default is 4. Arms run sequentially; independent batches within an arm may finish in any order.

Each planned batch is a separate one-request worker block. A failed transport, wrong model identifier, missing answer, or invalid answer discards that batch's outputs; other batches continue. `repeat_b20` repeats request content, not request timing or concurrent completion order. A model alias can change between calls; use a pinned model identifier when the provider supports one.

An arm closes transport admission and waits for already admitted callbacks to finish before finalizing request counts or starting the next arm. A response arriving after the worker deadline is counted as received but remains excluded from validated outputs. This wait includes transport teardown: the worker deadline is not a hard bound on the whole benchmark's wall-clock duration. An injected transport must eventually return or raise; the live HTTP transport enforces its own request timeout.

## Explicit external input and live evaluation

External input must be a JSON object with this schema:

```json
{
  "question": "Which category fits the record?",
  "options": ["category-a", "category-b"],
  "records": [
    {"id": "record-001", "row": {"description": "Example input"}, "expected": "category-a"},
    {"id": "record-002", "row": {"description": "Another example"}}
  ]
}
```

`id` is a unique nonempty string, `row` is a JSON object, and `expected` is optional. An expected label must be one of the nonempty, unique string options. The input must contain at least one record and at least two options. Unknown envelope or record fields, duplicate JSON keys, duplicate IDs, nonfinite numbers, and invalid worker inputs are rejected. `--input` cannot be combined with `--rows` or `--seed`.

Live mode is explicit and requires the HTTPS endpoint, request model, expected response model, and the **name** of an environment variable containing the credential. Obtain that credential through your environment's approved secret tooling; do not place it in command arguments or repository files.

```sh
python3 -m benchmarks.worker_batching --live \
  --endpoint "$JEV_ENDPOINT" \
  --model "$JEV_MODEL" \
  --expected-model "$JEV_EXPECTED_MODEL" \
  --api-key-env JEV_API_KEY \
  --input /path/outside/repository/dataset.json
```

The named credential is consumed from the child process environment once. The supplied input stays at its original path and is sent to the explicitly selected endpoint in live mode. The benchmark never copies input into the repository. Offline mock evaluation of external inputs requires an allowed `color` field; it does not infer arbitrary categories.

For local validation or a retained-response replay, import `load_dataset`, `generate_dataset`, `mock_response`, and `run_benchmark`. Call `run_benchmark(dataset, config, transport, mode="offline_replay")` with a `Config` and a callable `transport(body, timeout)` returning a Jev response. No HTTP transport is created by this function. Replays validate the supplied response mapping; they cannot demonstrate the behavior of new live requests. Keep private input and response files outside the repository.

## Report contract

The CLI writes one aggregate JSON report to stdout. It includes `mode` (`mock` or `live_model`), a measurement scope, and an interpretation warning. The importable runner additionally supports `offline_replay`. No raw question, options, record IDs, rows, expected labels, returned choices, response bodies, endpoint, credential, credential-variable name, or private path appears in the report or benchmark errors.

| Field | Meaning |
| --- | --- |
| `samples` | Original record count, shared by all arms |
| `planned_requests` | All planned requests, including batches that later fail |
| `actual_requests` | Transport invocations attempted across all arms, including failed attempts |
| `attempted_requests`, `attempted_rows` | Per-arm transport invocations and records submitted through them |
| `received_responses` | Transport invocations that returned a response, before worker validation |
| `received_answer_keys` | Expected local answer keys present in those responses; these may be invalid |
| `validated_requests` | Batches whose entire response passed the worker contract |
| `failed_batches` | Planned batches without fully validated worker output |
| `returned_rows`, `validated_rows` | Fully validated output records; these two counts are equal |
| `missing_rows` | Original record count minus fully validated output count |
| `shared_validated_rows` | IDs with validated outputs in both the baseline and comparison arm |
| `flipped_rows`, `flip_rate` | Unequal paired choices and `flipped_rows / shared_validated_rows` |
| `accuracy.labeled_rows` | Original records having an explicit expected label |
| `accuracy.validated_labeled_rows` | Labeled records with a fully validated output in that arm |
| `accuracy.missing_labeled_rows` | Labeled records excluded because their batch failed |
| `accuracy.correct_rows`, `accuracy.rate` | Matching labeled outputs and `correct_rows / validated_labeled_rows` |

Flip rate is `null` when there are no shared successes. Accuracy is `null` when there are no validated labeled records. Failed or missing rows never become fabricated answers. Both rates condition on successful validation and can therefore have selection bias: always inspect missing counts and the stated denominators.

`reproducibility.dataset_sha256` hashes canonical JSON of the complete dataset, including question, ordered options, original record order, IDs, and optional labels. Canonical serialization sorts object keys, preserves array order and Unicode, uses compact separators, and rejects nonfinite numbers. Each `plan_sha256` hashes the ordered batch manifest containing original IDs and exact request bodies; raw manifest content is not emitted. Request and expected model identifiers are hashed as well. `source_sha256` fingerprints the actual benchmark and worker/protocol/transport source file contents, including uncommitted changes. Python version, limits, state format, concurrency, and synthetic seed are reported. Hashes are reproducibility checks, not anonymization guarantees for guessable inputs.

Exit status is `0` when all batches validated, including runs with nonzero flips or incorrect labels. It is `1` if any batch failed; the aggregate report remains available. Preflight, argument, input, or configuration failures exit `1`, emit no report, and write only `{"error":"benchmark_failed"}` to stderr. There is no remote diagnostic body or traceback. The benchmark measures observed choices in one campaign; it does not prove stability across models, deployments, time, or other datasets.

Focused offline validation:

```sh
python3 -m unittest discover -s tests -p test_benchmark.py -v
```
