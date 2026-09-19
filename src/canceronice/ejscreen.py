"""EPA EJScreen (every archived edition, 2015-2024) -> Iceberg: land block group and
tract CSVs whole, derive the raw environmental indicators (not the demographic or
composite EJ/Supplemental indexes -- SPEC.md's "land raw, derive later if wanted").

**Upstream is gone; the archive is not.** EPA removed EJScreen from epa.gov in
February 2025 (SPEC.md's motivating "every release stays queryable" example -- #97).
`gaftp.epa.gov/EJScreen/` 404s (checked 2026-09-19). This module reads a DOI-bearing,
checksummed third-party archive instead: EDGI's "EPA Environmental Justice Screening
Tool (EJ Screen) data, 2015-2024" on Zenodo, DOI 10.5281/zenodo.14767363
(https://zenodo.org/records/14767363), one zip per edition, deposited 2025-01-29.

**Licence.** The Zenodo deposit's own licence field is Creative Commons Attribution
4.0 International (verified 2026-09-19 via `GET zenodo.org/api/records/14767363` ->
`"license": {"id": "cc-by-4.0"}`, and via the record page's "License:" field: "Creative
Commons Attribution 4.0 International") -- redistribution with attribution is
explicitly granted regardless of the underlying data's own status. That underlying
status independently permits reuse too: EPA's own disclaimers page states "Unless
otherwise specified, geospatial data produced by the EPA is by default in the public
domain and is not subject to domestic copyright protection under 17 U.S.C. Sec 105."
(https://www.epa.gov/web-policies-and-procedures/epa-disclaimers, checked 2026-09-19)
-- EJScreen's block group/tract files are exactly such EPA-produced geospatial data.

**Version axis: the edition** (2015-2024), landed as `ejscreen_edition`. Geography
vintage per the issue: 2015-2021 editions carry 2010-vintage Census geography,
2022-2024 carry 2020-vintage (`GEO_VINTAGE`).

**Members, verified 2026-09-19** by range-fetching each edition's real Zenodo zip's
central directory (the archive runs 1.1-5.9 GB per edition; only the needed member's
bytes are ever fetched -- see `_fetch_zip_member`) and decompressing each candidate
file's header. `MEMBERS` records the exact path inside each edition's zip. Two
editions (2023, 2024) ship several near-duplicate dated folders, some marked
"DoNotUse" -- `MEMBERS` points at one without that marker (2023: "2.22_September_
UseMe", not its "..._Patch_UseMe" sibling; 2024: "2.32_August_UseMe", the higher-
numbered of two "useMe"-marked drops).

**Tract exists for 2021-2024 only** (`TRACT_EDITIONS`) -- EPA's own separate tract
CSV, not an aggregation this module computes. 2015-2020 ship block group only.

**Column layout drifts every few editions**; EJScreen's own indicator names are
stable (`CONCEPT_COLUMN`) even though the surrounding ~130-380 percentile/breakdown/
demographic columns are not and are not worth hand-declaring a shifting union schema
for (unlike cdc_svi.py's two families, this source's editions don't cleanly bucket
into two). Instead: the handful of columns this module actually parses are declared
and verified per edition (`CONCEPT_COLUMN`, checked against the real header before
landing); everything else lands verbatim in `extra_json` (one JSON object per row,
the whole source row, text-valued) -- the same "declared columns for what's used, JSON
for the rest" shape SPEC.md's Facilities section already uses for
`facility.site.attributes_json` (a DuckDB MAP-to-Arrow crash forced that one; here it
is a 10-edition verification budget, not a crash, but the shape fits the same need).
No cell is dropped -- "land raw whole" still holds -- it is just not all individually
typed.

Verified indicator presence per edition, from real headers (module CONCEPT_COLUMN):
PM25/OZONE/DSLPM/PTRAF/PRE1960PCT/PNPL/PRMP/PTSDF/PWDIS are in every edition (2015
under different lowercase/dotted names: pm/o3/dpm/traffic.score/pctpre1960/
proximity.npl/proximity.rmp/proximity.tsdf/proximity.npdes). CANCER/RESP (NATA-based
air toxics cancer risk / respiratory hazard index) run 2015-2023, dropped in 2024 in
favour of RSEI_AIR (an RSEI-based air toxics score introduced in 2023, alongside
CANCER/RESP that one transition year). UST (underground storage tank proximity)
starts 2021. NO2 and DWATER (drinking-water non-compliance) start 2024 only.

**Suppression.** No numeric missing-value sentinel (SVI's -999, say) was found in any
sampled real row across editions -- EJScreen's convention is a blank field, which
DuckDB's `nullstr=''` already lands as NULL; `value_status` is `not_available`
wherever the concept column is NULL after `TRY_CAST`, `reported` otherwise.

**Not derived here** (ponytail, follow-up):
  - **Block group is landed (`raw.ejscreen__blockgroup`, every edition -- "the single
    widest environmental file" per the issue) but not derived into
    `measure.observation`.** `geography.unit.level` documents 'block_group' as a
    valid value but no source populates it yet (census_gazetteer.py lands county and
    tract only) -- the issue's own fallback ("land EJScreen's tract-level files
    first") is what this module takes. Revisit once a Gazetteer block-group source
    lands (or add one) -- geo_id there is not FK-enforced by Iceberg either way, so
    nothing here breaks; it would just start resolving.
  - **2015** is landed and its own five indicator-adjacent columns are mapped
    (`CONCEPT_COLUMN["2015"]`), same as every other edition -- no exclusion, unlike
    SVI's 2000/2010.
  - **The composite EJ Index / Supplemental Index** (which combine these exposure
    indicators with demographics) are not landed or derived -- the issue asks for the
    environmental indicators "raw value"; the indexes are a `derive later if wanted`
    per SPEC.md.
  - **Period**: `period_start = period_end = ejscreen_edition` -- these are annual
    modeled/monitored estimates (PM2.5, ozone, traffic, proximity counts), not a
    5-year ACS pooled window like SVI's ACS-derived concepts; no interval is
    published for any of them, so `interval_level`/`lower`/`upper` are always NULL.
"""

