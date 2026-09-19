"""CDC/ATSDR Social Vulnerability Index (SVI) -> Iceberg.

Upstream: https://www.atsdr.cdc.gov/place-health/php/svi/ -- county and tract CSVs for
US-wide editions 2000, 2010, 2014, 2016, 2018, 2020, 2022 (SPEC.md § Sources: "area-level
context index... land every published edition"). The download page
(svi-data-documentation-download.html) is a form; the underlying files were found by
reading its `js/loadXML.js` and live at
`https://svi.cdc.gov/Documents/Data/<edition>/csv/states_counties/SVI_<edition>_US_county.csv`
(county) and `.../csv/states/SVI_<edition>_US.csv` (tract), verified 2026-09-18 by
downloading all 14 real files.

**Licence.** CDC/ATSDR's own materials-use page states: "Most of the information on the
CDC and ATSDR websites is not subject to copyright, is in the public domain, and may be
freely used or reproduced without obtaining copyright permission."
(https://www.cdc.gov/other/agencymaterials.html, checked 2026-09-18). SVI is produced by
ATSDR's Geospatial Research, Analysis & Services Program, a federal agency, in the course
of employees' official duties -- also a U.S. Government work under 17 U.S.C. § 105.

**Version axis: the edition** (2000, 2010, 2014, 2016, 2018, 2020, 2022), landed as
`svi_edition`. "Methods and themes changed between editions, so an edition is a different
measure definition, not just a new period" (#40) -- verified directly: theme membership
(which raw variables feed RPL_THEME1-4) differs between the two landed layout families
(see below), so derived measure_ids embed the family, not just the edition.

**Editions landed: 2014, 2016, 2018, 2020, 2022 (county); 2022 only (tract).**
ponytail: **2000 and 2010 are NOT landed.** Both are real published editions, but neither
fits safely into the two real (2014-family, 2020-family) layouts this module unions into
one raw table:
  - 2010's real header names a column "STATE" holding the 2-digit state FIPS code (e.g.
    '01'), while every 2014+ layout names its full state NAME column "STATE" (e.g.
    'Alabama') -- the same column name means a different kind of value depending on
    edition. Landing both under one shared "STATE" column would be a silent semantic trap
    for anyone querying raw directly; safely landing 2010 needs either a second raw table
    or a renaming scheme, more machinery than this PR's scope.
  - 2010 has no E_/M_ ACS estimates or MOEs at all for several concepts this issue derives
    (structural Census counts only: AGE65, MINORITY, GROUPQ have no M_ column in 2010).
  - **The 2000 county/tract CSVs ship with two header lines**, verified by downloading the
    real files: line 1 (`STATE_FIPS,CNTY_FIPS,...,G1V1R,...,USG1V1P,...`) does not match
    the data that follows it at all (its columns don't correspond to the values in any
    row); line 2 (`ST,COU,STCNTY,...,P_POV,...,RPL_THEME1,...,F_POV,...`) does -- e.g.
    Autauga County, AL's row 3 gives P_POV=0.1092, a plausible real 2000 poverty rate,
    lining up with line 2's column names, not line 1's. Line 1 appears to be a stale,
    vestigial header CDC never removed. Even using the real (line 2) header, 2000 has no
    E_/M_ raw ACS variables at all (understandable -- SVI 2000 draws on Census 2000 SF3,
    a full count, not a 5-year survey), so none of this issue's requested component
    measures would be derivable from it anyway.
Both are tracked as a follow-up rather than invented around here; report this in the PR.

**Three real column contracts (`LAYOUT`/`COUNTY_COLUMNS`), two methodology families
(`FAMILY`).** A layout is what header a real file has; a family is which measure_ids and
theme composition an edition asserts (module code keeps these separate on purpose --
collapsing them loses the fact that 2016/2018 share 2014's methodology but not its exact
header):
  - layout `"2014"` -- county 2014 only (127 columns, includes AFFGEOID and the trailing
    Shape/Shape.STArea()/Shape.STLength(), excluded from the union below).
  - layout `"2016"` -- county 2016, 2018 (verified byte-identical to each other; 2014's
    127 columns minus those same 4).
  - layout `"2020"` -- county 2020, 2022, and **tract 2022** (verified: the 2022 tract
    file's header is byte-identical to the 2020/2022 county header -- same layout,
    different geography grain -- so tract lands via the same code path with no new
    layout to declare; this is why tract landing extends past 2022 trivially but isn't
    wired up for other editions here).
  - family `"2014"` -- editions 2014, 2016, 2018 (layouts "2014" and "2016": identical
    concept columns and theme composition, verified from each real header's column
    order, despite the "2014"-only geometry/AFFGEOID columns).
  - family `"2020"` -- editions 2020, 2022 (layout "2020").
`raw.svi__county` unions all three layouts' columns (the "2020" layout plus the 13
columns unique to "2014": AFFGEOID and the POV/PCI group, since 2020 renamed poverty to
POV150 and dropped PCI from the methodology -- ArcGIS geometry columns excluded, per
SPEC.md's geometry non-goal); `raw.svi__tract` is exactly the "2020" layout.

**Geography vintage moves mid-series, same pattern as places.py**: Connecticut's
`FIPS`/`STCNTY` values are the legacy 8 counties (09001-09015) through the 2020 edition
and the 9 planning regions (09110-09190) from 2022 on -- verified against each edition's
real file. `GEO_VINTAGE` records 2010 for 2014-2020 and 2020 for 2022.

**Period**: the ACS 5-year window each edition's own documentation states under "Methods
> Variables Used" (svi.cdc.gov/map25/data/docs/SVI<edition>Documentation*.pdf, checked
2026-09-18): 2014 -> "2010-2014 (5-year)"; 2016 -> "2012-2016"; 2018 -> "2014-2018";
2020 -> "2016-2020"; 2022 -> "2018-2022".

**Suppression.** -999 is SVI's numeric missing sentinel, published in both raw estimate
(E_/M_) and percent/percentile (EP_/MP_/EPL_/RPL_/SPL_) columns, confirmed present in
every landed edition's real file -> `not_available`, never a number (SPEC.md Acceptance
C). A margin of error can be -999 while its paired estimate is valid (or vice versa) --
each is checked independently; a suppressed EP_ value never derives a lower/upper from a
present MP_, and a present EP_ with a suppressed MP_ still derives `value` with no
interval.

**Derived**: `RPL_THEMES`/`RPL_THEME1-4` (rate_basis='index', method='derived') as
`measure_id = 'SVI:RPL_THEMES:<family>'` / `'SVI:RPL_THEME<n>:<family>'`, family '2014' or
'2020' (`FAMILY[edition]`) since theme composition genuinely differs between them
(see `THEME_DESC`) -- plus the simple E_/EP_ component variables named in #40: poverty
(100% threshold through 2018 as `SVI:poverty`, 150% threshold from 2020 as
`SVI:poverty150` -- CDC's own renamed variable, confirmed EP_POV -> EP_POV150 in the real
files), no HS diploma, uninsured, no vehicle, age 65+, minority, limited English -- all
six of the latter keep one stable measure_id across both families (verified: identical
column names/meaning in both layouts). `interval_level=0.90` (ACS MOE) where both EP_ and
MP_ are present and unsuppressed.

`measure.definition`/`measure.stratum` writes are scoped to the measure_ids this edition's
family actually asserts (`In("measure_id", ids)`), never wholesale per source -- SPEC.md's
"never replace wholesale per source" rule (issue #76): ingesting the 2020-family after the
2014-family must not delete the 2014-family's own theme definitions, since a 2014
`measure.observation` row's FK still points at them.

ponytail: `PCI` (per capita income, 2014-2018 only) and the structural variables this
issue doesn't ask for (age17, disability, single-parent, group quarters, housing type,
crowding, race breakdowns added 2020, no-internet) land in raw but are not derived --
"simple and cancer-relevant" per #40 names the seven above; add more alongside
`CONCEPT_COLUMN`/`CONCEPT_LABEL` if a later measure needs them.
"""

