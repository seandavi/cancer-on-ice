"""HRSA Area Health Resources Files (AHRF), county level -> Iceberg.

County-level workforce and facility supply data -- the supply-side context
for a catchment (SPEC.md § Sources -- first tranche). Upstream:
https://data.hrsa.gov/topics/health-workforce/ahrf (topic page) and
https://data.hrsa.gov/data/download?data=AHRF (downloads), annual releases.

**Distribution format, checked 2026-09-18.** The download page's real hrefs
(fetched directly, not summarized) show county-level AHRF as ASCII fixed-width
for 1999-2018, and CSV + SAS + ASCII from the 2022-2023 release on
(`AHRF_CSV_2022-2023.zip`, `AHRF%202023-2024%20CSV.zip`,
`AHRF_2024-2025_CSV.zip`). Only those three CSV-format releases are ever
in scope for this module; earlier fixed-width-only editions are left out.

ponytail: only the current 2024-2025 release is landed. 2022-2023 and
2023-2024 are also CSV-format and could be added the same way RUCC's editions
would be, by declaring their own header/COLUMNS constants first -- not done
here since nothing yet needs the history.

**The CSV zip is one enormous county-row-per-file plus redundant subset
extracts, not several independent thematic files.** Downloaded and inspected
directly: `AHRF_2024-2025_CSV.zip` contains `AHRF2025.csv` (3,235 rows x
4,352 columns -- one row per county/county-equivalent, every field) plus
seven "thematic" files (`env`, `exp`, `geo`, `hf`, `hp`, `pop`, `utl`) that
are column *subsets* of the same master file, for people who only want one
category -- verified column-for-column identical to the master file (a
handful of last-column mismatches turned out to be a CRLF artifact of the
comparison, not real data). Landing the one master file is landing the whole
release; the thematic files add nothing.

## Licence -- this is the load-bearing finding of this module

The topic page states, verbatim: "Usage limitations: None" (checked
2026-09-18, https://data.hrsa.gov/topics/health-workforce/ahrf, rendered
page). That is the generic, page-level claim the issue's `license:cleared`
label was presumably set from.

The release's OWN bundled technical documentation
(`AHRF_USER_TECH_2024-2025.zip` -> `Technical Documentation/AHRF 2024-2025
Technical Documentation.xlsx`, downloaded and opened directly, 2026-09-18)
says something narrower and, for two entire categories of field, contrary:

> "The Area Health Resources Files are made available by the Bureau of
> Health Workforce. Reproduction for re-use or sale is not authorized
> without the expressed permission of the Bureau. Further, data from the
> American Dental Association, the American Hospital Association, and the
> American Medical Association are subject to copyright restrictions; these
> data may not be copied or reproduced in whole or in part without the
> prior consent of the copyright owner."

and its own source-abbreviation glossary, in the same file:

> "AHA Survey Database = American Hospital Association Hospital Facilities
> Database (Copyright)"
> "AMA Phys Master File = American Medical Association Physician Master
> File (Copyright)"

The per-field data dictionary in the same document (columns FIELD/CAT/YEAR OF
DATA/VARIABLE NAME/CHARACTERISTICS/SOURCE/DATE ON) names, for every one of
AHRF's ~4,352 county columns, which of ~50 sources it came from. Checked
programmatically against the real 2024-2025 file: 2,653 of the 4,352 columns
are sourced from `AMA Phys Master File`, `AHA Survey Database 22/23`, or `ADA
Masterfile` -- copyrighted, third-party, and (per the quote above) NOT
redistributable without that owner's consent. This includes essentially
every physician-specialty count (oncology, radiation oncology, gastroenterology,
radiology, dermatology, total M.D.s -- all `AMA Phys Master File`) and every
hospital/bed count (`hosp_*`, `*_beds_*` -- all `AHA Survey Database`).

**Consequence for this module, and for the issue that asked for it (#39):**
the issue's requested curated list -- "active MDs, primary-care physicians,
oncologists, gastroenterologists, radiologists, dermatologists, hospitals,
hospital beds" -- is drawn entirely from these two copyrighted sources and is
NOT landed here, in raw or derived, under any circumstance (AGENTS.md hard
gate: "License before ingest... license:unknown is a hard stop"; here the
finding is stronger than "unknown" -- it is affirmatively NOT cleared for
these fields). What ships instead is the curated set built from AHRF's
genuinely public-domain columns: HRSA's own program data, and other federal
agencies' (see `MEASURE_DEFINITIONS` below and the PR description).

The remaining ~1,699 columns (geography/administrative fields with no listed
source, plus fields sourced from the Census Bureau, CMS, USDA ERS, HRSA's own
Data Warehouse/NHSC, EPA, NCHS, BLS, the VA, etc.) are U.S. government work,
public domain per 17 U.S.C. Sec 105 ("Data and content created by government
employees within the scope of their employment are not subject to domestic
copyright protection... Government works are by default in the U.S. Public
Domain.", https://resources.data.gov/open-licenses/) -- the same standing
quote the other sources in this repo cite. `land_raw` computes this
column split itself, at ingest time, straight from the release's own
technical documentation (see `_excluded_columns`), rather than a hand-copied
list going stale.

## Landing WHOLE, in LONG form

AHRF is very wide even after removing the copyrighted third: 1,699 public
columns x 3,235 counties. Measured locally against the real file: declaring
one NestedField (with a `doc`, per house rule) for each of ~1,700 columns is
not a schema anyone could review or maintain, and the vast majority would
never be read by anything this repo derives. So raw lands LONG instead --
`(ahrf_release, file, fips, column_name, value)`, one row per (county,
field) cell, verbatim strings -- which is still landing every eligible cell
of the release whole (SPEC.md ADR-0002's "land raw whole", just at cell
rather than wide-row granularity; ADR-0002 doesn't mandate one physical
column per source field, only that nothing is thrown away or pre-filtered by
guessing what will matter later). Measured: unpivoting the real release
takes ~5s and produces 5,493,030 rows locally.

ponytail: because raw is long and every cell is addressed by name rather than
position, this module's header check does NOT reproduce AHRF's 4,352-column
header verbatim the way the other sources' COLUMNS contracts do (that would
be ~100KB of literal column names for no real safety gain here: a reordered
or additional column is harmless to a name-addressed unpivot). Instead
`land_raw` checks that `fips_st_cnty` exists and that every column this
module actually reads (`CURATED_FIELDS`, below) is present and not excluded
as copyrighted, and fails loudly naming exactly which ones are not -- the
part of "drift" that would actually break something downstream.

## Version axis and the three time axes (SPEC.md § Versioning)

`source_release` is the release label itself, `"2024-2025"` -- AHRF's own
citable edition name (`method="release_number"`), the same for every row
regardless of which data year a given column describes. `period_start` /
`period_end` come from each column's OWN year suffix instead (e.g.
`hospcs_24` describes 2024, `pers_noins_lt65_pct_21` describes 2021) --
exactly the SPEC.md distinction the issue calls out ("One release carries
many data years: the period comes from each field's year suffix, not the
release"). Getting this backwards (using 2024-2025 as the period) would
silently claim every measure describes the same year, which is false --
`np_npi_24` and `np_npi_23`, both in the SAME 2024-2025 release, describe
different data years AND (see below) different geography.

## Geography vintage: inferred per ROW from the real file, not per column family

AHRF's HPSA fields state no vintage of their own, so this is inferred
directly, as SPEC.md requires when a source doesn't document one. The real
2024-2025 file carries BOTH Connecticut's 8 pre-2022 counties (09001-09015)
AND its 9 post-2022 planning regions (09110-09190) as separate rows in the
SAME file -- and likewise both Alaska's pre-2019 Valdez-Cordova (02261) and
its current Chugach/Copper River census areas (02063/02066) -- verified by
downloading and querying the real CSV directly. Different source families
populate different halves of that split: CMS Provider of Services and HRSA
Data Warehouse fields (FQHC/RHC/hospice/ambulatory-surgery-center counts,
HPSA codes) are non-NULL on the legacy CT counties and NULL on the planning
regions; Census SAHIE and CMS NPI File fields are the reverse. More
tellingly, the split isn't even fixed per source: `np_npi_24` (2024 data)
populates only the 9 planning regions, while `np_npi_23` (2023 data, same
column FAMILY, same release) populates only the 8 legacy counties --
confirming Connecticut's real 2022 transition actually reached this
particular CMS feed between the 2023 and 2024 data years. So geo_vintage
here is a property of which FIPS code a ROW carries, not of which measure or
year produced it: `LEGACY_FIPS` (the pre-2022 CT counties plus
Valdez-Cordova) get `2010`; everything else gets `2020`. This one rule
correctly reproduces every case above without touching source family or
year at all.

## Missing data

Every curated column, checked directly against the real file: a source cell
is either a plain non-negative integer/decimal string, or blank. Blank is
AHRF's only missing marker for these columns (no `.`, `-999`, `*` sentinel
seen) and is mapped to `value_status = 'not_available'`, never a number and
never silently dropped -- a blank cell for a county still gets a raw row
(`value = NULL`) and a `measure.observation` row, because AHRF publishes a
column for every county row; it just leaves some cells empty rather than
omitting the row (unlike RUCC's Rose Island, which omits the row entirely).
A `0` (e.g. Chugach's `fedly_qualfd_hlth_ctr_24 = 0`) is a real reported
zero, never read as missing.
"""

