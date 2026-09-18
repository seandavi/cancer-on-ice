"""CDC PLACES: Local Data for Better Health, COUNTY data -> Iceberg.

Model-based small-area estimates for chronic disease, screening and behavioral
risk factors, at (county, measure, stratification-type) grain (SPEC.md §
Sources — first tranche: "model-based tract/county screening & behaviors").

**Licence.** Public domain. The dataset's own Socrata metadata states
`"license": {"name": "Public Domain"}` (checked against
https://data.cdc.gov/api/views/swc5-untb.json, 2026-09-18), attributed to
"Centers for Disease Control and Prevention, National Center for Chronic
Disease Prevention and Health Promotion, Division of Population Health"; see
also https://www.cdc.gov/places.

**Version axis: the annual release is a separate Socrata dataset.** PLACES
does not version one URL — each year's county file is its own dataset id on
data.cdc.gov, discovered via the Socrata catalog API
(`https://api.us.socrata.com/api/catalog/v1?domains=data.cdc.gov&q=PLACES
county data release`, checked 2026-09-18). `RELEASES` records every
long-format "...County Data <year> release" id found that way — NOT the
parallel "County Data (GIS Friendly Format)" datasets, which are wide
(one row per county) rather than the long (one row per county/measure/type)
shape this module wants. `source_release` downstream is the release year
label itself (PLACES publishes no separate release-number scheme).

**The header is not stable across editions.** `COLUMNS` states the CURRENT
shape — verified identical on the 2024 and 2025 releases, the two this module
actually lands (see docstring below) — and lands only those; an older
release fails the header check by design rather than silently mis-landing
(mirrors hgnc.py's dated-archive handling):
  - the 2020 export (`dv4u-3x3q`) has no `LocationID` at all — a `Latitude`
    column holding real latitude text sits in its place, and its point column
    is misspelled `Geolocatioin` — upstream's own defect, confirmed against
    https://data.cdc.gov/api/views/dv4u-3x3q.json's column list, not a scrape
    artifact.
  - 2021-2023 lack `TotalPop18plus` (added starting with the 2024 release).
ponytail: only the 2024/2025 header is supported. Landing an older release
means declaring its own COLUMNS first, same as hgnc.py's dated archives.

**Geography vintage moves mid-series.** Connecticut's `LocationID`s are the
legacy 8 counties (09001-09015) through the 2023 release and the 9 planning
regions (09110-09190) from the 2024 release on — verified directly against
each release's real file (PLACES' own methodology notes the 2022 Census
change but not which release picks it up). `GEO_VINTAGE` records 2010 for the
legacy-county releases and 2020 for the planning-region ones, the same two
vintage labels SPEC.md's Milestones use for the pre/post-2022 Census county
definitions.

**Suppression.** The only footnote either landed release carries is
"Estimates suppressed for population less than 50" (0 rows in the 2024
release, 66 in 2025) -> `value_status = 'suppressed_small_count'`.
`FOOTNOTE_STATUS` enumerates it explicitly; any other footnote text raises
`SystemExit` in `transform` rather than guessing.

**No cervix/lung screening measure in county data.** The county file's 40
measures include `COLON_SCREEN` (colorectal) and `MAMMOUSE` (mammography) but
no cervical-cancer-screening measure — BRFSS dropped that module from the
data years this dataset draws on. All 40 measures land and derive regardless
of cancer relevance, per instructions.

ponytail: `TotalPopulation`/`TotalPop18plus` are NOT this measure's
denominator — PLACES publishes model-based prevalence with no published
numerator/denominator pair, only the population the estimate covers. They
land in raw and stay there; `measure.observation.denominator` is NULL.

ponytail: `measure.definition` has no `valid_from`/`valid_to` (SPEC.md calls
it "replaced wholesale per source"), so it has no history either. The 2024
release's `ISOLATION` measure became `LONELINESS` in 2025 (same slot,
different MeasureId) — real data, checked directly. Deriving 2025 after 2024
therefore drops the `PLACES:ISOLATION:*` definition rows even though their
2024 `measure.observation` rows are untouched (measure.observation IS
Type-2 versioned and scoped by source_release). This is the FK-outlives-its-
definition gap SPEC.md's versioning model already leaves unsettled for
measure.definition generally; not something to invent a fix for here.
"""

import re
import urllib.request

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge

# data.cdc.gov dataset id for each "PLACES: Local Data for Better Health,
# County Data <year> release" (long format; NOT "GIS Friendly Format", which
# is wide). Found via the Socrata catalog API, confirmed against each
# dataset's own metadata, 2026-09-18.
RELEASES = {
    "2020": "dv4u-3x3q",
    "2021": "pqpp-u99h",
    "2022": "duw2-7jbt",
    "2023": "h3ej-a9ec",
    "2024": "fu4u-a9bh",
    "2025": "swc5-untb",
}