import io
import struct
import tempfile
import urllib.request
import zipfile
import zlib
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import And, EqualTo, In

from . import merge

USER_AGENT = "cancerOnIce/1.0 (+https://github.com/seandavi/cancer-on-ice)"
ZENODO_RECORD = "https://zenodo.org/records/14767363/files"

EDITIONS = ("2015", "2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024")
TRACT_EDITIONS = ("2021", "2022", "2023", "2024")

# 2015-2021 editions carry 2010-vintage Census geography, 2022-2024 carry 2020
# (issue #97's own note, consistent with the Alaska/Connecticut FIPS changes
# cdc_svi.py verified for the same years in a sibling source).
GEO_VINTAGE = {e: (2010 if int(e) <= 2021 else 2020) for e in EDITIONS}

# Path of the real member inside https://zenodo.org/records/14767363/files/<edition>.zip
# holding the block group / tract CSV (verified 2026-09-19, module docstring). A path
# ending ".csv" is the CSV directly; ending ".csv.zip" is one more zip to open.
MEMBERS = {
    ("2015", "blockgroup"): "2015/EJSCREEN_20150505.csv.zip",
    ("2016", "blockgroup"): "2016/EJSCREEN_V3_USPR_090216_CSV.zip",
    ("2017", "blockgroup"): "2017/EJSCREEN_2017_USPR_Public.csv",
    ("2018", "blockgroup"): "2018/EJSCREEN_2018_USPR_csv.zip",
    ("2019", "blockgroup"): "2019/EJSCREEN_2019_USPR.csv.zip",
    ("2020", "blockgroup"): "2020/EJSCREEN_2020_USPR.csv.zip",
    ("2021", "blockgroup"): "2021/EJSCREEN_2021_USPR.csv.zip",
    ("2021", "tract"): "2021/EJSCREEN_2021_USPR_Tracts.csv.zip",
    ("2022", "blockgroup"): "2022/EJSCREEN_2022_with_AS_CNMI_GU_VI.csv.zip",
    ("2022", "tract"): "2022/EJSCREEN_2022_Full_with_AS_CNMI_GU_VI_Tracts.csv.zip",
    ("2023", "blockgroup"): "2023/2.22_September_UseMe/EJSCREEN_2023_BG_with_AS_CNMI_GU_VI.csv.zip",
    ("2023", "tract"): "2023/2.22_September_UseMe/EJSCREEN_2023_Tracts_with_AS_CNMI_GU_VI.csv.zip",
    ("2024", "blockgroup"): "2024/2.32_August_UseMe/EJSCREEN_2024_BG_with_AS_CNMI_GU_VI.csv.zip",
    ("2024", "tract"): "2024/2.32_August_UseMe/EJScreen_2024_Tract_with_AS_CNMI_GU_VI.csv.zip",
}