import tempfile
import urllib.request
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import And, EqualTo, In

from . import merge

USER_AGENT = "cancerOnIce/1.0 (+https://github.com/seandavi/cancer-on-ice)"
BASE = "https://svi.cdc.gov/Documents/Data"

EDITIONS = ("2014", "2016", "2018", "2020", "2022")
TRACT_EDITIONS = ("2022",)

# Which COUNTY_COLUMNS/TRACT_COLUMNS layout each edition's file uses (module docstring).
# 2016 and 2018 are verified byte-identical to each other but NOT to 2014 -- 2014 alone
# carries AFFGEOID and the trailing Shape/Shape.STArea()/Shape.STLength() columns -- so
# they get their own "2016" layout key, distinct from "2014"'s.
LAYOUT = {"2014": "2014", "2016": "2016", "2018": "2016", "2020": "2020", "2022": "2020"}

# Which measure-definition/theme-composition FAMILY each edition belongs to -- coarser
# than LAYOUT: 2014, 2016 and 2018 use three different raw column *contracts* (LAYOUT)
# but share one *methodology* (identical theme composition and concept columns; module
# docstring), so they share one family and its measure_ids. 2020 and 2022 share the
# other family (and, unlike 2014-vs-2016, also share their raw column contract).
FAMILY = {"2014": "2014", "2016": "2014", "2018": "2014", "2020": "2020", "2022": "2020"}

