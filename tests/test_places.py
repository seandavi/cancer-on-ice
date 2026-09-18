"""CDC PLACES: land whole -> derive definitions/stratum/observations, two
releases coexisting.

The fixtures are handcrafted at test time from real rows copied verbatim out
of the actual 2024 and 2025 county-data CSVs (fu4u-a9bh, swc5-untb) — the
header, a Colorado county in both releases, a Connecticut planning region, an
Alaska county (leading-zero FIPS), the national aggregate row (LocationID
'59'), a suppressed cell with its real footnote text, and the real
ISOLATION->LONELINESS measure rename — so the test never touches the network.
"""

import pytest
from pyiceberg.expressions import EqualTo

from canceronice import catalog, places

REL1, REL2 = "2026.08", "2026.09"


def line(**cells):
    return ",".join(cells.get(c, "") for c in places.COLUMNS)


# 2024 release (fu4u-a9bh): Denver County both prevalence types, a Connecticut
# planning region (09110 -- 2024 already uses the post-2022 regions), an
# Alaska county with a leading-zero FIPS, the national row, and ISOLATION
# (renamed to LONELINESS in 2025).
DATA_2024 = [
    line(Year="2022", StateAbbr="CO", StateDesc="Colorado", LocationName="Denver",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="Current cigarette smoking among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="14.2",
         Low_Confidence_Limit="12.6", High_Confidence_Limit="15.9",
         TotalPopulation="713252", TotalPop18plus="584904", LocationID="08031",
         CategoryID="RISKBEH", MeasureId="CSMOKING", DataValueTypeID="CrdPrv",
         Short_Question_Text="Current Cigarette Smoking",
         Geolocation="POINT (-104.876829827118 39.7613507378355)"),
    line(Year="2022", StateAbbr="CO", StateDesc="Colorado", LocationName="Denver",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="Current cigarette smoking among adults", Data_Value_Unit="%",
         Data_Value_Type="Age-adjusted prevalence", Data_Value="14.3",
         Low_Confidence_Limit="12.6", High_Confidence_Limit="16.0",
         TotalPopulation="713252", TotalPop18plus="584904", LocationID="08031",
         CategoryID="RISKBEH", MeasureId="CSMOKING", DataValueTypeID="AgeAdjPrv",
         Short_Question_Text="Current Cigarette Smoking",
         Geolocation="POINT (-104.876829827118 39.7613507378355)"),
    line(Year="2022", StateAbbr="CT", StateDesc="Connecticut", LocationName="Capitol",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="Current cigarette smoking among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="12.1",
         Low_Confidence_Limit="10.9", High_Confidence_Limit="13.4",
         TotalPopulation="981447", TotalPop18plus="783914", LocationID="09110",
         CategoryID="RISKBEH", MeasureId="CSMOKING", DataValueTypeID="CrdPrv",
         Short_Question_Text="Current Cigarette Smoking",
         Geolocation="POINT (-72.5720699045246 41.8184543884154)"),
    line(Year="2022", StateAbbr="AK", StateDesc="Alaska", LocationName="Anchorage",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="Current cigarette smoking among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="13.4",
         Low_Confidence_Limit="12.0", High_Confidence_Limit="14.9",
         TotalPopulation="287145", TotalPop18plus="219637", LocationID="02020",
         CategoryID="RISKBEH", MeasureId="CSMOKING", DataValueTypeID="CrdPrv",
         Short_Question_Text="Current Cigarette Smoking",
         Geolocation="POINT (-149.112545841578 61.150482370682)"),
    line(Year="2022", StateAbbr="US", StateDesc="United States", LocationName="",
         DataSource="BRFSS", Category="Prevention",
         Measure="Mammography use among women aged 50-74 years", Data_Value_Unit="%",
         Data_Value_Type="Age-adjusted prevalence", Data_Value="76.0",
         Low_Confidence_Limit="75.4", High_Confidence_Limit="76.7",
         TotalPopulation="333287557", TotalPop18plus="260836730", LocationID="59",
         CategoryID="PREVENT", MeasureId="MAMMOUSE", DataValueTypeID="AgeAdjPrv",
         Short_Question_Text="Mammography", Geolocation=""),
    line(Year="2022", StateAbbr="AL", StateDesc="Alabama", LocationName="Franklin",
         DataSource="BRFSS", Category="Health-Related Social Needs",
         Measure="Feeling socially isolated among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="34.2",
         Low_Confidence_Limit="29.2", High_Confidence_Limit="39.4",
         TotalPopulation="31932", TotalPop18plus="24099", LocationID="01059",
         CategoryID="SOCLNEED", MeasureId="ISOLATION", DataValueTypeID="CrdPrv",
         Short_Question_Text="Social Isolation",
         Geolocation="POINT (-87.8436750232892 34.4417388803873)"),
]

