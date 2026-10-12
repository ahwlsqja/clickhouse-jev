# Contributing

Small, focused changes are welcome. Open an issue to discuss changes to the SQL API or request/response contract before implementing them.

## Run the tests

With Python 3.10 or newer:

```sh
python3 -m unittest discover -s tests -v
```

To include the actual ClickHouse executable-UDF integration:

```sh
docker build -t clickhouse-jev .
docker run --rm --network none --entrypoint python3 \
  clickhouse-jev -m unittest discover -s tests -v
```

The image build needs network access to install system packages. The tests themselves use loopback mocks and synthetic fixtures with external networking disabled. No model account or API key is needed.

## Changes

- Add a regression test for a behavior change and check that it fails before the fix.
- Keep runtime dependencies limited to the Python standard library.
- Preserve row ordering, bounded concurrency and the block failure boundary.
- Keep errors free of input rows, remote response bodies and credentials.
- Update the README and installation guide when public behavior changes.
- Include the test command and result in your pull request.

Use invented, minimal examples in issues and tests. Do not attach customer data, production exports, credentials, private traces or internal benchmark artifacts. Describe sensitive failures using a synthetic reproduction.
