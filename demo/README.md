# Real-server demo

The demo exercises the installed UDF against a real ClickHouse server. All rows are synthetic, and a deterministic mock supplies the model responses. It verifies the connection and execution behavior, not model accuracy.

## Run

From the repository root:

```sh
docker compose up --build --exit-code-from demo --attach demo
```

The shared image is built once. Compose waits for the mock and ClickHouse health checks before running the client. The client registers the SQL wrappers, creates the dedicated `jev_demo.items` table, and asserts the filter, classification, score and normalized-score results.

The first build downloads an image and system packages. Runtime services use an internal Docker network and publish no host ports. The public `demo` / `demo` credentials are only for this disposable example; no external account or API key is used.

Successful execution ends with `Demo passed` and exit status 0. A setup error, query failure or result mismatch makes the command fail. Once the client exits, Compose stops the other services.

```sh
docker compose down --volumes
```

This removes the demo's containers and network. Storage uses temporary filesystems and is not retained after the containers stop.

## Interactive SQL

To keep the server running:

```sh
docker compose up --build --wait clickhouse
docker compose run --rm --no-deps demo
docker compose exec clickhouse clickhouse-client --user demo --password demo
```

The client run prepares the synthetic table and checks the results. It replaces the demo table on every invocation. In the interactive shell, try:

```sql
SELECT id, item, jev_prob(toJSONString(map('color', color)), 'Is the item red?') AS probability
FROM jev_demo.items
ORDER BY id
LIMIT 10
SETTINGS max_rows_to_read=10, max_execution_time=10;
```

Use `docker compose down --volumes` when finished. This setup does not modify an existing ClickHouse instance.

## Failure check

After a successful build, deliberately reject mock requests:

```sh
JEV_DEMO_MOCK_MODE=error docker compose up --no-build --force-recreate --exit-code-from demo --attach demo
```

Expect `Demo failed` and a nonzero exit status. Run `docker compose down --volumes` afterward. Leaving the variable unset restores the successful mock response mode.

CI runs the normal demo and the failure check. It verifies the demo client's exit status, so a container startup failure is not accepted as proof that query failures propagate.