# Connecticut's 2022 county -> planning-region switch; same value for county and tract
# (verified: 2022 tract STCNTY carries 09110-09190 same as 2022 county FIPS).
GEO_VINTAGE = {"2014": 2010, "2016": 2010, "2018": 2010, "2020": 2010, "2022": 2020}

# The ACS 5-year window each edition documents under "Methods > Variables Used" in its
# own PDF (module docstring), as (period_start, period_end).
ACS_WINDOW = {
    "2014": ("2010", "2014"), "2016": ("2012", "2016"), "2018": ("2014", "2018"),
    "2020": ("2016", "2020"), "2022": ("2018", "2022"),
}

COUNTY_COLUMNS = {
    "2014": (
        "AFFGEOID", "ST", "STATE", "ST_ABBR", "COUNTY", "FIPS", "LOCATION", "AREA_SQMI",
        "E_TOTPOP", "M_TOTPOP", "E_HU", "M_HU", "E_HH", "M_HH", "E_POV", "M_POV",
        "E_UNEMP", "M_UNEMP", "E_PCI", "M_PCI", "E_NOHSDP", "M_NOHSDP", "E_AGE65",
        "M_AGE65", "E_AGE17", "M_AGE17", "E_DISABL", "M_DISABL", "E_SNGPNT", "M_SNGPNT",
        "E_MINRTY", "M_MINRTY", "E_LIMENG", "M_LIMENG", "E_MUNIT", "M_MUNIT", "E_MOBILE",
        "M_MOBILE", "E_CROWD", "M_CROWD", "E_NOVEH", "M_NOVEH", "E_GROUPQ", "M_GROUPQ",
        "EP_POV", "MP_POV", "EP_UNEMP", "MP_UNEMP", "EP_PCI", "MP_PCI", "EP_NOHSDP",
        "MP_NOHSDP", "EP_AGE65", "MP_AGE65", "EP_AGE17", "MP_AGE17", "EP_DISABL",
        "MP_DISABL", "EP_SNGPNT", "MP_SNGPNT", "EP_MINRTY", "MP_MINRTY", "EP_LIMENG",
        "MP_LIMENG", "EP_MUNIT", "MP_MUNIT", "EP_MOBILE", "MP_MOBILE", "EP_CROWD",
        "MP_CROWD", "EP_NOVEH", "MP_NOVEH", "EP_GROUPQ", "MP_GROUPQ", "EPL_POV",
        "EPL_UNEMP", "EPL_PCI", "EPL_NOHSDP", "SPL_THEME1", "RPL_THEME1", "EPL_AGE65",
        "EPL_AGE17", "EPL_DISABL", "EPL_SNGPNT", "SPL_THEME2", "RPL_THEME2", "EPL_MINRTY",
        "EPL_LIMENG", "SPL_THEME3", "RPL_THEME3", "EPL_MUNIT", "EPL_MOBILE", "EPL_CROWD",
        "EPL_NOVEH", "EPL_GROUPQ", "SPL_THEME4", "RPL_THEME4", "SPL_THEMES", "RPL_THEMES",
        "F_POV", "F_UNEMP", "F_PCI", "F_NOHSDP", "F_THEME1", "F_AGE65", "F_AGE17",
        "F_DISABL", "F_SNGPNT", "F_THEME2", "F_MINRTY", "F_LIMENG", "F_THEME3", "F_MUNIT",
        "F_MOBILE", "F_CROWD", "F_NOVEH", "F_GROUPQ", "F_THEME4", "F_TOTAL", "E_UNINSUR",
        "M_UNINSUR", "EP_UNINSUR", "MP_UNINSUR", "E_DAYPOP", "Shape", "Shape.STArea()",
        "Shape.STLength()",
    ),
    "2020": (
        "ST", "STATE", "ST_ABBR", "STCNTY", "COUNTY", "FIPS", "LOCATION", "AREA_SQMI",
        "E_TOTPOP", "M_TOTPOP", "E_HU", "M_HU", "E_HH", "M_HH", "E_POV150", "M_POV150",
        "E_UNEMP", "M_UNEMP", "E_HBURD", "M_HBURD", "E_NOHSDP", "M_NOHSDP", "E_UNINSUR",
        "M_UNINSUR", "E_AGE65", "M_AGE65", "E_AGE17", "M_AGE17", "E_DISABL", "M_DISABL",
        "E_SNGPNT", "M_SNGPNT", "E_LIMENG", "M_LIMENG", "E_MINRTY", "M_MINRTY", "E_MUNIT",
        "M_MUNIT", "E_MOBILE", "M_MOBILE", "E_CROWD", "M_CROWD", "E_NOVEH", "M_NOVEH",
        "E_GROUPQ", "M_GROUPQ", "EP_POV150", "MP_POV150", "EP_UNEMP", "MP_UNEMP",
        "EP_HBURD", "MP_HBURD", "EP_NOHSDP", "MP_NOHSDP", "EP_UNINSUR", "MP_UNINSUR",
        "EP_AGE65", "MP_AGE65", "EP_AGE17", "MP_AGE17", "EP_DISABL", "MP_DISABL",
        "EP_SNGPNT", "MP_SNGPNT", "EP_LIMENG", "MP_LIMENG", "EP_MINRTY", "MP_MINRTY",
        "EP_MUNIT", "MP_MUNIT", "EP_MOBILE", "MP_MOBILE", "EP_CROWD", "MP_CROWD",
        "EP_NOVEH", "MP_NOVEH", "EP_GROUPQ", "MP_GROUPQ", "EPL_POV150", "EPL_UNEMP",
        "EPL_HBURD", "EPL_NOHSDP", "EPL_UNINSUR", "SPL_THEME1", "RPL_THEME1", "EPL_AGE65",
        "EPL_AGE17", "EPL_DISABL", "EPL_SNGPNT", "EPL_LIMENG", "SPL_THEME2", "RPL_THEME2",
        "EPL_MINRTY", "SPL_THEME3", "RPL_THEME3", "EPL_MUNIT", "EPL_MOBILE", "EPL_CROWD",
        "EPL_NOVEH", "EPL_GROUPQ", "SPL_THEME4", "RPL_THEME4", "SPL_THEMES", "RPL_THEMES",
        "F_POV150", "F_UNEMP", "F_HBURD", "F_NOHSDP", "F_UNINSUR", "F_THEME1", "F_AGE65",
        "F_AGE17", "F_DISABL", "F_SNGPNT", "F_LIMENG", "F_THEME2", "F_MINRTY", "F_THEME3",
        "F_MUNIT", "F_MOBILE", "F_CROWD", "F_NOVEH", "F_GROUPQ", "F_THEME4", "F_TOTAL",
        "E_DAYPOP", "E_NOINT", "M_NOINT", "E_AFAM", "M_AFAM", "E_HISP", "M_HISP",
        "E_ASIAN", "M_ASIAN", "E_AIAN", "M_AIAN", "E_NHPI", "M_NHPI", "E_TWOMORE",
        "M_TWOMORE", "E_OTHERRACE", "M_OTHERRACE", "EP_NOINT", "MP_NOINT", "EP_AFAM",
        "MP_AFAM", "EP_HISP", "MP_HISP", "EP_ASIAN", "MP_ASIAN", "EP_AIAN", "MP_AIAN",
        "EP_NHPI", "MP_NHPI", "EP_TWOMORE", "MP_TWOMORE", "EP_OTHERRACE", "MP_OTHERRACE",
    ),
}
# 2016 and 2018's real header (verified byte-identical to each other) is 2014's layout
# minus AFFGEOID and the trailing Shape/Shape.STArea()/Shape.STLength() columns.
_DROP_2014_GEOMETRY = ("AFFGEOID", "Shape", "Shape.STArea()", "Shape.STLength()")
COUNTY_COLUMNS["2016"] = tuple(c for c in COUNTY_COLUMNS["2014"] if c not in _DROP_2014_GEOMETRY)

