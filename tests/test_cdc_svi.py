"""CDC/ATSDR SVI: land whole -> derive percentile + component observations, two
real layout families coexisting without one clobbering the other's definitions.

Fixtures are real byte-for-byte excerpts of the actual downloaded CSVs (verified
2026-09-18 against svi.cdc.gov's real files):
  - tiny_svi_county_2018.csv (2014-family layout): Autauga County AL, Fairfield
    County CT (still the legacy county FIPS in 2018), Doña Ana County NM (non-ASCII
    name), and Rio Arriba County NM with a real partial suppression (POV/theme1/
    overall RPL_THEMES = -999, every other concept still reported).
  - tiny_svi_county_2020.csv / tiny_svi_county_2022.csv (2020-family layout):
    Autauga, a Connecticut county/planning-region row (09001 legacy in 2020, 09110
    planning region in 2022 -- the real geo_vintage switch), Doña Ana.
  - tiny_svi_tract_2022.csv (2020-family layout, tract grain): an Autauga County
    tract, a Connecticut (Capitol) planning-region tract, and a real fully/partially
    suppressed tract (Trigg County, KY 21221980100: poverty150 reported, no high
    school diploma and no vehicle suppressed, RPL_THEME1/2/4 and RPL_THEMES
    suppressed but RPL_THEME3 = 0.0).
"""

from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.expressions import EqualTo

from canceronice import cdc_svi, merge

REL = "2026.08"
FIX = Path(__file__).parent
COUNTY_2018 = str(FIX / "tiny_svi_county_2018.csv")
COUNTY_2020 = str(FIX / "tiny_svi_county_2020.csv")
COUNTY_2022 = str(FIX / "tiny_svi_county_2022.csv")
TRACT_2022 = str(FIX / "tiny_svi_tract_2022.csv")


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat):
    edition, n = cdc_svi.land_raw(cat, REL, "2018", "county", url=COUNTY_2018)
    assert edition == "2018"
    assert n == 4

    raw = rows(cat, "raw.svi__county")
    assert len(raw) == 4
    assert {r["FIPS"] for r in raw} == {"01001", "09001", "35013", "35039"}
    assert {r["svi_edition"] for r in raw} == {"2018"}
    assert {r["landed_in"] for r in raw} == {REL}
    # unparsed: the real -999 sentinel lands as text, not a number
    rio_arriba = next(r for r in raw if r["FIPS"] == "35039")
    assert rio_arriba["EP_POV"] == "-999.0"
    assert rio_arriba["RPL_THEMES"] == "-999.0"
    # a column only the "2020" layout has is NULL for a 2014-family edition
    assert rio_arriba["STCNTY"] is None
    # non-ASCII name survives verbatim
    assert next(r for r in raw if r["FIPS"] == "35013")["LOCATION"] == "Doña Ana County, New Mexico"

    # re-landing the same edition replaces it rather than appending
    cdc_svi.land_raw(cat, REL, "2018", "county", url=COUNTY_2018)
    assert len(rows(cat, "raw.svi__county")) == 4


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("AFFGEOID,ST,STATE\n0500000US01001,01,ALABAMA\n")
    with pytest.raises(SystemExit, match="header is not the declared"):
        cdc_svi.land_raw(cat, REL, "2018", "county", url=str(bad))


def test_unknown_edition_or_tract_combo_rejected(cat):
    with pytest.raises(SystemExit, match="no known layout"):
        cdc_svi.land_raw(cat, REL, "2010", "county", url=COUNTY_2018)
    with pytest.raises(SystemExit, match="tract is only landed"):
        cdc_svi.land_raw(cat, REL, "2018", "tract", url=COUNTY_2018)


def test_2020_family_lands_into_the_same_union_table(cat):
    cdc_svi.land_raw(cat, REL, "2020", "county", url=COUNTY_2020)
    raw = {r["FIPS"]: r for r in rows(cat, "raw.svi__county",
                                       row_filter=EqualTo("svi_edition", "2020"))}
    assert set(raw) == {"01001", "09001", "35013"}
    # Connecticut is still the legacy county FIPS in 2020 (planning regions from 2022)
    assert raw["09001"]["STCNTY"] == "09001"
    # a column only the "2014" layout has is NULL for a 2020-family edition
    assert raw["01001"]["AFFGEOID"] is None
    assert raw["01001"]["EP_POV150"] is not None