import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge

USER_AGENT = "cancerOnIce/1.0 (+https://github.com/seandavi/cancer-on-ice)"

# AHRF release label -> (CSV zip URL, technical documentation zip URL). Only
# the current release is populated; 2023-2024 and 2022-2023 are also
# CSV-format (see module docstring) and could be added the same way, by
# declaring their own header/CURATED_FIELDS check first -- not done here
# since nothing yet needs the history.
RELEASES = {
    "2024-2025": ("https://data.hrsa.gov/DataDownload/AHRF/AHRF_2024-2025_CSV.zip",
                 "https://data.hrsa.gov/DataDownload/AHRF/AHRF_USER_TECH_2024-2025.zip"),
}

CSV_MEMBER = re.compile(r"^AHRF\d{4}\.csv$", re.IGNORECASE)
# The tech-doc zip also carries a data-dictionary CROSSWALK workbook
# (AHRFCrosswalk2025.xlsx) alongside the technical documentation itself --
# verified 2026-09-18 against the real zip -- so match on name, not just
# extension.
TECHDOC_MEMBER = re.compile(r"Technical Documentation.*\.xlsx$", re.IGNORECASE)

# Sources the release's own technical documentation flags as copyrighted
# third-party content (see module docstring) -- never landed, in raw or
# anywhere else. Matched as a prefix against the documentation's own SOURCE
# column (e.g. "AHA Survey Database 23"), not a fixed list of column names,
# so this stays correct without hand-maintaining it.
COPYRIGHT_SOURCE_PREFIXES = ("AMA ", "AHA ", "ADA ")

