"""Declared Iceberg schemas — the single source of table structure and meaning.

Tables are never created from an inferred Arrow schema. Two things required by
SPEC.md cannot be expressed that way: identifier fields, which are the merge
key, and per-column `doc`, which is what makes the catalog self-describing.

A column exists here only once something populates it. Columns whose source has
not landed yet are added by schema evolution when it does, rather than shipped
as permanent NULLs that read as "we have this" when we do not.
"""

import time
from dataclasses import dataclass, field

from pyiceberg.exceptions import RESTError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DoubleType, IntegerType, NestedField, StringType

VALID_FROM = (
    "The cancerOnIce release from which this version of the record is valid. "
    "A row is one *version*: any change to any attribute closes the previous "
    "row and opens a new one, so the value here is not necessarily when the "
    "record first existed. It is when cancerOnIce first carried this version, "
    "not when the source published it."
)
VALID_TO = (
    "The cancerOnIce release at which this version stopped being current, "
    "exclusive. NULL means this is the current version — it does not mean "
    "unknown. Queries wanting current data must filter on valid_to IS NULL; "
    "queries wanting release R want "
    "valid_from <= R AND (valid_to IS NULL OR valid_to > R). This is a "
    "cancerOnIce release, not the date the source changed the record."
)


@dataclass(frozen=True)
class TableDef:
    """A declared table.

    `business_key` is what identifies a *record* — what a merge joins on to
    decide whether a row is new, changed or retired. The Iceberg identifier
    fields are the *row* key, which is the business key plus `valid_from`,
    because every change opens a new version row. Declaring only the business
    key to Iceberg would assert a uniqueness this model does not have; deriving
    one from the other keeps them from drifting. A table with no `valid_from`
    column (a plain lookup, replaced wholesale via `merge.write`) is keyed by
    its business key alone.
    """

    schema: Schema
    comment: str
    business_key: tuple = ()
    # Identity-partition columns, for pruning only: merge-scope containment, not
    # partitioning, is the correctness mechanism.
    partition_by: tuple = ()
    properties: dict = field(default_factory=dict)

    def iceberg_schema(self):
        if not self.business_key:
            return self.schema
        names = list(self.business_key)
        if any(f.name == "valid_from" for f in self.schema.fields):
            names.append("valid_from")
        ids = [self.schema.find_field(n).field_id for n in names]
        return Schema(*self.schema.fields, identifier_field_ids=ids)


NAMESPACES = {
    "raw": "Sources as landed, verbatim, per source release.",
    "geography": "Units, vintages, crosswalks, aliases — the spine every fact joins through.",
    "population": "Denominators (SEER county/tract population, ACS universes).",
    "measure": "Every published number, in one stacked long table, plus its definitions.",
    "facility": "Places care happens (mammography, FQHC, providers, screening).",
    "catchment": "Cancer centers and the geographies they claim.",
    "resource": "Pointers to large or non-redistributable data (NaNDA, geometries).",
    "provenance": "What each cancerOnIce release was built from, and how we know.",
}

VALUE_STATUSES = (
    "reported", "suppressed_small_count", "suppressed_complementary",
    "suppressed_reliability", "not_available", "not_applicable",
)

