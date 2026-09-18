import argparse

from . import catalog, census_gazetteer, schemas


def _print(counts):
    for name, c in counts.items():
        print(f"{name:40} {c['written']:>10,} written  {c['unchanged']:>10,} unchanged"
              if isinstance(c, dict) else f"{name:40} {c:>10,} rows")


def main():
    p = argparse.ArgumentParser(prog="canceronice")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create every declared namespace and table")
    sub.add_parser("tables", help="list catalog tables")

    # --- raw: census gazetteer ---
    gz = sub.add_parser("gazetteer", help="land one Census Gazetteer vintage's county + "
                        "tract files, then derive geography.unit")
    gz.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    gz.add_argument("--year", required=True, type=int, help="gazetteer vintage year, e.g. 2024")
    gz.add_argument("--county-url", help="an already-downloaded counties zip/txt; skips download")
    gz.add_argument("--tract-url", help="an already-downloaded tracts zip/txt; skips download")
    gz.add_argument("--state-url", help="an already-downloaded state.txt; skips download")

    # --- raw: cdc places ---
    # A parallel agent adds an `ingest-*` subparser here.

    # --- raw: usda ers rucc ---
    # A parallel agent adds an `ingest-*` subparser here.

    args = p.parse_args()

    cat = catalog()
    if args.cmd == "init":
        for identifier in schemas.TABLES:
            schemas.create(cat, identifier)
        print(f"created {len(schemas.TABLES)} tables across {len(schemas.NAMESPACES)} namespaces")

    # --- raw: census gazetteer ---
    elif args.cmd == "gazetteer":
        _print(census_gazetteer.ingest(cat, args.release, args.year,
                                       args.county_url, args.tract_url, args.state_url))

    # --- raw: cdc places ---
    # A parallel agent adds its dispatch branch here.

    # --- raw: usda ers rucc ---
    # A parallel agent adds its dispatch branch here.

    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