# The 2022 tract file's header is byte-identical to the 2020/2022 county header
# (verified 2026-09-18) -- same layout, different geography grain.
TRACT_COLUMNS = {"2020": COUNTY_COLUMNS["2020"]}

# raw.svi__county's own column order: the "2020" layout plus the columns unique to
# "2014" (module docstring), excluding the ArcGIS geometry byproducts (AFFGEOID is kept
# -- a real, useful identifier, unlike the other three). Missing-in-an-edition columns
# land NULL, same technique as census_gazetteer.py's FULL_COUNTY_COLUMNS.
_COUNTY_2014_ONLY = tuple(c for c in COUNTY_COLUMNS["2014"]
                          if c not in COUNTY_COLUMNS["2020"]
                          and c not in ("Shape", "Shape.STArea()", "Shape.STLength()"))
FULL_COUNTY_COLUMNS = COUNTY_COLUMNS["2020"] + _COUNTY_2014_ONLY
FULL_TRACT_COLUMNS = TRACT_COLUMNS["2020"]

# root variable name (after E_/M_/EP_/MP_) for each concept, per layout family -- the six
# not ending in "150" are identically named and defined in both families (verified).
CONCEPT_COLUMN = {
    "2014": {"poverty": "POV", "no_hs_diploma": "NOHSDP", "uninsured": "UNINSUR",
             "no_vehicle": "NOVEH", "age_65_plus": "AGE65", "minority": "MINRTY",
             "limited_english": "LIMENG"},
    "2020": {"poverty150": "POV150", "no_hs_diploma": "NOHSDP", "uninsured": "UNINSUR",
             "no_vehicle": "NOVEH", "age_65_plus": "AGE65", "minority": "MINRTY",
             "limited_english": "LIMENG"},
}

