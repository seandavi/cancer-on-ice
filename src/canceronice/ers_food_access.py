"""USDA ERS Food Access Research Atlas -> Iceberg, in the same two phases as
the other sources.

Upstream: https://www.ers.usda.gov/data-products/food-access-research-atlas/
download-the-data -- census-tract low-income/low-access ("food desert") flags
and the population counts behind them (SPEC.md § Sources -- first tranche).
As of the site's 2026-07-27 update, ERS has renamed the 2019 product "Large
Retailer Access Map (LRAM) (formerly known as the Food Access Research Atlas
(FARA))" and added a 2025 "SNAP-authorized Retailer Access Map (SRAM)" on
2020 tracts; both still link the original 2019/2015/2010/2006 FARA files
under "Archived Versions" (checked 2026-09-18) -- this module lands those
FARA-era editions only. `source` stays 'FARA' downstream since that is the
name issue #42 and every fixture/measure_id here use.

**Licence.** USDA ERS is a federal agency; its work product is a U.S.
Government work. Per resources.data.gov/open-licenses/ (the definition
data.gov uses for every federal agency's "Public Domain" listing, already
the basis for ers_rucc.py's licence in this repo): "Data and content created
by government employees within the scope of their employment are not
subject to domestic copyright protection under 17 U.S.C. Sec 105. Government
works are by default in the U.S. Public Domain." (checked 2026-09-18). No
registration or attribution is required to redistribute.

**Version axis is the edition.** Verified by downloading each file
(2026-09-18) from the Download-the-Data page's real links:
  - 2019: https://www.ers.usda.gov/media/5627/2019-large-retailer-access-map-
    lram-formerly-known-as-the-food-access-research-atlas-fara-data.zip --
    a zip of a 147-column, comma-delimited, UTF-8 CSV (`Food Access Research
    Atlas.csv`) plus a ReadMe and a VariableLookup sheet, 72,531 data rows.
  - 2015: https://www.ers.usda.gov/media/5623/2015-food-access-research-
    atlas-fara-data-and-documentation.zip -- a zip of a 147-column .xlsx
    (`FoodAccessResearchAtlasData2015.xlsx`, sheet "Food Access Research
    Atlas") plus its PDF documentation, 72,864 data rows. Read with DuckDB's
    `excel` extension (`INSTALL excel; LOAD excel;`) via `read_xlsx` --
    `.xls`-only sources (2006, below) stay out rather than adding a
    dependency, same call as ers_rucc.py's 2013 omission.

Both editions publish the exact same 147 columns in the exact same order --
verified by diffing the two real headers column-for-column -- differing only
in the case of one column: 2019 spells it `Pop2010`, 2015 spells it
`POP2010`. `COLUMNS` below carries the two real (verified) headers keyed by
edition; the landed `raw.ers__food_access` table uses 2019's spelling as the
canonical column name for that field, so `PROVENANCE` needs no separate
union-schema handling the way census_gazetteer.py's three real layouts do.

ponytail: **2010 and the archived 2006 "Food Desert Locator" are not
landed.** 2010 (https://www.ers.usda.gov/media/5624/... .zip, verified
2026-09-18) is a real *third* layout -- 65 columns in a different order, no
race/ethnicity breakdown, no PovertyRate/MedianFamilyIncome, plus `Rural`
and `UATYP10` columns the later editions drop -- tractable in principle but
a third COLUMNS layout is more than this module needs for #42's ask (2019
"and 2015 if its layout is tractable", which it is). 2006
(https://www.ers.usda.gov/media/5625/archived-2006-food-desert-locator.zip)
ships only as a legacy `.xls`, unreadable by DuckDB's `excel` extension
(`.xlsx`-only), same constraint as ers_rucc.py's 2013. Revisit either if a
later milestone needs pre-2015 food-access history.

**Geography vintage: 2010 census tracts, for both landed editions.** ERS's
own Documentation page states the 2019 (LRAM) estimates are "based on ... the
2010 Decennial Census" and the 2015 estimates likewise "based on ... the 2010
Decennial Census" (only the 2025 SRAM, not landed here, moves to 2020 tracts)
-- checked against https://www.ers.usda.gov/data-products/food-access-
research-atlas/documentation, 2026-09-18. The downloaded files confirm it
directly: Connecticut's tracts in both editions keep the legacy county-based
11-digit FIPS prefix 09001-09015 (e.g. tract 09001010101, Fairfield County),
not the 2022 planning-region prefixes (09110-09190) that show up in
county-level sources landed elsewhere in this repo from their 2022+ releases
(ers_rucc.py, places.py) -- 2010-vintage Census *tract* geography is
unaffected by that later *county-level* administrative reorganization.

**Missing-value sentinel differs by edition -- enumerated, not guessed.**
2019's CSV uses the literal text `NULL` for every missing count/share cell
(e.g. 71,025 of 72,531 rows for `lapop20`); there are zero genuinely blank
cells anywhere in the file (checked with DuckDB's `nullstr=''`, i.e. an
empty field would already read as SQL NULL and none do). 2015's xlsx has no
missing-value sentinel at all for any column this module derives -- every
cell holds a real number, 0 where a distance threshold has no low-access
population, confirmed by reading the raw sheet XML directly (e.g. `lapop20`
cells are literal `<v>0</v>`, not blank). `_check_sentinels` fails loudly on
any non-numeric, non-'NULL' text in a derived column, rather than silently
mis-parsing a third convention this module hasn't seen.

**Share columns are on different scales per edition -- a real landmine.**
Recomputing from the published counts: 2015's `...share` columns are 0-1
fractions (tract 01001020100's `lapop1share` = 0.70998, and
`lapop1` / `Pop2010` = 1357.48 / 1912 = 0.70998 exactly); 2019's are 0-100
percentages (the same tract's `lapop1` / `Pop2010` * 100 = 1896 / 1912 * 100
= 99.16, rounding to the published 99.19 -- the small residual is the
published integer counts themselves being rounded from the same underlying
floats 2015 keeps unrounded). `SHARE_SCALE` normalizes both onto the 0-100
percent scale for `measure.observation.value` (`rate_basis='percent'`) so
the two editions are comparable there; `raw.ers__food_access` keeps each
edition's own published scale verbatim, unmodified (SPEC.md: raw lands
whole).

ponytail: only 10 of the 147 columns are derived into `measure.observation`
-- the 4 LILA flags plus the half-mile and 1-mile population / low-income /
no-vehicle share triples, the distance thresholds a catchment access-gap
recipe (SPEC.md § Recipes #3) actually reaches for. The 10-mile and 20-mile
thresholds, every race/ethnicity breakdown (`la*1`, `la*10`, `la*20`, `la*half`
per race), SNAP, poverty rate, median family income, and the group-quarters
columns all land in `raw.ers__food_access` and stay queryable there via SQL;
add a `measure.definition` row alongside these if a later recipe needs one.
"""

