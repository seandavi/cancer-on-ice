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
    from . import places
    pl = sub.add_parser("places", help="land a CDC PLACES county-data release, then derive")
    pl.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    pl.add_argument("--places-release", dest="places_release", choices=sorted(places.RELEASES),
                    help="PLACES county-data release year (default: latest known)")

    # --- raw: usda ers rucc ---
    # A parallel agent adds an `ingest-*` subparser here.

    args = p.parse_args()

    cat = catalog()
    if args.cmd == "init":
        for identifier in schemas.TABLES:
            schemas.create(cat, identifier)
        print(f"created {len(schemas.TABLES)} tables across {len(schemas.NAMESPACES)} namespaces")

    # --- raw: census gazetteer ---
    # A parallel agent adds its dispatch branch here.

    # --- raw: cdc places ---
    elif args.cmd == "places":
        _print(places.ingest(cat, args.release, args.places_release))

    # --- raw: usda ers rucc ---
    # A parallel agent adds its dispatch branch here.

    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