# (label, universe) for measure.definition, keyed by concept.
CONCEPT_LABEL = {
    "poverty": ("Persons below the poverty threshold (100% of the federal poverty "
                "level)", "population for whom poverty status is determined"),
    "poverty150": ("Persons below 150% of the federal poverty level",
                   "population for whom poverty status is determined"),
    "no_hs_diploma": ("Persons age 25+ with no high school diploma",
                       "population age 25 and older"),
    "uninsured": ("Civilian noninstitutionalized population with no health insurance",
                  "civilian noninstitutionalized population"),
    "no_vehicle": ("Households with no vehicle available", "occupied housing units"),
    "age_65_plus": ("Persons aged 65 and older", "total population"),
    "minority": ("Minority population (all persons except white, non-Hispanic)",
                 "total population"),
    "limited_english": ('Persons age 5+ who speak English "less than well"',
                        "population age 5 and older"),
}

# Theme composition per family, read directly off each real header's column order
# (module docstring) -- not from CDC's prose, which this module doesn't otherwise parse.
THEME_DESC = {
    "2014": {
        1: "Socioeconomic Status (poverty, unemployment, income, education)",
        2: "Household Composition & Disability (age 65+, age 17 and under, disability, "
           "single-parent households)",
        3: "Minority Status & Language (minority population, limited English)",
        4: "Housing Type & Transportation (multi-unit housing, mobile homes, crowding, "
           "no vehicle, group quarters)",
    },
    "2020": {
        1: "Socioeconomic Status (poverty, unemployment, housing cost burden, education, "
           "uninsured)",
        2: "Household Characteristics (age 65+, age 17 and under, disability, "
           "single-parent households, limited English)",
        3: "Racial & Ethnic Minority Status (minority population)",
        4: "Housing Type & Transportation (multi-unit housing, mobile homes, crowding, "
           "no vehicle, group quarters)",
    },
}


