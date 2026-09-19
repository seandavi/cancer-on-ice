## DuckDB CLI

Everything else on this page is a wrapper around this. No account, no
token, nothing to install beyond `duckdb` itself:

```sh
duckdb -csv -c "
INSTALL iceberg; LOAD iceberg;
ATTACH 'canceronice' AS coi (
    TYPE ICEBERG, URI 'https://icegate-canceronice.seandavi.workers.dev', AUTHORIZATION_TYPE 'none'
);
SELECT * FROM coi.geography.unit LIMIT 5;
"
```

`-csv` is one of several output modes (`-json`, `-table`, `-parquet` isn't
one but `COPY ... TO 'x.parquet'` is); drop it for a formatted table on a
terminal. To save a query's result to a file instead of printing it:

```sh
duckdb -c "
INSTALL iceberg; LOAD iceberg;
ATTACH 'canceronice' AS coi (
    TYPE ICEBERG, URI 'https://icegate-canceronice.seandavi.workers.dev', AUTHORIZATION_TYPE 'none'
);
COPY (SELECT * FROM coi.measure.observation WHERE source = 'PLACES') TO 'places.parquet';
"
```

**`URI` vs `ENDPOINT`.** DuckDB renamed the Iceberg attach option; which one
your build accepts depends on version, and this was tested with two
different ones on this host:

| Tested with | Version | `URI` | `ENDPOINT` |
| --- | --- | --- | --- |
| `duckdb` CLI | `v2.1.0-alpha40519` (nightly) | accepted, no warning | accepted, prints `WARNING: The Iceberg attach option 'endpoint' is deprecated; use 'uri' instead` |
| Python `duckdb` package | `1.5.5` (this repo's own pinned minimum, `duckdb>=1.4`) | rejected: `Unhandled options found: uri` | accepted |

So: use `URI` on a recent DuckDB build; if you get `Unhandled options found:
uri`, your DuckDB predates the rename and `ENDPOINT` (as the README also
notes) is what you need instead. When in doubt, try `URI` first and fall
back to `ENDPOINT` on that error.
