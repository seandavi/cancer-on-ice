"""US Census Bureau Gazetteer Files -> geography.unit, the non-geometry half
of the geography spine (SPEC.md § Geography).

Upstream: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html
lists one national county file and one national tract file per vintage year,
zipped tab- (or, from 2025, pipe-) delimited text under
https://www2.census.gov/geo/docs/maps-data/data/gazetteer/, e.g.
`2024_Gazetteer/2024_Gaz_counties_national.zip`. The 2010 files live at the
same host without a year folder or prefix (`Gaz_counties_national.zip`) and
carry two extra 2010-Census columns, POP10/HU10, that later vintages drop.

**Licence.** U.S. Census Bureau content is a federal government work. Per
resources.data.gov/open-licenses/ (the definition data.gov uses for every
federal agency's "Public Domain" listing): "Data and content created by
government employees within the scope of their employment are not subject to
domestic copyright protection under 17 U.S.C. § 105. Government works are by
default in the U.S. Public Domain." No registration or attribution is
required to redistribute.

**Version axis: release.** Each file is one dated annual (or decennial)
edition, e.g. "2024_Gazetteer" — a citable label the source itself publishes,
not a retrieval date, so `merge.manifest` is called with
`method="release_number"` and `version=<the gazetteer year>` (SPEC.md's
vintage rule for sources with no other version label).

**Geography vintage.** The gazetteer year *is* the boundary vintage: Census's
own TIGER/Line documentation states legal boundaries and names in a given
year's shapefiles are "as of January 1" of that year (confirmed against the
2020, 2022 and 2023 TIGER/Line pages), and the gazetteer files are released
under the same year label as extracts of the matching TIGER/Line vintage — the
switch from Connecticut's eight counties (present through the 2020 files) to
its nine planning regions, FIPS 09110-09190 (present from at least 2024),
lines up with Connecticut's real-world 2022 change and confirms the two
follow the same clock.

**Header quirk.** The 2010 and "modern" (2011-2024) layouts pad their last
header column, INTPTLONG, with trailing spaces before the newline; the 2025+
layout does not, and also gained a fully-qualified `GEOIDFQ` column and
switched delimiter from tab to pipe. Three real layouts, two file kinds ->
`COUNTY_COLUMNS`/`TRACT_COLUMNS` hold one COLUMNS contract per layout instead
of a single module-level COLUMNS (the source-common convention assumes one
layout per source; this source has three, verified 2026-09-18 against the
real 2010, 2020 and 2026 files). A header matching none of them raises
SystemExit before anything is read, same as a single-layout source.

**Encoding quirk.** The 2010 county file has one stray Latin-1 byte instead of
UTF-8 (NM 35013, "Doña Ana County"); every other vintage checked, and the 2010
tract file (which has no NAME column to carry the accent), are clean UTF-8.
`_ensure_utf8` decodes the whole file as Latin-1 on a UTF-8 failure — safe
because the rest of the file is plain ASCII, where Latin-1 and UTF-8 agree —
recovering the intended character exactly rather than raising or replacing it
with U+FFFD.

ponytail: only counties and tracts are landed (SPEC.md M1 asks for county +
tract, 2010 + 2020 + latest). Places, county subdivisions, ZCTAs, PUMAs and
every other gazetteer file are not — add one alongside these two if a later
milestone needs it.

ponytail: state names come from `raw.census__state_fips`
(https://www2.census.gov/geo/docs/reference/state.txt, landed once per
release, not per vintage — FIPS-to-state assignments don't move on the
gazetteer's cadence). If that file can't be reached, `land_state_fips` warns
and returns 0 rather than failing the whole ingest; `transform` then falls
back to the state's USPS code as its name, and says so in the same way.

ponytail: layout choice is decided purely by matching the file's own header
against the three known contracts, not by branching on the year — so a year
outside 2010/2011-2024/2025+ that happens to match a known layout still lands
correctly, and one that doesn't fails loudly rather than guessing.

ponytail: no `geography.crosswalk` row for the Connecticut county -> planning
region transition. Each vintage's units stand on their own; a weighted
crosswalk (SPEC.md Acceptance B) is a separate, still-open piece of work.
"""

