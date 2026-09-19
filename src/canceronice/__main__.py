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
    from . import places
    pl = sub.add_parser("places", help="land a CDC PLACES county-data release, then derive")
    pl.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    pl.add_argument("--places-release", dest="places_release", choices=sorted(places.RELEASES),
                    help="PLACES county-data release year (default: latest known)")

    # --- raw: usda ers rucc ---
    from . import ers_rucc
    rc = sub.add_parser("rucc", help="land the USDA ERS Rural-Urban Continuum Codes, then derive")
    rc.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    rc.add_argument("--url", help="an already-downloaded CSV file or alternate URL")

    # --- derived: geography alias ---
    # geography.alias — FIPS renames and recodes that are not boundary changes (#26).
    from . import geography_alias
    ga = sub.add_parser("geo-alias", help="land the curated county-recode CSV, then derive "
                        "geography.alias")
    ga.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    ga.add_argument("--path", help="an alternate curated CSV; defaults to the packaged one")

    # --- derived: measure cancer site ---
    # measure.cancer_site — SEER site recode <-> ICD-O-3 <-> ICD-10 <-> NCIt / MONDO (#31).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: cdc atsdr svi ---
    # CDC/ATSDR Social Vulnerability Index, every published edition (#40).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: usda ers ruca ---
    from . import ers_ruca
    ru = sub.add_parser("ruca", help="land a USDA ERS RUCA tract edition, then derive")
    ru.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    ru.add_argument("--edition", choices=sorted(ers_ruca.EDITIONS),
                    help="RUCA edition (default: latest known)")
    ru.add_argument("--url", help="an already-downloaded file or alternate URL")

    # --- raw: usda ers food access ---
    from . import ers_food_access
    fa = sub.add_parser("food-access", help="land a USDA ERS Food Access Research Atlas "
                        "edition, then derive")
    fa.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    fa.add_argument("--edition", choices=sorted(ers_food_access.EDITIONS),
                    help="atlas edition (default: latest known)")
    fa.add_argument("--url", help="an already-downloaded zip/csv/xlsx file or alternate URL")

    # --- raw: hrsa ahrf ---
    # HRSA Area Health Resources Files, county (#39).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: hrsa hpsa and health centers ---
    # HRSA HPSA designations and health-center sites; declares facility.site (#38).
    from . import hrsa_sites
    hs = sub.add_parser("hrsa-sites", help="land HRSA health-center sites + primary-care HPSA "
                        "designations, then derive facility.site and county HPSA measures")
    hs.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    hs.add_argument("--hc-url", dest="hc_url", help="an already-downloaded health-center sites CSV or alternate URL")
    hs.add_argument("--hpsa-url", dest="hpsa_url", help="an already-downloaded HPSA primary-care CSV or alternate URL")
    hs.add_argument("--retrieved-on", dest="retrieved_on",
                    help="ISO date to record as the version (default: today)")

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
    elif args.cmd == "places":
        _print(places.ingest(cat, args.release, args.places_release))

    # --- raw: usda ers rucc ---
    elif args.cmd == "rucc":
        _print(ers_rucc.ingest(cat, args.release, args.url))

    # --- derived: geography alias ---
    elif args.cmd == "geo-alias":
        _print(geography_alias.ingest(cat, args.release, args.path))

    # --- derived: measure cancer site ---
    # measure.cancer_site — SEER site recode <-> ICD-O-3 <-> ICD-10 <-> NCIt / MONDO (#31).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: cdc atsdr svi ---
    # CDC/ATSDR Social Vulnerability Index, every published edition (#40).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: usda ers ruca ---
    elif args.cmd == "ruca":
        _print(ers_ruca.ingest(cat, args.release, args.edition, args.url))

    # --- raw: usda ers food access ---
    elif args.cmd == "food-access":
        _print(ers_food_access.ingest(cat, args.release, args.edition, args.url))

    # --- raw: hrsa ahrf ---
    # HRSA Area Health Resources Files, county (#39).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: hrsa hpsa and health centers ---
    # HRSA HPSA designations and health-center sites; declares facility.site (#38).
    elif args.cmd == "hrsa-sites":
        _print(hrsa_sites.ingest(cat, args.release, args.hc_url, args.hpsa_url, args.retrieved_on))

    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