# Connecticut's 8 pre-2022 counties and Alaska's pre-2019 Valdez-Cordova --
# still carried as separate rows in the current file by some source feeds
# (see module docstring). Everything else is the current (2020-vintage)
# county geography.
LEGACY_FIPS = frozenset({
    "09001", "09003", "09005", "09007", "09009", "09011", "09013", "09015", "02261",
})
LEGACY_GEO_VINTAGE = 2010
CURRENT_GEO_VINTAGE = 2020

SENTINEL = "__CANCERONICE_NULL_SENTINEL__"

# The cancer-relevant curated set (issue #39), restricted to fields NOT
# sourced from AMA/AHA/ADA (see module docstring): (AHRF column, measure_id,
# the data year the column's OWN suffix encodes -- period, not source_release).
CURATED_FIELDS = (
    ("fedly_qualfd_hlth_ctr_24", "AHRF:FQHC", 2024),
    ("fedly_qualfd_hlth_ctr_23", "AHRF:FQHC", 2023),
    ("rural_hlth_clincs_24", "AHRF:RHC", 2024),
    ("rural_hlth_clincs_23", "AHRF:RHC", 2023),
    ("hospcs_24", "AHRF:HOSPICE", 2024),
    ("hospcs_23", "AHRF:HOSPICE", 2023),
    ("ambultry_surg_ctr_24", "AHRF:AMB_SURG_CTR", 2024),
    ("ambultry_surg_ctr_23", "AHRF:AMB_SURG_CTR", 2023),
    ("pers_noins_lt65_pct_22", "AHRF:UNINSURED_LT65_PCT", 2022),
    ("pers_noins_lt65_pct_21", "AHRF:UNINSURED_LT65_PCT", 2021),
    ("hpsa_prim_care_25", "AHRF:HPSA_PRIM_CARE", 2025),
    ("hpsa_prim_care_24", "AHRF:HPSA_PRIM_CARE", 2024),
    ("hpsa_mentl_hlth_25", "AHRF:HPSA_MENTAL_HEALTH", 2025),
    ("hpsa_mentl_hlth_24", "AHRF:HPSA_MENTAL_HEALTH", 2024),
    ("np_npi_24", "AHRF:NP_NPI", 2024),
    ("np_npi_23", "AHRF:NP_NPI", 2023),
    ("pa_npi_24", "AHRF:PA_NPI", 2024),
    ("pa_npi_23", "AHRF:PA_NPI", 2023),
    ("aprn_npi_24", "AHRF:APRN_NPI", 2024),
    ("aprn_npi_23", "AHRF:APRN_NPI", 2023),
)

