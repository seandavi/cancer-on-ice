import argparse

from . import catalog, schemas


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
    # A parallel agent adds an `ingest-*` subparser here.

    # --- raw: cdc places ---
    # A parallel agent adds an `ingest-*` subparser here.

    # --- raw: usda ers rucc ---
    from . import ers_rucc
    rc = sub.add_parser("rucc", help="land the USDA ERS Rural-Urban Continuum Codes, then derive")
    rc.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    rc.add_argument("--url", help="an already-downloaded CSV file or alternate URL")

    args = p.parse_args()

    cat = catalog()
    if args.cmd == "init":
        for identifier in schemas.TABLES:
            schemas.create(cat, identifier)
        print(f"created {len(schemas.TABLES)} tables across {len(schemas.NAMESPACES)} namespaces")

    # --- raw: census gazetteer ---
    # A parallel agent adds its dispatch branch here.

    # --- raw: cdc places ---
    # A parallel agent adds its dispatch branch here.

    # --- raw: usda ers rucc ---
    elif args.cmd == "rucc":
        _print(ers_rucc.ingest(cat, args.release, args.url))

    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
