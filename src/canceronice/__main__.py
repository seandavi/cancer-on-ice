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
    pl.add_argument("--places-release", dest="places_release",
                    choices=sorted(set(places.RELEASES) | set(places.TRACT_RELEASES)),
                    help="PLACES release year (default: latest known for --level)")
    pl.add_argument("--level", choices=("county", "tract"), default="county",
                    help="geography level to land (default: county) (#30)")
    pl.add_argument("--url", help="an already-downloaded CSV file or alternate URL")

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
    from . import cancer_site
    cs = sub.add_parser("cancer-site", help="land the SEER site recode + cause-of-death recode, "
                        "then derive measure.cancer_site")
    cs.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    cs.add_argument("--site-url", help="an already-downloaded site-recode text file or alternate URL")
    cs.add_argument("--cod-url", help="an already-downloaded cause-of-death recode text file or alternate URL")

    # --- raw: cdc atsdr svi ---
    from . import cdc_svi
    sv = sub.add_parser("svi", help="land one CDC/ATSDR SVI edition's county or tract "
                        "file, then derive")
    sv.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    sv.add_argument("--edition", choices=sorted(cdc_svi.EDITIONS, key=int),
                    help="SVI edition (default: latest known)")
    sv.add_argument("--level", choices=("county", "tract"), default="county",
                    help="geography level to land (default: county; tract is only "
                         f"landed for {cdc_svi.TRACT_EDITIONS})")
    sv.add_argument("--url", help="an already-downloaded CSV file or alternate URL")

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
    from . import hrsa_ahrf
    ah = sub.add_parser("ahrf", help="land the HRSA Area Health Resources Files (county), then derive")
    ah.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    ah.add_argument("--ahrf-release", dest="ahrf_release", choices=sorted(hrsa_ahrf.RELEASES),
                    help="AHRF release, e.g. 2024-2025 (default: latest known)")
    ah.add_argument("--csv-url", help="an already-downloaded AHRF county CSV (or its zip); "
                    "skips download")
    ah.add_argument("--techdoc-url", help="an already-downloaded technical documentation "
                    "xlsx (or its zip); skips download")

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

    # --- raw: state cancer profiles ---
    # State Cancer Profiles, all vintages (#27).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: census acs ---
    # ACS 5-year, the Cancer InFocus indicator subset (#29).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: cdc places tract ---
    # CDC PLACES tract releases (#30).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: fda mqsa ---
    # FDA MQSA certified mammography facilities (#36).
    from . import fda_mqsa
    mq = sub.add_parser("mqsa", help="land the weekly FDA MQSA certified facility list, "
                        "then derive facility.site")
    mq.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    mq.add_argument("--url", help="an already-downloaded zip/txt file or alternate URL")
    mq.add_argument("--retrieved-on", dest="retrieved_on",
                    help="ISO date to record as the version (default: today)")

    # --- raw: epa sdwis ---
    from . import epa_sdwis
    sd = sub.add_parser("sdwis", help="land one EPA SDWIS quarterly SDWA bulk download, "
                        "then derive county-year health-based violation counts")
    sd.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    sd.add_argument("--url", help="an already-downloaded SDWA zip file or alternate URL")
    sd.add_argument("--pws-url", dest="pws_url", help="an already-downloaded pub_water_systems CSV")
    sd.add_argument("--geo-url", dest="geo_url", help="an already-downloaded geographic_areas CSV")
    sd.add_argument("--viol-url", dest="viol_url", help="an already-downloaded violations_enforcement CSV")
    sd.add_argument("--ansi-url", dest="ansi_url", help="an already-downloaded ref_ansi_areas CSV")

    # --- raw: fcc broadband ---
    # FCC Broadband Data Collection (#54).
    # The local `from . import <module>` and the subparser goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: bls laus ---
    # BLS Local Area Unemployment Statistics, county (#94).
    from . import bls_laus
    bl = sub.add_parser("laus", help="land a BLS LAUS county retrieval vintage, then derive")
    bl.add_argument("--release", required=True, help="cancerOnIce release, e.g. 2026.09")
    bl.add_argument("--county-url", dest="county_url",
                    help="an already-downloaded la.data.64.County file or alternate URL")
    bl.add_argument("--area-url", dest="area_url", help="an already-downloaded la.area file or alternate URL")
    bl.add_argument("--series-url", dest="series_url", help="an already-downloaded la.series file or alternate URL")
    bl.add_argument("--measure-url", dest="measure_url", help="an already-downloaded la.measure file or alternate URL")
    bl.add_argument("--footnote-url", dest="footnote_url", help="an already-downloaded la.footnote file or alternate URL")
    bl.add_argument("--vintage", help="ISO date to record as the version (default: today)")
    bl.add_argument("--since", type=int,
                    help="derive years >= this (default: this year minus 10)")

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
        _print(places.ingest(cat, args.release, args.places_release, args.url, args.level))

    # --- raw: usda ers rucc ---
    elif args.cmd == "rucc":
        _print(ers_rucc.ingest(cat, args.release, args.url))

    # --- derived: geography alias ---
    elif args.cmd == "geo-alias":
        _print(geography_alias.ingest(cat, args.release, args.path))

    # --- derived: measure cancer site ---
    elif args.cmd == "cancer-site":
        _print(cancer_site.ingest(cat, args.release, args.site_url, args.cod_url))

    # --- raw: cdc atsdr svi ---
    elif args.cmd == "svi":
        _print(cdc_svi.ingest(cat, args.release, args.edition, args.level, args.url))

    # --- raw: usda ers ruca ---
    elif args.cmd == "ruca":
        _print(ers_ruca.ingest(cat, args.release, args.edition, args.url))

    # --- raw: usda ers food access ---
    elif args.cmd == "food-access":
        _print(ers_food_access.ingest(cat, args.release, args.edition, args.url))

    # --- raw: hrsa ahrf ---
    # HRSA Area Health Resources Files, county (#39).
    elif args.cmd == "ahrf":
        _print(hrsa_ahrf.ingest(cat, args.release, args.ahrf_release, args.csv_url,
                                args.techdoc_url))

    # --- raw: hrsa hpsa and health centers ---
    # HRSA HPSA designations and health-center sites; declares facility.site (#38).
    elif args.cmd == "hrsa-sites":
        _print(hrsa_sites.ingest(cat, args.release, args.hc_url, args.hpsa_url, args.retrieved_on))

    # --- raw: state cancer profiles ---
    # State Cancer Profiles, all vintages (#27).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: census acs ---
    # ACS 5-year, the Cancer InFocus indicator subset (#29).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: cdc places tract ---
    # CDC PLACES tract releases (#30).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: fda mqsa ---
    # FDA MQSA certified mammography facilities (#36).
    elif args.cmd == "mqsa":
        _print(fda_mqsa.ingest(cat, args.release, args.url, args.retrieved_on))

    # --- raw: epa sdwis ---
    elif args.cmd == "sdwis":
        _print(epa_sdwis.ingest(cat, args.release, args.url, args.pws_url, args.geo_url,
                                args.viol_url, args.ansi_url))

    # --- raw: fcc broadband ---
    # FCC Broadband Data Collection (#54).
    # The `elif args.cmd == ...` dispatch branch goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: bls laus ---
    # BLS Local Area Unemployment Statistics, county (#94).
    elif args.cmd == "laus":
        _print(bls_laus.ingest(cat, args.release, args.county_url, args.area_url, args.series_url,
                               args.measure_url, args.footnote_url, args.vintage, args.since))

    else:
        for ns in cat.list_namespaces():
            for t in cat.list_tables(ns):
                print(".".join(t))


if __name__ == "__main__":
    main()