# The geography-id column's real name -- "ID" (12-digit block group / 11-digit tract
# GEOID) in every edition except 2015, which names it "FIPS".
ID_COLUMN = {"2015": "FIPS"}

# concept -> real column name, per edition, verified against the real header (module
# docstring). A concept absent from an edition's dict is simply not published that
# year and lands NULL there, in raw and (so) in measure.observation.
_CLASSIC = dict(pm25="PM25", ozone="OZONE", dslpm="DSLPM", cancer="CANCER", resp="RESP",
                ptraf="PTRAF", pre1960pct="PRE1960PCT", pnpl="PNPL", prmp="PRMP",
                ptsdf="PTSDF", pwdis="PWDIS")
CONCEPT_COLUMN = {
    "2015": dict(pm25="pm", ozone="o3", dslpm="dpm", cancer="cancer", resp="resp",
                 ptraf="traffic.score", pre1960pct="pctpre1960", pnpl="proximity.npl",
                 prmp="proximity.rmp", ptsdf="proximity.tsdf", pwdis="proximity.npdes"),
    "2016": dict(_CLASSIC), "2017": dict(_CLASSIC), "2018": dict(_CLASSIC),
    "2019": dict(_CLASSIC), "2020": dict(_CLASSIC),
    "2021": dict(_CLASSIC, ust="UST"),
    "2022": dict(_CLASSIC, ust="UST"),
    "2023": dict(_CLASSIC, ust="UST", rsei_air="RSEI_AIR"),
    "2024": dict(pm25="PM25", ozone="OZONE", dslpm="DSLPM", rsei_air="RSEI_AIR",
                 ptraf="PTRAF", pre1960pct="PRE1960PCT", pnpl="PNPL", prmp="PRMP",
                 ptsdf="PTSDF", ust="UST", pwdis="PWDIS", no2="NO2", dwater="DWATER"),
}
CONCEPTS = ("pm25", "ozone", "dslpm", "cancer", "resp", "rsei_air", "ptraf", "pre1960pct",
            "pnpl", "prmp", "ptsdf", "ust", "pwdis", "no2", "dwater")

CONCEPT_LABEL = {
    "pm25": "PM2.5 concentration (annual mean), modeled",
    "ozone": "Ozone concentration (seasonal average), modeled",
    "dslpm": "Diesel particulate matter concentration, modeled (NATA)",
    "cancer": "Air toxics cancer risk, modeled (NATA); not published from 2024",
    "resp": "Air toxics respiratory hazard index, modeled (NATA); not published from 2024",
    "rsei_air": "Air toxics concentration score (RSEI-based); published from 2023",
    "ptraf": "Traffic proximity and volume near major roads",
    "pre1960pct": "Percent of housing units built before 1960 (lead paint indicator)",
    "pnpl": "Proximity to National Priorities List (Superfund) sites",
    "prmp": "Proximity to Risk Management Plan (RMP) facilities",
    "ptsdf": "Proximity to hazardous waste treatment/storage/disposal facilities",
    "ust": "Proximity to leaking/underground storage tanks; published from 2021",
    "pwdis": "Proximity-weighted modeled toxic concentration from NPDES wastewater dischargers",
    "no2": "NO2 (nitrogen dioxide) concentration, modeled; published from 2024 only",
    "dwater": "Drinking water non-compliance indicator; published from 2024 only",
}