# measure.definition content, one entry per measure_id above. All method =
# 'direct': every one is a plain administrative count or code, not modeled,
# except UNINSURED_LT65_PCT (Census SAHIE is itself a small-area *model*).
MEASURE_DEFINITIONS = {
    "AHRF:FQHC": dict(
        label="Federally Qualified Health Centers", units=None, rate_basis="count",
        method="direct",
        doc="Count of Medicare-certified Federally Qualified Health Centers in the county. "
            "Source: CMS Provider of Services file (AHRF field fedly_qualfd_hlth_ctr_*)."),
    "AHRF:RHC": dict(
        label="Rural Health Clinics", units=None, rate_basis="count", method="direct",
        doc="Count of Medicare-certified Rural Health Clinics in the county. "
            "Source: CMS Provider of Services file (AHRF field rural_hlth_clincs_*)."),
    "AHRF:HOSPICE": dict(
        label="Hospices", units=None, rate_basis="count", method="direct",
        doc="Count of Medicare-certified hospice providers in the county -- end-of-life "
            "cancer care access. Source: CMS Provider of Services file (AHRF field hospcs_*)."),
    "AHRF:AMB_SURG_CTR": dict(
        label="Ambulatory Surgery Centers", units=None, rate_basis="count", method="direct",
        doc="Count of Medicare-certified ambulatory surgery centers in the county -- "
            "outpatient surgical/procedural oncology access. Source: CMS Provider of "
            "Services file (AHRF field ambultry_surg_ctr_*)."),
    "AHRF:UNINSURED_LT65_PCT": dict(
        label="Percent uninsured, under age 65", units="percent", rate_basis="percent",
        method="model_based", universe="civilian population under age 65",
        doc="Small-area estimate of the percent of the population under 65 without health "
            "insurance. Source: U.S. Census Bureau Small Area Health Insurance Estimates "
            "(SAHIE) (AHRF field pers_noins_lt65_pct_*)."),
    "AHRF:HPSA_PRIM_CARE": dict(
        label="Primary Care Health Professional Shortage Area designation",
        units="code 0-2", rate_basis="index", method="direct",
        doc="HRSA Health Professional Shortage Area code for primary care: 0 = county not "
            "designated, 1 = whole county designated, 2 = part of county designated. "
            "Source: HRSA Data Warehouse (AHRF field hpsa_prim_care_*)."),
    "AHRF:HPSA_MENTAL_HEALTH": dict(
        label="Mental Health Health Professional Shortage Area designation",
        units="code 0-2", rate_basis="index", method="direct",
        doc="HRSA Health Professional Shortage Area code for mental health: 0 = county not "
            "designated, 1 = whole county designated, 2 = part of county designated -- "
            "supportive/psychosocial oncology care access. Source: HRSA Data Warehouse "
            "(AHRF field hpsa_mentl_hlth_*)."),
    "AHRF:NP_NPI": dict(
        label="Nurse practitioners with an active NPI", units=None, rate_basis="count",
        method="direct",
        doc="Count of nurse practitioners with an active National Provider Identifier in "
            "the county -- advanced-practice primary-care workforce supply. Source: CMS "
            "NPI File / NPPES (AHRF field np_npi_*)."),
    "AHRF:PA_NPI": dict(
        label="Physician assistants with an active NPI", units=None, rate_basis="count",
        method="direct",
        doc="Count of physician assistants with an active National Provider Identifier in "
            "the county. Source: CMS NPI File / NPPES (AHRF field pa_npi_*)."),
    "AHRF:APRN_NPI": dict(
        label="Advanced practice registered nurses with an active NPI", units=None,
        rate_basis="count", method="direct",
        doc="Count of advanced practice registered nurses with an active National Provider "
            "Identifier in the county. Source: CMS NPI File / NPPES (AHRF field aprn_npi_*)."),
}