# Connecticut's county -> planning-region switch (Census, 2022) lands in the
# 2024 PLACES release (LocationIDs 09110-09190); 2020-2023 still carry the 8
# legacy counties (09001-09015) — checked against each release's real file.
GEO_VINTAGE = {"2020": 2010, "2021": 2010, "2022": 2010, "2023": 2010,
               "2024": 2020, "2025": 2020}

# The CSV header, in file order — the 2024/2025 shape (see module docstring
# for why earlier releases differ and are not supported by this contract).
COLUMNS = (
    "Year", "StateAbbr", "StateDesc", "LocationName", "DataSource", "Category",
    "Measure", "Data_Value_Unit", "Data_Value_Type", "Data_Value",
    "Data_Value_Footnote_Symbol", "Data_Value_Footnote", "Low_Confidence_Limit",
    "High_Confidence_Limit", "TotalPopulation", "TotalPop18plus", "LocationID",
    "CategoryID", "MeasureId", "DataValueTypeID", "Short_Question_Text",
    "Geolocation",
)

DATA_VALUE_TYPE = {"Crude prevalence": "crude", "Age-adjusted prevalence": "age_adjusted"}

# value_status for every distinct suppression footnote seen in the two landed
# releases (SPEC.md Acceptance C). An unmapped footnote is a hard stop, not a
# silent default.
FOOTNOTE_STATUS = {
    "Estimates suppressed for population less than 50": "suppressed_small_count",
}


def _url(dataset_id):
    return f"https://data.cdc.gov/api/views/{dataset_id}/rows.csv?accessType=DOWNLOAD"


def _header(url):
    """The file's first line, split on comma. Only that line is fetched."""
    if url.startswith("http"):
        req = urllib.request.Request(url, headers={"User-Agent": "cancerOnIce/0.1"})
        with urllib.request.urlopen(req) as r:
            first = r.readline()
    else:
        with open(url) as r:
            first = r.readline()
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    return tuple(first.rstrip("\r\n").split(","))


def _case(column, mapping):
    """SQL CASE over an explicit {value: mapped_value} dict — no ELSE, so an
    unmapped input surfaces as NULL for the caller to catch, never a guess."""
    whens = " ".join(f"WHEN {column} = '{k}' THEN '{v}'" for k, v in mapping.items())
    return f"CASE {whens} END"


