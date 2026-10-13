# clickhouse-jev — ask your ClickHouse rows questions in plain language

[![CI](https://github.com/ahwlsqja/clickhouse-jev/actions/workflows/ci.yml/badge.svg)](https://github.com/ahwlsqja/clickhouse-jev/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Filter, rank and classify rows with natural-language conditions, directly in SQL.

`clickhouse-jev` connects ClickHouse executable UDFs to [TypeSafe's Jev](https://docs.typesafe.ai), a model that returns structured probabilities, choices and scores. It uses Python's standard library and evaluates the JSON fields you explicitly pass to it. No embeddings or vector index are required.

Inspired by [pg-jev](https://github.com/realZachi/pg-jev), with a separate implementation for ClickHouse's columnar execution model.

```sql
-- Filter support tickets.
SELECT id, subject
FROM tickets
WHERE jev(
    toJSONString(map('subject', subject, 'body', body)),
    'The customer is asking for a refund.',
    0.7
);

-- Rank messages by a natural-language condition.
SELECT subject,
       jev_prob(toJSONString(map('body', body)),
                'The message describes an urgent service outage.') AS urgency
FROM tickets
ORDER BY urgency DESC
LIMIT 20;

-- Choose from a fixed set of labels.
SELECT jev_choice(
    toJSONString(map('body', body)),
    'Which team should handle this ticket?',
    toJSONString(['billing', 'technical', 'sales'])
) AS team, count()
FROM tickets
GROUP BY team;

-- Score products on ordered levels.
SELECT name, jev_score(
    toJSONString(map('name', name, 'description', description)),
    'How luxurious is this product?',
    toJSONString(['budget', 'mid-range', 'premium', 'luxury'])
) AS luxury
FROM products
ORDER BY luxury DESC;
```

These examples assume the functions have been installed. `row_json` is a JSON object encoded as a string; options are a JSON array encoded as a string. The threshold for `jev()` is explicit.

## How it works

1. ClickHouse sends a block of rows to an `executable_pool` worker using JSONEachRow with a row-count header.
2. The worker groups rows with the same question, judgment type and options, then divides each group into small API batches.
3. Requests run with bounded concurrency. The worker validates the model identity, answer keys, types, probability distributions and choice/score consistency.
4. Answers are restored to their original row positions. A block is written only after all its batches succeed.

The worker processes the rows it receives from ClickHouse. It does not discover or read ahead through tables. There is no result cache or automatic retry.

## Run the server demo

From a checkout of this repository, with Docker and Docker Compose available:

```sh
docker compose up --build --exit-code-from demo --attach demo
```

This starts a real ClickHouse server and a local mock API, installs the UDF, loads three synthetic rows, and checks filtering, classification, scoring and normalized scoring. No API key is needed. The mock uses deterministic rules; it does not run an AI model.

You will see results such as:

```text
Filter: [{"id": 1, "item": "red apple"}]
Classification: [{"id": 1, "color": "red"}, {"id": 2, "color": "yellow"}, {"id": 3, "color": "green"}]
Scoring: [{"id": 1, "score": 0, "normalized": 0}, {"id": 2, "score": 2, "normalized": 1}, {"id": 3, "score": 0, "normalized": 0}]
Demo passed: real ClickHouse server, synthetic fixtures, local mock.
```

The command exits with the demo's status and stops the services. Remove the stopped containers and network with `docker compose down --volumes`. Runtime traffic stays on an internal Docker network, no host ports are published, and database storage is temporary. The first image build needs network access to download the image and system packages.

See [the demo guide](demo/README.md) for interactive SQL and failure testing.

## Compare batch behavior

From a source checkout:

```sh
python3 -m benchmarks.worker_batching
```

This runs an offline benchmark with synthetic inputs and a deterministic mock.
It compares batch sizes 1, 5 and 20, reversed order within each batch of 20,
and an identical repeat. The aggregate JSON report distinguishes failed
judgments from successful comparisons and records the comparison denominators.
Mock results verify the measurement code; they do not measure model accuracy
or stability.

The [benchmark guide](benchmarks/README.md) also describes explicit live-model
runs and external input files. Keep private inputs and their reports outside
this checkout. The benchmark controls worker batches directly; it does not
measure ClickHouse query planning or server throughput.

## Install

Requirements:

- Python **3.10+** on the host that executes the UDF.
- A ClickHouse deployment that permits executable UDFs and access to its server configuration.
- A Jev-compatible System One endpoint and any credentials required by that endpoint.

The integration tests use ClickHouse **26.8**. Check your hosting provider's support for executable UDFs before installing on a managed service.

See the [support scope](docs/support.md) and [0.1.0 release candidate notes](docs/releases/0.1.0.md)
for tested configurations and remaining validation limits.

### From source

```sh
git clone https://github.com/ahwlsqja/clickhouse-jev.git
cd clickhouse-jev

python3 scripts/install.py \
  --output-dir .local/jev \
  --python /usr/bin/python3 \
  --endpoint https://openrouter.ai/api/v1/systemone \
  --model typesafe/jev-1.13 \
  --expected-model typesafe/jev-1.13-20260917 \
  --api-key-env JEV_API_KEY \
  --batch-size 20 \
  --concurrency 4
```

Use the Python executable available on your server. The worker's environment must already contain `JEV_API_KEY`, supplied by your service manager or secret provider. The installer records the environment variable's **name**, not its value.

The installer creates `config.xml`, `jev_function.xml`, `jev-worker` and `functions.sql` in the chosen directory. It does not change a running server. Keep the source checkout at its installed path, or set `--package-root` to its location on the execution host.

For ClickHouse local, put the generated `functions.sql` before your query in one SQL file:

```sh
cat .local/jev/functions.sql examples/basic.sql > .local/jev/example.sql
clickhouse-local --config-file .local/jev/config.xml \
  --queries-file .local/jev/example.sql
```

For a server, connect the generated executable-function configuration to the server and register the SQL wrappers. See [Installation](docs/installation.md) for the file layout and configuration details.

### Try it without an API key

The test suite runs an in-process mock endpoint using synthetic fixtures:

```sh
docker build -t clickhouse-jev .
docker run --rm --network none --entrypoint python3 \
  clickhouse-jev -m unittest discover -s tests -v
```

Building the image downloads system packages. The test container has external networking disabled and does not require an API credential.

## Functions

| Function | Returns | Purpose |
| --- | --- | --- |
| `jev(row_json, condition, threshold)` | `UInt8` | Whether the probability meets the explicit threshold |
| `jev_prob(row_json, condition)` | `Float64` | Probability in the range 0–1 |
| `jev_choice(row_json, question, options_json)` | `String` | One of the supplied labels |
| `jev_score(row_json, question, levels_json)` | `Float64` | Expected position on ordered levels, from 0 to n−1 |
| `jev_score_norm(row_json, question, levels_json)` | `Float64` | Expected position normalized to 0–1 |
| `jev_eval(row_json, question, kind, options_json)` | `String` | Validated answer encoded as JSON |

`kind` is `noul`, `choice` or `score`. For `noul`, use `'[]'` for `options_json`. Choice and score require at least two distinct, nonempty options.

## Settings

Set these flags when running `scripts/install.py`. The same worker flags are available through `python3 -m clickhouse_jev`.

| Flag | Default | Scope |
| --- | --- | --- |
| `--endpoint` | required | System One API URL |
| `--model` | required | Model identifier sent in requests |
| `--expected-model` | required | Exact model identifier accepted in responses |
| `--batch-size` | `20` | Maximum rows per model request |
| `--concurrency` | `4` | Maximum submitted requests per worker |
| `--max-block-rows` | `10000` | Maximum rows accepted in one ClickHouse block |
| `--max-row-chars` | `10000` | Maximum characters in each `row_json` |
| `--max-request-bytes` | `1000000` | Maximum serialized request size |
| `--deadline-seconds` | `30` | Evaluation deadline for one input block |
| `--state-format` | `string` | Encode `state` as a JSON string or `object` |
| `--api-key-env` | unset | Name of the environment variable containing the key |
| `--api-key-fd` | unset | File descriptor from which to read the key once |
| `--allow-http` | disabled | Permit HTTP for an explicitly trusted endpoint |

The installer also accepts `--pool-size` (default `1`) for ClickHouse's executable pool. Worker concurrency is per process; multiple workers or queries can multiply total API concurrency.

Model aliases and returned version identifiers may change independently. Pin an expected response model deliberately: mismatched responses fail instead of being silently accepted.

## Writing good conditions

- State the question precisely and use a stable set of labels or ordered levels.
- Keep arithmetic, dates and exact matches in SQL.
- Pass only the fields needed for the judgment. `toJSONString(map(...))` makes that projection explicit.
- Inspect probabilities before choosing a threshold.
- Treat batch size, batch membership and row order as part of the evaluation setup. Repeated judgments can vary.

## Limits and failure behavior

- Rows sent to the configured endpoint leave the database process. Use data you are authorized to send there.
- SQL optimization determines when and on which rows a UDF runs. `LIMIT` is not a global API-call or billing limit, and an unused UDF result may not be evaluated.
- A failed request or invalid answer fails its whole input block. Earlier successful blocks may already have been returned. Already-issued model requests may still incur cost.
- The worker has no global spend limit. Block size, concurrency and deadlines are local limits, not an account-wide budget.
- Block memory is bounded by the configured row and input-size limits; there is no separate total-memory budget. The deadline starts after the input block is read, and is not a guarantee about DNS or OS process shutdown time.
- Responses must have the expected model and row keys, finite values, and valid option distributions. Probability sums allow an absolute difference of `0.02` from 1. Choice probabilities within `1e-12` of the maximum are treated as tied. Score expectations use an absolute tolerance of `0.02`. The original answer is preserved.
- Validation checks the response contract. It does not establish semantic accuracy or probability calibration.

Adjust ClickHouse's `max_block_size` to fit the worker's `--max-block-rows` when necessary. Do not assume every query plan produces identical model batches.

## Development

No runtime Python packages are required. From a source checkout:

```sh
python3 -m unittest discover -s tests -v
```

ClickHouse integration tests are skipped when `clickhouse-local` is unavailable. Run the Docker command above to include them. All fixtures are synthetic; tests cover output ordering, mixed questions, duplicate inputs, Unicode, failure boundaries, deadlines and credential handling.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution and test guidance.

## License

[MIT](LICENSE). Attribution and the applicable PostgreSQL License notice for material inspired by pg-jev are preserved in [NOTICE](NOTICE).

Jev, TypeSafe and ClickHouse are trademarks of their respective owners. This project is not affiliated with or endorsed by their owners.