def _fetch_member(url, tmpdir, pattern, tag):
    """The one file inside `url` (a zip, downloaded and cached in `tmpdir`)
    whose basename matches `pattern`. A local, non-zip path (tests) is used
    directly."""
    if not url.startswith("http"):
        path = Path(url)
        if path.suffix != ".zip":
            return path
        src = path
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        zpath = Path(tmpdir) / f"{tag}.zip"
        with urllib.request.urlopen(req) as r, open(zpath, "wb") as f:
            f.write(r.read())
        src = zpath
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if pattern.search(Path(n).name)]
        if len(names) != 1:
            raise SystemExit(f"hrsa_ahrf: {url} has {len(names)} member(s) matching "
                             f"{pattern.pattern!r}, expected 1")
        z.extract(names[0], tmpdir)
        return Path(tmpdir) / names[0]


def _excluded_columns(con, techdoc_path):
    """Column names the release's own technical documentation attributes to
    a copyrighted third-party source (see module docstring) -- read straight
    from the documentation's per-field SOURCE column rather than a hand-kept
    list, so this can't silently go stale."""
    con.execute("INSTALL excel; LOAD excel;")
    prefixes = " OR ".join(f"trim(F) LIKE '{p}%'" for p in COPYRIGHT_SOURCE_PREFIXES)
    rows = con.sql(f"""
        SELECT DISTINCT trim(A) AS field
        FROM read_xlsx('{techdoc_path}', all_varchar=true, header=false, range='A1:G8000')
        WHERE {prefixes}
    """).fetchall()
    excluded = {r[0] for r in rows}
    if not excluded:
        raise SystemExit(f"hrsa_ahrf: no column in {techdoc_path} matched a copyrighted "
                         f"source ({COPYRIGHT_SOURCE_PREFIXES}); the technical documentation "
                         f"parse is probably broken -- refusing to land, since that would "
                         f"silently include copyrighted AMA/AHA/ADA data")
    return excluded


def land_raw(cat, release, ahrf_release=None, csv_url=None, techdoc_url=None):
    """Phase 1: the master county CSV, landed LONG and whole -- every column
    except the ones the release's own technical documentation flags as
    copyrighted (see module docstring). Returns (ahrf_release, rows).
    """
    ahrf_release = ahrf_release or max(RELEASES)
    if ahrf_release not in RELEASES:
        raise SystemExit(f"hrsa_ahrf: no known AHRF release {ahrf_release!r}; "
                         f"known releases: {sorted(RELEASES)}")
    default_csv_url, default_techdoc_url = RELEASES[ahrf_release]
    csv_url = csv_url or default_csv_url
    techdoc_url = techdoc_url or default_techdoc_url
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _fetch_member(csv_url, tmp, CSV_MEMBER, "csv")
        techdoc_path = _fetch_member(techdoc_url, tmp, TECHDOC_MEMBER, "techdoc")

        header = con.sql(f"SELECT * FROM read_csv('{csv_path}', header=true, "
                         f"all_varchar=true) LIMIT 0").columns
        if "fips_st_cnty" not in header:
            raise SystemExit(f"hrsa_ahrf: {csv_url} has no fips_st_cnty column")

        excluded = _excluded_columns(con, techdoc_path)
        eligible = [c for c in header if c != "fips_st_cnty" and c not in excluded]
        bad = sorted({f for f, _, _ in CURATED_FIELDS} - set(eligible))
        if bad:
            raise SystemExit(f"hrsa_ahrf: curated field(s) {bad} are not landable from "
                             f"{csv_url} (missing from the file, or excluded as copyrighted) "
                             f"-- update CURATED_FIELDS/MEASURE_DEFINITIONS")

        # coalesce/CASE round-trip keeps a blank source cell as NULL through
        # UNPIVOT, which otherwise drops NULL cells outright rather than
        # keeping a (fips, column_name, NULL) row -- AHRF's blank cells are
        # real, present cells (see module docstring), not absent ones.
        coalesced = ", ".join(f'coalesce("{c}", \'{SENTINEL}\') AS "{c}"' for c in eligible)
        collist = ", ".join(f'"{c}"' for c in eligible)
        arrow = con.sql(f"""
            SELECT '{ahrf_release}' AS ahrf_release, '{csv_path.name}' AS file,
                   fips_st_cnty AS fips, column_name,
                   CASE WHEN value = '{SENTINEL}' THEN NULL ELSE value END AS value,
                   '{release}' AS landed_in
            FROM (
                UNPIVOT (SELECT fips_st_cnty, {coalesced}
                         FROM read_csv('{csv_path}', header=true, all_varchar=true))
                ON {collist}
                INTO NAME column_name VALUE value
            )
        """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"hrsa_ahrf: {csv_url} yielded no rows")

    n = merge.write(cat, "raw.hrsa__ahrf", arrow, EqualTo("ahrf_release", ahrf_release))
    merge.manifest(cat, release, "hrsa_ahrf", csv_url, n, version=ahrf_release,
                   method="release_number")
    return ahrf_release, n


