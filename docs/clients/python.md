## Python

No new dependency: `duckdb` (already this project's own dependency,
`duckdb>=1.4` in `pyproject.toml`) is all it takes.

```python
import duckdb
con = duckdb.connect()
con.sql("INSTALL iceberg; LOAD iceberg; "
        "ATTACH 'canceronice' AS coi (TYPE ICEBERG, ENDPOINT 'https://icegate-canceronice.seandavi.workers.dev', AUTHORIZATION_TYPE 'none');")
```

Run here with this project's own pinned `duckdb` (`1.5.5`, from
`uv run python`): `ENDPOINT` is required, not `URI` — that version's
Iceberg extension raised `_duckdb.Error: Unhandled options found: uri`
until swapped for `ENDPOINT`, confirming the README's own "DuckDB &le; 1.5
needs `ENDPOINT`" note. See [duckdb-cli.md](duckdb-cli.md) for the same
comparison against a newer CLI build.