import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import duckdb
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge

USER_AGENT = "cancer-on-ice/0.1 (+https://github.com/seandavi/cancer-on-ice)"
BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer"
STATE_FIPS_URL = "https://www2.census.gov/geo/docs/reference/state.txt"
STATE_COLUMNS = ("STATE", "STUSAB", "STATE_NAME", "STATENS")

# One COLUMNS contract per real layout (see module docstring). Order matches
# the file, header names exactly as Census spells them.
COUNTY_COLUMNS = {
    "2010": ("USPS", "GEOID", "ANSICODE", "NAME", "POP10", "HU10", "ALAND", "AWATER",
             "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG"),
    "modern": ("USPS", "GEOID", "ANSICODE", "NAME", "ALAND", "AWATER",
               "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG"),
    "2025": ("USPS", "GEOID", "GEOIDFQ", "ANSICODE", "NAME", "ALAND", "AWATER",
             "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG"),
}
TRACT_COLUMNS = {
    "2010": ("USPS", "GEOID", "POP10", "HU10", "ALAND", "AWATER",
             "ALAND_SQMI", "AWATER_SQMI", "INTPTLAT", "INTPTLONG"),
    "modern": ("USPS", "GEOID", "ALAND", "AWATER", "ALAND_SQMI", "AWATER_SQMI",
               "INTPTLAT", "INTPTLONG"),
    "2025": ("USPS", "GEOID", "GEOIDFQ", "ALAND", "AWATER", "ALAND_SQMI", "AWATER_SQMI",
             "INTPTLAT", "INTPTLONG"),
}
# raw.census__gazetteer_{counties,tracts}' own column order: the union of
# every layout, so a layout missing a column (POP10/HU10 outside 2010,
# GEOIDFQ before 2025) just lands NULL for it there.
FULL_COUNTY_COLUMNS = ("usps", "geoid", "geoidfq", "ansicode", "name", "pop10", "hu10",
                       "aland", "awater", "aland_sqmi", "awater_sqmi", "intptlat", "intptlong")
FULL_TRACT_COLUMNS = ("usps", "geoid", "geoidfq", "pop10", "hu10",
                      "aland", "awater", "aland_sqmi", "awater_sqmi", "intptlat", "intptlong")


def _zip_url(kind, year):
    if year == 2010:
        return f"{BASE}/Gaz_{kind}_national.zip"
    return f"{BASE}/{year}_Gazetteer/{year}_Gaz_{kind}_national.zip"


def _fetch_txt(url, tmpdir):
    """The gazetteer text file at `url`: downloaded and unzipped if it's an
    `https://` zip, used as-is otherwise (a local .txt, how the offline tests
    stay offline)."""
    if url.startswith("http"):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        zpath = Path(tmpdir) / "gazetteer.zip"
        with urllib.request.urlopen(req) as r, open(zpath, "wb") as f:
            f.write(r.read())
        src = zpath
    else:
        src = Path(url)
    if src.suffix != ".zip":
        return src
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if n.endswith(".txt")]
        if len(names) != 1:
            raise SystemExit(f"census gazetteer: {url} has {len(names)} .txt member(s), expected 1")
        z.extract(names[0], tmpdir)
        return Path(tmpdir) / names[0]


def _ensure_utf8(path):
    """The 2010 county file has one stray Latin-1 byte instead of UTF-8: NM
    35013 is 'Do\\xf1a Ana County' (verified 2026-09-18 against the real
    download; every other byte in every layout checked is plain ASCII).
    Decoding the whole file as Latin-1 on a UTF-8 failure recovers the
    intended character ('Doña Ana County') exactly rather than dropping or
    corrupting the row, since Latin-1 and ASCII agree on every byte the file
    otherwise uses."""
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
        return path
    except UnicodeDecodeError:
        fixed = path.with_name(path.stem + ".utf8" + path.suffix)
        fixed.write_text(raw.decode("latin-1"), encoding="utf-8")
        return fixed