def _url(edition, level):
    if level == "county":
        return f"{BASE}/{edition}/csv/states_counties/SVI_{edition}_US_county.csv"
    return f"{BASE}/{edition}/csv/states/SVI_{edition}_US.csv"


def _fetch(url):
    """The file's bytes. A local path (tests) is read directly; a remote URL goes
    through urllib with a descriptive User-Agent."""
    if not url.startswith("http"):
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r:
        return r.read()


def _header(raw_bytes):
    """The file's header line, BOM stripped (every real SVI CSV checked carries a UTF-8
    BOM) and split on comma."""
    first = raw_bytes.split(b"\n", 1)[0].rstrip(b"\r")
    return tuple(first.decode("utf-8-sig").split(","))


def land_raw(cat, release, edition=None, level="county", url=None):
    """Phase 1: one edition's county or tract file, verbatim and whole, replaced per
    (edition, level)."""
    edition = edition or max(EDITIONS, key=int)
    if edition not in EDITIONS:
        raise SystemExit(f"cdc_svi: no known layout for edition {edition!r}; "
                         f"landed editions: {sorted(EDITIONS, key=int)}")
    if level == "tract" and edition not in TRACT_EDITIONS:
        raise SystemExit(f"cdc_svi: tract is only landed for {TRACT_EDITIONS}; "
                         f"{edition!r} is not one of them")
    layout = LAYOUT[edition]
    columns = (COUNTY_COLUMNS if level == "county" else TRACT_COLUMNS)[layout]
    full_columns = FULL_COUNTY_COLUMNS if level == "county" else FULL_TRACT_COLUMNS
    identifier = f"raw.svi__{level}"

    url = url or _url(edition, level)
    raw_bytes = _fetch(url)
    if (header := _header(raw_bytes)) != columns:
        raise SystemExit(f"cdc_svi: {url} header is not the declared {layout!r} layout; "
                         f"differs in {sorted(set(header) ^ set(columns))}")

    with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        # raw.svi__county's schema is the union of every layout landed (module
        # docstring); a column this edition's own layout doesn't have lands NULL, same
        # technique as census_gazetteer.py's _land_file. The three ArcGIS geometry
        # columns (2014 only) are deliberately never selected, even though the real file
        # has them -- excluded from the union entirely, not just NULLed.
        select = ", ".join(f'"{c}"' if c in columns else f'NULL::VARCHAR AS "{c}"'
                           for c in full_columns)
        con = duckdb.connect()
        # Dialect stated, not sniffed: comma-delimited, double-quoted where a cell needs
        # it (LOCATION contains a comma, e.g. "Autauga County, Alabama"). all_varchar
        # keeps raw unparsed and -999 landed as text, not silently cast to a number.
        arrow = con.sql(f"""
            SELECT {select}, '{edition}' AS svi_edition, '{release}' AS landed_in
            FROM read_csv('{tmp.name}', header=true, all_varchar=true, delim=',',
                          quote='"', escape='"', nullstr='')
        """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"cdc_svi: {url} yielded no rows")

    n = merge.write(cat, identifier, arrow, EqualTo("svi_edition", edition))
    merge.manifest(cat, release, f"svi_{level}", url, n, version=edition,
                   method="release_number")
    return edition, n