import tempfile
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge

USER_AGENT = "cancerOnIce/1.0 (+https://github.com/seandavi/cancer-on-ice)"
GEO_VINTAGE = 2010

EDITIONS = {
    "2019": dict(
        url="https://www.ers.usda.gov/media/5627/2019-large-retailer-access-map-lram-"
            "formerly-known-as-the-food-access-research-atlas-fara-data.zip",
        suffix=".csv",
    ),
    "2015": dict(
        url="https://www.ers.usda.gov/media/5623/2015-food-access-research-atlas-fara-"
            "data-and-documentation.zip",
        suffix=".xlsx",
        sheet="Food Access Research Atlas",
    ),
}

# 2019's header, in file order -- also the canonical column-name spelling for
# raw.ers__food_access (see module docstring: the two editions publish the
# same 147 columns in the same order, differing only in Pop2010's case).
COLUMNS_2019 = (
    'CensusTract', 'State', 'County', 'Urban', 'Pop2010',
    'OHU2010', 'GroupQuartersFlag', 'NUMGQTRS', 'PCTGQTRS', 'LILATracts_1And10',
    'LILATracts_halfAnd10', 'LILATracts_1And20', 'LILATracts_Vehicle', 'HUNVFlag', 'LowIncomeTracts',
    'PovertyRate', 'MedianFamilyIncome', 'LA1and10', 'LAhalfand10', 'LA1and20',
    'LATracts_half', 'LATracts1', 'LATracts10', 'LATracts20', 'LATractsVehicle_20',
    'LAPOP1_10', 'LAPOP05_10', 'LAPOP1_20', 'LALOWI1_10', 'LALOWI05_10',
    'LALOWI1_20', 'lapophalf', 'lapophalfshare', 'lalowihalf', 'lalowihalfshare',
    'lakidshalf', 'lakidshalfshare', 'laseniorshalf', 'laseniorshalfshare', 'lawhitehalf',
    'lawhitehalfshare', 'lablackhalf', 'lablackhalfshare', 'laasianhalf', 'laasianhalfshare',
    'lanhopihalf', 'lanhopihalfshare', 'laaianhalf', 'laaianhalfshare', 'laomultirhalf',
    'laomultirhalfshare', 'lahisphalf', 'lahisphalfshare', 'lahunvhalf', 'lahunvhalfshare',
    'lasnaphalf', 'lasnaphalfshare', 'lapop1', 'lapop1share', 'lalowi1',
    'lalowi1share', 'lakids1', 'lakids1share', 'laseniors1', 'laseniors1share',
    'lawhite1', 'lawhite1share', 'lablack1', 'lablack1share', 'laasian1',
    'laasian1share', 'lanhopi1', 'lanhopi1share', 'laaian1', 'laaian1share',
    'laomultir1', 'laomultir1share', 'lahisp1', 'lahisp1share', 'lahunv1',
    'lahunv1share', 'lasnap1', 'lasnap1share', 'lapop10', 'lapop10share',
    'lalowi10', 'lalowi10share', 'lakids10', 'lakids10share', 'laseniors10',
    'laseniors10share', 'lawhite10', 'lawhite10share', 'lablack10', 'lablack10share',
    'laasian10', 'laasian10share', 'lanhopi10', 'lanhopi10share', 'laaian10',
    'laaian10share', 'laomultir10', 'laomultir10share', 'lahisp10', 'lahisp10share',
    'lahunv10', 'lahunv10share', 'lasnap10', 'lasnap10share', 'lapop20',
    'lapop20share', 'lalowi20', 'lalowi20share', 'lakids20', 'lakids20share',
    'laseniors20', 'laseniors20share', 'lawhite20', 'lawhite20share', 'lablack20',
    'lablack20share', 'laasian20', 'laasian20share', 'lanhopi20', 'lanhopi20share',
    'laaian20', 'laaian20share', 'laomultir20', 'laomultir20share', 'lahisp20',
    'lahisp20share', 'lahunv20', 'lahunv20share', 'lasnap20', 'lasnap20share',
    'TractLOWI', 'TractKids', 'TractSeniors', 'TractWhite', 'TractBlack',
    'TractAsian', 'TractNHOPI', 'TractAIAN', 'TractOMultir', 'TractHispanic',
    'TractHUNV', 'TractSNAP',
)
# 2015's only difference from 2019 is POP2010's case (see module docstring).
COLUMNS = {
    "2019": COLUMNS_2019,
    "2015": tuple("POP2010" if c == "Pop2010" else c for c in COLUMNS_2019),
}