def _detect(path, columns_by_layout):
    """Which of this file kind's known layouts `path`'s header matches, and its
    delimiter. Whitespace Census pads the last header column with (see module
    docstring) is stripped before comparing; a header matching none of the
    known layouts fails loudly rather than landing a guessed shape."""
    with open(path, "rb") as f:
        first = f.readline().decode("utf-8", "replace")
    delim = "|" if "|" in first.splitlines()[0] else "\t"
    header = tuple(first.rstrip().split(delim))
    for layout, cols in columns_by_layout.items():
        if header == cols:
            return layout, delim
    raise SystemExit(f"census gazetteer: header {header} matches no known layout "
                     f"(known: {columns_by_layout})")


def _land_file(cat, release, year, url, tmpdir, columns_by_layout, full_columns, identifier):
    path = _ensure_utf8(_fetch_txt(url, tmpdir))
    layout, delim = _detect(path, columns_by_layout)
    names = [c.lower() for c in columns_by_layout[layout]]
    select = ", ".join(c if c in names else f"NULL::VARCHAR AS {c}" for c in full_columns)
    con = duckdb.connect()
    # quote='' and escape='' — these files are never quoted, and letting DuckDB
    # sniff a quote character (its default when one isn't stated) can pair a
    # stray byte with an unrelated later line into one bad multi-line record.
    arrow = con.sql(f"""
        SELECT {select}, {year} AS gazetteer_year, '{release}' AS landed_in
        FROM read_csv('{path}', header=false, skip=1, delim='{delim}', quote='', escape='',
                      names={names!r}, all_varchar=true)
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"census gazetteer: {url} yielded no rows")
    return merge.write(cat, identifier, arrow, EqualTo("gazetteer_year", year))


def land_raw(cat, release, year, county_url=None, tract_url=None):
    """Phase 1: one boundary vintage's county and tract files, landed verbatim
    and whole. Both are small enough (85k rows at most) to read in one shot —
    nothing here needs the NCBI dumps' streaming machinery. Returns
    `(year, {identifier: rows})`.
    """
    county_url = county_url or _zip_url("counties", year)
    tract_url = tract_url or _zip_url("tracts", year)
    with tempfile.TemporaryDirectory() as tmp:
        n_counties = _land_file(cat, release, year, county_url, tmp,
                                COUNTY_COLUMNS, FULL_COUNTY_COLUMNS,
                                "raw.census__gazetteer_counties")
        n_tracts = _land_file(cat, release, year, tract_url, tmp,
                              TRACT_COLUMNS, FULL_TRACT_COLUMNS,
                              "raw.census__gazetteer_tracts")
    # version_method='release_number': the gazetteer year is a citable label
    # Census itself publishes, not a retrieval date.
    merge.manifest(cat, release, "census_gazetteer", county_url, n_counties,
                   version=str(year), method="release_number")
    return year, {"raw.census__gazetteer_counties": n_counties,
                 "raw.census__gazetteer_tracts": n_tracts}


def land_state_fips(cat, release, url=None):
    """State FIPS reference, landed once per release (not per gazetteer year —
    FIPS-to-state assignments don't move on the gazetteer's cadence). A source
    that can't be reached at ingest time is not a reason to fail the whole
    ingest: warn and return 0; `transform` falls back to the USPS code as the
    state's name and says so.
    """
    url = url or STATE_FIPS_URL
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}) if url.startswith("http") else url
        opener = urllib.request.urlopen if url.startswith("http") else open
        with opener(req) as r:
            first = r.readline()
    except urllib.error.URLError as err:
        print(f"census gazetteer: state.txt unreachable ({err}); "
             f"state names will fall back to USPS codes")
        return 0
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    header = tuple(first.rstrip("\r\n").split("|"))
    if header != STATE_COLUMNS:
        raise SystemExit(f"census gazetteer: {url} header {header} is not {STATE_COLUMNS}")

    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT state AS state, stusab AS stusab, state_name AS state_name, statens AS statens,
               '{release}' AS landed_in
        FROM read_csv('{url}', header=true, delim='|', quote='', escape='', all_varchar=true)
    """).to_arrow_table()
    return merge.write(cat, "raw.census__state_fips", arrow, AlwaysTrue())