TABLES = {
    "provenance.release": TableDef(
        schema=Schema(
            NestedField(1, "release", StringType(), required=True,
                        doc="cancerOnIce release, YYYY.MM with zero-padded corrections "
                            "YYYY.MM.NN. Zero-padded because '2026.10.10' sorts before "
                            "'2026.10.2' and release ordering would otherwise invert."),
            NestedField(2, "source", StringType(), required=True,
                        doc="Source key, e.g. scp, places, acs."),
            NestedField(3, "source_version", StringType(),
                        doc="The upstream version in the SOURCE'S OWN vocabulary, never "
                            "normalised. NULL only where nothing at all is knowable."),
            NestedField(4, "version_method", StringType(), required=True,
                        doc="How the version was determined: release_number, "
                            "http_last_modified, etag, ftp_index_probe, retrieval_date, "
                            "unavailable. 'unavailable' is a legitimate value — a source that "
                            "publishes no version is recorded as such, never given a "
                            "fabricated one."),
            NestedField(5, "retrieved_at", StringType(), required=True,
                        doc="UTC timestamp at which the source was fetched."),
            NestedField(6, "url", StringType(), doc="Canonical URL fetched."),
            NestedField(7, "checksum", StringType(), doc="SHA-256 of the retrieved bytes, where computed."),
            NestedField(8, "row_count", IntegerType(),
                        doc="Rows landed from this source, as a cheap integrity check."),
        ),
        business_key=("release", "source"),
        comment="One row per (cancerOnIce release, source): what this release was built from. "
                "This is what makes a release reproducible — resolve it here to each "
                "source's own version, then query each table at that release. Durable "
                "by design: unlike Iceberg snapshot summaries it does not expire.",
        properties={},
    ),

    "geography.unit": TableDef(
        schema=Schema(
            NestedField(1, "geo_id", StringType(), required=True,
                        doc="Canonical id: '<level>:<fips>', e.g. 'county:08031'. Part of the "
                            "business key together with vintage."),
            NestedField(2, "level", StringType(), required=True,
                        doc="One of: nation, state, county, tract, block_group, zcta, place, "
                            "custom."),
            NestedField(3, "fips", StringType(), doc="The bare code, without the level prefix."),
            NestedField(4, "vintage", IntegerType(), required=True,
                        doc="Boundary vintage year, e.g. 2020. Part of the business key: a FIPS "
                            "code without a vintage is an assembly name without a patch level — "
                            "boundaries move (Connecticut's 2022 move from counties to planning "
                            "regions, tract redraws each decennial census)."),
            NestedField(5, "name", StringType(), doc="Human-readable name."),
            NestedField(6, "parent_geo_id", StringType(),
                        doc="Containing unit's geo_id, in the same vintage."),
            NestedField(7, "aland_m2", DoubleType(), doc="Land area, square meters."),
            NestedField(8, "awater_m2", DoubleType(), doc="Water area, square meters."),
            NestedField(9, "centroid_lat", DoubleType(), doc="Centroid latitude, WGS84."),
            NestedField(10, "centroid_lon", DoubleType(), doc="Centroid longitude, WGS84."),
            NestedField(11, "geometry_uri", StringType(),
                        doc="Pointer to the boundary geometry (GeoParquet / PMTiles on R2), not "
                            "the geometry itself — geometry is referenced, never the primary "
                            "artifact (SPEC.md Non-goals). NULL where no geometry is pointed to "
                            "yet."),
            NestedField(12, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(13, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("geo_id", "vintage"),
        comment="Geography — the spine every fact joins through (SPEC.md § Geography). Every "
                "level and vintage Census has ever drawn, one row per unit per vintage, with "
                "full Type-2 history via valid_from/valid_to.",
    ),

    "measure.definition": TableDef(
        schema=Schema(
            NestedField(1, "measure_id", StringType(), required=True, doc="Business key."),
            NestedField(2, "source", StringType(), required=True,
                        doc="The asserting provider that defines this measure, e.g. 'SCP', "
                            "'PLACES', 'ACS'."),
            NestedField(3, "label", StringType(), doc="Human-readable measure name."),
            NestedField(4, "units", StringType(), doc="Unit of the published value."),
            NestedField(5, "universe", StringType(), doc="Population the rate is computed over."),
            NestedField(6, "rate_basis", StringType(),
                        doc="One of: per_100000, percent, count, index."),
            NestedField(7, "age_adjustment", StringType(),
                        doc="Standard population used for age adjustment (e.g. '2000 US "
                            "standard'), or NULL when the measure is not age-adjusted."),
            NestedField(8, "method", StringType(),
                        doc="One of: direct, model_based, survey_direct, derived."),
            NestedField(9, "cancer_site_code", StringType(),
                        doc="FK into measure.cancer_site when this measure is cancer-specific; "
                            "NULL otherwise."),
            NestedField(10, "doc", StringType(), doc="Prose description of the measure."),
        ),
        business_key=("measure_id",),
        comment="One row per published measure definition (SPEC.md § Measures). A lookup "
                "table, not versioned in place — replaced wholesale per source via merge.write.",
    ),

    "measure.stratum": TableDef(
        schema=Schema(
            NestedField(1, "stratum_id", StringType(), required=True, doc="Business key."),
            NestedField(2, "source", StringType(), required=True,
                        doc="The provider whose native categories this stratum uses."),
            NestedField(3, "sex", StringType(), doc="Source-native sex category, or NULL."),
            NestedField(4, "age_group", StringType(), doc="Source-native age group, or NULL."),
            NestedField(5, "race_ethnicity", StringType(),
                        doc="Source-native race/ethnicity category, or NULL. Never harmonized "
                            "in place — mappings live in measure.stratum_map instead "
                            "(SPEC.md § Measures)."),
            NestedField(6, "stage", StringType(), doc="Source-native disease stage, or NULL."),
            NestedField(7, "other", StringType(), doc="Any other source-native stratifier."),
            NestedField(8, "scheme", StringType(), required=True,
                        doc="The classification scheme this stratum's categories come from, "
                            "e.g. 'SCP_RACE_2024', 'OMB_1997', 'OMB_SPD15_2024'. Schemes diverge "
                            "across releases of the same source; this is expected."),
        ),
        business_key=("stratum_id",),
        comment="Source-native stratification, one row per distinct combination a source "
                "publishes (SPEC.md § Measures). A lookup table, replaced wholesale per source "
                "via merge.write.",
    ),

    "measure.observation": TableDef(
        schema=Schema(
            NestedField(1, "source", StringType(), required=True,
                        doc="Asserting provider: 'SCP' | 'PLACES' | 'ACS' | 'SVI' | ... . Part "
                            "of the business key and of every writer's merge scope, so "
                            "providers stack in one table and none can retire another's rows."),
            NestedField(2, "source_release", StringType(), required=True,
                        doc="The upstream edition that published this value — a vintage or "
                            "release label (SPEC.md § Versioning model), distinct from the "
                            "period the value describes and from valid_from/valid_to."),
            NestedField(3, "measure_id", StringType(), required=True,
                        doc="FK measure.definition."),
            NestedField(4, "geo_id", StringType(), required=True, doc="FK geography.unit."),
            NestedField(5, "geo_vintage", IntegerType(), required=True,
                        doc="FK geography.unit's vintage, together with geo_id."),
            NestedField(6, "period_start", StringType(), required=True,
                        doc="Start of the time period this estimate describes, e.g. '2018' for "
                            "a five-year pooled 2018-2022 rate. The time the estimate is ABOUT, "
                            "distinct from source_release (when it was published) and "
                            "valid_from (when cancerOnIce first served it)."),
            NestedField(7, "period_end", StringType(), required=True,
                        doc="End of the time period this estimate describes, e.g. '2022'."),
            NestedField(8, "stratum_id", StringType(), required=True,
                        doc="FK measure.stratum."),
            NestedField(9, "value", DoubleType(),
                        doc="The published value. NULL whenever value_status != 'reported' — "
                            "no suppressed cell ever reads as a number (SPEC.md Acceptance C)."),
            NestedField(10, "lower", DoubleType(), doc="Interval lower bound, when published."),
            NestedField(11, "upper", DoubleType(), doc="Interval upper bound, when published."),
            NestedField(12, "interval_level", DoubleType(),
                        doc="Confidence/margin level of lower/upper: 0.90 (ACS MOE), 0.95 "
                            "(SCP CI), or NULL when no interval is published."),
            NestedField(13, "numerator", DoubleType(), doc="Numerator, when published."),
            NestedField(14, "denominator", DoubleType(), doc="Denominator, when published."),
            NestedField(15, "value_status", StringType(), required=True,
                        doc="Closed enum, never NULL: 'reported', 'suppressed_small_count', "
                            "'suppressed_complementary', 'suppressed_reliability', "
                            "'not_available', 'not_applicable' (SPEC.md § Suppression is a "
                            "value, not a NULL). Enforced before write by "
                            "merge.check_observations."),
            NestedField(16, "reliability_flag", StringType(),
                        doc="Published separately from suppression, e.g. an unstable RSE flag."),
            NestedField(17, "trend", StringType(), doc="Source-published trend call, if any."),
            NestedField(18, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(19, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("source", "source_release", "measure_id", "geo_id", "geo_vintage",
                      "period_start", "period_end", "stratum_id"),
        partition_by=("source",),
        comment="Every published number, one stacked long table across sources (SPEC.md § "
                "Measures). Merge scope is (source, source_release) — a new SCP vintage never "
                "retires a PLACES row. Full Type-2 history via valid_from/valid_to.",
    ),

    # --- raw: census gazetteer ---
    # Census Gazetteer Files (one row per geographic unit: GEOID, name, land/water
    # area, internal-point lat/lon) are the lightweight companion to TIGER/Line —
    # they populate geography.unit's non-geometry columns without pulling in
    # shapefiles. Public domain. One raw table per (level, vintage) file.
    "raw.census__gazetteer_counties": TableDef(
        schema=Schema(
            NestedField(1, "usps", StringType(), doc="Two-letter USPS state/territory abbreviation."),
            NestedField(2, "geoid", StringType(), required=True,
                        doc="5-digit state+county FIPS code, e.g. '08031'. Unique per row within "
                            "one gazetteer_year."),
            NestedField(3, "geoidfq", StringType(),
                        doc="Fully qualified GEOID used to join data.census.gov tables, e.g. "
                            "'0500000US08031'. Only present from the 2025 gazetteer layout; NULL "
                            "in earlier vintages."),
            NestedField(4, "ansicode", StringType(), doc="ANSI feature code for the unit."),
            NestedField(5, "name", StringType(), doc="County (or state-equivalent) name."),
            NestedField(6, "pop10", StringType(),
                        doc="2010 Census population count. Only the 2010 gazetteer layout carries "
                            "this column; NULL in every other vintage."),
            NestedField(7, "hu10", StringType(),
                        doc="2010 Census housing unit count. Only the 2010 gazetteer layout "
                            "carries this column; NULL in every other vintage."),
            NestedField(8, "aland", StringType(), doc="Land area, square meters, as published."),
            NestedField(9, "awater", StringType(), doc="Water area, square meters, as published."),
            NestedField(10, "aland_sqmi", StringType(), doc="Land area, square miles, as published."),
            NestedField(11, "awater_sqmi", StringType(), doc="Water area, square miles, as published."),
            NestedField(12, "intptlat", StringType(), doc="Internal point latitude, as published."),
            NestedField(13, "intptlong", StringType(), doc="Internal point longitude, as published."),
            NestedField(14, "gazetteer_year", IntegerType(), required=True,
                        doc="The gazetteer vintage year this row was published under, e.g. 2024. "
                            "Version column: raw is replaced wholesale per value of this column, "
                            "so every landed vintage accumulates rather than overwrites the last."),
            NestedField(15, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="Census Gazetteer county file landed verbatim and whole, every column, one row "
                "per county-equivalent per vintage year. Three real upstream layouts (2010 with "
                "POP10/HU10, 2011-2024 without them, 2025+ with GEOIDFQ and a pipe delimiter) "
                "land into this one union schema; absent columns are NULL. Licence: U.S. "
                "government work, public domain (17 U.S.C. § 105).",
    ),

    "raw.census__gazetteer_tracts": TableDef(
        schema=Schema(
            NestedField(1, "usps", StringType(), doc="Two-letter USPS state/territory abbreviation."),
            NestedField(2, "geoid", StringType(), required=True,
                        doc="11-digit state+county+tract FIPS code, e.g. '08031000100'. Unique "
                            "per row within one gazetteer_year."),
            NestedField(3, "geoidfq", StringType(),
                        doc="Fully qualified GEOID used to join data.census.gov tables, e.g. "
                            "'1400000US08031000100'. Only present from the 2025 gazetteer layout; "
                            "NULL in earlier vintages."),
            NestedField(4, "pop10", StringType(),
                        doc="2010 Census population count. Only the 2010 gazetteer layout carries "
                            "this column; NULL in every other vintage."),
            NestedField(5, "hu10", StringType(),
                        doc="2010 Census housing unit count. Only the 2010 gazetteer layout "
                            "carries this column; NULL in every other vintage."),
            NestedField(6, "aland", StringType(), doc="Land area, square meters, as published."),
            NestedField(7, "awater", StringType(), doc="Water area, square meters, as published."),
            NestedField(8, "aland_sqmi", StringType(), doc="Land area, square miles, as published."),
            NestedField(9, "awater_sqmi", StringType(), doc="Water area, square miles, as published."),
            NestedField(10, "intptlat", StringType(), doc="Internal point latitude, as published."),
            NestedField(11, "intptlong", StringType(), doc="Internal point longitude, as published."),
            NestedField(12, "gazetteer_year", IntegerType(), required=True,
                        doc="The gazetteer vintage year this row was published under, e.g. 2024. "
                            "Version column: raw is replaced wholesale per value of this column, "
                            "so every landed vintage accumulates rather than overwrites the last."),
            NestedField(13, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="Census Gazetteer tract file landed verbatim and whole, every column, one row "
                "per census tract per vintage year. The tract file carries no NAME column — "
                "tracts are numbered, not named. Three real upstream layouts, same union-schema "
                "treatment as raw.census__gazetteer_counties. Licence: U.S. government work, "
                "public domain (17 U.S.C. § 105).",
    ),

    "raw.census__state_fips": TableDef(
        schema=Schema(
            NestedField(1, "state", StringType(), required=True,
                        doc="2-digit state/territory FIPS code, e.g. '09'."),
            NestedField(2, "stusab", StringType(), doc="Two-letter USPS abbreviation, e.g. 'CT'."),
            NestedField(3, "state_name", StringType(), doc="Full state/territory name, e.g. 'Connecticut'."),
            NestedField(4, "statens", StringType(), doc="GNIS ANSI feature code for the state."),
            NestedField(5, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="Census's state/state-equivalent FIPS code reference "
                "(https://www2.census.gov/geo/docs/reference/state.txt), landed whole and replaced "
                "wholesale each time — not versioned by gazetteer year, since FIPS-to-state "
                "assignment doesn't move on that cadence. Feeds geography.unit's state-level "
                "names. Licence: U.S. government work, public domain (17 U.S.C. § 105).",
    ),

    # --- raw: cdc places ---
    # CDC PLACES: model-based tract/county small-area estimates for chronic
    # disease, screening and behaviors, published as one annual release
    # (SPEC.md § Sources — first tranche). Public domain; measure.definition rows
    # for PLACES set method = 'model_based'.
    "raw.places__county": TableDef(
        schema=Schema(
            NestedField(1, "Year", StringType(), required=True,
                        doc="BRFSS survey year this row's estimate is based on, e.g. '2022'."),
            NestedField(2, "StateAbbr", StringType(),
                        doc="Two-letter state postal abbreviation, or 'US' for the national "
                            "aggregate row."),
            NestedField(3, "StateDesc", StringType(),
                        doc="State name, or 'United States' for the national row."),
            NestedField(4, "LocationName", StringType(),
                        doc="County name; empty for the national row."),
            NestedField(5, "DataSource", StringType(),
                        doc="Survey the estimate is modeled from, e.g. 'BRFSS'."),
            NestedField(6, "Category", StringType(),
                        doc="Measure category, e.g. 'Health Outcomes', 'Prevention'."),
            NestedField(7, "Measure", StringType(),
                        doc="Full measure description including its universe, e.g. 'Current "
                            "cigarette smoking among adults'."),
            NestedField(8, "Data_Value_Unit", StringType(), doc="Unit of Data_Value, e.g. '%'."),
            NestedField(9, "Data_Value_Type", StringType(),
                        doc="'Crude prevalence' or 'Age-adjusted prevalence'."),
            NestedField(10, "Data_Value", StringType(),
                        doc="The published estimate, unparsed; empty (NULL) when suppressed — "
                            "see Data_Value_Footnote."),
            NestedField(11, "Data_Value_Footnote_Symbol", StringType(),
                        doc="Footnote marker on Data_Value, or NULL."),
            NestedField(12, "Data_Value_Footnote", StringType(),
                        doc="Footnote text explaining a missing Data_Value, or NULL."),
            NestedField(13, "Low_Confidence_Limit", StringType(),
                        doc="95% CI lower bound, unparsed."),
            NestedField(14, "High_Confidence_Limit", StringType(),
                        doc="95% CI upper bound, unparsed."),
            NestedField(15, "TotalPopulation", StringType(),
                        doc="Total population of the location, per the source's own population "
                            "estimate. NOT this measure's denominator — PLACES publishes no "
                            "numerator/denominator pair; kept for reference only."),
            NestedField(16, "TotalPop18plus", StringType(),
                        doc="Adult (18+) population of the location, per the source's own "
                            "population estimate. Not used as a denominator, for the same reason "
                            "as TotalPopulation."),
            NestedField(17, "LocationID", StringType(), required=True,
                        doc="County FIPS code (5 digits, leading zero kept), or '59' for the "
                            "national aggregate row (paired with StateAbbr='US')."),
            NestedField(18, "CategoryID", StringType(), doc="Short code for Category."),
            NestedField(19, "MeasureId", StringType(), required=True,
                        doc="Short code for Measure, e.g. 'CSMOKING'."),
            NestedField(20, "DataValueTypeID", StringType(),
                        doc="Short code for Data_Value_Type, e.g. 'CrdPrv', 'AgeAdjPrv'."),
            NestedField(21, "Short_Question_Text", StringType(),
                        doc="Short label for Measure, e.g. 'Current Smoking'."),
            NestedField(22, "Geolocation", StringType(),
                        doc="County centroid as a WKT POINT string, or NULL for the national "
                            "row."),
            NestedField(23, "places_release", StringType(), required=True,
                        doc="The PLACES county-data release year this row came from, e.g. "
                            "'2025' (a key of places.RELEASES). Raw is replaced wholesale per "
                            "value of this column."),
        ),
        comment="CDC PLACES county-data release, landed verbatim and whole: one row per "
                "(county, measure, stratification type) model-based small-area estimate "
                "(SPEC.md § Sources — first tranche). Public domain.",
    ),

    # --- raw: usda ers rucc ---
    # USDA ERS Rural-Urban Continuum Codes: county-level rurality classification,
    # published per edition (SPEC.md § Sources — first tranche). Public domain;
    # feeds facility/access-gap recipes as a rurality covariate.
    #
    # A parallel agent fills this in; leave this marker and the surrounding
    # blank space untouched so independent branches merge cleanly.
    "raw.ers__rucc": TableDef(
        schema=Schema(
            NestedField(1, "FIPS", StringType(), required=True,
                        doc="5-digit county (or county-equivalent) FIPS code, as published — "
                            "zero-padded string, e.g. '01001'."),
            NestedField(2, "State", StringType(), required=True, doc="USPS state abbreviation."),
            NestedField(3, "County_Name", StringType(), required=True,
                        doc="County (or equivalent) name, as published."),
            NestedField(4, "Attribute", StringType(), required=True,
                        doc="Which fact this row carries: 'Population_2020', "
                            "'RUCC_<edition>' (e.g. 'RUCC_2023'), or 'Description'. Long "
                            "format — one row per (FIPS, Attribute)."),
            NestedField(5, "Value", StringType(),
                        doc="The value for Attribute, unparsed. NULL only where the source "
                            "cell itself is blank; a FIPS entirely missing an Attribute row "
                            "(e.g. no RUCC_2023 for a zero-population entity) is absent from "
                            "this table rather than present with a NULL Value."),
            NestedField(6, "rucc_edition", StringType(), required=True,
                        doc="The RUCC edition this row was published in, e.g. '2023' — the "
                            "version axis for this source. Raw is replaced wholesale per "
                            "value of this column."),
            NestedField(7, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="USDA ERS Rural-Urban Continuum Codes landed verbatim and whole, long format: "
                "one row per (county, attribute) exactly as published. Public domain (U.S. "
                "Government work, 17 U.S.C. Sec 105).",
    ),

    # --- derived: geography alias ---
    # geography.alias — FIPS renames and recodes that are not boundary changes (#26).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- derived: measure cancer site ---
    # measure.cancer_site — SEER site recode <-> ICD-O-3 <-> ICD-10 <-> NCIt / MONDO (#31).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: cdc atsdr svi ---
    # CDC/ATSDR Social Vulnerability Index, every published edition (#40).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.
    "raw.svi__county": TableDef(
        schema=Schema(
            NestedField(1, "ST", StringType(), doc="2-digit state FIPS code."),
            NestedField(2, "STATE", StringType(), doc="State name, as published (case varies by edition: uppercase through 2018, title case from 2020)."),
            NestedField(3, "ST_ABBR", StringType(), doc="2-letter USPS state abbreviation."),
            NestedField(4, "STCNTY", StringType(), doc="5-digit state+county FIPS code. Equal to FIPS at county level; at tract level, the containing county's code."),
            NestedField(5, "COUNTY", StringType(), doc="County (or county-equivalent) name, as published."),
            NestedField(6, "FIPS", StringType(), required=True,
                        doc="Geographic identifier: 5-digit county FIPS in raw.svi__county, "
                            "11-digit tract FIPS in raw.svi__tract."),
            NestedField(7, "LOCATION", StringType(), doc="Human-readable location name, e.g. 'Autauga County, Alabama' (county) or 'Census Tract 4001.01; Capitol Planning Region; Connecticut' (tract)."),
            NestedField(8, "AREA_SQMI", StringType(), doc="Land area of the unit, square miles, as published."),
            NestedField(9, "E_TOTPOP", StringType(), doc="ACS/Census estimate (count): total population."),
            NestedField(10, "M_TOTPOP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total population."),
            NestedField(11, "E_HU", StringType(), doc="ACS/Census estimate (count): total housing units."),
            NestedField(12, "M_HU", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total housing units."),
            NestedField(13, "E_HH", StringType(), doc="ACS/Census estimate (count): total households."),
            NestedField(14, "M_HH", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total households."),
            NestedField(15, "E_POV150", StringType(), doc="ACS/Census estimate (count): persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(16, "M_POV150", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(17, "E_UNEMP", StringType(), doc="ACS/Census estimate (count): civilian (age 16+) population unemployed."),
            NestedField(18, "M_UNEMP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian (age 16+) population unemployed."),
            NestedField(19, "E_HBURD", StringType(), doc="ACS/Census estimate (count): occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(20, "M_HBURD", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(21, "E_NOHSDP", StringType(), doc="ACS/Census estimate (count): persons age 25+ with no high school diploma."),
            NestedField(22, "M_NOHSDP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons age 25+ with no high school diploma."),
            NestedField(23, "E_UNINSUR", StringType(), doc="ACS/Census estimate (count): civilian noninstitutionalized population with no health insurance."),
            NestedField(24, "M_UNINSUR", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian noninstitutionalized population with no health insurance."),
            NestedField(25, "E_AGE65", StringType(), doc="ACS/Census estimate (count): persons aged 65 and older."),
            NestedField(26, "M_AGE65", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons aged 65 and older."),
            NestedField(27, "E_AGE17", StringType(), doc="ACS/Census estimate (count): persons aged 17 and under."),
            NestedField(28, "M_AGE17", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons aged 17 and under."),
            NestedField(29, "E_DISABL", StringType(), doc="ACS/Census estimate (count): civilian noninstitutionalized population with a disability."),
            NestedField(30, "M_DISABL", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian noninstitutionalized population with a disability."),
            NestedField(31, "E_SNGPNT", StringType(), doc="ACS/Census estimate (count): single-parent households."),
            NestedField(32, "M_SNGPNT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: single-parent households."),
            NestedField(33, "E_LIMENG", StringType(), doc="ACS/Census estimate (count): persons age 5+ who speak English \"less than well\"."),
            NestedField(34, "M_LIMENG", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons age 5+ who speak English \"less than well\"."),
            NestedField(35, "E_MINRTY", StringType(), doc="ACS/Census estimate (count): minority population (all persons except white, non-Hispanic)."),
            NestedField(36, "M_MINRTY", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: minority population (all persons except white, non-Hispanic)."),
            NestedField(37, "E_MUNIT", StringType(), doc="ACS/Census estimate (count): housing units in structures with 10 or more units."),
            NestedField(38, "M_MUNIT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: housing units in structures with 10 or more units."),
            NestedField(39, "E_MOBILE", StringType(), doc="ACS/Census estimate (count): mobile homes."),
            NestedField(40, "M_MOBILE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: mobile homes."),
            NestedField(41, "E_CROWD", StringType(), doc="ACS/Census estimate (count): occupied housing units with more people than rooms."),
            NestedField(42, "M_CROWD", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: occupied housing units with more people than rooms."),
            NestedField(43, "E_NOVEH", StringType(), doc="ACS/Census estimate (count): households with no vehicle available."),
            NestedField(44, "M_NOVEH", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: households with no vehicle available."),
            NestedField(45, "E_GROUPQ", StringType(), doc="ACS/Census estimate (count): persons in group quarters."),
            NestedField(46, "M_GROUPQ", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons in group quarters."),
            NestedField(47, "EP_POV150", StringType(), doc="ACS/Census percent estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018), as a percentage of the relevant universe."),
            NestedField(48, "MP_POV150", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(49, "EP_UNEMP", StringType(), doc="ACS/Census percent estimate: civilian (age 16+) population unemployed, as a percentage of the relevant universe."),
            NestedField(50, "MP_UNEMP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian (age 16+) population unemployed."),
            NestedField(51, "EP_HBURD", StringType(), doc="ACS/Census percent estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(52, "MP_HBURD", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(53, "EP_NOHSDP", StringType(), doc="ACS/Census percent estimate: persons age 25+ with no high school diploma, as a percentage of the relevant universe."),
            NestedField(54, "MP_NOHSDP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons age 25+ with no high school diploma."),
            NestedField(55, "EP_UNINSUR", StringType(), doc="ACS/Census percent estimate: civilian noninstitutionalized population with no health insurance, as a percentage of the relevant universe."),
            NestedField(56, "MP_UNINSUR", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian noninstitutionalized population with no health insurance."),
            NestedField(57, "EP_AGE65", StringType(), doc="ACS/Census percent estimate: persons aged 65 and older, as a percentage of the relevant universe."),
            NestedField(58, "MP_AGE65", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons aged 65 and older."),
            NestedField(59, "EP_AGE17", StringType(), doc="ACS/Census percent estimate: persons aged 17 and under, as a percentage of the relevant universe."),
            NestedField(60, "MP_AGE17", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons aged 17 and under."),
            NestedField(61, "EP_DISABL", StringType(), doc="ACS/Census percent estimate: civilian noninstitutionalized population with a disability, as a percentage of the relevant universe."),
            NestedField(62, "MP_DISABL", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian noninstitutionalized population with a disability."),
            NestedField(63, "EP_SNGPNT", StringType(), doc="ACS/Census percent estimate: single-parent households, as a percentage of the relevant universe."),
            NestedField(64, "MP_SNGPNT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: single-parent households."),
            NestedField(65, "EP_LIMENG", StringType(), doc="ACS/Census percent estimate: persons age 5+ who speak English \"less than well\", as a percentage of the relevant universe."),
            NestedField(66, "MP_LIMENG", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons age 5+ who speak English \"less than well\"."),
            NestedField(67, "EP_MINRTY", StringType(), doc="ACS/Census percent estimate: minority population (all persons except white, non-Hispanic), as a percentage of the relevant universe."),
            NestedField(68, "MP_MINRTY", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: minority population (all persons except white, non-Hispanic)."),
            NestedField(69, "EP_MUNIT", StringType(), doc="ACS/Census percent estimate: housing units in structures with 10 or more units, as a percentage of the relevant universe."),
            NestedField(70, "MP_MUNIT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: housing units in structures with 10 or more units."),
            NestedField(71, "EP_MOBILE", StringType(), doc="ACS/Census percent estimate: mobile homes, as a percentage of the relevant universe."),
            NestedField(72, "MP_MOBILE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: mobile homes."),
            NestedField(73, "EP_CROWD", StringType(), doc="ACS/Census percent estimate: occupied housing units with more people than rooms, as a percentage of the relevant universe."),
            NestedField(74, "MP_CROWD", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: occupied housing units with more people than rooms."),
            NestedField(75, "EP_NOVEH", StringType(), doc="ACS/Census percent estimate: households with no vehicle available, as a percentage of the relevant universe."),
            NestedField(76, "MP_NOVEH", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: households with no vehicle available."),
            NestedField(77, "EP_GROUPQ", StringType(), doc="ACS/Census percent estimate: persons in group quarters, as a percentage of the relevant universe."),
            NestedField(78, "MP_GROUPQ", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons in group quarters."),
            NestedField(79, "EPL_POV150", StringType(), doc="Percentile rank (0-1) of persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(80, "EPL_UNEMP", StringType(), doc="Percentile rank (0-1) of civilian (age 16+) population unemployed, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(81, "EPL_HBURD", StringType(), doc="Percentile rank (0-1) of occupied housing units spending 30% or more of household income on housing costs (2020 edition onward), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(82, "EPL_NOHSDP", StringType(), doc="Percentile rank (0-1) of persons age 25+ with no high school diploma, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(83, "EPL_UNINSUR", StringType(), doc="Percentile rank (0-1) of civilian noninstitutionalized population with no health insurance, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(84, "SPL_THEME1", StringType(), doc="Sum of theme 1's EPL_ percentile-rank components. Theme 1 is Socioeconomic Status (poverty, unemployment, income, education) in the 2014/2016/2018 editions, or Socioeconomic Status (poverty, unemployment, housing cost burden, education, uninsured) in 2020/2022."),
            NestedField(85, "RPL_THEME1", StringType(), doc="Percentile ranking (0-1) for theme 1, among all units landed in this edition. Theme 1 is Socioeconomic Status (poverty, unemployment, income, education) in the 2014/2016/2018 editions, or Socioeconomic Status (poverty, unemployment, housing cost burden, education, uninsured) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME1:<family>'."),
            NestedField(86, "EPL_AGE65", StringType(), doc="Percentile rank (0-1) of persons aged 65 and older, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(87, "EPL_AGE17", StringType(), doc="Percentile rank (0-1) of persons aged 17 and under, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(88, "EPL_DISABL", StringType(), doc="Percentile rank (0-1) of civilian noninstitutionalized population with a disability, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(89, "EPL_SNGPNT", StringType(), doc="Percentile rank (0-1) of single-parent households, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(90, "EPL_LIMENG", StringType(), doc="Percentile rank (0-1) of persons age 5+ who speak English \"less than well\", among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(91, "SPL_THEME2", StringType(), doc="Sum of theme 2's EPL_ percentile-rank components. Theme 2 is Household Composition & Disability (age 65+, age 17-, disability, single-parent households) in the 2014/2016/2018 editions, or Household Characteristics (age 65+, age 17-, disability, single-parent households, limited English) in 2020/2022."),
            NestedField(92, "RPL_THEME2", StringType(), doc="Percentile ranking (0-1) for theme 2, among all units landed in this edition. Theme 2 is Household Composition & Disability (age 65+, age 17-, disability, single-parent households) in the 2014/2016/2018 editions, or Household Characteristics (age 65+, age 17-, disability, single-parent households, limited English) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME2:<family>'."),
            NestedField(93, "EPL_MINRTY", StringType(), doc="Percentile rank (0-1) of minority population (all persons except white, non-Hispanic), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(94, "SPL_THEME3", StringType(), doc="Sum of theme 3's EPL_ percentile-rank components. Theme 3 is Minority Status & Language (minority population, limited English) in the 2014/2016/2018 editions, or Racial & Ethnic Minority Status (minority population) in 2020/2022."),
            NestedField(95, "RPL_THEME3", StringType(), doc="Percentile ranking (0-1) for theme 3, among all units landed in this edition. Theme 3 is Minority Status & Language (minority population, limited English) in the 2014/2016/2018 editions, or Racial & Ethnic Minority Status (minority population) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME3:<family>'."),
            NestedField(96, "EPL_MUNIT", StringType(), doc="Percentile rank (0-1) of housing units in structures with 10 or more units, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(97, "EPL_MOBILE", StringType(), doc="Percentile rank (0-1) of mobile homes, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(98, "EPL_CROWD", StringType(), doc="Percentile rank (0-1) of occupied housing units with more people than rooms, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(99, "EPL_NOVEH", StringType(), doc="Percentile rank (0-1) of households with no vehicle available, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(100, "EPL_GROUPQ", StringType(), doc="Percentile rank (0-1) of persons in group quarters, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(101, "SPL_THEME4", StringType(), doc="Sum of theme 4's EPL_ percentile-rank components. Theme 4 is Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in the 2014/2016/2018 editions, or Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in 2020/2022."),
            NestedField(102, "RPL_THEME4", StringType(), doc="Percentile ranking (0-1) for theme 4, among all units landed in this edition. Theme 4 is Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in the 2014/2016/2018 editions, or Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME4:<family>'."),
            NestedField(103, "SPL_THEMES", StringType(), doc="Sum of SPL_THEME1-4 (theme sub-scores)."),
            NestedField(104, "RPL_THEMES", StringType(), doc="Overall SVI percentile ranking (0-1) across all four themes, among all units landed in this edition; derived into measure.observation as 'SVI:RPL_THEMES:<family>' (family '2014' for the 2014/2016/2018 editions, '2020' for 2020/2022 -- theme composition differs between them; see measure.definition.doc)."),
            NestedField(105, "F_POV150", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018))"),
            NestedField(106, "F_UNEMP", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian (age 16+) population unemployed)"),
            NestedField(107, "F_HBURD", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (occupied housing units spending 30% or more of household income on housing costs (2020 edition onward))"),
            NestedField(108, "F_NOHSDP", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons age 25+ with no high school diploma)"),
            NestedField(109, "F_UNINSUR", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian noninstitutionalized population with no health insurance)"),
            NestedField(110, "F_THEME1", StringType(), doc="Flag: 1 if theme 1's RPL_THEME1 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(111, "F_AGE65", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons aged 65 and older)"),
            NestedField(112, "F_AGE17", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons aged 17 and under)"),
            NestedField(113, "F_DISABL", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian noninstitutionalized population with a disability)"),
            NestedField(114, "F_SNGPNT", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (single-parent households)"),
            NestedField(115, "F_LIMENG", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons age 5+ who speak English \"less than well\")"),
            NestedField(116, "F_THEME2", StringType(), doc="Flag: 1 if theme 2's RPL_THEME2 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(117, "F_MINRTY", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (minority population (all persons except white, non-Hispanic))"),
            NestedField(118, "F_THEME3", StringType(), doc="Flag: 1 if theme 3's RPL_THEME3 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(119, "F_MUNIT", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (housing units in structures with 10 or more units)"),
            NestedField(120, "F_MOBILE", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (mobile homes)"),
            NestedField(121, "F_CROWD", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (occupied housing units with more people than rooms)"),
            NestedField(122, "F_NOVEH", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (households with no vehicle available)"),
            NestedField(123, "F_GROUPQ", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons in group quarters)"),
            NestedField(124, "F_THEME4", StringType(), doc="Flag: 1 if theme 4's RPL_THEME4 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(125, "F_TOTAL", StringType(), doc="Flag: 1 if RPL_THEMES is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(126, "E_DAYPOP", StringType(), doc="ACS/Census estimate (count): daytime population estimate."),
            NestedField(127, "E_NOINT", StringType(), doc="ACS/Census estimate (count): households with no internet access (2020 edition onward)."),
            NestedField(128, "M_NOINT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: households with no internet access (2020 edition onward)."),
            NestedField(129, "E_AFAM", StringType(), doc="ACS/Census estimate (count): Black or African American alone population (2020 edition onward)."),
            NestedField(130, "M_AFAM", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Black or African American alone population (2020 edition onward)."),
            NestedField(131, "E_HISP", StringType(), doc="ACS/Census estimate (count): Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(132, "M_HISP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(133, "E_ASIAN", StringType(), doc="ACS/Census estimate (count): Asian alone population (2020 edition onward)."),
            NestedField(134, "M_ASIAN", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Asian alone population (2020 edition onward)."),
            NestedField(135, "E_AIAN", StringType(), doc="ACS/Census estimate (count): American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(136, "M_AIAN", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(137, "E_NHPI", StringType(), doc="ACS/Census estimate (count): Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(138, "M_NHPI", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(139, "E_TWOMORE", StringType(), doc="ACS/Census estimate (count): population of two or more races (2020 edition onward)."),
            NestedField(140, "M_TWOMORE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: population of two or more races (2020 edition onward)."),
            NestedField(141, "E_OTHERRACE", StringType(), doc="ACS/Census estimate (count): population of some other race alone (2020 edition onward)."),
            NestedField(142, "M_OTHERRACE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: population of some other race alone (2020 edition onward)."),
            NestedField(143, "EP_NOINT", StringType(), doc="ACS/Census percent estimate: households with no internet access (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(144, "MP_NOINT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: households with no internet access (2020 edition onward)."),
            NestedField(145, "EP_AFAM", StringType(), doc="ACS/Census percent estimate: Black or African American alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(146, "MP_AFAM", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Black or African American alone population (2020 edition onward)."),
            NestedField(147, "EP_HISP", StringType(), doc="ACS/Census percent estimate: Hispanic or Latino population, any race (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(148, "MP_HISP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(149, "EP_ASIAN", StringType(), doc="ACS/Census percent estimate: Asian alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(150, "MP_ASIAN", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Asian alone population (2020 edition onward)."),
            NestedField(151, "EP_AIAN", StringType(), doc="ACS/Census percent estimate: American Indian and Alaska Native alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(152, "MP_AIAN", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(153, "EP_NHPI", StringType(), doc="ACS/Census percent estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(154, "MP_NHPI", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(155, "EP_TWOMORE", StringType(), doc="ACS/Census percent estimate: population of two or more races (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(156, "MP_TWOMORE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: population of two or more races (2020 edition onward)."),
            NestedField(157, "EP_OTHERRACE", StringType(), doc="ACS/Census percent estimate: population of some other race alone (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(158, "MP_OTHERRACE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: population of some other race alone (2020 edition onward)."),
            NestedField(159, "AFFGEOID", StringType(), doc="Census fully qualified GEOID for the unit, e.g. '0500000US01001'. Present in the 2014 layout only."),
            NestedField(160, "E_POV", StringType(), doc="ACS/Census estimate (count): persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020)."),
            NestedField(161, "M_POV", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020)."),
            NestedField(162, "E_PCI", StringType(), doc="ACS/Census estimate (count): per capita income, dollars (2014-2018 editions only; dropped from the SVI methodology starting with the 2020 edition)."),
            NestedField(163, "M_PCI", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: per capita income, dollars (2014-2018 editions only; dropped from the SVI methodology starting with the 2020 edition)."),
            NestedField(164, "EP_POV", StringType(), doc="ACS/Census percent estimate: persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020), as a percentage of the relevant universe."),
            NestedField(165, "MP_POV", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020)."),
            NestedField(166, "EP_PCI", StringType(), doc="Per capita income, dollars, duplicated verbatim from E_PCI -- CDC's own naming quirk: PCI has no separate percent form, but the source still prefixes it EP_ (confirmed identical to E_PCI in the real file)."),
            NestedField(167, "MP_PCI", StringType(), doc="Margin of error (90% confidence) for E_PCI/EP_PCI (dollars, not a percent)."),
            NestedField(168, "EPL_POV", StringType(), doc="Percentile rank (0-1) of persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(169, "EPL_PCI", StringType(), doc="Percentile rank (0-1) of per capita income, dollars (2014-2018 editions only; dropped from the SVI methodology starting with the 2020 edition), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(170, "F_POV", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons below the poverty threshold (100% of the federal poverty level; 2014-2018 editions only, replaced by POV150 from 2020))"),
            NestedField(171, "F_PCI", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (per capita income, dollars (2014-2018 editions only; dropped from the SVI methodology starting with the 2020 edition))"),
            NestedField(172, "svi_edition", StringType(), required=True,
                        doc="The SVI edition this row was published in, e.g. '2022' -- the "
                            "version axis for this source (module docstring). Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(173, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="CDC/ATSDR Social Vulnerability Index county file landed verbatim and "
                "whole, one row per county per edition (SPEC.md § Sources -- first "
                "tranche). Union of the two real layouts this module lands (2014-family, "
                "2020-family; module docstring) -- a column absent from an edition's own "
                "layout is NULL for that edition. Public domain (17 U.S.C. § 105; "
                "https://www.cdc.gov/other/agencymaterials.html).",
    ),

    "raw.svi__tract": TableDef(
        schema=Schema(
            NestedField(1, "ST", StringType(), doc="2-digit state FIPS code."),
            NestedField(2, "STATE", StringType(), doc="State name, as published (case varies by edition: uppercase through 2018, title case from 2020)."),
            NestedField(3, "ST_ABBR", StringType(), doc="2-letter USPS state abbreviation."),
            NestedField(4, "STCNTY", StringType(), doc="5-digit state+county FIPS code. Equal to FIPS at county level; at tract level, the containing county's code."),
            NestedField(5, "COUNTY", StringType(), doc="County (or county-equivalent) name, as published."),
            NestedField(6, "FIPS", StringType(), required=True,
                        doc="Geographic identifier: 5-digit county FIPS in raw.svi__county, "
                            "11-digit tract FIPS in raw.svi__tract."),
            NestedField(7, "LOCATION", StringType(), doc="Human-readable location name, e.g. 'Autauga County, Alabama' (county) or 'Census Tract 4001.01; Capitol Planning Region; Connecticut' (tract)."),
            NestedField(8, "AREA_SQMI", StringType(), doc="Land area of the unit, square miles, as published."),
            NestedField(9, "E_TOTPOP", StringType(), doc="ACS/Census estimate (count): total population."),
            NestedField(10, "M_TOTPOP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total population."),
            NestedField(11, "E_HU", StringType(), doc="ACS/Census estimate (count): total housing units."),
            NestedField(12, "M_HU", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total housing units."),
            NestedField(13, "E_HH", StringType(), doc="ACS/Census estimate (count): total households."),
            NestedField(14, "M_HH", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: total households."),
            NestedField(15, "E_POV150", StringType(), doc="ACS/Census estimate (count): persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(16, "M_POV150", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(17, "E_UNEMP", StringType(), doc="ACS/Census estimate (count): civilian (age 16+) population unemployed."),
            NestedField(18, "M_UNEMP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian (age 16+) population unemployed."),
            NestedField(19, "E_HBURD", StringType(), doc="ACS/Census estimate (count): occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(20, "M_HBURD", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(21, "E_NOHSDP", StringType(), doc="ACS/Census estimate (count): persons age 25+ with no high school diploma."),
            NestedField(22, "M_NOHSDP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons age 25+ with no high school diploma."),
            NestedField(23, "E_UNINSUR", StringType(), doc="ACS/Census estimate (count): civilian noninstitutionalized population with no health insurance."),
            NestedField(24, "M_UNINSUR", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian noninstitutionalized population with no health insurance."),
            NestedField(25, "E_AGE65", StringType(), doc="ACS/Census estimate (count): persons aged 65 and older."),
            NestedField(26, "M_AGE65", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons aged 65 and older."),
            NestedField(27, "E_AGE17", StringType(), doc="ACS/Census estimate (count): persons aged 17 and under."),
            NestedField(28, "M_AGE17", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons aged 17 and under."),
            NestedField(29, "E_DISABL", StringType(), doc="ACS/Census estimate (count): civilian noninstitutionalized population with a disability."),
            NestedField(30, "M_DISABL", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: civilian noninstitutionalized population with a disability."),
            NestedField(31, "E_SNGPNT", StringType(), doc="ACS/Census estimate (count): single-parent households."),
            NestedField(32, "M_SNGPNT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: single-parent households."),
            NestedField(33, "E_LIMENG", StringType(), doc="ACS/Census estimate (count): persons age 5+ who speak English \"less than well\"."),
            NestedField(34, "M_LIMENG", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons age 5+ who speak English \"less than well\"."),
            NestedField(35, "E_MINRTY", StringType(), doc="ACS/Census estimate (count): minority population (all persons except white, non-Hispanic)."),
            NestedField(36, "M_MINRTY", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: minority population (all persons except white, non-Hispanic)."),
            NestedField(37, "E_MUNIT", StringType(), doc="ACS/Census estimate (count): housing units in structures with 10 or more units."),
            NestedField(38, "M_MUNIT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: housing units in structures with 10 or more units."),
            NestedField(39, "E_MOBILE", StringType(), doc="ACS/Census estimate (count): mobile homes."),
            NestedField(40, "M_MOBILE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: mobile homes."),
            NestedField(41, "E_CROWD", StringType(), doc="ACS/Census estimate (count): occupied housing units with more people than rooms."),
            NestedField(42, "M_CROWD", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: occupied housing units with more people than rooms."),
            NestedField(43, "E_NOVEH", StringType(), doc="ACS/Census estimate (count): households with no vehicle available."),
            NestedField(44, "M_NOVEH", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: households with no vehicle available."),
            NestedField(45, "E_GROUPQ", StringType(), doc="ACS/Census estimate (count): persons in group quarters."),
            NestedField(46, "M_GROUPQ", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: persons in group quarters."),
            NestedField(47, "EP_POV150", StringType(), doc="ACS/Census percent estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018), as a percentage of the relevant universe."),
            NestedField(48, "MP_POV150", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018)."),
            NestedField(49, "EP_UNEMP", StringType(), doc="ACS/Census percent estimate: civilian (age 16+) population unemployed, as a percentage of the relevant universe."),
            NestedField(50, "MP_UNEMP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian (age 16+) population unemployed."),
            NestedField(51, "EP_HBURD", StringType(), doc="ACS/Census percent estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(52, "MP_HBURD", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: occupied housing units spending 30% or more of household income on housing costs (2020 edition onward)."),
            NestedField(53, "EP_NOHSDP", StringType(), doc="ACS/Census percent estimate: persons age 25+ with no high school diploma, as a percentage of the relevant universe."),
            NestedField(54, "MP_NOHSDP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons age 25+ with no high school diploma."),
            NestedField(55, "EP_UNINSUR", StringType(), doc="ACS/Census percent estimate: civilian noninstitutionalized population with no health insurance, as a percentage of the relevant universe."),
            NestedField(56, "MP_UNINSUR", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian noninstitutionalized population with no health insurance."),
            NestedField(57, "EP_AGE65", StringType(), doc="ACS/Census percent estimate: persons aged 65 and older, as a percentage of the relevant universe."),
            NestedField(58, "MP_AGE65", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons aged 65 and older."),
            NestedField(59, "EP_AGE17", StringType(), doc="ACS/Census percent estimate: persons aged 17 and under, as a percentage of the relevant universe."),
            NestedField(60, "MP_AGE17", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons aged 17 and under."),
            NestedField(61, "EP_DISABL", StringType(), doc="ACS/Census percent estimate: civilian noninstitutionalized population with a disability, as a percentage of the relevant universe."),
            NestedField(62, "MP_DISABL", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: civilian noninstitutionalized population with a disability."),
            NestedField(63, "EP_SNGPNT", StringType(), doc="ACS/Census percent estimate: single-parent households, as a percentage of the relevant universe."),
            NestedField(64, "MP_SNGPNT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: single-parent households."),
            NestedField(65, "EP_LIMENG", StringType(), doc="ACS/Census percent estimate: persons age 5+ who speak English \"less than well\", as a percentage of the relevant universe."),
            NestedField(66, "MP_LIMENG", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons age 5+ who speak English \"less than well\"."),
            NestedField(67, "EP_MINRTY", StringType(), doc="ACS/Census percent estimate: minority population (all persons except white, non-Hispanic), as a percentage of the relevant universe."),
            NestedField(68, "MP_MINRTY", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: minority population (all persons except white, non-Hispanic)."),
            NestedField(69, "EP_MUNIT", StringType(), doc="ACS/Census percent estimate: housing units in structures with 10 or more units, as a percentage of the relevant universe."),
            NestedField(70, "MP_MUNIT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: housing units in structures with 10 or more units."),
            NestedField(71, "EP_MOBILE", StringType(), doc="ACS/Census percent estimate: mobile homes, as a percentage of the relevant universe."),
            NestedField(72, "MP_MOBILE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: mobile homes."),
            NestedField(73, "EP_CROWD", StringType(), doc="ACS/Census percent estimate: occupied housing units with more people than rooms, as a percentage of the relevant universe."),
            NestedField(74, "MP_CROWD", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: occupied housing units with more people than rooms."),
            NestedField(75, "EP_NOVEH", StringType(), doc="ACS/Census percent estimate: households with no vehicle available, as a percentage of the relevant universe."),
            NestedField(76, "MP_NOVEH", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: households with no vehicle available."),
            NestedField(77, "EP_GROUPQ", StringType(), doc="ACS/Census percent estimate: persons in group quarters, as a percentage of the relevant universe."),
            NestedField(78, "MP_GROUPQ", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: persons in group quarters."),
            NestedField(79, "EPL_POV150", StringType(), doc="Percentile rank (0-1) of persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(80, "EPL_UNEMP", StringType(), doc="Percentile rank (0-1) of civilian (age 16+) population unemployed, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(81, "EPL_HBURD", StringType(), doc="Percentile rank (0-1) of occupied housing units spending 30% or more of household income on housing costs (2020 edition onward), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(82, "EPL_NOHSDP", StringType(), doc="Percentile rank (0-1) of persons age 25+ with no high school diploma, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(83, "EPL_UNINSUR", StringType(), doc="Percentile rank (0-1) of civilian noninstitutionalized population with no health insurance, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(84, "SPL_THEME1", StringType(), doc="Sum of theme 1's EPL_ percentile-rank components. Theme 1 is Socioeconomic Status (poverty, unemployment, income, education) in the 2014/2016/2018 editions, or Socioeconomic Status (poverty, unemployment, housing cost burden, education, uninsured) in 2020/2022."),
            NestedField(85, "RPL_THEME1", StringType(), doc="Percentile ranking (0-1) for theme 1, among all units landed in this edition. Theme 1 is Socioeconomic Status (poverty, unemployment, income, education) in the 2014/2016/2018 editions, or Socioeconomic Status (poverty, unemployment, housing cost burden, education, uninsured) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME1:<family>'."),
            NestedField(86, "EPL_AGE65", StringType(), doc="Percentile rank (0-1) of persons aged 65 and older, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(87, "EPL_AGE17", StringType(), doc="Percentile rank (0-1) of persons aged 17 and under, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(88, "EPL_DISABL", StringType(), doc="Percentile rank (0-1) of civilian noninstitutionalized population with a disability, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(89, "EPL_SNGPNT", StringType(), doc="Percentile rank (0-1) of single-parent households, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(90, "EPL_LIMENG", StringType(), doc="Percentile rank (0-1) of persons age 5+ who speak English \"less than well\", among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(91, "SPL_THEME2", StringType(), doc="Sum of theme 2's EPL_ percentile-rank components. Theme 2 is Household Composition & Disability (age 65+, age 17-, disability, single-parent households) in the 2014/2016/2018 editions, or Household Characteristics (age 65+, age 17-, disability, single-parent households, limited English) in 2020/2022."),
            NestedField(92, "RPL_THEME2", StringType(), doc="Percentile ranking (0-1) for theme 2, among all units landed in this edition. Theme 2 is Household Composition & Disability (age 65+, age 17-, disability, single-parent households) in the 2014/2016/2018 editions, or Household Characteristics (age 65+, age 17-, disability, single-parent households, limited English) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME2:<family>'."),
            NestedField(93, "EPL_MINRTY", StringType(), doc="Percentile rank (0-1) of minority population (all persons except white, non-Hispanic), among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(94, "SPL_THEME3", StringType(), doc="Sum of theme 3's EPL_ percentile-rank components. Theme 3 is Minority Status & Language (minority population, limited English) in the 2014/2016/2018 editions, or Racial & Ethnic Minority Status (minority population) in 2020/2022."),
            NestedField(95, "RPL_THEME3", StringType(), doc="Percentile ranking (0-1) for theme 3, among all units landed in this edition. Theme 3 is Minority Status & Language (minority population, limited English) in the 2014/2016/2018 editions, or Racial & Ethnic Minority Status (minority population) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME3:<family>'."),
            NestedField(96, "EPL_MUNIT", StringType(), doc="Percentile rank (0-1) of housing units in structures with 10 or more units, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(97, "EPL_MOBILE", StringType(), doc="Percentile rank (0-1) of mobile homes, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(98, "EPL_CROWD", StringType(), doc="Percentile rank (0-1) of occupied housing units with more people than rooms, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(99, "EPL_NOVEH", StringType(), doc="Percentile rank (0-1) of households with no vehicle available, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(100, "EPL_GROUPQ", StringType(), doc="Percentile rank (0-1) of persons in group quarters, among all units landed in this edition; one of the components summed into this variable's SPL_THEME."),
            NestedField(101, "SPL_THEME4", StringType(), doc="Sum of theme 4's EPL_ percentile-rank components. Theme 4 is Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in the 2014/2016/2018 editions, or Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in 2020/2022."),
            NestedField(102, "RPL_THEME4", StringType(), doc="Percentile ranking (0-1) for theme 4, among all units landed in this edition. Theme 4 is Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in the 2014/2016/2018 editions, or Housing Type & Transportation (multi-unit housing, mobile homes, crowding, no vehicle, group quarters) in 2020/2022; derived into measure.observation as 'SVI:RPL_THEME4:<family>'."),
            NestedField(103, "SPL_THEMES", StringType(), doc="Sum of SPL_THEME1-4 (theme sub-scores)."),
            NestedField(104, "RPL_THEMES", StringType(), doc="Overall SVI percentile ranking (0-1) across all four themes, among all units landed in this edition; derived into measure.observation as 'SVI:RPL_THEMES:<family>' (family '2014' for the 2014/2016/2018 editions, '2020' for 2020/2022 -- theme composition differs between them; see measure.definition.doc)."),
            NestedField(105, "F_POV150", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons below 150% of the federal poverty level (2020 edition onward; replaces the 100%-threshold POV variable used through 2018))"),
            NestedField(106, "F_UNEMP", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian (age 16+) population unemployed)"),
            NestedField(107, "F_HBURD", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (occupied housing units spending 30% or more of household income on housing costs (2020 edition onward))"),
            NestedField(108, "F_NOHSDP", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons age 25+ with no high school diploma)"),
            NestedField(109, "F_UNINSUR", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian noninstitutionalized population with no health insurance)"),
            NestedField(110, "F_THEME1", StringType(), doc="Flag: 1 if theme 1's RPL_THEME1 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(111, "F_AGE65", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons aged 65 and older)"),
            NestedField(112, "F_AGE17", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons aged 17 and under)"),
            NestedField(113, "F_DISABL", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (civilian noninstitutionalized population with a disability)"),
            NestedField(114, "F_SNGPNT", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (single-parent households)"),
            NestedField(115, "F_LIMENG", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons age 5+ who speak English \"less than well\")"),
            NestedField(116, "F_THEME2", StringType(), doc="Flag: 1 if theme 2's RPL_THEME2 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(117, "F_MINRTY", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (minority population (all persons except white, non-Hispanic))"),
            NestedField(118, "F_THEME3", StringType(), doc="Flag: 1 if theme 3's RPL_THEME3 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(119, "F_MUNIT", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (housing units in structures with 10 or more units)"),
            NestedField(120, "F_MOBILE", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (mobile homes)"),
            NestedField(121, "F_CROWD", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (occupied housing units with more people than rooms)"),
            NestedField(122, "F_NOVEH", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (households with no vehicle available)"),
            NestedField(123, "F_GROUPQ", StringType(), doc="Flag: 1 if this variable's percentile rank is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed. (persons in group quarters)"),
            NestedField(124, "F_THEME4", StringType(), doc="Flag: 1 if theme 4's RPL_THEME4 is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(125, "F_TOTAL", StringType(), doc="Flag: 1 if RPL_THEMES is in the edition's flagged (high-vulnerability) range, 0 otherwise; -999 where not computed."),
            NestedField(126, "E_DAYPOP", StringType(), doc="ACS/Census estimate (count): daytime population estimate."),
            NestedField(127, "E_NOINT", StringType(), doc="ACS/Census estimate (count): households with no internet access (2020 edition onward)."),
            NestedField(128, "M_NOINT", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: households with no internet access (2020 edition onward)."),
            NestedField(129, "E_AFAM", StringType(), doc="ACS/Census estimate (count): Black or African American alone population (2020 edition onward)."),
            NestedField(130, "M_AFAM", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Black or African American alone population (2020 edition onward)."),
            NestedField(131, "E_HISP", StringType(), doc="ACS/Census estimate (count): Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(132, "M_HISP", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(133, "E_ASIAN", StringType(), doc="ACS/Census estimate (count): Asian alone population (2020 edition onward)."),
            NestedField(134, "M_ASIAN", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Asian alone population (2020 edition onward)."),
            NestedField(135, "E_AIAN", StringType(), doc="ACS/Census estimate (count): American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(136, "M_AIAN", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(137, "E_NHPI", StringType(), doc="ACS/Census estimate (count): Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(138, "M_NHPI", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(139, "E_TWOMORE", StringType(), doc="ACS/Census estimate (count): population of two or more races (2020 edition onward)."),
            NestedField(140, "M_TWOMORE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: population of two or more races (2020 edition onward)."),
            NestedField(141, "E_OTHERRACE", StringType(), doc="ACS/Census estimate (count): population of some other race alone (2020 edition onward)."),
            NestedField(142, "M_OTHERRACE", StringType(), doc="Margin of error (90% confidence) for the paired E_ estimate: population of some other race alone (2020 edition onward)."),
            NestedField(143, "EP_NOINT", StringType(), doc="ACS/Census percent estimate: households with no internet access (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(144, "MP_NOINT", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: households with no internet access (2020 edition onward)."),
            NestedField(145, "EP_AFAM", StringType(), doc="ACS/Census percent estimate: Black or African American alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(146, "MP_AFAM", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Black or African American alone population (2020 edition onward)."),
            NestedField(147, "EP_HISP", StringType(), doc="ACS/Census percent estimate: Hispanic or Latino population, any race (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(148, "MP_HISP", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Hispanic or Latino population, any race (2020 edition onward)."),
            NestedField(149, "EP_ASIAN", StringType(), doc="ACS/Census percent estimate: Asian alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(150, "MP_ASIAN", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Asian alone population (2020 edition onward)."),
            NestedField(151, "EP_AIAN", StringType(), doc="ACS/Census percent estimate: American Indian and Alaska Native alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(152, "MP_AIAN", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: American Indian and Alaska Native alone population (2020 edition onward)."),
            NestedField(153, "EP_NHPI", StringType(), doc="ACS/Census percent estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(154, "MP_NHPI", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: Native Hawaiian and Other Pacific Islander alone population (2020 edition onward)."),
            NestedField(155, "EP_TWOMORE", StringType(), doc="ACS/Census percent estimate: population of two or more races (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(156, "MP_TWOMORE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: population of two or more races (2020 edition onward)."),
            NestedField(157, "EP_OTHERRACE", StringType(), doc="ACS/Census percent estimate: population of some other race alone (2020 edition onward), as a percentage of the relevant universe."),
            NestedField(158, "MP_OTHERRACE", StringType(), doc="Margin of error (90% confidence) for the paired EP_ percent estimate: population of some other race alone (2020 edition onward)."),
            NestedField(159, "svi_edition", StringType(), required=True,
                        doc="The SVI edition this row was published in, e.g. '2022' -- the "
                            "version axis for this source (module docstring). Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(160, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="CDC/ATSDR Social Vulnerability Index tract file landed verbatim and "
                "whole, one row per census tract per edition. Only the 2022 edition is "
                "landed (module docstring); same '2020' layout as raw.svi__county's "
                "2020/2022 rows, verified byte-identical. Public domain (17 U.S.C. § "
                "105; https://www.cdc.gov/other/agencymaterials.html).",
    ),
    # --- raw: usda ers ruca ---
    # USDA ERS Rural-Urban Commuting Area codes, tract level (#41).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: usda ers food access ---
    # USDA ERS Food Access Research Atlas (#42).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: hrsa ahrf ---
    # HRSA Area Health Resources Files, county (#39).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: hrsa hpsa and health centers ---
    # HRSA HPSA designations and health-center sites; declares facility.site (#38).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.
}


def is_rate_limit(err):
    """R2 Data Catalog's catalog-wide write limit, however pyiceberg surfaces it.

    The REST error's *message* is 'TooManyRequestsException: Rate limit
    exceeded…' — the class is a plain RESTError and the text has no '429', so
    matching on either alone misses it (biocOnIce hit this in production).
    """
    text = f"{type(err).__name__}: {err}"
    return "429" in text or "TooManyRequests" in text


def rate_limited(call):
    """Run one catalog write, waiting out R2 Data Catalog's catalog-wide 429.

    Creating a namespace or a table is a write request like any other, so with
    two loads running it can be refused for rate alone; the retrying commit
    paths (merge.overwrite) never see it because it fails before them.
    Anything but a 429 is raised as it is.
    """
    for _ in range(8):
        try:
            return call()
        except RESTError as err:
            if not is_rate_limit(err):
                raise
            time.sleep(65)
    return call()


def create(cat, identifier):
    """Create the table if absent, with its declared schema, comment and properties."""
    ns = identifier.split(".")[0]
    # Remembered on the catalog object itself, not in a module-level set keyed by
    # id(cat): ids are recycled once a catalog is garbage-collected, so a fresh
    # catalog (every test, or a second one in a process) inherited a dead
    # catalog's "already ensured" and then hit NoSuchNamespaceError.
    ensured = cat.__dict__.setdefault("_canceronice_namespaces", set())
    if ns not in ensured:
        rate_limited(lambda: cat.create_namespace_if_not_exists(
            ns, properties={"comment": NAMESPACES[ns]}))
        ensured.add(ns)
    d = TABLES[identifier]
    # An empty PartitionSpec() is Iceberg's unpartitioned spec, so this is
    # uniform whether or not the table declares partition_by.
    spec = PartitionSpec(*[
        PartitionField(source_id=d.schema.find_field(n).field_id, field_id=1000 + i,
                       transform=IdentityTransform(), name=n)
        for i, n in enumerate(d.partition_by)])
    table = rate_limited(lambda: cat.create_table_if_not_exists(
        identifier, schema=d.iceberg_schema(), partition_spec=spec,
        properties={"comment": d.comment, **d.properties}))
    return _evolve(table, d, identifier)


def _evolve(table, d, identifier):
    """Add columns the declaration has gained since the table was created.

    Without this a new column in a TableDef never reaches a live table, and the
    cast in merge.write fails on it. Only optional columns can be added: existing
    rows read NULL for them. A new *required* column has no value for the rows
    already there, so that is a rebuild and this refuses it rather than guessing.

    ponytail: additions only. A changed type, a dropped column or a changed doc
    string is left alone — handle those when one actually happens.
    """
    live = {f.name for f in table.schema().fields}
    missing = [f for f in d.schema.fields if f.name not in live]
    if not missing:
        return table
    required = [f.name for f in missing if f.required]
    if required:
        raise ValueError(f"{identifier}: declared required column(s) {required} are not in the "
                         "live table; Iceberg cannot add a required column to existing rows — "
                         "rebuild the table")
    with table.update_schema() as update:
        for f in missing:
            update.add_column(f.name, f.field_type, doc=f.doc)