# column -> doc, for the four 0/1 low-income-low-access flags (rate_basis='index').
FLAG_MEASURES = {
    "LILATracts_1And10": "Low-income and low-access tract, measured at 1 mile for urban "
        "areas and 10 miles for rural areas.",
    "LILATracts_halfAnd10": "Low-income and low-access tract, measured at 1/2 mile for "
        "urban areas and 10 miles for rural areas.",
    "LILATracts_1And20": "Low-income and low-access tract, measured at 1 mile for urban "
        "areas and 20 miles for rural areas.",
    "LILATracts_Vehicle": "Low-income and low-access tract using vehicle access, or "
        "low-income and low-access tract measured at 20 miles.",
}
# column -> (numerator column, denominator column, universe, doc), for the
# half-mile and 1-mile share measures (rate_basis='percent').
SHARE_MEASURES = {
    "lapophalfshare": ("lapophalf", "Pop2010", "tract total population (2010 Census)",
        "Share of tract population living beyond 1/2 mile from the nearest supermarket."),
    "lalowihalfshare": ("lalowihalf", "Pop2010", "tract total population (2010 Census)",
        "Share of tract population that is low-income, living beyond 1/2 mile from the "
        "nearest supermarket."),
    "lahunvhalfshare": ("lahunvhalf", "OHU2010", "tract occupied housing units (2010 Census)",
        "Share of tract housing units without a vehicle, living beyond 1/2 mile from the "
        "nearest supermarket."),
    "lapop1share": ("lapop1", "Pop2010", "tract total population (2010 Census)",
        "Share of tract population living beyond 1 mile from the nearest supermarket."),
    "lalowi1share": ("lalowi1", "Pop2010", "tract total population (2010 Census)",
        "Share of tract population that is low-income, living beyond 1 mile from the "
        "nearest supermarket."),
    "lahunv1share": ("lahunv1", "OHU2010", "tract occupied housing units (2010 Census)",
        "Share of tract housing units without a vehicle, living beyond 1 mile from the "
        "nearest supermarket."),
}
# 2015's shares are 0-1 fractions, 2019's are 0-100 percentages (module
# docstring); this normalizes measure.observation.value to percent for both.
SHARE_SCALE = {"2019": 1, "2015": 100}