def test_derives_definitions_stratum_and_observations(cat):
    counts = cdc_svi.ingest(cat, REL, "2018", "county", url=COUNTY_2018)
    assert counts["raw.svi__county"] == 4

    defs = {d["measure_id"]: d for d in rows(cat, "measure.definition", row_filter="source = 'SVI'")}
    assert len(defs) == 12  # 5 percentile + 7 component measures, 2014-family
    assert defs["SVI:RPL_THEMES:2014"]["rate_basis"] == "index"
    assert defs["SVI:RPL_THEMES:2014"]["method"] == "derived"
    assert defs["SVI:poverty"]["rate_basis"] == "percent"
    assert defs["SVI:poverty"]["method"] == "survey_direct"
    assert "SVI:poverty150" not in defs  # 2014-family never asserts the 2020 variant

    strata = rows(cat, "measure.stratum", row_filter="source = 'SVI'")
    assert [s["stratum_id"] for s in strata] == ["SVI:ALL"]

    obs = {(r["geo_id"], r["measure_id"]): r
           for r in rows(cat, "measure.observation", row_filter="source = 'SVI'")}
    assert obs[("county:01001", "SVI:RPL_THEMES:2014")]["value"] == pytest.approx(0.4354)
    assert obs[("county:01001", "SVI:RPL_THEMES:2014")]["geo_vintage"] == 2018  # the fixture's own edition year
    assert obs[("county:01001", "SVI:RPL_THEMES:2014")]["period_start"] == "2014"
    assert obs[("county:01001", "SVI:RPL_THEMES:2014")]["period_end"] == "2018"
    assert obs[("county:01001", "SVI:RPL_THEMES:2014")]["interval_level"] is None

    # SPEC.md Acceptance C: the real -999 sentinel never reads as a number, and it is
    # checked independently per measure -- Rio Arriba's poverty/theme1/overall are
    # suppressed but its other themes and components are not.
    assert obs[("county:35039", "SVI:poverty")]["value"] is None
    assert obs[("county:35039", "SVI:poverty")]["value_status"] == "not_available"
    assert obs[("county:35039", "SVI:RPL_THEME1:2014")]["value_status"] == "not_available"
    assert obs[("county:35039", "SVI:RPL_THEMES:2014")]["value_status"] == "not_available"
    assert obs[("county:35039", "SVI:RPL_THEME2:2014")]["value_status"] == "reported"
    assert obs[("county:35039", "SVI:no_hs_diploma")]["value_status"] == "reported"
    assert obs[("county:35039", "SVI:no_hs_diploma")]["value"] == pytest.approx(13.8)

    # MOE -> lower/upper at interval_level 0.90 (ACS convention)
    ok = obs[("county:01001", "SVI:poverty")]
    assert ok["interval_level"] == 0.90
    assert ok["lower"] == pytest.approx(ok["value"] - obs_moe(cat, "01001", "MP_POV"))


def obs_moe(cat, fips, column):
    raw = next(r for r in rows(cat, "raw.svi__county") if r["FIPS"] == fips)
    return float(raw[column])


def test_two_families_coexist_without_clobbering_definitions(cat):
    """SPEC.md / AGENTS.md: measure.definition is never replaced wholesale per source
    (issue #76) -- deriving the 2020-family after the 2014-family must not delete the
    2014-family's own theme/percentile definitions."""
    cdc_svi.ingest(cat, REL, "2018", "county", url=COUNTY_2018)
    cdc_svi.ingest(cat, REL, "2020", "county", url=COUNTY_2020)

    ids = {d["measure_id"] for d in rows(cat, "measure.definition", row_filter="source = 'SVI'")}
    assert "SVI:RPL_THEMES:2014" in ids
    assert "SVI:RPL_THEMES:2020" in ids
    assert "SVI:poverty" in ids
    assert "SVI:poverty150" in ids
    # stable across both families
    assert ids.issuperset({"SVI:no_hs_diploma", "SVI:uninsured", "SVI:no_vehicle",
                           "SVI:age_65_plus", "SVI:minority", "SVI:limited_english"})