def transform(cat, release, ahrf_release):
    """Phase 2: the curated cancer-relevant measures (see module docstring),
    unpivoted back out of the long raw table by column name.

    Scoped to `ahrf_release`'s rows: raw accumulates every landed release, so
    an unscoped read would derive from all of them at once.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.hrsa__ahrf").scan(
        row_filter=EqualTo("ahrf_release", ahrf_release)).to_arrow())

    definition = pa.Table.from_pylist([
        dict(measure_id=mid, source="AHRF", label=d["label"], units=d["units"],
             universe=d.get("universe"), rate_basis=d["rate_basis"], age_adjustment=None,
             method=d["method"], cancer_site_code=None, doc=d["doc"])
        for mid, d in MEASURE_DEFINITIONS.items()
    ])

    # AHRF publishes county totals with no stratification.
    stratum = pa.Table.from_pylist([dict(
        stratum_id="AHRF:ALL", source="AHRF", sex=None, age_group=None,
        race_ethnicity=None, stage=None, other=None, scheme="AHRF_TOTAL")])

    field_map = ", ".join(f"('{col}', '{mid}', {year})" for col, mid, year in CURATED_FIELDS)
    legacy_fips = ", ".join(f"'{f}'" for f in LEGACY_FIPS)
    # geo_vintage is a property of the FIPS code itself (see module
    # docstring), not of which measure or period produced the row.
    observation = con.sql(f"""
        WITH field_map(column_name, measure_id, period_year) AS (VALUES {field_map})
        SELECT 'AHRF' AS source, '{ahrf_release}' AS source_release, m.measure_id,
               'county:' || lpad(r.fips, 5, '0') AS geo_id,
               CASE WHEN r.fips IN ({legacy_fips}) THEN {LEGACY_GEO_VINTAGE}
                    ELSE {CURRENT_GEO_VINTAGE} END AS geo_vintage,
               m.period_year::VARCHAR AS period_start, m.period_year::VARCHAR AS period_end,
               'AHRF:ALL' AS stratum_id,
               TRY_CAST(r.value AS DOUBLE) AS value,
               NULL::DOUBLE AS lower, NULL::DOUBLE AS upper, NULL::DOUBLE AS interval_level,
               NULL::DOUBLE AS numerator, NULL::DOUBLE AS denominator,
               CASE WHEN r.value IS NOT NULL THEN 'reported' ELSE 'not_available' END
                   AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM raw r JOIN field_map m ON r.column_name = m.column_name
    """).to_arrow_table()
    merge.check_observations(observation)

    scope = EqualTo("source", "AHRF")
    return {
        "measure.definition": merge.write(cat, "measure.definition", definition, scope),
        "measure.stratum": merge.write(cat, "measure.stratum", stratum, scope),
        "measure.observation": merge.merge(
            cat, "measure.observation", observation, release,
            And(scope, EqualTo("source_release", ahrf_release))),
    }


def ingest(cat, release, ahrf_release=None, csv_url=None, techdoc_url=None):
    ahrf_release, n = land_raw(cat, release, ahrf_release, csv_url, techdoc_url)
    return {"raw.hrsa__ahrf": n, **transform(cat, release, ahrf_release)}