def _member(url, tmpdir, suffix):
    """The edition's data file at `url`: downloaded and unzipped if it's a
    zip, used as-is otherwise (a local .csv/.xlsx, how offline tests stay
    offline)."""
    if url.startswith("http"):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        zpath = Path(tmpdir) / "atlas.zip"
        with urllib.request.urlopen(req) as r, open(zpath, "wb") as f:
            f.write(r.read())
        src = zpath
    else:
        src = Path(url)
    if src.suffix != ".zip":
        return src
    with zipfile.ZipFile(src) as z:
        # Each zip also carries a ReadMe and a VariableLookup sheet (2019) or
        # the PDF documentation (2015) -- excluded by name so exactly one
        # member matches the data file's own suffix.
        names = [n for n in z.namelist() if n.endswith(suffix)
                 and "readme" not in n.lower() and "variablelookup" not in n.lower()]
        if len(names) != 1:
            raise SystemExit(f"ers_food_access: {url} has {len(names)} {suffix} data "
                             f"member(s) among {z.namelist()}, expected 1")
        z.extract(names[0], tmpdir)
        return Path(tmpdir) / names[0]


def _source_sql(path, meta):
    if meta["suffix"] == ".csv":
        # Dialect stated, not sniffed: plain comma-delimited, double-quoted
        # where a cell needs it. nullstr='' -- a genuinely blank cell (none
        # seen in the real files) is NULL; the 'NULL' sentinel text is not.
        return (f"read_csv('{path}', header=true, all_varchar=true, delim=',', "
                f"quote='\"', escape='\"', nullstr='')")
    return f"read_xlsx('{path}', sheet='{meta['sheet']}', all_varchar=true)"


def _header(con, path, meta):
    if meta["suffix"] == ".csv":
        with open(path, encoding="utf-8") as f:
            return tuple(f.readline().rstrip("\r\n").split(","))
    return tuple(con.sql(f"SELECT * FROM {_source_sql(path, meta)} LIMIT 0").columns)


def land_raw(cat, release, edition=None, url=None):
    """Phase 1: one FARA edition, verbatim and whole, replaced per edition."""
    edition = edition or max(EDITIONS, key=int)
    if edition not in EDITIONS:
        raise SystemExit(f"ers_food_access: unknown edition {edition!r}; "
                         f"known editions: {sorted(EDITIONS)}")
    meta = EDITIONS[edition]
    fetch_url = url or meta["url"]

    con = duckdb.connect()
    if meta["suffix"] == ".xlsx":
        con.sql("INSTALL excel; LOAD excel;")
    with tempfile.TemporaryDirectory() as tmp:
        path = _member(fetch_url, tmp, meta["suffix"])
        if (header := _header(con, path, meta)) != COLUMNS[edition]:
            raise SystemExit(f"ers_food_access: {fetch_url} header is not the declared "
                             f"{edition} layout; differs in "
                             f"{sorted(set(header) ^ set(COLUMNS[edition]))}")

        # Select in the source's own column order, aliased to 2019's spelling
        # (the one case difference -- see module docstring).
        select = ", ".join(f'"{src}" AS "{canon}"'
                           for src, canon in zip(COLUMNS[edition], COLUMNS_2019))
        arrow = con.sql(f"""
            SELECT {select}, '{edition}' AS atlas_edition, '{release}' AS landed_in
            FROM {_source_sql(path, meta)}
        """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"ers_food_access: {fetch_url} yielded no rows")

    n = merge.write(cat, "raw.ers__food_access", arrow, EqualTo("atlas_edition", edition))
    merge.manifest(cat, release, "ers_food_access", fetch_url, n, version=edition,
                   method="release_number")
    return edition, n


def _check_sentinels(con, edition, columns):
    """Every value in `columns` must be either the edition's 'NULL' sentinel
    (see module docstring -- 2015 has none) or something TRY_CAST can parse
    as a number. Anything else is a missing-value convention this module
    hasn't enumerated, and is a hard stop rather than a silent NULL."""
    checks = " UNION ALL ".join(
        f'SELECT \'{c}\' AS col, "{c}" AS val FROM raw '
        f'WHERE "{c}" != \'NULL\' AND TRY_CAST("{c}" AS DOUBLE) IS NULL'
        for c in columns)
    bad = con.sql(checks).fetchall()
    if bad:
        raise SystemExit(f"ers_food_access: unmapped non-numeric value(s) in {edition}: "
                         f"{sorted(set(bad))}")