def _concept_select(level, geo_vintage, family):
    """One level's rows, columns aliased to family-neutral names (geo_id/geo_vintage plus
    TRY_CAST'd RPL_THEME*/E_*/EP_*/M_*/MP_* for this family's concepts) so the rest of
    `transform` doesn't need to know which family produced them."""
    concepts = CONCEPT_COLUMN[family]
    concept_cols = ", ".join(
        f'TRY_CAST("E_{root}" AS DOUBLE) AS e_{c}, TRY_CAST("M_{root}" AS DOUBLE) AS m_{c}, '
        f'TRY_CAST("EP_{root}" AS DOUBLE) AS ep_{c}, TRY_CAST("MP_{root}" AS DOUBLE) AS mp_{c}'
        for c, root in concepts.items()
    )
    return f"""
        SELECT '{level}:' || FIPS AS geo_id, {geo_vintage} AS geo_vintage,
               TRY_CAST(RPL_THEMES AS DOUBLE) AS rpl_themes,
               TRY_CAST(RPL_THEME1 AS DOUBLE) AS rpl_theme1,
               TRY_CAST(RPL_THEME2 AS DOUBLE) AS rpl_theme2,
               TRY_CAST(RPL_THEME3 AS DOUBLE) AS rpl_theme3,
               TRY_CAST(RPL_THEME4 AS DOUBLE) AS rpl_theme4,
               {concept_cols}
        FROM {level}_raw
    """