def _http_get(url, range_start=None, range_end=None):
    headers = {"User-Agent": USER_AGENT}
    if range_start is not None:
        headers["Range"] = f"bytes={range_start}-{range_end}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        return r.read()


def _content_length(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(req) as r:
        return int(r.headers["Content-Length"])


def _zip64_sizes(extra, off, csize, usize):
    """Zip64 extended-information extra field (header id 1): the fields that
    overflowed 32 bits in the central directory record, in order usize, csize,
    offset -- only the ones that were actually 0xFFFFFFFF are present."""
    i = 0
    while i < len(extra):
        hid, hsz = struct.unpack("<HH", extra[i:i + 4])
        if hid == 1:
            vals = extra[i + 4:i + 4 + hsz]
            fields = iter(struct.unpack(f"<{len(vals) // 8}Q", vals[:len(vals) // 8 * 8]))
            if usize == 0xFFFFFFFF:
                usize = next(fields)
            if csize == 0xFFFFFFFF:
                csize = next(fields)
            if off == 0xFFFFFFFF:
                off = next(fields)
            return off, csize, usize
        i += 4 + hsz
    return off, csize, usize


def _fetch_zip_member(url, member_path):
    """One named member's bytes out of a remote ZIP too large to download whole
    (EJScreen's Zenodo year archives run 1.1-5.9 GB; this module needs one ~2-30%
    slice of each). A ZIP's End-Of-Central-Directory record (and, for a >4 GB
    archive, its ZIP64 counterpart) sits at the tail and is tiny; it gives every
    member's exact offset and size, so only that member's bytes are ever fetched, via
    HTTP Range -- "one download per file" in spirit even though it is a few ranged
    requests rather than one whole-file GET (AGENTS.md "be polite to upstreams").

    Returns the final CSV's bytes: `member_path` ending ".csv" is returned as-is
    (decompressed); ending ".csv.zip" is one more (small, now-local) zip to open.
    """
    size = _content_length(url)
    tail = _http_get(url, max(0, size - 2_000_000), size - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit(f"ejscreen: no End-Of-Central-Directory record found in {url}")
    (_, _, _, _, _, cd_size, cd_offset, _) = struct.unpack(
        "<4sHHHHIIH", tail[eocd:eocd + 22])
    if cd_offset == 0xFFFFFFFF:
        # >4 GB archive (2023, 2024): the real offset/size are in the ZIP64 End-Of-
        # Central-Directory record, found via the locator that precedes the regular
        # EOCD (APPNOTE.TXT 4.3.15/4.3.16).
        loc = tail.rfind(b"PK\x06\x07", 0, eocd)
        z64_eocd_offset = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        z64 = _http_get(url, z64_eocd_offset, z64_eocd_offset + 55)
        cd_size, cd_offset = struct.unpack("<QQ", z64[40:56])
    cd = _http_get(url, cd_offset, cd_offset + cd_size - 1)

    i = 0
    while i < len(cd) - 4:
        if cd[i:i + 4] != b"PK\x01\x02":
            break
        (_, _, _, _, method, _, _, _, csize, usize, nlen, elen, clen, _, _, _,
         off) = struct.unpack("<4sHHHHHHIIIHHHHHII", cd[i:i + 46])
        name = cd[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        extra = cd[i + 46 + nlen:i + 46 + nlen + elen]
        if name == member_path:
            if off == 0xFFFFFFFF or csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
                off, csize, usize = _zip64_sizes(extra, off, csize, usize)
            return _extract_at(url, off, csize, method, member_path)
        i += 46 + nlen + elen + clen
    raise SystemExit(f"ejscreen: {member_path!r} not found in {url}")


def _extract_at(url, offset, csize, method, member_path):
    lfh = _http_get(url, offset, offset + 30 + 1024)
    _, _, _, lmethod, _, _, _, _, _, nlen, elen = struct.unpack("<4sHHHHHIIIHH", lfh[:30])
    data_off = offset + 30 + nlen + elen
    data = _http_get(url, data_off, data_off + csize - 1)
    payload = zlib.decompress(data, -15) if lmethod == 8 else data
    if not member_path.endswith(".zip"):
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise SystemExit(f"ejscreen: expected exactly one .csv in {member_path}, "
                             f"found {csvs}")
        return z.read(csvs[0])


def _fetch_csv_bytes(edition, level, url):
    """The level's CSV bytes for one edition: an override `url` (remote whole-file,
    or a local path -- how tests avoid a real network call) if given, else the real
    Zenodo archive member (module docstring)."""
    if url:
        if url.startswith("http"):
            data = _http_get(url)
        else:
            data = Path(url).read_bytes()
        if url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
                if len(csvs) != 1:
                    raise SystemExit(f"ejscreen: expected exactly one .csv in {url}, "
                                     f"found {csvs}")
                return z.read(csvs[0])
        return data
    member_path = MEMBERS[(edition, level)]
    outer_url = f"{ZENODO_RECORD}/{edition}.zip?download=1"
    return _fetch_zip_member(outer_url, member_path)


def land_raw(cat, release, edition=None, level="blockgroup", url=None):
    """Phase 1: one edition's block group or tract CSV, landed whole. Concept
    columns this module knows about (CONCEPT_COLUMN) are typed and verified against
    the real header; everything else lands in `extra_json` (module docstring)."""
    edition = edition or max(EDITIONS, key=int)
    if edition not in EDITIONS:
        raise SystemExit(f"ejscreen: no known edition {edition!r}; landed editions: "
                         f"{sorted(EDITIONS, key=int)}")
    if level not in ("blockgroup", "tract"):
        raise SystemExit(f"ejscreen: level must be 'blockgroup' or 'tract', got {level!r}")
    if level == "tract" and edition not in TRACT_EDITIONS:
        raise SystemExit(f"ejscreen: tract is only landed for {TRACT_EDITIONS}; "
                         f"{edition!r} is not one of them")
    identifier = f"raw.ejscreen__{level}"
    id_col = ID_COLUMN.get(edition, "ID")
    concepts = CONCEPT_COLUMN[edition]

    csv_bytes = _fetch_csv_bytes(edition, level, url)
    try:
        csv_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # A real stray non-UTF-8 byte in a display name column (verified 2026-09-19:
        # 2023 block group, "Do\xf1a Ana County" written Latin-1 mid-file while the
        # rest of the file is valid UTF-8 -- the same quirk census_gazetteer.py's
        # 2010 county file has, but that file is ASCII apart from the stray byte, so
        # it safely re-decodes the whole thing as Latin-1; this file has other real
        # UTF-8 multi-byte names (Puerto Rico places) that a blanket Latin-1 re-decode
        # would corrupt, so only the one bad byte is replaced here instead.
        csv_bytes = csv_bytes.decode("utf-8", errors="replace").encode("utf-8")

    with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
        tmp.write(csv_bytes)
        tmp.flush()
        con = duckdb.connect()
        header = [c[0] for c in con.sql(
            f"SELECT * FROM read_csv('{tmp.name}', header=true, all_varchar=true, "
            f"nullstr='') LIMIT 0"
        ).description]
        missing = [c for c in (id_col, *concepts.values()) if c not in header]
        if missing:
            raise SystemExit(f"ejscreen: {edition} {level} file is missing declared "
                             f"column(s) {missing}; real header does not match "
                             f"CONCEPT_COLUMN/ID_COLUMN -- verify and update them")

        concept_select = ", ".join(
            f'"{concepts[c]}" AS {c}' if c in concepts else f"NULL::VARCHAR AS {c}"
            for c in CONCEPTS
        )
        arrow = con.sql(f"""
            SELECT "{id_col}" AS geo_id, {concept_select},
                   '{edition}' AS ejscreen_edition, '{release}' AS landed_in,
                   to_json(t) AS extra_json
            FROM read_csv('{tmp.name}', header=true, all_varchar=true, nullstr='') AS t
        """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"ejscreen: {edition} {level} file yielded no rows")

    n = merge.write(cat, identifier, arrow, EqualTo("ejscreen_edition", edition))
    source_url = url or f"{ZENODO_RECORD}/{edition}.zip?download=1#{MEMBERS[(edition, level)]}"
    merge.manifest(cat, release, f"ejscreen_{level}", source_url, n, version=edition,
                   method="release_number")
    return edition, n


def transform(cat, release, edition):
    """Phase 2: measure.definition / measure.stratum / measure.observation for one
    edition, from raw.ejscreen__tract (block group is not yet derivable -- module
    docstring). A no-op (returns None) for an edition with no tract file landed."""
    try:
        raw = cat.load_table("raw.ejscreen__tract").scan(
            row_filter=EqualTo("ejscreen_edition", edition)).to_arrow()
    except NoSuchTableError:
        return None
    if not raw.num_rows:
        return None

    con = duckdb.connect()
    con.register("t", raw)
    concepts = CONCEPT_COLUMN[edition]
    geo_vintage = GEO_VINTAGE[edition]

    definition = pa.Table.from_pylist([
        dict(measure_id=f"EJSCREEN:{c}", source="EJSCREEN", label=CONCEPT_LABEL[c],
             units=None, universe=None,
             rate_basis="percent" if c == "pre1960pct" else "index",
             age_adjustment=None, method="model_based", cancer_site_code=None,
             doc=f"{CONCEPT_LABEL[c]}. EPA EJScreen block-group/tract environmental "
                 f"indicator; see the technical documentation "
                 f"(https://www.epa.gov/ejscreen/technical-documentation-ejscreen) "
                 f"for the exact modeling method.")
        for c in CONCEPTS
    ])
    def_ids = definition.column("measure_id").to_pylist()

    stratum = pa.Table.from_pylist([dict(
        stratum_id="EJSCREEN:ALL", source="EJSCREEN", sex=None, age_group=None,
        race_ethnicity=None, stage=None, other=None, scheme="EJSCREEN_TOTAL",
    )])

    obs_sql = " UNION ALL ".join(f"""
        SELECT 'EJSCREEN' AS source, '{edition}' AS source_release,
               'EJSCREEN:{c}' AS measure_id, 'tract:' || geo_id AS geo_id,
               {geo_vintage} AS geo_vintage, '{edition}' AS period_start,
               '{edition}' AS period_end, 'EJSCREEN:ALL' AS stratum_id,
               TRY_CAST({c} AS DOUBLE) AS value,
               NULL::DOUBLE AS lower, NULL::DOUBLE AS upper,
               NULL::DOUBLE AS interval_level, NULL::DOUBLE AS numerator,
               NULL::DOUBLE AS denominator,
               CASE WHEN TRY_CAST({c} AS DOUBLE) IS NULL THEN 'not_available'
                    ELSE 'reported' END AS value_status,
               NULL::VARCHAR AS reliability_flag, NULL::VARCHAR AS trend
        FROM t
    """ for c in concepts)
    observation = con.sql(obs_sql).to_arrow_table()
    merge.check_observations(observation)

    scope = EqualTo("source", "EJSCREEN")
    return {
        "measure.definition": merge.write(cat, "measure.definition", definition,
                                          And(scope, In("measure_id", def_ids))),
        "measure.stratum": merge.write(cat, "measure.stratum", stratum,
                                       And(scope, In("stratum_id", ["EJSCREEN:ALL"]))),
        "measure.observation": merge.merge(
            cat, "measure.observation", observation, release,
            And(scope, EqualTo("source_release", edition))),
    }


def ingest(cat, release, edition=None, level="blockgroup", url=None):
    edition, n = land_raw(cat, release, edition, level, url)
    result = {f"raw.ejscreen__{level}": n}
    if level == "tract":
        derived = transform(cat, release, edition)
        if derived:
            result.update(derived)
    return result