def land_raw(cat, release, places_release=None, url=None):
    """Phase 1: one county-data release, verbatim and whole.

    `places_release` selects a year from RELEASES (default: the latest known);
    `url` overrides the download, e.g. an already-fetched fixture in tests.
    Returns (places_release, rows).
    """
    places_release = places_release or max(RELEASES, key=int)
    if places_release not in RELEASES:
        raise SystemExit(f"places: no known dataset id for release {places_release!r}; "
                         f"known releases: {sorted(RELEASES)}")
    fetch_url = url or _url(RELEASES[places_release])
    if (header := _header(fetch_url)) != COLUMNS:
        raise SystemExit(f"places: {fetch_url} header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(COLUMNS))}")

    con = duckdb.connect()
    select = ", ".join(f'"{c}"' for c in COLUMNS)
    # Dialect stated, not sniffed: plain comma-delimited, double-quoted where a
    # cell needs it (Measure text never contains a comma; Geolocation's WKT
    # POINT does not either). all_varchar keeps raw unparsed; an empty cell is
    # PLACES' missing marker and reads as NULL.
    arrow = con.sql(f"""
        SELECT {select}, '{places_release}' AS places_release
        FROM read_csv('{fetch_url}', header=true, all_varchar=true, delim=',',
                      quote='"', escape='"', nullstr='')
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"places: {fetch_url} yielded no rows")

    n = merge.write(cat, "raw.places__county", arrow, EqualTo("places_release", places_release))
    merge.manifest(cat, release, "places", fetch_url, n, version=places_release,
                   method="release_number")
    return places_release, n


def _universe(measure_text):
    """The population a measure is computed over, read off the source's own
    sentence (SPEC.md: 'universe from the measure text where it states one').
    PLACES states it as '... among <universe>'; a bare 'adults' is BRFSS' own
    shorthand for 'adults aged >=18 years', confirmed against the PLACES data
    dictionary's fuller historical measure names (m35w-spkz, 2026-09-18)."""
    m = re.search(r"among (.+)$", measure_text)
    if not m:
        return None
    text = m.group(1)
    return "adults aged >=18 years" if text == "adults" else text


def transform(cat, release, places_release):
    """Phase 2: measure.definition / measure.stratum / measure.observation.

    Scoped to `places_release`'s rows: raw accumulates every landed release,
    so an unscoped read would derive from all of them at once.
    """
    geo_vintage = GEO_VINTAGE[places_release]
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.places__county").scan(
        row_filter=EqualTo("places_release", places_release)).to_arrow())

    # Checked in Python, not SQL: a plain membership test sidesteps quoting a
    # dict of arbitrary text into an IN-list (and an empty dict, when a test
    # wants to force the "unmapped" branch, breaks IN()'s SQL syntax anyway).
    seen_types = {t for (t,) in con.sql("SELECT DISTINCT Data_Value_Type FROM raw").fetchall()}
    if bad_type := seen_types - set(DATA_VALUE_TYPE):
        raise SystemExit(f"places: unknown Data_Value_Type(s): {sorted(bad_type)}")

    footnotes = con.sql("SELECT DISTINCT Data_Value_Footnote FROM raw "
                        "WHERE Data_Value IS NULL").fetchall()
    if unmapped := {f for (f,) in footnotes if f not in FOOTNOTE_STATUS}:
        raise SystemExit(f"places: unmapped suppression footnote(s) in {places_release}: "
                         f"{sorted(unmapped, key=str)}")

    dvt_case = _case("Data_Value_Type", DATA_VALUE_TYPE)

    # One measure.definition row per (MeasureId, Data_Value_Type) — the two
    # variants are different measures (crude vs. age-adjusted prevalence of
    # the same thing), not a stratification of one.
    defs = con.sql(f"""
        SELECT DISTINCT MeasureId, {dvt_case} AS variant, Measure, Category,
               Data_Value_Unit, Short_Question_Text
        FROM raw
    """).fetchall()
    definition = pa.Table.from_pylist([
        dict(measure_id=f"PLACES:{measure_id}:{variant}", source="PLACES",
             label=short_text, units=unit, universe=_universe(measure_text),
             rate_basis="percent",
             age_adjustment="2000 US standard population" if variant == "age_adjusted" else None,
             method="model_based", cancer_site_code=None,
             doc=f"{measure_text}. Category: {category}.")
        for measure_id, variant, measure_text, category, unit, short_text in defs
    ])

    # PLACES publishes no stratification within a county estimate — one
    # all-persons stratum covers every row.
    stratum = pa.Table.from_pylist([dict(
        stratum_id="PLACES:ALL", source="PLACES", sex=None, age_group=None,
        race_ethnicity=None, stage=None, other=None, scheme="PLACES_TOTAL",
    )])

    value_status = _case("Data_Value_Footnote", FOOTNOTE_STATUS)
    observation = con.sql(f"""
        SELECT 'PLACES' AS source, '{places_release}' AS source_release,
               'PLACES:' || MeasureId || ':' || {dvt_case} AS measure_id,
               CASE WHEN StateAbbr = 'US' THEN 'nation:US'
                    ELSE 'county:' || lpad(LocationID, 5, '0') END AS geo_id,
               {geo_vintage} AS geo_vintage,
               Year AS period_start, Year AS period_end,
               'PLACES:ALL' AS stratum_id,
               TRY_CAST(Data_Value AS DOUBLE) AS value,
               TRY_CAST(Low_Confidence_Limit AS DOUBLE) AS lower,
               TRY_CAST(High_Confidence_Limit AS DOUBLE) AS upper,
               0.95 AS interval_level,
               NULL::DOUBLE AS numerator,
               -- TotalPopulation/TotalPop18plus are not this measure's
               -- denominator (see module docstring); they stay in raw only.
               NULL::DOUBLE AS denominator,
               CASE WHEN Data_Value IS NOT NULL THEN 'reported' ELSE {value_status} END
                   AS value_status,
               NULL::VARCHAR AS reliability_flag,
               NULL::VARCHAR AS trend
        FROM raw
    """).to_arrow_table()
    merge.check_observations(observation)

    return {
        "measure.definition": merge.write(cat, "measure.definition", definition,
                                          EqualTo("source", "PLACES")),
        "measure.stratum": merge.write(cat, "measure.stratum", stratum,
                                       EqualTo("source", "PLACES")),
        "measure.observation": merge.merge(
            cat, "measure.observation", observation, release,
            And(EqualTo("source", "PLACES"), EqualTo("source_release", places_release))),
    }


def ingest(cat, release, places_release=None, url=None):
    places_release, n = land_raw(cat, release, places_release, url)
    return {"raw.places__county": n, **transform(cat, release, places_release)}
