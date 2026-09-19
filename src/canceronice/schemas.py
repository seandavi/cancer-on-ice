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
    # Both files are daily-refreshed dumps that overwrite themselves in place
    # (no edition label) -- version axis is retrieval date, landed as
    # `retrieved_on`. Both real headers end in a stray trailing comma that
    # produces a spurious empty final column name while every data row has one
    # fewer field than the header; `null_padding=true` in hrsa_sites.py's
    # read_csv call pads that phantom trailing field with NULL rather than
    # shifting every real column over, so the 55 (health centers) / 65 (HPSA)
    # real columns below land at their correct values -- see hrsa_sites.py.
    "raw.hrsa__health_center_sites": TableDef(
        schema=Schema(
            NestedField(1, "Health Center Type", StringType(), required=True,
                        doc="'Federally Qualified Health Center (FQHC)' or '...FQHC) Look-Alike'."),
            NestedField(2, "Health Center Number", StringType(), doc="Grantee's HRSA-assigned number, e.g. 'H80CS00305'."),
            NestedField(3, "BHCMIS Organization Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(4, "BPHC Assigned Number", StringType(), required=True,
                        doc="Per-site identifier, e.g. 'BPS-H80-000078' -- verified unique across "
                            "every row of the file (2026-09-18); this is facility.site.facility_id."),
            NestedField(5, "Site Name", StringType(), doc="Site's own name; facility.site.name."),
            NestedField(6, "Site Address", StringType(), doc="Street address; part of facility.site.address."),
            NestedField(7, "Site City", StringType(), doc="Part of facility.site.address."),
            NestedField(8, "Site State Abbreviation", StringType(), doc="Part of facility.site.address."),
            NestedField(9, "Site Postal Code", StringType(), doc="Part of facility.site.address."),
            NestedField(10, "Site Telephone Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(11, "Site Web Address", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(12, "Operating Hours per Week", StringType(),
                        doc="Unparsed; NULL for some Administrative sites. facility.site.attributes_json's "
                            "'operating_hours_reported' flag is true iff this is non-NULL here."),
            NestedField(13, "Health Center Location Setting Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(14, "Health Center Service Delivery Site Location Setting Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(15, "Health Center Status Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(16, "Site Status Description", StringType(),
                        doc="Always 'Active' in this file (it lists current sites only); "
                            "facility.site.attributes_json's 'status' key."),
            NestedField(17, "FQHC Site Medicare Billing Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(18, "FQHC Site NPI Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(19, "Health Center Location Identification Number", StringType(),
                        doc="A lookup code (only 3 distinct values file-wide, 2026-09-18) -- "
                            "not a per-site id; see 'BPHC Assigned Number' for that."),
            NestedField(20, "Health Center Location Type Description", StringType(),
                        doc="'Permanent' | 'Seasonal' | 'Mobile Van'; facility.site.attributes_json's 'site_type' key."),
            NestedField(21, "Health Center Type Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(22, "Health Center Type Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(23, "Health Center Operator Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(24, "Health Center Operator Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(25, "Health Center Operating Schedule Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(26, "Health Center Operational Schedule Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(27, "Health Center Operating Calendar Surrogate Key", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(28, "Health Center Operating Calendar", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(29, "Site Added to Scope this Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(30, "Health Center Name", StringType(),
                        doc="Grantee organization's name; facility.site.attributes_json's 'grantee_name' key."),
            NestedField(31, "Health Center Organization Street Address", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(32, "Health Center Organization City", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(33, "Health Center Organization State", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(34, "Health Center Organization ZIP Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(35, "Grantee Organization Type Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(36, "Geocoding Artifact Address Primary X Coordinate", StringType(),
                        doc="Longitude, unparsed; facility.site.lon (TRY_CAST to double -- 73 rows are blank, "
                            "2026-09-18)."),
            NestedField(37, "Geocoding Artifact Address Primary Y Coordinate", StringType(),
                        doc="Latitude, unparsed; facility.site.lat."),
            NestedField(38, "U.S. - Mexico Border 100 Kilometer Indicator", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(39, "U.S. - Mexico Border County Indicator", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(40, "State and County Federal Information Processing Standard Code", StringType(), required=True,
                        doc="5-digit county FIPS, e.g. '12031'; facility.site.geo_id is 'county:'+this. "
                            "The file carries current (2020-vintage) codes only -- Connecticut's nine "
                            "planning regions 09110-09190 and Alaska's current census areas (e.g. 02063 "
                            "Chugach, 02066 Copper River), no legacy codes seen (2026-09-18)."),
            NestedField(41, "Complete County Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(42, "County Equivalent Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(43, "County Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(44, "HHS Region Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(45, "HHS Region Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(46, "State FIPS Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(47, "State Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(48, "State FIPS and Congressional District Number Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(49, "Congressional District Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(50, "Congressional District Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(51, "Congressional District Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(52, "U.S. Congressional Representative Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(53, "Name of U.S. Senator Number One", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(54, "Name of U.S. Senator Number Two", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(55, "Data Warehouse Record Create Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(56, "retrieved_on", StringType(), required=True,
                        doc="Date this daily-refreshed dump was fetched, ISO YYYY-MM-DD -- the version "
                            "axis for this source (it publishes no edition label). Raw is replaced "
                            "wholesale per value of this column."),
            NestedField(57, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="HRSA Health Center Service Delivery and Look-Alike Sites, landed verbatim and whole "
                "(SPEC.md § Sources — first tranche). Public domain; HRSA's own Data Usage Terms & "
                "Conditions for this dataset state 'Usage limitations: None' "
                "(https://data.hrsa.gov/data/download, checked 2026-09-18).",
    ),

    "raw.hrsa__hpsa_primary_care": TableDef(
        schema=Schema(
            NestedField(1, "HPSA Name", StringType(), doc="Shortage area's own name -- an organisation/place name, never an individual."),
            NestedField(2, "HPSA ID", StringType(), required=True, doc="HRSA's designation id."),
            NestedField(3, "Designation Type", StringType(), required=True,
                        doc="'Geographic HPSA' | 'High Needs Geographic HPSA' (the only two counted into "
                            "measure.observation) | 'HPSA Population' | 'Federally Qualified Health "
                            "Center' | '...Look A Like' | 'Rural Health Clinic' | 'Correctional "
                            "Facility' | 'Other Facility' | 'Indian Health Service, Tribal Health, and "
                            "Urban Indian Health Organizations' -- the rest are population-group or "
                            "facility designations that don't carry a usable geo_id (SPEC.md gap noted "
                            "in issue #38); landed here but not derived."),
            NestedField(4, "HPSA Discipline Class", StringType(), required=True, doc="'Primary Care' for every row in this file."),
            NestedField(5, "HPSA Score", StringType(),
                        doc="HRSA's shortage-severity score, unparsed; feeds "
                            "measure.observation's HPSA:pc_max_score."),
            NestedField(6, "PC MCTA Score", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(7, "Primary State Abbreviation", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(8, "HPSA Status", StringType(), required=True,
                        doc="'Designated' | 'Withdrawn' | 'Proposed For Withdrawal'. Withdrawn / "
                            "proposed-for-withdrawal is a real status, not suppression -- carried as-is; "
                            "only 'Designated' rows feed measure.observation."),
            NestedField(9, "HPSA Designation Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(10, "HPSA Designation Last Update Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(11, "Metropolitan Indicator", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(12, "HPSA Geography Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(13, "HPSA Degree of Shortage", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(14, "Withdrawn Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(15, "HPSA FTE", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(16, "HPSA Designation Population", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(17, "% of Population Below 100% Poverty", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(18, "HPSA Formal Ratio", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(19, "HPSA Population Type", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(20, "Rural Status", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(21, "Longitude", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(22, "Latitude", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(23, "BHCMIS Organization Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(24, "Break in Designation", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(25, "Common County Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(26, "Common Postal Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(27, "Common Region Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(28, "Common State Abbreviation", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(29, "Common State County FIPS Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(30, "Common State FIPS Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(31, "Common State Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(32, "County Equivalent Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(33, "County or County Equivalent Federal Information Processing Standard Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(34, "Discipline Class Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(35, "HPSA Address", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(36, "HPSA City", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(37, "HPSA Component Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(38, "HPSA Component Source Identification Number", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(39, "HPSA Component State Abbreviation", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(40, "HPSA Component Type Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(41, "HPSA Component Type Description", StringType(),
                        doc="'Single County' | 'County Subdivision' | 'Census Tract' -- the HPSA's own "
                            "sub-county component granularity; a multi-component HPSA has one row per "
                            "component, all sharing the same county FIPS (deduplicated by HPSA ID + FIPS "
                            "before counting, see hrsa_sites.py)."),
            NestedField(42, "HPSA Designation Population Type Description", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(43, "HPSA Estimated Served Population", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(44, "HPSA Estimated Underserved Population", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(45, "HPSA Metropolitan Indicator Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(46, "HPSA Population Type Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(47, "HPSA Postal Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(48, "HPSA Provider Ratio Goal", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(49, "HPSA Resident Civilian Population", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(50, "HPSA Shortage", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(51, "HPSA Status Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(52, "HPSA Type Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(53, "HPSA Withdrawn Date String", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(54, "Primary State FIPS Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(55, "Primary State Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(56, "Provider Type", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(57, "Rural Status Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(58, "State Abbreviation", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(59, "State and County Federal Information Processing Standard Code", StringType(), required=True,
                        doc="5-digit county FIPS for a real designation, but 'XXXXX' / 'XXX' for some "
                            "Withdrawn / Proposed For Withdrawal rows (masked, not a real code -- verified "
                            "2026-09-18; none seen on a currently-Designated Geographic/High Needs row). "
                            "measure.observation's geo_id is 'county:'+this, restricted to rows matching "
                            "5 digits."),
            NestedField(60, "State FIPS Code", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(61, "State Name", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(62, "U.S. - Mexico Border 100 Kilometer Indicator", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(63, "U.S. - Mexico Border County Indicator", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(64, "Data Warehouse Record Create Date", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(65, "Data Warehouse Record Create Date Text", StringType(), doc="As published by HRSA; not used downstream."),
            NestedField(66, "retrieved_on", StringType(), required=True,
                        doc="Date this daily-refreshed dump was fetched, ISO YYYY-MM-DD -- the version "
                            "axis for this source. Raw is replaced wholesale per value of this column."),
            NestedField(67, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        comment="HRSA primary-care Health Professional Shortage Area designations "
                "(BCD_HPSA_FCT_DET_PC.csv), landed verbatim and whole, every status including "
                "Withdrawn (SPEC.md § Sources — first tranche). Public domain; HRSA's own Data Usage "
                "Terms & Conditions for this dataset state 'Usage limitations: None' "
                "(https://data.hrsa.gov/data/download, checked 2026-09-18). Dental and mental-health "
                "HPSA files are not landed by this module (issue #38 asks for primary care at minimum).",
    ),

    # facility.site (SPEC.md § Facilities) -- first facility source declares the shared table.
    # `attributes` was specified as map<string,string>; a DuckDB MAP -> Arrow -> this Iceberg
    # schema round-trip aborts the process (Arrow C++ validator: "Map array keys array should
    # have no nulls", not a catchable Python exception -- confirmed with a minimal repro
    # 2026-09-18), so it is a JSON string column here instead; SPEC.md is updated to match.
    "facility.site": TableDef(
        schema=Schema(
            NestedField(1, "facility_id", StringType(), required=True,
                        doc="Source's own stable per-site id. For HRSA_HC, HRSA's 'BPHC Assigned "
                            "Number' -- verified unique across the whole file. Part of the business key."),
            NestedField(2, "source", StringType(), required=True,
                        doc="Asserting provider, e.g. 'HRSA_HC'. Part of the business key and of every "
                            "writer's merge scope, so sources stack in one table without one retiring "
                            "another's rows."),
            NestedField(3, "source_release", StringType(),
                        doc="The source's own release label, when it publishes one. NOT part of the "
                            "business key -- versioning here is deliberately unsettled (issue #19). NULL "
                            "for a continuously-refreshed snapshot source like HRSA_HC: the snapshot date "
                            "is recorded in provenance.release and raw's own version column instead, "
                            "since repeating it here would make every unchanged row look changed on every "
                            "ingest; when cancerOnIce saw a given version of the row is "
                            "valid_from/valid_to, not source_release."),
            NestedField(4, "kind", StringType(), required=True,
                        doc="'fqhc' | 'rhc' | 'mammography' | 'lung_screening' | 'provider' | 'hospital'. "
                            "HRSA_HC rows are 'fqhc' for both true FQHCs and FQHC Look-Alikes."),
            NestedField(5, "name", StringType(), doc="Site's own name."),
            NestedField(6, "address", StringType(), doc="Single-line street address, city, state, postal code."),
            NestedField(7, "lat", DoubleType(), doc="Latitude, WGS84, as published by the source."),
            NestedField(8, "lon", DoubleType(), doc="Longitude, WGS84, as published by the source."),
            NestedField(9, "geo_id", StringType(),
                        doc="FK geography.unit. SPEC.md calls for tract-level geo_id here; HRSA_HC "
                            "publishes only county FIPS, so this is 'county:'+FIPS for that source -- a "
                            "documented gap, not a tract lookup this module performs."),
            NestedField(10, "geo_vintage", IntegerType(),
                        doc="FK geography.unit's vintage. 2020 for HRSA_HC: the file's Connecticut rows "
                            "carry the nine 2022 planning regions (09110-09190) and its Alaska rows carry "
                            "the current census areas (e.g. 02063, 02066), never a legacy code (verified "
                            "2026-09-18)."),
            NestedField(11, "attributes_json", StringType(),
                        doc="JSON object of source-specific attributes, keys documented per source. "
                            "HRSA_HC keys: site_type ('Permanent'|'Seasonal'|'Mobile Van'), "
                            "operating_hours_reported ('true'|'false'), grantee_name, grantee_id, status."),
            NestedField(12, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(13, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("facility_id", "source"),
        comment="Places care happens (SPEC.md § Facilities). First writer: HRSA_HC (health-center "
                "service delivery / look-alike sites). Full Type-2 history via valid_from/valid_to -- a "
                "site absent from a later snapshot is retired by the merge, which is the point for a "
                "source that only ever serves today's list.",
    ),
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