def transform(cat, release, edition):
    """Phase 2: measure.definition / measure.stratum / measure.observation for one
    edition, from whichever of raw.svi__county / raw.svi__tract are landed for it.

    Rebuilding from raw every time (rather than only the level just landed) is what
    keeps this idempotent and safe to call after landing either level alone: measure.
    observation's merge scope is (source, source_release) -- not per-level -- so a
    tract-only re-derive would otherwise retire the county rows landed earlier for the
    same edition, and vice versa (merge.merge's `incoming` must be the complete state
    within its scope).
    """
    family = FAMILY[edition]
    geo_vintage = GEO_VINTAGE[edition]
    period_start, period_end = ACS_WINDOW[edition]
    con = duckdb.connect()

    con.register("county_raw", cat.load_table("raw.svi__county").scan(
        row_filter=EqualTo("svi_edition", edition)).to_arrow())
    levels = [_concept_select("county", geo_vintage, family)]
    if edition in TRACT_EDITIONS:
        try:
            con.register("tract_raw", cat.load_table("raw.svi__tract").scan(
                row_filter=EqualTo("svi_edition", edition)).to_arrow())
            levels.append(_concept_select("tract", geo_vintage, family))
        except NoSuchTableError:
            pass  # tract not landed yet for this edition; county-only is still valid
    con.execute(f"CREATE OR REPLACE TABLE combined AS {' UNION ALL '.join(levels)}")

    concepts = CONCEPT_COLUMN[family]
    theme_desc = THEME_DESC[family]

    definition = pa.Table.from_pylist(
        [dict(measure_id=f"SVI:RPL_THEMES:{family}", source="SVI",
              label="Overall SVI percentile ranking", units="percentile (0-1)",
              universe=None, rate_basis="index", age_adjustment=None, method="derived",
              cancer_site_code=None,
              doc=f"Overall SVI vulnerability percentile ranking across all four themes, "
                  f"{family}-family methodology (editions in this family: "
                  f"{[e for e, fam in FAMILY.items() if fam == family]}).")] +
        [dict(measure_id=f"SVI:RPL_THEME{n}:{family}", source="SVI",
              label=f"Theme {n} percentile ranking", units="percentile (0-1)",
              universe=None, rate_basis="index", age_adjustment=None, method="derived",
              cancer_site_code=None,
              doc=f"Theme {n} vulnerability percentile ranking: {theme_desc[n]}.")
         for n in (1, 2, 3, 4)] +
        [dict(measure_id=f"SVI:{c}", source="SVI", label=CONCEPT_LABEL[c][0],
              units="percent", universe=CONCEPT_LABEL[c][1], rate_basis="percent",
              age_adjustment=None, method="survey_direct", cancer_site_code=None,
              doc=f"{CONCEPT_LABEL[c][0]}, as a percent of {CONCEPT_LABEL[c][1]} "
                  f"(ACS estimate).")
         for c in concepts]
    )
    def_ids = definition.column("measure_id").to_pylist()

    stratum = pa.Table.from_pylist([dict(
        stratum_id="SVI:ALL", source="SVI", sex=None, age_group=None, race_ethnicity=None,
        stage=None, other=None, scheme="SVI_TOTAL",
    )])

    theme_obs = " UNION ALL ".join(f"""
        SELECT 'SVI' AS source, '{edition}' AS source_release,
               'SVI:RPL_THEME{suffix}:{family}' AS measure_id, geo_id, geo_vintage,
               '{period_start}' AS period_start, '{period_end}' AS period_end,
               'SVI:ALL' AS stratum_id,
               CASE WHEN {col} = -999 THEN NULL ELSE {col} END AS value,
               NULL::DOUBLE AS lower, NULL::DOUBLE AS upper, NULL::DOUBLE AS interval_level,
               NULL::DOUBLE AS numerator, NULL::DOUBLE AS denominator,
               CASE WHEN {col} = -999 THEN 'not_available' ELSE 'reported' END AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM combined
    """ for suffix, col in [("S", "rpl_themes")] + [(n, f"rpl_theme{n}") for n in (1, 2, 3, 4)])

    concept_obs = " UNION ALL ".join(f"""
        SELECT 'SVI' AS source, '{edition}' AS source_release, 'SVI:{c}' AS measure_id,
               geo_id, geo_vintage, '{period_start}' AS period_start,
               '{period_end}' AS period_end, 'SVI:ALL' AS stratum_id,
               CASE WHEN ep_{c} = -999 THEN NULL ELSE ep_{c} END AS value,
               CASE WHEN ep_{c} = -999 OR mp_{c} = -999 THEN NULL ELSE ep_{c} - mp_{c} END AS lower,
               CASE WHEN ep_{c} = -999 OR mp_{c} = -999 THEN NULL ELSE ep_{c} + mp_{c} END AS upper,
               CASE WHEN ep_{c} = -999 OR mp_{c} = -999 THEN NULL ELSE 0.90 END AS interval_level,
               NULL::DOUBLE AS numerator, NULL::DOUBLE AS denominator,
               CASE WHEN ep_{c} = -999 THEN 'not_available' ELSE 'reported' END AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM combined
    """ for c in concepts)

    observation = con.sql(f"{theme_obs} UNION ALL {concept_obs}").to_arrow_table()
    merge.check_observations(observation)

    scope = EqualTo("source", "SVI")
    return {
        "measure.definition": merge.write(cat, "measure.definition", definition,
                                          And(scope, In("measure_id", def_ids))),
        "measure.stratum": merge.write(cat, "measure.stratum", stratum,
                                       And(scope, In("stratum_id", ["SVI:ALL"]))),
        "measure.observation": merge.merge(
            cat, "measure.observation", observation, release,
            And(scope, EqualTo("source_release", edition))),
    }


def ingest(cat, release, edition=None, level="county", url=None):
    edition, n = land_raw(cat, release, edition, level, url)
    return {f"raw.svi__{level}": n, **transform(cat, release, edition)}
