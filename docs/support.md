# Support scope

`0.1.0` is an experimental first-release candidate. The scope below describes the implementation and its public validation; it is not a claim of model accuracy or compatibility with every ClickHouse deployment.

## Environment matrix

| Area | Status | Scope and evidence |
| --- | --- | --- |
| ClickHouse local and server | Tested baseline | ClickHouse **26.8.21.10** with Python **3.10.12** in the development image. The [Dockerfile](../Dockerfile) pins the ClickHouse 26.8 image by digest; Python is installed from the image's system packages. [Integration tests](../tests/test_integration.py) exercise `clickhouse-local`; the [server demo](../demo/README.md) exercises a real server. |
| Python | Requirement | Python **3.10+**, with no third-party runtime Python packages. This is declared in [package metadata](../pyproject.toml); it does not establish a tested interpreter-version matrix. Building the package uses setuptools. |
| Executable UDF hosting | Requirement | Permission to run `executable_pool` workers and control the execution host, server configuration, script paths and SQL UDF metadata paths. The [installer](../scripts/install.py) generates files; it does not configure or restart an existing server. See [installation](installation.md). |
| Other ClickHouse versions or operating systems | Not yet verified | The public integration setup covers the pinned Docker image, not a broader version or OS matrix. |
| ClickHouse Cloud and other managed services | Not yet verified | Availability of custom executable workers and configuration must be confirmed for the intended service. No managed-service compatibility is claimed. |
| Model endpoint | Requirement; mock-tested | A compatible System One JSON endpoint, a request model and an exact expected response model. Public tests use deterministic local mocks. They do not establish compatibility, availability or quality for a particular live provider/model version. |
| Clustered/distributed deployment | Not yet verified | The public setup uses a single server. Worker installation, credentials and aggregate request concurrency across hosts require deployment-specific validation. |

The [CI workflow](../.github/workflows/ci.yml) runs offline tests, the real-server demo and a deliberate API-error demo. Fixtures are synthetic, and no external model account is required. An image build requires network access; the test run disables external networking, and the demo uses an internal Docker network.

## Implemented behavior

- `jev_eval` returns a validated answer as a JSON string. The [SQL wrappers](../sql/functions.sql) expose `jev_prob`, `jev`, `jev_choice`, `jev_score` and `jev_score_norm`.
- Inputs contain an explicitly supplied JSON object, question, judgment kind and options. Supported kinds are `noul`, `choice` and `score`; choice and score require at least two distinct, nonblank options. See the [protocol](../clickhouse_jev/protocol.py) and its [tests](../tests/test_protocol.py).
- The [worker](../clickhouse_jev/worker.py) groups compatible inputs, batches model requests, bounds submitted requests per process and restores input order. [Worker tests](../tests/test_worker.py) cover mixed questions, duplicate rows, Unicode, reverse completion and failure boundaries.
- Responses require the exact configured model, complete answer keys, finite values and valid distributions. Choice and score consistency are checked. The validator preserves the accepted answer; it does not establish semantic correctness or probability calibration.
- A failed request or invalid answer emits no output for its input block. Earlier successful blocks can already have been returned, and submitted requests can still incur charges. There is no query-wide rollback of external calls.
- [Transport](../clickhouse_jev/transport.py) supports HTTPS, explicitly permitted HTTP for a trusted endpoint, and optional bearer credentials read once from an environment variable or file descriptor. [Transport tests](../tests/test_transport.py) cover size bounds, deadlines and errors that omit remote bodies and credentials. Credential delivery to a real executable pool remains the deployment's responsibility.

## Limits of this release

There is no result cache, automatic retry or global API-call/spend limit. Batch size, block size, input size, per-worker concurrency and deadlines bound local work; they do not bound costs across queries, workers or hosts. A SQL `LIMIT` does not impose a model-call budget.

The block deadline starts after the block is read. It is not a strict guarantee about DNS lookup or OS process shutdown time. There is no separate total-memory budget. SQL planning determines which rows reach the UDF and how blocks are formed; batch membership, order and repeated live judgments may affect results.

The public mock tests demonstrate the connection and response contract. They do not demonstrate live-model accuracy, calibration, repeatability, production throughput or cost. Evaluate these properties with representative data and the intended endpoint before relying on them.

See the [0.1.0 candidate notes](releases/0.1.0.md) for the release validation checklist.