# 2025 release (swc5-untb): the same Denver measure with a changed value, the
# national row, LONELINESS (ISOLATION's real successor), and a genuinely
# suppressed cell with its real footnote text.
DATA_2025 = [
    line(Year="2023", StateAbbr="CO", StateDesc="Colorado", LocationName="Denver",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="Current cigarette smoking among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="10.3",
         Low_Confidence_Limit="7.9", High_Confidence_Limit="13.0",
         TotalPopulation="716577", TotalPop18plus="589711", LocationID="08031",
         CategoryID="RISKBEH", MeasureId="CSMOKING", DataValueTypeID="CrdPrv",
         Short_Question_Text="Current Cigarette Smoking",
         Geolocation="POINT (-104.876829827118 39.7613507378355)"),
    line(Year="2022", StateAbbr="US", StateDesc="United States", LocationName="",
         DataSource="BRFSS", Category="Prevention",
         Measure="Mammography use among women aged 50-74 years", Data_Value_Unit="%",
         Data_Value_Type="Age-adjusted prevalence", Data_Value="76.0",
         Low_Confidence_Limit="75.4", High_Confidence_Limit="76.7",
         TotalPopulation="334914895", TotalPop18plus="262083034", LocationID="59",
         CategoryID="PREVENT", MeasureId="MAMMOUSE", DataValueTypeID="AgeAdjPrv",
         Short_Question_Text="Mammography", Geolocation=""),
    line(Year="2023", StateAbbr="AL", StateDesc="Alabama", LocationName="Franklin",
         DataSource="BRFSS", Category="Health-Related Social Needs",
         Measure="Loneliness among adults", Data_Value_Unit="%",
         Data_Value_Type="Crude prevalence", Data_Value="35.9",
         Low_Confidence_Limit="30.6", High_Confidence_Limit="41.7",
         TotalPopulation="31802", TotalPop18plus="23799", LocationID="01059",
         CategoryID="SOCLNEED", MeasureId="LONELINESS", DataValueTypeID="CrdPrv",
         Short_Question_Text="Loneliness",
         Geolocation="POINT (-87.8436750232892 34.4417388803873)"),
    line(Year="2023", StateAbbr="TX", StateDesc="Texas", LocationName="Loving",
         DataSource="BRFSS", Category="Health Risk Behaviors",
         Measure="No leisure-time physical activity among adults", Data_Value_Unit="%",
         Data_Value_Type="Age-adjusted prevalence", Data_Value="",
         Data_Value_Footnote_Symbol="*",
         Data_Value_Footnote="Estimates suppressed for population less than 50",
         Low_Confidence_Limit="", High_Confidence_Limit="",
         TotalPopulation="43", TotalPop18plus="30", LocationID="48301",
         CategoryID="RISKBEH", MeasureId="LPA", DataValueTypeID="AgeAdjPrv",
         Short_Question_Text="Physical Inactivity",
         Geolocation="POINT (-103.579939867014 31.8491416757447)"),
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("CANCERONICE_WAREHOUSE", str(tmp_path / "warehouse"))
    monkeypatch.delenv("CANCERONICE_URI", raising=False)
    return catalog()


def write(path, data):
    path.write_text("\n".join([",".join(places.COLUMNS), *data]) + "\n")
    return str(path)


@pytest.fixture
def csv2024(tmp_path):
    return write(tmp_path / "tiny_places_2024.csv", DATA_2024)


@pytest.fixture
def csv2025(tmp_path):
    return write(tmp_path / "tiny_places_2025.csv", DATA_2025)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat, csv2024):
    places_release, n = places.land_raw(cat, REL1, places_release="2024", url=csv2024)
    assert places_release == "2024" and n == 6

    raw = rows(cat, "raw.places__county")
    assert len(raw) == 6
    denver = next(r for r in raw if r["LocationID"] == "08031" and r["Data_Value_Type"] == "Crude prevalence")
    assert denver["Data_Value"] == "14.2" and denver["places_release"] == "2024"
    # an empty cell is PLACES' missing marker and reads as NULL
    assert denver["Data_Value_Footnote"] is None
    # leading zeros survive on both a county and a national-row FIPS-like code
    assert {r["LocationID"] for r in raw if r["StateAbbr"] == "AK"} == {"02020"}

    # re-landing the same release replaces it rather than appending
    places.land_raw(cat, REL1, places_release="2024", url=csv2024)
    assert len(rows(cat, "raw.places__county")) == 6


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    header = [c for c in places.COLUMNS if c != "TotalPop18plus"]  # the 2021-2023 shape
    bad = tmp_path / "old.csv"
    bad.write_text(",".join(header) + "\n")
    with pytest.raises(SystemExit, match="TotalPop18plus"):
        places.land_raw(cat, REL1, places_release="2024", url=str(bad))


def test_unknown_places_release_fails(cat):
    with pytest.raises(SystemExit, match="2019"):
        places.land_raw(cat, REL1, places_release="2019")