def transform(cat, release, year):
    """Phase 2: this vintage's nation/state/county/tract rows into
    geography.unit. Scoped to `vintage = year` — loading a later vintage never
    retires an earlier one's rows (SPEC.md Acceptance B)."""
    con = duckdb.connect()
    con.register("counties", cat.load_table("raw.census__gazetteer_counties").scan(
        row_filter=EqualTo("gazetteer_year", year)).to_arrow())
    con.register("tracts", cat.load_table("raw.census__gazetteer_tracts").scan(
        row_filter=EqualTo("gazetteer_year", year)).to_arrow())

    try:
        con.register("state_fips", cat.load_table("raw.census__state_fips").scan().to_arrow())
        name_expr, join_sql = "coalesce(sf.state_name, sk.usps)", \
            "LEFT JOIN state_fips sf ON sf.state = sk.fips"
    except NoSuchTableError:
        print("census gazetteer: raw.census__state_fips not landed; "
             "state names will fall back to USPS codes")
        name_expr, join_sql = "sk.usps", ""

    unit = con.sql(f"""
        SELECT 'nation:US' AS geo_id, 'nation' AS level, 'US' AS fips, {year} AS vintage,
               'United States' AS name, CAST(NULL AS VARCHAR) AS parent_geo_id,
               CAST(NULL AS DOUBLE) AS aland_m2, CAST(NULL AS DOUBLE) AS awater_m2,
               CAST(NULL AS DOUBLE) AS centroid_lat, CAST(NULL AS DOUBLE) AS centroid_lon,
               CAST(NULL AS VARCHAR) AS geometry_uri
      UNION ALL
        SELECT 'state:' || sk.fips, 'state', sk.fips, {year}, {name_expr}, 'nation:US',
               NULL, NULL, NULL, NULL, NULL
        FROM (SELECT DISTINCT substr(geoid, 1, 2) AS fips, usps FROM counties) sk
        {join_sql}
      UNION ALL
        SELECT 'county:' || c.geoid, 'county', c.geoid, {year}, c.name,
               'state:' || substr(c.geoid, 1, 2),
               TRY_CAST(c.aland AS DOUBLE), TRY_CAST(c.awater AS DOUBLE),
               TRY_CAST(c.intptlat AS DOUBLE), TRY_CAST(c.intptlong AS DOUBLE),
               CAST(NULL AS VARCHAR)
        FROM counties c
      UNION ALL
        SELECT 'tract:' || t.geoid, 'tract', t.geoid, {year}, CAST(NULL AS VARCHAR),
               'county:' || substr(t.geoid, 1, 5),
               TRY_CAST(t.aland AS DOUBLE), TRY_CAST(t.awater AS DOUBLE),
               TRY_CAST(t.intptlat AS DOUBLE), TRY_CAST(t.intptlong AS DOUBLE),
               CAST(NULL AS VARCHAR)
        FROM tracts t
    """).to_arrow_table()

    return {"geography.unit": merge.merge(cat, "geography.unit", unit, release, EqualTo("vintage", year))}


def ingest(cat, release, year, county_url=None, tract_url=None, state_url=None):
    year, raw_counts = land_raw(cat, release, year, county_url, tract_url)
    n_state = land_state_fips(cat, release, state_url)
    return {**raw_counts, "raw.census__state_fips": n_state, **transform(cat, release, year)}