def test_county_and_tract_combine_without_retiring_each_other(cat):
    """measure.observation's merge scope is (source, source_release), not per-level --
    landing county then tract (or vice versa) for the same edition must accumulate,
    never retire the other level's rows (module docstring)."""
    cdc_svi.ingest(cat, REL, "2022", "county", url=COUNTY_2022)
    cdc_svi.ingest(cat, REL, "2022", "tract", url=TRACT_2022)

    live = rows(cat, "measure.observation",
               row_filter="source = 'SVI' AND source_release = '2022' AND valid_to IS NULL")
    levels = {r["geo_id"].split(":")[0] for r in live}
    assert levels == {"county", "tract"}

    # re-deriving from county alone (e.g. a county-only re-ingest) must not retire tract
    cdc_svi.ingest(cat, "2026.09", "2022", "county", url=COUNTY_2022)
    live2 = rows(cat, "measure.observation",
                row_filter="source = 'SVI' AND source_release = '2022' AND valid_to IS NULL")
    assert {r["geo_id"].split(":")[0] for r in live2} == {"county", "tract"}
    assert len(live2) == len(live)

    # the 2022 geography switch: county FIPS 09110 is a planning region, vintage is the
    # edition's own year (2022), not a landed Gazetteer vintage
    ct = next(r for r in live2 if r["geo_id"] == "county:09110")
    assert ct["geo_vintage"] == 2022
    ct_tract = next(r for r in live2 if r["geo_id"].startswith("tract:09110"))
    assert ct_tract["geo_vintage"] == 2022

    # a real partially-suppressed tract row: poverty150 reported, two concepts and
    # three of the five percentile measures suppressed, one theme percentile is a
    # genuine 0.0 (not suppressed) -- 0.0 must not be mistaken for NULL/not_available.
    trigg = {r["measure_id"]: r for r in live2 if r["geo_id"] == "tract:21221980100"}
    assert trigg["SVI:poverty150"]["value"] == 100.0
    assert trigg["SVI:poverty150"]["value_status"] == "reported"
    assert trigg["SVI:no_hs_diploma"]["value_status"] == "not_available"
    assert trigg["SVI:RPL_THEME3:2020"]["value"] == 0.0
    assert trigg["SVI:RPL_THEME3:2020"]["value_status"] == "reported"
    assert trigg["SVI:RPL_THEMES:2020"]["value_status"] == "not_available"


def test_rerun_is_idempotent(cat):
    cdc_svi.ingest(cat, REL, "2018", "county", url=COUNTY_2018)
    counts = cdc_svi.ingest(cat, "2026.09", "2018", "county", url=COUNTY_2018)
    assert counts["measure.observation"]["written"] == 0
    assert counts["measure.observation"]["unchanged"] == 48  # 4 counties x 12 measures
    assert counts["measure.definition"] == 12
    assert counts["measure.stratum"] == 1


def other_observation():
    return dict(source="OTHER", source_release="V0", measure_id="OTHER:x", geo_id="county:99999",
               geo_vintage=2020, period_start="2020", period_end="2020", stratum_id="OTHER:none",
               value=1.0, lower=None, upper=None, interval_level=None, numerator=None,
               denominator=None, value_status="reported", reliability_flag=None, trend=None)


def test_svi_does_not_retire_another_writer(cat):
    other = pa.Table.from_pylist([other_observation()])
    merge.merge(cat, "measure.observation", other, REL, EqualTo("source", "OTHER"))

    cdc_svi.ingest(cat, REL, "2018", "county", url=COUNTY_2018)
    cdc_svi.ingest(cat, "2026.09", "2018", "county", url=COUNTY_2018)

    live_other = rows(cat, "measure.observation",
                      row_filter="source = 'OTHER' AND valid_to IS NULL")
    assert len(live_other) == 1
    assert live_other[0]["valid_from"] == REL


def test_every_column_is_documented(cat):
    cdc_svi.ingest(cat, REL, "2018", "county", url=COUNTY_2018)
    cdc_svi.ingest(cat, REL, "2022", "tract", url=TRACT_2022)
    for identifier in ("raw.svi__county", "raw.svi__tract"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment")
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