def test_derives_definitions_stratum_and_observations(cat, csv2024):
    counts = places.ingest(cat, REL1, places_release="2024", url=csv2024)
    assert counts["raw.places__county"] == 6

    defs = {d["measure_id"]: d for d in rows(cat, "measure.definition")}
    assert {"PLACES:CSMOKING:crude", "PLACES:CSMOKING:age_adjusted",
            "PLACES:MAMMOUSE:age_adjusted", "PLACES:ISOLATION:crude"} <= set(defs)
    crude = defs["PLACES:CSMOKING:crude"]
    assert crude["universe"] == "adults aged >=18 years" and crude["age_adjustment"] is None
    assert crude["method"] == "model_based" and crude["rate_basis"] == "percent"
    age = defs["PLACES:CSMOKING:age_adjusted"]
    assert age["age_adjustment"] == "2000 US standard population"
    mammo = defs["PLACES:MAMMOUSE:age_adjusted"]
    assert mammo["universe"] == "women aged 50-74 years"

    strata = rows(cat, "measure.stratum")
    assert [s["stratum_id"] for s in strata] == ["PLACES:ALL"]
    assert strata[0]["scheme"] == "PLACES_TOTAL"

    obs = {(o["measure_id"], o["geo_id"]): o
           for o in rows(cat, "measure.observation", row_filter=EqualTo("source", "PLACES"))}
    denver = obs[("PLACES:CSMOKING:crude", "county:08031")]
    assert (denver["value"], denver["lower"], denver["upper"]) == (14.2, 12.6, 15.9)
    assert denver["interval_level"] == 0.95 and denver["denominator"] is None
    assert denver["value_status"] == "reported" and denver["geo_vintage"] == 2020
    assert denver["period_start"] == denver["period_end"] == "2022"
    assert denver["source_release"] == "2024" and denver["stratum_id"] == "PLACES:ALL"

    ct = obs[("PLACES:CSMOKING:crude", "county:09110")]  # planning region, not zero-padded further
    assert ct["geo_id"] == "county:09110"
    us = obs[("PLACES:MAMMOUSE:age_adjusted", "nation:US")]
    assert us["value"] == 76.0


def test_suppression_maps_status_and_leaves_value_null(cat, csv2025):
    places.ingest(cat, REL2, places_release="2025", url=csv2025)
    obs = rows(cat, "measure.observation", row_filter=EqualTo("source_release", "2025"))
    lpa = next(o for o in obs if o["measure_id"] == "PLACES:LPA:age_adjusted")
    assert lpa["value_status"] == "suppressed_small_count"
    assert lpa["value"] is None and lpa["lower"] is None and lpa["upper"] is None

    reported = [o for o in obs if o["value_status"] == "reported"]
    assert all(o["value"] is not None for o in reported)


def test_unmapped_footnote_raises(cat, csv2025, monkeypatch):
    monkeypatch.setattr(places, "FOOTNOTE_STATUS", {})
    places.land_raw(cat, REL2, places_release="2025", url=csv2025)
    with pytest.raises(SystemExit, match="unmapped suppression footnote"):
        places.transform(cat, REL2, "2025")


def test_two_releases_coexist_and_first_is_not_retired(cat, csv2024, csv2025):
    places.ingest(cat, REL1, places_release="2024", url=csv2024)
    places.ingest(cat, REL2, places_release="2025", url=csv2025)

    releases = {o["source_release"] for o in
               rows(cat, "measure.observation", row_filter=EqualTo("source", "PLACES"))}
    assert releases == {"2024", "2025"}

    denver_2024 = next(o for o in rows(cat, "measure.observation") if
                       o["measure_id"] == "PLACES:CSMOKING:crude" and o["source_release"] == "2024")
    denver_2025 = next(o for o in rows(cat, "measure.observation") if
                       o["measure_id"] == "PLACES:CSMOKING:crude" and o["source_release"] == "2025")
    assert denver_2024["value"] == 14.2 and denver_2024["valid_to"] is None  # untouched
    assert denver_2025["value"] == 10.3 and denver_2025["valid_to"] is None  # a distinct business key

    # measure.definition has no history (SPEC.md: replaced wholesale per
    # source) -- the 2025 ingest drops the 2024-only ISOLATION definition even
    # though the 2024 observation row survives.
    defs = {d["measure_id"] for d in rows(cat, "measure.definition")}
    assert "PLACES:ISOLATION:crude" not in defs and "PLACES:LONELINESS:crude" in defs
    still_there = [o for o in rows(cat, "measure.observation")
                  if o["measure_id"] == "PLACES:ISOLATION:crude"]
    assert len(still_there) == 1


def test_rerun_is_idempotent(cat, csv2024):
    places.ingest(cat, REL1, places_release="2024", url=csv2024)
    counts = places.ingest(cat, "2026.10", places_release="2024", url=csv2024)
    assert counts["measure.observation"]["written"] == 0
    assert counts["measure.observation"]["unchanged"] == 6


def test_every_column_is_documented(cat, csv2024):
    places.ingest(cat, REL1, places_release="2024", url=csv2024)
    for identifier in ("raw.places__county", "measure.definition", "measure.stratum",
                      "measure.observation"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
