# Installation

The source checkout contains the Python worker, an installer and SQL wrappers. Python 3.10 or newer is required. The installer uses only the standard library.

## Generate the files

Run the command in the [README](../README.md#from-source) with paths and endpoint settings for your host. The output directory contains:

| File | Purpose |
| --- | --- |
| `jev-worker` | Executable wrapper that starts the Python worker |
| `jev_function.xml` | Defines the four-argument `jev_eval` executable UDF |
| `config.xml` | Connects ClickHouse to the generated paths |
| `functions.sql` | Defines the probability, predicate, choice and score wrappers |

`--python` must name a Python executable available to the ClickHouse OS user. `--package-root` must refer to this checkout on that host. The generated wrapper uses this directory as `PYTHONPATH`; moving the checkout requires regenerating the files.

The generated configuration sets `user_scripts_path`, `user_defined_path` and `user_defined_executable_functions_config`. It is suitable for an isolated ClickHouse local instance. On a server with existing executable or SQL UDFs, merge these settings into your existing layout so their paths remain valid.

## ClickHouse local

Place the generated SQL wrappers before your query in one file, and supply the generated configuration:

```sh
cat .local/jev/functions.sql examples/basic.sql > .local/jev/example.sql
clickhouse-local --config-file .local/jev/config.xml \
  --queries-file .local/jev/example.sql
```

The example uses synthetic rows, but it calls the configured model endpoint and may incur API charges. For a local test without model calls, use the Docker test command in the README.

## ClickHouse server

1. Place the worker source and generated files on the execution host. Give the ClickHouse OS user read access to the package and XML files, execute access to `jev-worker`, and the appropriate writable location for SQL UDF metadata.
2. Add the generated executable-function settings to your server configuration. Keep paths consistent with the server's allowed scripts directory and existing UDFs.
3. Supply the API key to the worker using your service manager or secret provider. The installer does not fetch or store credentials.
4. Apply the configuration using your deployment's normal procedure, then register the wrappers:

   ```sh
   clickhouse-client --multiquery < .local/jev/functions.sql
   ```

   Adjust the input path to the generated SQL file on your host. Use your normal client authentication settings.

5. Run a small query and inspect the result before evaluating a larger table. Ensure `max_block_size` is compatible with the worker's `--max-block-rows`.

The installer does not restart or reconfigure a running server. Hosting providers may restrict executable UDFs; installation requires control over the corresponding server configuration and execution environment.

## Credentials

Use exactly one of these options:

- `--api-key-env JEV_API_KEY`: the worker reads the value once and removes it from its environment. The variable must be supplied to the worker process by your deployment.
- `--api-key-fd N`: the worker reads a bounded key from an already-open file descriptor once, then closes it. A wrapper can arrange this pipe immediately before starting the worker.

Do not assume that an arbitrary file descriptor opened by a ClickHouse parent process survives into every executable-pool worker. Use a wrapper that creates the descriptor, an explicitly supplied environment variable, or an authentication proxy.

Never put a key in SQL, a command-line argument, a generated configuration file or a committed file. Errors omit remote response bodies and credential values. A local mock or authenticated proxy can run without either credential option. Plain HTTP requires `--allow-http`.

## API compatibility

The worker sends a JSON object containing `model`, `state` and `questions`. `state` contains `condition` and `rows`; `questions` uses keys `r0`, `r1`, and so on. The default `state` encoding is a JSON string. Use `--state-format object` only with a compatible endpoint.

Responses must match `--expected-model` exactly and provide one answer for every request key. The supported kinds are `noul`, `choice` and `score`. See [protocol.py](../clickhouse_jev/protocol.py) and [the mock server](../tests/mock_api.py) for the implemented contract.