def transform(cat, release, edition):
    """Phase 2: the 4 LILA flags + 6 half-mile/1-mile share measures (module
    docstring), plus their measure.definition and shared stratum.

    Scoped to `edition`'s rows: raw accumulates every landed edition, so an
    unscoped read would derive from all of them at once.
    """
    scale = SHARE_SCALE[edition]
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.ers__food_access").scan(
        row_filter=EqualTo("atlas_edition", edition)).to_arrow())

    numeric_cols = set(FLAG_MEASURES) | set(SHARE_MEASURES)
    for num, den, _, _ in SHARE_MEASURES.values():
        numeric_cols |= {num, den}
    _check_sentinels(con, edition, numeric_cols)

    definition = pa.Table.from_pylist(
        [dict(measure_id=f"FARA:{col}", source="FARA", label=col, units="flag (0/1)",
              universe="census tract", rate_basis="index", age_adjustment=None,
              method="derived", cancer_site_code=None, doc=doc)
         for col, doc in FLAG_MEASURES.items()] +
        [dict(measure_id=f"FARA:{col}", source="FARA", label=col, units="percent",
              universe=universe, rate_basis="percent", age_adjustment=None,
              method="derived", cancer_site_code=None, doc=doc)
         for col, (_, _, universe, doc) in SHARE_MEASURES.items()])

    # FARA publishes no stratification within a tract estimate -- one
    # all-persons stratum covers every row (same pattern as ers_rucc.py).
    stratum = pa.Table.from_pylist([dict(
        stratum_id="FARA:ALL", source="FARA", sex=None, age_group=None,
        race_ethnicity=None, stage=None, other=None, scheme="FARA_NONE",
    )])

    geo_id = "'tract:' || lpad(CensusTract, 11, '0')"
    flag_sql = " UNION ALL ".join(f"""
        SELECT 'FARA' AS source, '{edition}' AS source_release, 'FARA:{col}' AS measure_id,
               {geo_id} AS geo_id, {GEO_VINTAGE} AS geo_vintage,
               '{edition}' AS period_start, '{edition}' AS period_end, 'FARA:ALL' AS stratum_id,
               TRY_CAST("{col}" AS DOUBLE) AS value,
               NULL::DOUBLE AS lower, NULL::DOUBLE AS upper, NULL::DOUBLE AS interval_level,
               NULL::DOUBLE AS numerator, NULL::DOUBLE AS denominator,
               CASE WHEN TRY_CAST("{col}" AS DOUBLE) IS NOT NULL THEN 'reported'
                    ELSE 'not_available' END AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM raw
    """ for col in FLAG_MEASURES)
    share_sql = " UNION ALL ".join(f"""
        SELECT 'FARA' AS source, '{edition}' AS source_release, 'FARA:{col}' AS measure_id,
               {geo_id} AS geo_id, {GEO_VINTAGE} AS geo_vintage,
               '{edition}' AS period_start, '{edition}' AS period_end, 'FARA:ALL' AS stratum_id,
               TRY_CAST("{col}" AS DOUBLE) * {scale} AS value,
               NULL::DOUBLE AS lower, NULL::DOUBLE AS upper, NULL::DOUBLE AS interval_level,
               TRY_CAST("{num}" AS DOUBLE) AS numerator, TRY_CAST("{den}" AS DOUBLE) AS denominator,
               CASE WHEN TRY_CAST("{col}" AS DOUBLE) IS NOT NULL THEN 'reported'
                    ELSE 'not_available' END AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM raw
    """ for col, (num, den, _, _) in SHARE_MEASURES.items())
    observation = con.sql(f"{flag_sql} UNION ALL {share_sql}").to_arrow_table()
    merge.check_observations(observation)

    scope = EqualTo("source", "FARA")
    return {
        "measure.definition": merge.write(cat, "measure.definition", definition, scope),
        "measure.stratum": merge.write(cat, "measure.stratum", stratum, scope),
        "measure.observation": merge.merge(
            cat, "measure.observation", observation, release,
            And(scope, EqualTo("source_release", edition))),
    }


def ingest(cat, release, edition=None, url=None):
    edition, n = land_raw(cat, release, edition, url)
    return {"raw.ers__food_access": n, **transform(cat, release, edition)}
