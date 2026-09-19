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
from pyiceberg.table import TableProperties
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import DoubleType, IntegerType, NestedField, StringType

# DuckDB's own default row-group size (issue #120). Sorted data plus a row
# group this small is what makes a one-measure, one-state query decode one or
# two row groups instead of a whole file's worth: Parquet min/max statistics
# are per row group, so a smaller group is a tighter (and more numerous)
# set of statistics to prune against. Verified (pyiceberg 0.12.0, io/pyarrow.py):
# `write.parquet.row-group-limit` is honoured as a ROW count, default 1,048,576
# -- the byte-based `write.parquet.row-group-size-bytes` property exists as a
# name but its writer path is not implemented (warns "not implemented" and is
# ignored), so row count is the only lever that actually works.
ROW_GROUP_ROWS = 122_880

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
    # Physical clustering, for pruning only (issue #120): merge.overwrite ORDER
    # BYs the final table by this before every write, so a sorted column's
    # Parquet row-group min/max statistics actually exclude row groups. PyIceberg
    # never enforces this on write -- the ORDER BY is what does the work; the
    # declared Iceberg table sort order (set in schemas.create/_evolve) is only
    # a hint for a reader that looks at it.
    sort_by: tuple = ()
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
            NestedField(3, "source_version", StringType(), required=True,
                        doc="The upstream version in the SOURCE'S OWN vocabulary, never "
                            "normalised. Part of the business key together with release and "
                            "source (#78): several source releases can land under one catalog "
                            "release, and keying on (release, source) alone kept only the last "
                            "one landed. Always populated — merge.manifest defaults it to the "
                            "retrieval date when the source publishes no version label of its "
                            "own, so it is never NULL."),
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
        business_key=("release", "source", "source_version"),
        sort_by=("release", "source", "source_version"),
        comment="One row per (cancerOnIce release, source, source_version): what this release "
                "was built from. This is what makes a release reproducible — resolve it here to "
                "each source's own version, then query each table at that release. Durable by "
                "design: unlike Iceberg snapshot summaries it does not expire. Keyed on "
                "source_version too (#78) so landing several source releases of one source "
                "under one catalog release keeps a row for each.",
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
        sort_by=("vintage", "level", "geo_id"),
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
        sort_by=("measure_id",),
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
        sort_by=("stratum_id",),
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
        sort_by=("source_release", "measure_id", "geo_id", "period_start", "stratum_id"),
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
        sort_by=("gazetteer_year", "geoid"),
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
        sort_by=("gazetteer_year", "geoid"),
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
        sort_by=("state",),
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
        sort_by=("places_release", "LocationID", "MeasureId", "DataValueTypeID"),
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
        sort_by=("rucc_edition", "FIPS", "Attribute"),
        comment="USDA ERS Rural-Urban Continuum Codes landed verbatim and whole, long format: "
                "one row per (county, attribute) exactly as published. Public domain (U.S. "
                "Government work, 17 U.S.C. Sec 105).",
    ),

    # --- derived: geography alias ---
    # geography.alias — FIPS renames and recodes that are not boundary changes (#26).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.
    "raw.geography__county_recodes": TableDef(
        schema=Schema(
            NestedField(1, "old_fips", StringType(), required=True,
                        doc="5-digit county FIPS code before the change, as published, "
                            "zero-padded, e.g. '46113'."),
            NestedField(2, "old_name", StringType(), required=True,
                        doc="County (or equivalent) name before the change, as published."),
            NestedField(3, "new_fips", StringType(), required=True,
                        doc="5-digit county FIPS code after the change, as published."),
            NestedField(4, "new_name", StringType(), required=True,
                        doc="County (or equivalent) name after the change, as published."),
            NestedField(5, "effective_year", StringType(), required=True,
                        doc="Calendar year the change took effect, unparsed."),
            NestedField(6, "change_type", StringType(), required=True,
                        doc="One of: recode, rename, recode_and_rename."),
            NestedField(7, "source_url", StringType(), required=True,
                        doc="The Census county-changes decade page this row was transcribed "
                            "from."),
            NestedField(8, "note", StringType(),
                        doc="The exact Census sentence this row was transcribed from, quoted."),
            NestedField(9, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("old_fips", "new_fips"),
        comment="Curated CSV of Census 'Substantial Changes to Counties' name/code changes "
                "(county_recodes.csv, committed in the package — the upstream is prose, not a "
                "data file), landed verbatim and whole. Replaced wholesale each time it is "
                "re-curated, like raw.census__state_fips — there is no upstream edition to key "
                "an overwrite scope on. Licence: U.S. government work, public domain "
                "(17 U.S.C. § 105).",
    ),

    "geography.alias": TableDef(
        schema=Schema(
            NestedField(1, "old_geo_id", StringType(), required=True,
                        doc="geography.unit-style id ('county:<fips>') before the change. Part "
                            "of the business key together with new_geo_id. Carries no vintage — "
                            "unlike geography.unit, an alias is a pure code mapping a join "
                            "resolves regardless of which vintage the observation came from."),
            NestedField(2, "new_geo_id", StringType(), required=True,
                        doc="geography.unit-style id ('county:<fips>') after the change. Part "
                            "of the business key together with old_geo_id."),
            NestedField(3, "effective_year", IntegerType(), required=True,
                        doc="Calendar year the change took effect, e.g. 2015."),
            NestedField(4, "change_type", StringType(), required=True,
                        doc="One of: recode (code changed, name did not), rename (name changed, "
                            "code did not — not useful for resolving a join, since old_geo_id "
                            "would equal new_geo_id, so none are seeded), recode_and_rename "
                            "(both changed, e.g. Shannon -> Oglala Lakota County)."),
            NestedField(5, "old_name", StringType(), required=True, doc="Name before the change."),
            NestedField(6, "new_name", StringType(), required=True, doc="Name after the change."),
            NestedField(7, "source", StringType(), required=True,
                        doc="Asserting provider, e.g. 'CENSUS_COUNTY_CHANGES'. Part of every "
                            "writer's merge scope, so a second source of aliases (NHGIS, say) "
                            "could stack here without retiring this one's rows."),
            NestedField(8, "source_url", StringType(), required=True,
                        doc="The page this row's change was documented on."),
            NestedField(9, "note", StringType(),
                        doc="Citation: the exact source sentence this row was transcribed from."),
            NestedField(10, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(11, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("old_geo_id", "new_geo_id"),
        sort_by=("old_geo_id", "new_geo_id"),
        comment="FIPS renames and re-codings that are NOT boundary changes (SPEC.md § "
                "geography.alias, #26): a join on an old code resolves through here to the "
                "current one instead of dropping (SPEC.md Acceptance B). Real boundary changes "
                "(splits, merges, Connecticut's planning regions) belong to geography.crosswalk "
                "(#25) instead, since a 1:1 alias would misrepresent them.",
    ),

    # --- derived: measure cancer site ---
    # measure.cancer_site — SEER site recode <-> ICD-O-3 <-> ICD-10 <-> NCIt / MONDO (#31).
    "raw.seer__site_recode": TableDef(
        schema=Schema(
            NestedField(1, "site_group", StringType(), required=True,
                        doc="Site/group label, exactly as published -- including the source's "
                            "own leading-space indentation (4 spaces per nesting level), its "
                            "only hierarchy marker."),
            NestedField(2, "icdo3_site", StringType(),
                        doc="ICD-O-3 topography range for this row, unparsed. Blank for a group "
                            "heading rather than a site (e.g. 'Colon and Rectum')."),
            NestedField(3, "icdo3_histology", StringType(),
                        doc="ICD-O-3 histology qualifier for this row, unparsed -- an exclusion "
                            "range (e.g. 'excluding 9050-9055, 9140, 9590-9993') for most codes, "
                            "an inclusion range for a few (e.g. Melanoma's '8720-8790')."),
            NestedField(4, "recode", StringType(),
                        doc="The numeric site-recode code, as published (a trailing space on "
                            "some rows is a real upstream artifact, kept verbatim). Blank for a "
                            "group heading. A code can be defined by more than one row (e.g. "
                            "31040 has two: see module docstring); all land here."),
            NestedField(5, "row_order", IntegerType(), required=True,
                        doc="0-based position in the source file, preserved so multi-row code "
                            "definitions concatenate in publication order."),
            NestedField(6, "site_recode_edition", StringType(), required=True,
                        doc="Which SEER site-recode edition this row came from, e.g. "
                            "'icdo3_dwhoheme' (Site Recode ICD-O-3/WHO 2008 -- the edition "
                            "State Cancer Profiles' own FAQ names for incidence). Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(7, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("site_recode_edition", "row_order"),
        comment="SEER Site Recode ICD-O-3/WHO 2008, landed verbatim and whole from the "
                "source's own semicolon-delimited text file (SPEC.md § measure.cancer_site). "
                "Public domain (NCI/U.S. Government work).",
    ),

    "raw.seer__cod_recode": TableDef(
        schema=Schema(
            NestedField(1, "cod_group", StringType(), required=True,
                        doc="Cause-of-death group label, exactly as published -- including the "
                            "source's own leading-space indentation."),
            NestedField(2, "icd8", StringType(), doc="ICD-8 range, unparsed. Usually blank -- "
                                                     "this table's own header carries the column "
                                                     "but most rows only state ICD-9/ICD-10."),
            NestedField(3, "icd9", StringType(), doc="ICD-9 (1979-1998) range, unparsed."),
            NestedField(4, "icd10", StringType(), doc="ICD-10 (1999+) range, unparsed."),
            NestedField(5, "recode", StringType(),
                        doc="The numeric recode code, as published. Blank for a group heading; "
                            "the literal sentinel '--' for 'All Malignant Cancers', which has no "
                            "code of its own (SCP's FAQ: 'All Cancers refers to all invasive "
                            "cancers combined')."),
            NestedField(6, "row_order", IntegerType(), required=True,
                        doc="0-based position within the 'Neoplasm Causes of Death' table in the "
                            "source file."),
            NestedField(7, "cod_recode_edition", StringType(), required=True,
                        doc="Which SEER Cause of Death Recode edition this row came from, e.g. "
                            "'1969_d03012018' (COD Recode 1969+, current as of 03/01/2018 -- the "
                            "edition State Cancer Profiles' own FAQ names for mortality). Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(8, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("cod_recode_edition", "row_order"),
        comment="SEER Cause of Death Recode 1969+, 'Neoplasm Causes of Death' table only "
                "(the source file's other two tables -- non-neoplasm causes, and "
                "administrative codes -- are out of scope for a cancer-site bridge), landed "
                "verbatim and whole (SPEC.md § measure.cancer_site). Public domain "
                "(NCI/U.S. Government work).",
    ),

    "measure.cancer_site": TableDef(
        schema=Schema(
            NestedField(1, "cancer_site_code", StringType(), required=True,
                        doc="The SEER site-recode numeric code (raw.seer__site_recode.recode), "
                            "e.g. '26000' for Breast. Business key together with source_release."),
            NestedField(2, "label", StringType(), required=True, doc="Site/group name, as SEER "
                                                                      "publishes it."),
            NestedField(3, "parent_code", StringType(),
                        doc="Another row's cancer_site_code, for SEER's own nested groupings "
                            "(e.g. Cecum under 'Colon excluding Rectum' under 'Colon and "
                            "Rectum'). Always NULL for this edition: SEER's table assigns no "
                            "numeric code to any group heading, at any depth, so there is no "
                            "coded ancestor to record -- see module docstring."),
            NestedField(4, "icdo3_topography", StringType(),
                        doc="ICD-O-3 topography range(s), verbatim from raw.seer__site_recode "
                            "(joined with '; ' when a code's definition spans more than one "
                            "published row)."),
            NestedField(5, "icdo3_histology_exclusions", StringType(),
                        doc="ICD-O-3 histology qualifier(s), verbatim from "
                            "raw.seer__site_recode (an exclusion range for most codes, an "
                            "inclusion range for a few -- see that table's own column doc)."),
            NestedField(6, "icd10_mortality", StringType(),
                        doc="ICD-10 mortality range, verbatim from raw.seer__cod_recode, filled "
                            "only where that table's label matches this row's label exactly "
                            "(module docstring: 56 of 81 codes align this way). NULL where COD "
                            "groups this site more coarsely than incidence does, or the labels "
                            "genuinely differ, rather than guessed."),
            NestedField(7, "ncit_id", StringType(),
                        doc="NCI Thesaurus id (e.g. 'NCIT:C9335'), from the curated bridge "
                            "(src/canceronice/data/cancer_site_ontology.csv) for the State "
                            "Cancer Profiles sites it covers. NULL elsewhere -- never guessed "
                            "(module docstring lists the sites left unmapped and why)."),
            NestedField(8, "mondo_id", StringType(),
                        doc="MONDO Disease Ontology id (e.g. 'MONDO:0007254'), from the same "
                            "curated bridge. NULL elsewhere."),
            NestedField(9, "mapping_basis", StringType(),
                        doc="'curated' when ncit_id or mondo_id is filled (this project's own "
                            "researched mapping -- no official SEER-recode-to-ontology crosswalk "
                            "exists, checked 2026-09-18); NULL when neither is filled. No row "
                            "here is 'published' -- that value is reserved should SEER, NCIt or "
                            "MONDO ever publish such a mapping themselves."),
            NestedField(10, "source", StringType(), required=True,
                        doc="Always 'SEER' -- the recode's own publisher."),
            NestedField(11, "source_release", StringType(), required=True,
                        doc="The site-recode edition this row's cancer_site_code is defined "
                            "under, e.g. 'icdo3_dwhoheme'. Business key together with "
                            "cancer_site_code -- a later SEER edition never retires this one's "
                            "rows."),
            NestedField(12, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(13, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("cancer_site_code", "source_release"),
        sort_by=("cancer_site_code", "source_release"),
        comment="SEER site recode <-> ICD-O-3 topography/histology <-> ICD-10 (mortality) <-> "
                "NCIt/MONDO (SPEC.md § measure.cancer_site) -- the bridge to biocOnIce's "
                "`ontology` namespace. Full Type-2 history via valid_from/valid_to.",
    ),

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
        sort_by=("svi_edition", "FIPS"),
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
        sort_by=("svi_edition", "FIPS"),
        comment="CDC/ATSDR Social Vulnerability Index tract file landed verbatim and "
                "whole, one row per census tract per edition. Only the 2022 edition is "
                "landed (module docstring); same '2020' layout as raw.svi__county's "
                "2020/2022 rows, verified byte-identical. Public domain (17 U.S.C. § "
                "105; https://www.cdc.gov/other/agencymaterials.html).",
    ),
    # --- raw: usda ers ruca ---
    # USDA ERS Rural-Urban Commuting Area codes, tract level (#41). Public
    # domain; two genuinely different upstream layouts (2010 xlsx, 2020 csv)
    # share this table via ruca_edition, each edition's own columns NULL on
    # the other's rows -- see ers_ruca.py's docstring.
    "raw.ers__ruca_tract": TableDef(
        schema=Schema(
            NestedField(1, "ruca_edition", StringType(), required=True,
                        doc="The RUCA edition this row was published in, '2010' or '2020' -- "
                            "the version axis for this source. Raw is replaced wholesale per "
                            "value of this column."),
            NestedField(2, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
            # --- 2010 edition only (NULL on 2020 rows); from the revised (3 Jul
            # 2019) workbook's 'Data' sheet, 9 columns, header on row 2.
            NestedField(3, "fips_2010", StringType(),
                        doc="5-digit county FIPS code ('State-County FIPS Code' column). "
                            "2010 edition only."),
            NestedField(4, "state_2010", StringType(),
                        doc="USPS state abbreviation ('Select State' column). 2010 edition only."),
            NestedField(5, "county_name_2010", StringType(),
                        doc="County (or equivalent) name ('Select County' column). 2010 "
                            "edition only."),
            NestedField(6, "tract_fips_2010", StringType(),
                        doc="11-digit tract FIPS code on 2010-vintage county codes "
                            "('State-County-Tract FIPS Code' column) -- the geo_id source for "
                            "the 2010 edition. 2010 edition only."),
            NestedField(7, "primary_ruca_2010", StringType(),
                        doc="Primary RUCA code ('Primary RUCA Code 2010' column): 1-10 or 99 "
                            "(not coded). See measure.definition RUCA:primary for the scheme. "
                            "2010 edition only."),
            NestedField(8, "secondary_ruca_2010", StringType(),
                        doc="Secondary RUCA code ('Secondary RUCA Code, 2010 (see errata)' "
                            "column) -- the workbook's own errata note says this reflects a "
                            "3 Jul 2019 correction to 10,909 of 74,002 tracts' secondary codes; "
                            "primary codes were unaffected. See measure.definition "
                            "RUCA:secondary for the scheme. 2010 edition only."),
            NestedField(9, "population_2010", StringType(),
                        doc="2010 Census tract population ('Tract Population, 2010' column). "
                            "2010 edition only."),
            NestedField(10, "land_area_2010", StringType(),
                        doc="Land area, square miles ('Land Area (square miles), 2010' "
                            "column). 2010 edition only."),
            NestedField(11, "pop_density_2010", StringType(),
                        doc="Population per square mile ('Population Density (per square "
                            "mile), 2010' column); NULL where land area is 0. 2010 edition only."),
            # --- 2020 edition only (NULL on 2010 rows); column names kept as
            # published (already clean identifiers), 27 columns, plain CSV.
            NestedField(12, "TractFIPS23", StringType(),
                        doc="11-digit tract FIPS code on the county-equivalent codes ERS uses "
                            "as of 2023 (Connecticut's nine planning regions, 09110-09190, in "
                            "place of its eight legacy counties); 'N/A' for a handful of "
                            "zero-population Connecticut water tracts with no 2023 county "
                            "assignment. NOT used as geo_id -- see ers_ruca.py's docstring. "
                            "2020 edition only."),
            NestedField(13, "CountyFIPS23", StringType(),
                        doc="5-digit county-equivalent FIPS code paired with TractFIPS23, or "
                            "'N/A'. 2020 edition only."),
            NestedField(14, "CountyCode23", StringType(),
                        doc="3-digit county code (no state prefix) paired with TractFIPS23, "
                            "or 'N/A'. 2020 edition only."),
            NestedField(15, "CountyName23", StringType(),
                        doc="County or county-equivalent name (e.g. a Connecticut planning "
                            "region) paired with TractFIPS23, or 'N/A'. 2020 edition only."),
            NestedField(16, "TractFIPS20", StringType(),
                        doc="11-digit tract FIPS code on 2020-vintage county codes "
                            "(Connecticut's eight legacy counties, Alaska's post-2019 areas) -- "
                            "the geo_id source for the 2020 edition (see ers_ruca.py's "
                            "docstring). Always populated, unlike TractFIPS23. 2020 edition only."),
            NestedField(17, "TractCode20", StringType(),
                        doc="6-digit tract code (no state/county prefix) paired with "
                            "TractFIPS20. 2020 edition only."),
            NestedField(18, "TractName20", StringType(),
                        doc="Census tract name, e.g. 'Census Tract 9781'. 2020 edition only."),
            NestedField(19, "CountyFIPS20", StringType(),
                        doc="5-digit county FIPS code paired with TractFIPS20. 2020 edition only."),
            NestedField(20, "CountyCode20", StringType(),
                        doc="3-digit county code (no state prefix) paired with TractFIPS20. "
                            "2020 edition only."),
            NestedField(21, "CountyName20", StringType(),
                        doc="County name paired with TractFIPS20. 2020 edition only."),
            NestedField(22, "StateFIPS20", StringType(),
                        doc="2-digit state FIPS code. 2020 edition only."),
            NestedField(23, "StateName20", StringType(),
                        doc="State name. 2020 edition only."),
            NestedField(24, "UrbanAreaCode20", StringType(),
                        doc="Census urban area code the tract's core belongs to, or blank when "
                            "the tract is not an urban core. 2020 edition only."),
            NestedField(25, "UrbanAreaName20", StringType(),
                        doc="Census urban area name (may carry non-ASCII characters), or "
                            "blank. 2020 edition only."),
            NestedField(26, "UrbanCore", StringType(),
                        doc="'1' if the tract is part of an urban core, else '0'. 2020 "
                            "edition only."),
            NestedField(27, "UrbanCoreType", StringType(),
                        doc="Urban core classification driving PrimaryRUCA, e.g. 'Rural', "
                            "'Micro core'; 'Water' on every code-99 (not coded) tract checked. "
                            "2020 edition only."),
            NestedField(28, "PrimaryRUCA", StringType(),
                        doc="Primary RUCA code: 1-10 or 99 (not coded). See "
                            "measure.definition RUCA:primary for the scheme. 2020 edition only."),
            NestedField(29, "PrimaryRUCADescription", StringType(),
                        doc="ERS's own short label for PrimaryRUCA, e.g. 'Micropolitan high "
                            "commuting'. 2020 edition only."),
            NestedField(30, "PrimaryDestinationCode", StringType(),
                        doc="Urban area (or micro/small-town core tract) code the primary "
                            "commuting flow is measured against, or blank. 2020 edition only."),
            NestedField(31, "PrimaryDestinationName", StringType(),
                        doc="Name paired with PrimaryDestinationCode (may carry non-ASCII "
                            "characters). 2020 edition only."),
            NestedField(32, "SecondaryRUCA", StringType(),
                        doc="Secondary RUCA code: 1-10, 99, or a decimal sub-flag (e.g. "
                            "'7.2'). See measure.definition RUCA:secondary for the scheme. "
                            "2020 edition only."),
            NestedField(33, "SecondaryRUCADescription", StringType(),
                        doc="ERS's own short label for SecondaryRUCA, e.g. 'Small town core, "
                            "secondary flow to micro UA'. 2020 edition only."),
            NestedField(34, "SecondaryDestinationCode", StringType(),
                        doc="Destination code the secondary commuting flow is measured "
                            "against, or blank. 2020 edition only."),
            NestedField(35, "SecondaryDestinationName", StringType(),
                        doc="Name paired with SecondaryDestinationCode. 2020 edition only."),
            NestedField(36, "Population", StringType(),
                        doc="2020 Census tract population. 2020 edition only."),
            NestedField(37, "LandArea", StringType(),
                        doc="Land area, square miles. 2020 edition only."),
            NestedField(38, "PopDensity", StringType(),
                        doc="Population per square mile. 2020 edition only."),
        ),
        sort_by=("ruca_edition", "fips_2010", "TractFIPS20"),
        comment="USDA ERS Rural-Urban Commuting Area codes landed verbatim and whole, one row "
                "per census tract per edition. Public domain (U.S. Government work, 17 U.S.C. "
                "Sec 105). The 2010 and 2020 editions are different upstream layouts sharing "
                "this table via ruca_edition; each edition's own columns are NULL on the "
                "other's rows.",
    ),

    # --- raw: usda ers food access ---
    # USDA ERS Food Access Research Atlas (#42): census-tract low-income/low-access
    # ("food desert") flags and the population counts behind them, one row per
    # (tract, edition) landed whole -- all 147 published columns, verbatim. Public
    # domain (17 U.S.C. Sec 105); see ers_food_access.py's docstring for the two
    # editions' real headers, the 2010-tract geo_vintage evidence, and the
    # missing-value / share-scale differences between them.
    "raw.ers__food_access": TableDef(
        schema=Schema(
            NestedField(1, "CensusTract", StringType(), required=True,
                        doc="Census tract: Census tract number."),
            NestedField(2, "State", StringType(), doc="State: State name."),
            NestedField(3, "County", StringType(), doc="County: County name."),
            NestedField(4, "Urban", StringType(), doc="Urban tract: Flag for urban tract."),
            NestedField(5, "Pop2010", StringType(),
                        doc="Population, tract total: Population count from 2010 census."),
            NestedField(6, "OHU2010", StringType(),
                        doc="Housing units, total: Occupied housing unit count from 2010 census."),
            NestedField(7, "GroupQuartersFlag", StringType(),
                        doc="Group quarters, tract with high share: Flag for tract where >=67%."),
            NestedField(8, "NUMGQTRS", StringType(),
                        doc="Group quarters, tract population residing in, number: Count of tract "
                            "population residing in group quarters."),
            NestedField(9, "PCTGQTRS", StringType(),
                        doc="Group quarters, tract population residing in, share: Percent of tract "
                            "population residing in group quarters."),
            NestedField(10, "LILATracts_1And10", StringType(),
                        doc="Low income and low access tract measured at 1 mile for urban areas and "
                            "10 miles for rural areas: Flag for low-income and low access when "
                            "considering low accessibilty at 1 and 10 miles."),
            NestedField(11, "LILATracts_halfAnd10", StringType(),
                        doc="Low income and low access tract measured at 1/2 mile for urban areas "
                            "and 10 miles for rural areas: Flag for low-income and low access "
                            "when considering low accessibilty at 1/2 and 10 miles."),
            NestedField(12, "LILATracts_1And20", StringType(),
                        doc="Low income and low access tract measured at 1 mile for urban areas and "
                            "20 miles for rural areas: Flag for low-income and low access when "
                            "considering low accessibilty at 1 and 20 miles."),
            NestedField(13, "LILATracts_Vehicle", StringType(),
                        doc="Low income and low access tract using vehicle access or low income and "
                            "low access tract measured at 20 miles: Flag for low-income and low "
                            "access when considering vehicle access or at 20 miles."),
            NestedField(14, "HUNVFlag", StringType(),
                        doc="Vehicle access, tract with low vehicle access: Flag for tract where >= "
                            "100 of households do not have a vehicle, and beyond 1/2 mile from "
                            "supermarket."),
            NestedField(15, "LowIncomeTracts", StringType(),
                        doc="Low income tract: Flag for low income tract."),
            NestedField(16, "PovertyRate", StringType(),
                        doc="Tract poverty rate: Share of the tract population living with income at "
                            "or below the Federal poverty thresholds for family size."),
            NestedField(17, "MedianFamilyIncome", StringType(),
                        doc="Tract median family income: Tract median family income."),
            NestedField(18, "LA1and10", StringType(),
                        doc="Low access tract at 1 mile for urban areas and 10 miles for rural "
                            "areas: Flag for low access tract at 1 mile for urban areas or 10 "
                            "miles for rural areas."),
            NestedField(19, "LAhalfand10", StringType(),
                        doc="Low access tract at 1/2 mile for urban areas and 10 miles for rural "
                            "areas: Flag for low access tract at 1/2 mile for urban areas or 10 "
                            "miles for rural areas."),
            NestedField(20, "LA1and20", StringType(),
                        doc="Low access tract at 1 mile for urban areas and 20 miles for rural "
                            "areas: Flag for low access tract at 1 mile for urban areas or 20 "
                            "miles for rural areas."),
            NestedField(21, "LATracts_half", StringType(),
                        doc="Low access tract at 1/2 mile: Flag for low access tract when "
                            "considering 1/2 mile distance."),
            NestedField(22, "LATracts1", StringType(),
                        doc="Low access tract at 1 mile: Flag for low access tract when considering "
                            "1 mile distance."),
            NestedField(23, "LATracts10", StringType(),
                        doc="Low access tract at 10 miles: Flag for low access tract when "
                            "considering 10 mile distance."),
            NestedField(24, "LATracts20", StringType(),
                        doc="Low access tract at 20 miles: Flag for low access tract when "
                            "considering 20 mile distance."),
            NestedField(25, "LATractsVehicle_20", StringType(),
                        doc="Low access tract using vehicle access and at 20 miles in rural areas: "
                            "Flag for tract where >= 100 of households do not have a vehicle, "
                            "and beyond 1/2 mile from supermarket; or >= 500 individuals are "
                            "beyond 20 miles from supermarket ; or >= 33% of individuals are "
                            "beyond 20 miles from supermarket."),
            NestedField(26, "LAPOP1_10", StringType(),
                        doc="Low access, population at 1 mile for urban areas and 10 miles for rural "
                            "areas, number: Population count beyond 1 mile for urban areas or 10 "
                            "miles for rural areas from supermarket."),
            NestedField(27, "LAPOP05_10", StringType(),
                        doc="Low access, population at 1/2 mile for urban areas and 10 miles for "
                            "rural areas, number: Population count beyond 1/2 mile for urban "
                            "areas or 10 miles for rural areas from supermarket."),
            NestedField(28, "LAPOP1_20", StringType(),
                        doc="Low access, population at 1 mile for urban areas and 20 miles for rural "
                            "areas, number: Population count beyond 1 mile for urban areas or 20 "
                            "miles for rural areas from supermarket."),
            NestedField(29, "LALOWI1_10", StringType(),
                        doc="Low access, low-income population at 1 mile for urban areas and 10 "
                            "miles for rural areas, number: Low income population count beyond 1 "
                            "mile for urban areas or 10 miles for rural areas from supermarket."),
            NestedField(30, "LALOWI05_10", StringType(),
                        doc="Low access, low-income population at 1/2 mile for urban areas and 10 "
                            "miles for rural areas, number: Low income population count beyond "
                            "1/2 mile for urban areas or 10 miles for rural areas from "
                            "supermarket."),
            NestedField(31, "LALOWI1_20", StringType(),
                        doc="Low access, low-income population at 1 mile for urban areas and 20 "
                            "miles for rural areas, number: Low income population count beyond 1 "
                            "mile for urban areas or 20 miles for rural areas from supermarket."),
            NestedField(32, "lapophalf", StringType(),
                        doc="Low access, population at 1/2 mile, number: Population count beyond 1/2 "
                            "mile from supermarket."),
            NestedField(33, "lapophalfshare", StringType(),
                        doc="Low access, population at 1/2 mile, share: Share of tract population "
                            "that are beyond 1/2 mile from supermarket."),
            NestedField(34, "lalowihalf", StringType(),
                        doc="Low access, low-income population at 1/2 mile, number: Low income "
                            "population count beyond 1/2 mile from supermarket."),
            NestedField(35, "lalowihalfshare", StringType(),
                        doc="Low access, low-income population at 1/2 mile, share: Share of tract "
                            "population that are low income individuals beyond 1/2 mile from "
                            "supermarket."),
            NestedField(36, "lakidshalf", StringType(),
                        doc="Low access, children age 0-17 at 1/2 mile, number: Kids population "
                            "count beyond 1/2 mile from supermarket."),
            NestedField(37, "lakidshalfshare", StringType(),
                        doc="Low access, children age 0-17 at 1/2 mile, share: Share of tract "
                            "population that are kids beyond 1/2 mile from supermarket."),
            NestedField(38, "laseniorshalf", StringType(),
                        doc="Low access, seniors age 65+ at 1/2 mile, number: Seniors population "
                            "count beyond 1/2 mile from supermarket."),
            NestedField(39, "laseniorshalfshare", StringType(),
                        doc="Low access, seniors age 65+ at 1/2 mile, share: Share of tract "
                            "population that are seniors beyond 1/2 mile from supermarket."),
            NestedField(40, "lawhitehalf", StringType(),
                        doc="Low access, White population at 1/2 mile, number: White population "
                            "count beyond 1/2 mile from supermarket."),
            NestedField(41, "lawhitehalfshare", StringType(),
                        doc="Low access, White population at 1/2 mile, share: Share of tract "
                            "population that are white beyond 1/2 mile from supermarket."),
            NestedField(42, "lablackhalf", StringType(),
                        doc="Low access, Black or African American population at 1/2 mile, number: "
                            "Black or African American population count beyond 1/2 mile from "
                            "supermarket."),
            NestedField(43, "lablackhalfshare", StringType(),
                        doc="Low access, Black or African American population at 1/2 mile, share: "
                            "Share of tract population that are Black or African American beyond "
                            "1/2 mile from supermarket."),
            NestedField(44, "laasianhalf", StringType(),
                        doc="Low access, Asian population at 1/2 mile, number: Asian population "
                            "count beyond 1/2 mile from supermarket."),
            NestedField(45, "laasianhalfshare", StringType(),
                        doc="Low access, Asian population at 1/2 mile, share: Share of tract "
                            "population that are Asian beyond 1/2 mile from supermarket."),
            NestedField(46, "lanhopihalf", StringType(),
                        doc="Low access, Native Hawaiian or Other Pacific Islander population at 1/2 "
                            "mile, number: Native Hawaiian or Other Pacific Islander population "
                            "count beyond 1/2 mile from supermarket."),
            NestedField(47, "lanhopihalfshare", StringType(),
                        doc="Low access, Native Hawaiian or Other Pacific Islander population at 1/2 "
                            "mile, share: Share of tract population that are Native Hawaiian or "
                            "Other Pacific Islander beyond 1/2 mile from supermarket."),
            NestedField(48, "laaianhalf", StringType(),
                        doc="Low access, American Indian or Alaska Native population at 1/2 mile, "
                            "number: American Indian or Alaska Native population count beyond "
                            "1/2 mile from supermarket."),
            NestedField(49, "laaianhalfshare", StringType(),
                        doc="Low access, American Indian or Alaska Native population at 1/2 mile, "
                            "share: Share of tract population that are American Indian or Alaska "
                            "Native beyond 1/2 mile from supermarket."),
            NestedField(50, "laomultirhalf", StringType(),
                        doc="Low access, Other/Multiple race population at 1/2 mile, number: "
                            "Other/Multiple race population count beyond 1/2 mile from "
                            "supermarket."),
            NestedField(51, "laomultirhalfshare", StringType(),
                        doc="Low access, Other/Multiple race population at 1/2 mile, share: Share of "
                            "tract population that are Other/Multiple race beyond 1/2 mile from "
                            "supermarket."),
            NestedField(52, "lahisphalf", StringType(),
                        doc="Low access, Hispanic or Latino population at 1/2 mile, number: Hispanic "
                            "or Latino ethnicity population count beyond 1/2 mile from "
                            "supermarket."),
            NestedField(53, "lahisphalfshare", StringType(),
                        doc="Low access, Hispanic or Latino population at 1/2 mile, share: Share of "
                            "tract population that are of Hispanic or Latino ethnicity beyond "
                            "1/2 mile from supermarket."),
            NestedField(54, "lahunvhalf", StringType(),
                        doc="Vehicle access, housing units without and low access at 1/2 mile, "
                            "number: Housing units without vehicle count beyond 1/2 mile from "
                            "supermarket."),
            NestedField(55, "lahunvhalfshare", StringType(),
                        doc="Vehicle access, housing units without and low access at 1/2 mile, "
                            "share: Share of tract housing units that are without vehicle and "
                            "beyond 1/2 mile from supermarket."),
            NestedField(56, "lasnaphalf", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 1/2 mile, number: "
                            "Housing units receiving SNAP benefits count beyond 1/2 mile from "
                            "supermarket."),
            NestedField(57, "lasnaphalfshare", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 1/2 mile, share: "
                            "Share of tract housing units receiving SNAP benefits count beyond "
                            "1/2 mile from supermarket."),
            NestedField(58, "lapop1", StringType(),
                        doc="Low access, population at 1 mile, number: Population count beyond 1 "
                            "mile from supermarket."),
            NestedField(59, "lapop1share", StringType(),
                        doc="Low access, population at 1 mile, share: Share of tract population that "
                            "are beyond 1 mile from supermarket."),
            NestedField(60, "lalowi1", StringType(),
                        doc="Low access, low-income population at 1 mile, number: Low income "
                            "population count beyond 1 mile from supermarket."),
            NestedField(61, "lalowi1share", StringType(),
                        doc="Low access, low-income population at 1 mile, share: Share of tract "
                            "population that are low income individuals beyond 1 mile from "
                            "supermarket."),
            NestedField(62, "lakids1", StringType(),
                        doc="Low access, children age 0-17 at 1 mile, number: Kids population count "
                            "beyond 1 mile from supermarket."),
            NestedField(63, "lakids1share", StringType(),
                        doc="Low access, children age 0-17 at 1 mile, share: Share of tract "
                            "population that are kids beyond 1 mile from supermarket."),
            NestedField(64, "laseniors1", StringType(),
                        doc="Low access, seniors age 65+ at 1 mile, number: Seniors population count "
                            "beyond 1 mile from supermarket."),
            NestedField(65, "laseniors1share", StringType(),
                        doc="Low access, seniors age 65+ at 1 mile, share: Share of tract population "
                            "that are seniors beyond 1 mile from supermarket."),
            NestedField(66, "lawhite1", StringType(),
                        doc="Low access, White population at 1 mile, number: White population count "
                            "beyond 1 mile from supermarket."),
            NestedField(67, "lawhite1share", StringType(),
                        doc="Low access, White population at 1 mile, share: Share of tract "
                            "population that are white beyond 1 mile from supermarket."),
            NestedField(68, "lablack1", StringType(),
                        doc="Low access, Black or African American population at 1 mile, number: "
                            "Black or African American population count beyond 1 mile from "
                            "supermarket."),
            NestedField(69, "lablack1share", StringType(),
                        doc="Low access, Black or African American population at 1 mile, share: "
                            "Share of tract population that are Black or African American beyond "
                            "1 mile from supermarket."),
            NestedField(70, "laasian1", StringType(),
                        doc="Low access, Asian population at 1 mile, number: Asian population count "
                            "beyond 1 mile from supermarket."),
            NestedField(71, "laasian1share", StringType(),
                        doc="Low access, Asian population at 1 mile, share: Share of tract "
                            "population that are Asian beyond 1 mile from supermarket."),
            NestedField(72, "lanhopi1", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 1 "
                            "mile, number: Native Hawaiian or Other Pacific Islander population "
                            "count beyond 1 mile from supermarket."),
            NestedField(73, "lanhopi1share", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 1 "
                            "mile, share: Share of tract population that are Native Hawaiian or "
                            "Other Pacific Islander beyond 1 mile from supermarket."),
            NestedField(74, "laaian1", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 1 mile, "
                            "number: American Indian or Alaska Native population count beyond 1 "
                            "mile from supermarket."),
            NestedField(75, "laaian1share", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 1 mile, "
                            "share: Share of tract population that are American Indian or Alaska "
                            "Native beyond 1 mile from supermarket."),
            NestedField(76, "laomultir1", StringType(),
                        doc="Low access, Other/Multiple race population at 1 mile, number: "
                            "Other/Multiple race population count beyond 1 mile from "
                            "supermarket."),
            NestedField(77, "laomultir1share", StringType(),
                        doc="Low access, Other/Multiple race population at 1 mile, share: Share of "
                            "tract population that are Other/Multiple race beyond 1 mile from "
                            "supermarket."),
            NestedField(78, "lahisp1", StringType(),
                        doc="Low access, Hispanic or Latino population at 1 mile, number: Hispanic "
                            "or Latino ethnicity population count beyond 1 mile from "
                            "supermarket."),
            NestedField(79, "lahisp1share", StringType(),
                        doc="Low access, Hispanic or Latino population at 1 mile, share: Share of "
                            "tract population that are of Hispanic or Latino ethnicity beyond 1 "
                            "mile from supermarket."),
            NestedField(80, "lahunv1", StringType(),
                        doc="Vehicle access, housing units without and low access at 1 mile, number: "
                            "Housing units without vehicle count beyond 1 mile from supermarket."),
            NestedField(81, "lahunv1share", StringType(),
                        doc="Vehicle access, housing units without and low access at 1 mile, share: "
                            "Share of tract housing units that are without vehicle and beyond 1 "
                            "mile from supermarket."),
            NestedField(82, "lasnap1", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 1 mile, number: "
                            "Housing units receiving SNAP benefits count beyond 1 mile from "
                            "supermarket."),
            NestedField(83, "lasnap1share", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 1 mile, share: "
                            "Share of tract housing units receiving SNAP benefits count beyond 1 "
                            "mile from supermarket."),
            NestedField(84, "lapop10", StringType(),
                        doc="Low access, population at 10 miles, number: Population count beyond 10 "
                            "miles from supermarket."),
            NestedField(85, "lapop10share", StringType(),
                        doc="Low access, population at 10 miles, share: Share of tract population "
                            "that are beyond 10 miles from supermarket."),
            NestedField(86, "lalowi10", StringType(),
                        doc="Low access, low-income population at 10 miles, number: Low income "
                            "population count beyond 10 miles from supermarket."),
            NestedField(87, "lalowi10share", StringType(),
                        doc="Low access, low-income population at 10 miles, share: Share of tract "
                            "population that are low income individuals beyond 10 miles from "
                            "supermarket."),
            NestedField(88, "lakids10", StringType(),
                        doc="Low access, children age 0-17 at 10 miles, number: Kids population "
                            "count beyond 10 miles from supermarket."),
            NestedField(89, "lakids10share", StringType(),
                        doc="Low access, children age 0-17 at 10 miles, share: Share of tract "
                            "population that are kids beyond 10 miles from supermarket."),
            NestedField(90, "laseniors10", StringType(),
                        doc="Low access, seniors age 65+ at 10 miles, number: Seniors population "
                            "count beyond 10 miles from supermarket."),
            NestedField(91, "laseniors10share", StringType(),
                        doc="Low access, seniors age 65+ at 10 miles, share: Share of tract "
                            "population that are seniors beyond 10 miles from supermarket."),
            NestedField(92, "lawhite10", StringType(),
                        doc="Low access, White population at 10 miles, number: White population "
                            "count beyond 10 miles from supermarket."),
            NestedField(93, "lawhite10share", StringType(),
                        doc="Low access, White population at 10 miles, share: Share of tract "
                            "population that are white beyond 10 miles from supermarket."),
            NestedField(94, "lablack10", StringType(),
                        doc="Low access, Black or African American population at 10 miles, number: "
                            "Black or African American population count beyond 10 miles from "
                            "supermarket."),
            NestedField(95, "lablack10share", StringType(),
                        doc="Low access, Black or African American population at 10 miles, share: "
                            "Share of tract population that are Black or African American beyond "
                            "10 miles from supermarket."),
            NestedField(96, "laasian10", StringType(),
                        doc="Low access, Asian population at 10 miles, number: Asian population "
                            "count beyond 10 miles from supermarket."),
            NestedField(97, "laasian10share", StringType(),
                        doc="Low access, Asian population at 10 miles, share: Share of tract "
                            "population that are Asian beyond 10 miles from supermarket."),
            NestedField(98, "lanhopi10", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 10 "
                            "miles, number: Native Hawaiian or Other Pacific Islander population "
                            "count beyond 10 miles from supermarket."),
            NestedField(99, "lanhopi10share", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 10 "
                            "miles, share: Share of tract population that are Native Hawaiian or "
                            "Other Pacific Islander beyond 10 miles from supermarket."),
            NestedField(100, "laaian10", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 10 miles, "
                            "number: American Indian or Alaska Native population count beyond 10 "
                            "miles from supermarket."),
            NestedField(101, "laaian10share", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 10 miles, "
                            "share: Share of tract population that are American Indian or Alaska "
                            "Native beyond 10 miles from supermarket."),
            NestedField(102, "laomultir10", StringType(),
                        doc="Low access, Other/Multiple race population at 10 miles, number: "
                            "Other/Multiple race population count beyond 10 miles from "
                            "supermarket."),
            NestedField(103, "laomultir10share", StringType(),
                        doc="Low access, Other/Multiple race population at 10 miles, share: Share of "
                            "tract population that are Other/Multiple race beyond 10 miles from "
                            "supermarket."),
            NestedField(104, "lahisp10", StringType(),
                        doc="Low access, Hispanic or Latino population at 10 miles, number: Hispanic "
                            "or Latino ethnicity population count beyond 10 miles from "
                            "supermarket."),
            NestedField(105, "lahisp10share", StringType(),
                        doc="Low access, Hispanic or Latino population at 10 miles, share: Share of "
                            "tract population that are of Hispanic or Latino ethnicity beyond 10 "
                            "miles from supermarket."),
            NestedField(106, "lahunv10", StringType(),
                        doc="Vehicle access, housing units without and low access at 10 miles, "
                            "number: Housing units without vehicle count beyond 10 miles from "
                            "supermarket."),
            NestedField(107, "lahunv10share", StringType(),
                        doc="Vehicle access, housing units without and low access at 10 miles, "
                            "share: Share of tract housing units that are without vehicle and "
                            "beyond 10 miles from supermarket."),
            NestedField(108, "lasnap10", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 10 miles, number: "
                            "Housing units receiving SNAP benefits count beyond 10 miles from "
                            "supermarket."),
            NestedField(109, "lasnap10share", StringType(),
                        doc="Low access,housing units receiving SNAP benefits at 10 miles, share: "
                            "Share of tract housing units receiving SNAP benefits count beyond "
                            "10 miles from supermarket."),
            NestedField(110, "lapop20", StringType(),
                        doc="Low access, population at 20 miles, number: Population count beyond 20 "
                            "miles from supermarket."),
            NestedField(111, "lapop20share", StringType(),
                        doc="Low access, population at 20 miles, share: Share of tract population "
                            "that are beyond 20 miles from supermarket."),
            NestedField(112, "lalowi20", StringType(),
                        doc="Low access, low-income population at 20 miles, number: Low income "
                            "population count beyond 20 miles from supermarket."),
            NestedField(113, "lalowi20share", StringType(),
                        doc="Low access, low-income population at 20 miles, share: Share of tract "
                            "population that are low income individuals beyond 20 miles from "
                            "supermarket."),
            NestedField(114, "lakids20", StringType(),
                        doc="Low access, children age 0-17 at 20 miles, number: Kids population "
                            "count beyond 20 miles from supermarket."),
            NestedField(115, "lakids20share", StringType(),
                        doc="Low access, children age 0-17 at 20 miles, share: Share of tract "
                            "population that are kids beyond 20 miles from supermarket."),
            NestedField(116, "laseniors20", StringType(),
                        doc="Low access, seniors age 65+ at 20 miles, number: Seniors population "
                            "count beyond 20 miles from supermarket."),
            NestedField(117, "laseniors20share", StringType(),
                        doc="Low access, seniors age 65+ at 20 miles, share: Share of tract "
                            "population that are seniors beyond 20 miles from supermarket."),
            NestedField(118, "lawhite20", StringType(),
                        doc="Low access, White population at 20 miles, number: White population "
                            "count beyond 20 miles from supermarket."),
            NestedField(119, "lawhite20share", StringType(),
                        doc="Low access, White population at 20 miles, share: Share of tract "
                            "population that are white beyond 20 miles from supermarket."),
            NestedField(120, "lablack20", StringType(),
                        doc="Low access, Black or African American population at 20 miles, number: "
                            "Black or African American population count beyond 20 miles from "
                            "supermarket."),
            NestedField(121, "lablack20share", StringType(),
                        doc="Low access, Black or African American population at 20 miles, share: "
                            "Share of tract population that are Black or African American beyond "
                            "20 miles from supermarket."),
            NestedField(122, "laasian20", StringType(),
                        doc="Low access, Asian population at 20 miles, number: Asian population "
                            "count beyond 20 miles from supermarket."),
            NestedField(123, "laasian20share", StringType(),
                        doc="Low access, Asian population at 20 miles, share: Share of tract "
                            "population that are Asian beyond 20 miles from supermarket."),
            NestedField(124, "lanhopi20", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 20 "
                            "miles, number: Native Hawaiian or Other Pacific Islander population "
                            "count beyond 20 miles from supermarket."),
            NestedField(125, "lanhopi20share", StringType(),
                        doc="Low access, Native Hawaiian and Other Pacific Islander population at 20 "
                            "miles, share: Share of tract population that are Native Hawaiian or "
                            "Other Pacific Islander beyond 20 miles from supermarket."),
            NestedField(126, "laaian20", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 20 miles, "
                            "number: American Indian or Alaska Native population count beyond 20 "
                            "miles from supermarket."),
            NestedField(127, "laaian20share", StringType(),
                        doc="Low access, American Indian and Alaska Native population at 20 miles, "
                            "share: Share of tract population that are American Indian or Alaska "
                            "Native beyond 20 miles from supermarket."),
            NestedField(128, "laomultir20", StringType(),
                        doc="Low access, Other/Multiple race population at 20 miles, number: "
                            "Other/Multiple race population count beyond 20 miles from "
                            "supermarket."),
            NestedField(129, "laomultir20share", StringType(),
                        doc="Low access, Other/Multiple race population at 20 miles, share: Share of "
                            "tract population that are Other/Multiple race beyond 20 miles from "
                            "supermarket."),
            NestedField(130, "lahisp20", StringType(),
                        doc="Low access, Hispanic or Latino population at 20 miles, number: Hispanic "
                            "or Latino ethnicity population count beyond 20 miles from "
                            "supermarket."),
            NestedField(131, "lahisp20share", StringType(),
                        doc="Low access, Hispanic or Latino population at 20 miles, share: Share of "
                            "tract population that are of Hispanic or Latino ethnicity beyond 20 "
                            "miles from supermarket."),
            NestedField(132, "lahunv20", StringType(),
                        doc="Vehicle access, housing units without and low access at 20 miles, "
                            "number: Housing units without vehicle count beyond 20 miles from "
                            "supermarket."),
            NestedField(133, "lahunv20share", StringType(),
                        doc="Vehicle access, housing units without and low access at 20 miles, "
                            "share: Share of tract housing units that are without vehicle and "
                            "beyond 20 miles from supermarket."),
            NestedField(134, "lasnap20", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 20 miles, number: "
                            "Housing units receiving SNAP benefits count beyond 20 miles from "
                            "supermarket."),
            NestedField(135, "lasnap20share", StringType(),
                        doc="Low access, housing units receiving SNAP benefits at 20 miles, share: "
                            "Share of tract housing units receiving SNAP benefits count beyond "
                            "20 miles from supermarket."),
            NestedField(136, "TractLOWI", StringType(),
                        doc="Tract low-income population, number: Total count of low-income "
                            "population in tract."),
            NestedField(137, "TractKids", StringType(),
                        doc="Tract children age 0-17, number: Total count of children age 0-17 in "
                            "tract."),
            NestedField(138, "TractSeniors", StringType(),
                        doc="Tract seniors age 65+, number: Total count of seniors age 65+ in tract."),
            NestedField(139, "TractWhite", StringType(),
                        doc="Tract White population, number: Total count of White population in "
                            "tract."),
            NestedField(140, "TractBlack", StringType(),
                        doc="Tract Black or African American population, number: Total count of "
                            "Black or African American population in tract."),
            NestedField(141, "TractAsian", StringType(),
                        doc="Tract Asian population, number: Total count of Asian population in "
                            "tract."),
            NestedField(142, "TractNHOPI", StringType(),
                        doc="Tract Native Hawaiian and Other Pacific Islander population, number: "
                            "Total count of Native Hawaiian and Other Pacific Islander "
                            "population in tract."),
            NestedField(143, "TractAIAN", StringType(),
                        doc="Tract American Indian and Alaska Native population, number: Total count "
                            "of American Indian and Alaska Native population in tract."),
            NestedField(144, "TractOMultir", StringType(),
                        doc="Tract Other/Multiple race population, number: Total count of "
                            "Other/Multiple race population in tract."),
            NestedField(145, "TractHispanic", StringType(),
                        doc="Tract Hispanic or Latino population, number: Total count of Hispanic or "
                            "Latino population in tract."),
            NestedField(146, "TractHUNV", StringType(),
                        doc="Tract housing units without a vehicle, number: Total count of housing "
                            "units without a vehicle in tract."),
            NestedField(147, "TractSNAP", StringType(),
                        doc="Tract housing units receiving SNAP benefits, number: Total count of "
                            "housing units receiving SNAP benefits in tract."),

            NestedField(148, "atlas_edition", StringType(), required=True,
                        doc="The Food Access Research Atlas edition this row was published "
                            "in, e.g. '2019' -- the version axis for this source (SPEC.md "
                            "Versioning model). Raw is replaced wholesale per value of this "
                            "column."),
            NestedField(149, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("atlas_edition", "CensusTract"),
        comment="USDA ERS Food Access Research Atlas landed verbatim and whole, every "
                "published column, one row per census tract per edition (SPEC.md § "
                "Sources -- first tranche). 2019 and 2015 editions only -- 2010 is a real "
                "third layout and the archived 2006 Food Desert Locator is a legacy .xls "
                "(see ers_food_access.py docstring). Public domain (17 U.S.C. § 105).",
    ),

    # --- raw: hrsa ahrf ---
    # HRSA Area Health Resources Files, county (#39). Landed LONG (one row per
    # county/field cell) rather than one wide row per county -- see
    # hrsa_ahrf.py's docstring for why, and for the licence finding that
    # excludes every AMA/AHA/ADA-sourced column (physician-specialty and
    # hospital/bed counts) from this table entirely.
    "raw.hrsa__ahrf": TableDef(
        schema=Schema(
            NestedField(1, "ahrf_release", StringType(), required=True,
                        doc="The AHRF release label, e.g. '2024-2025' -- the version column; "
                            "raw is replaced wholesale per value of this column. NOT the year a "
                            "value describes: one release carries many data years, encoded in "
                            "column_name's own numeric suffix (see hrsa_ahrf.py's docstring)."),
            NestedField(2, "file", StringType(), required=True,
                        doc="Which landed source file this cell came from, e.g. 'AHRF2025.csv'."),
            NestedField(3, "fips", StringType(), required=True,
                        doc="5-digit county (or county-equivalent) FIPS code, as published -- "
                            "zero-padded string, e.g. '01001'."),
            NestedField(4, "column_name", StringType(), required=True,
                        doc="The AHRF field name this cell's value came from, verbatim, e.g. "
                            "'fedly_qualfd_hlth_ctr_24'. Only fields NOT sourced from the AMA "
                            "Physician Masterfile, the AHA hospital survey, or the ADA Masterfile "
                            "are landed here -- those are copyrighted third-party content and are "
                            "excluded before landing, never reproduced in any namespace."),
            NestedField(5, "value", StringType(),
                        doc="The cell's raw string value, unparsed. NULL only where the source "
                            "CSV cell itself was blank -- AHRF's own missing-data marker for "
                            "these fields; a real reported 0 is never read as missing."),
            NestedField(6, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed this row."),
        ),
        sort_by=("ahrf_release", "fips", "column_name"),
        comment="HRSA Area Health Resources Files (AHRF), county level, landed LONG: one row "
                "per (county, field) cell rather than one enormously wide row per county "
                "(SPEC.md § Sources -- first tranche). Landed WHOLE for every field not withheld "
                "on licence grounds -- 'whole' as SPEC.md ADR-0002 uses it, at cell granularity "
                "rather than one wide table; public-domain federal-origin fields only. Licence: "
                "U.S. government work, public domain (17 U.S.C. Sec 105) for every field landed "
                "here. Fields sourced from the AMA Physician Masterfile, AHA hospital survey, or "
                "ADA Masterfile are copyrighted third-party content and are never landed.",
    ),

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
        sort_by=("retrieved_on", "BPHC Assigned Number"),
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
        sort_by=("retrieved_on", "HPSA ID"),
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
        sort_by=("source", "kind", "geo_id", "facility_id"),
        comment="Places care happens (SPEC.md § Facilities). First writer: HRSA_HC (health-center "
                "service delivery / look-alike sites). Full Type-2 history via valid_from/valid_to -- a "
                "site absent from a later snapshot is retired by the merge, which is the point for a "
                "source that only ever serves today's list.",
    ),

    # --- raw: state cancer profiles ---
    # State Cancer Profiles, all vintages (#27). scp.py.
    #
    # SCP_PROVENANCE (repeated in each table's comment, #65): this is a REPUBLISHED
    # SCRAPE, not the original archive. statecancerprofiles.cancer.gov (NCI/CDC) has
    # no API, bulk download or archive, so seandavi/state-cancer-profile-scraper
    # scrapes it and republishes each captured vintage as a versioned Zenodo deposit,
    # read here over anonymous HTTPS. The underlying content is a U.S. Government
    # work, public domain under 17 U.S.C. Sec 105; not endorsed by NCI/CDC.
    "raw.scp__incidence": TableDef(
        schema=Schema(
            NestedField(1, "reported_locale", StringType(),
                        doc="County/state name exactly as SCP renders it, including any "
                            "footnote markers glued on (e.g. 'Allen County, Kansas(2)'); "
                            "see locale/state for the plain name."),
            NestedField(2, "fips", StringType(),
                        doc="5-digit FIPS code as published: a county code, a state code "
                            "padded '<state>000', or the national sentinel '00000'."),
            NestedField(3, "2023_rural_urban_continuum_codesrural_urban_note", StringType(),
                        doc="SCP's own two header cells concatenated with no separator by "
                            "the upstream export (a verbatim upstream defect, not a scrape "
                            "artifact) -- rural/urban classification per the 2023 USDA "
                            "RUCC (2013 codes for Connecticut). NULL in V1/V2, which "
                            "predate this column."),
            NestedField(4, "age_adjusted_rate_per_100_000", StringType(),
                        doc="The published incidence rate, age-adjusted to the 2000 US "
                            "standard population. NULL whenever the cell is suppressed "
                            "or withheld."),
            NestedField(5, "lower_ci_rate", StringType(),
                        doc="95% CI lower bound, or the literal sentinel '*' when the "
                            "rate is suppressed -- landed as text so the sentinel "
                            "survives verbatim; TRY_CAST in transform turns it into a "
                            "clean NULL."),
            NestedField(6, "upper_ci_rate", StringType(),
                        doc="95% CI upper bound, or '*' when suppressed (see "
                            "lower_ci_rate)."),
            NestedField(7, "ci_rank", StringType(),
                        doc="SCP's CI*Rank statistic; only populated for 'By State' rows "
                            "from the 2026-05-28+ scrape. Landed for reference, not "
                            "derived."),
            NestedField(8, "lower_ci_rank", StringType(),
                        doc="Lower bound of ci_rank's interval; see ci_rank."),
            NestedField(9, "upper_ci_rank", StringType(),
                        doc="Upper bound of ci_rank's interval; see ci_rank."),
            NestedField(10, "average_annual_count", StringType(),
                        doc="Average annual case count for the period, or NULL when "
                            "suppressed. A small rounded value (e.g. '3') is not itself "
                            "evidence of suppression -- SCP rounds counts of 16+ that "
                            "are still below reporting precision; only "
                            "suppression_reason / a NULL rate means suppressed."),
            NestedField(11, "recent_trend", StringType(),
                        doc="SCP's trend call ('stable'/'rising'/'falling'), or a text "
                            "marker: '*' (trend not computable/suppressed) or "
                            "'[P1 note]' (state-law withholding). Landed verbatim; both "
                            "markers become trend=NULL in measure.observation."),
            NestedField(12, "recent_5_year_trend_in_rate", StringType(),
                        doc="Average Annual Percent Change (AAPC) point estimate, or "
                            "the '*'/'[P1 note]' markers (see recent_trend). Landed for "
                            "reference, not derived."),
            NestedField(13, "lower_ci_trend_in_rate", StringType(),
                        doc="AAPC's 95% CI lower bound; see recent_5_year_trend_in_rate."),
            NestedField(14, "upper_ci_trend_in_rate", StringType(),
                        doc="AAPC's 95% CI upper bound; see recent_5_year_trend_in_rate."),
            NestedField(15, "year", StringType(),
                        doc="SCP's own period label -- always the literal constant "
                            "'Latest 5-year average' in every row of every vintage "
                            "(verified); carries no real year. See scp.py's module "
                            "docstring for where the real period comes from."),
            NestedField(16, "sex", StringType(),
                        doc="'Both Sexes' | 'Male' | 'Female', as published."),
            NestedField(17, "stage", StringType(),
                        doc="'All Stages' | 'Late Stage (Regional & Distant)', as "
                            "published."),
            NestedField(18, "race", StringType(),
                        doc="Source-native race/ethnicity category, as published (6 "
                            "values; see the scraper's select_options.json)."),
            NestedField(19, "cancer", StringType(),
                        doc="SCP's cancer-site category label, as published (23 values, "
                            "including combined categories like 'Colon & Rectum' -- see "
                            "select_options.json and scp.py's SEER bridge)."),
            NestedField(20, "areatype", StringType(),
                        doc="SCP's query-mode flag ('By County'/'By State'); NOT "
                            "reliable for a row's own geographic level -- constant "
                            "'By County' even on the national aggregate row in V1. See "
                            "locale_type."),
            NestedField(21, "age", StringType(),
                        doc="Source-native age group, as published (7 values; see "
                            "select_options.json)."),
            NestedField(22, "state_fips", StringType(),
                        doc="2-digit state FIPS code, or '00' for the national row."),
            NestedField(23, "measurement", StringType(),
                        doc="Topic marker, constant 'incidence' for this table."),
            NestedField(24, "locale_type", StringType(),
                        doc="SCP's own geography-type tag ('county'/'state'/'national'/"
                            "'other'); NOT reliable for a row's own geographic level -- "
                            "known to misclassify real counties (Louisiana parishes, "
                            "Alaska boroughs, DC, Puerto Rico) as 'other'. Geo level is "
                            "derived from fips's own shape instead (scp.py's module "
                            "docstring)."),
            NestedField(25, "_extracted_at", StringType(),
                        doc="ISO timestamp the scraper recorded for this row's capture."),
            NestedField(26, "url", StringType(),
                        doc="The exact statecancerprofiles.cancer.gov query URL the "
                            "scraper captured this row from."),
            NestedField(27, "suppression_reason", StringType(),
                        doc="'suppressed_small_count' | 'withheld_state_law', or NULL "
                            "when reported. Absent from V1/V2 (NULL on every row there) "
                            "-- those vintages carry no suppression marker at all "
                            "(scp.py's module docstring); V3's own two values are the "
                            "complete enum found in the real files -- an unmapped value "
                            "raises SystemExit in transform rather than a guess."),
            NestedField(28, "percent_of_cases_with_late_stage", StringType(),
                        doc="A separate published statistic (not this measure's rate); "
                            "landed for reference, not derived."),
            NestedField(29, "locale", StringType(),
                        doc="Plain county/state name, footnote markers stripped "
                            "(compare reported_locale)."),
            NestedField(30, "state", StringType(),
                        doc="Full state name, or NULL for a county-level row (the "
                            "state is implied by fips)."),
            NestedField(31, "scp_vintage", StringType(), required=True,
                        doc="Which vintage this row belongs to: 'V1' | 'V2' | 'V3' "
                            "(scp.py's module docstring). Raw is replaced wholesale per "
                            "value of this column."),
            NestedField(32, "landed_in", StringType(), required=True,
                        doc="cancerOnIce release that landed this row."),
        ),
        sort_by=("scp_vintage", "fips", "cancer", "sex"),
        comment="State Cancer Profiles incidence, landed verbatim and whole, one row per "
                "(geography, cancer site, sex, age, race, stage) query cell, per vintage "
                "(SPEC.md § Sources -- first tranche, the flagship, #27). This is a "
                "REPUBLISHED SCRAPE, not the original archive: statecancerprofiles."
                "cancer.gov (NCI/CDC) has no API, bulk download or archive, so "
                "seandavi/state-cancer-profile-scraper scrapes it and republishes each "
                "captured vintage as a versioned Zenodo deposit, read here over anonymous "
                "HTTPS (#65). The underlying content is a U.S. Government work, public "
                "domain under 17 U.S.C. Sec 105; not endorsed by NCI/CDC.",
    ),

    "raw.scp__mortality": TableDef(
        schema=Schema(
            NestedField(1, "reported_locale", StringType(),
                        doc="County/state name exactly as SCP renders it, including any "
                            "footnote markers glued on (e.g. 'Allen County, Kansas(2)'); "
                            "see locale/state for the plain name."),
            NestedField(2, "fips", StringType(),
                        doc="5-digit FIPS code as published: a county code, a state code "
                            "padded '<state>000', or the national sentinel '00000'."),
            NestedField(3, "2023_rural_urban_continuum_codesrural_urban_note", StringType(),
                        doc="SCP's own two header cells concatenated with no separator by "
                            "the upstream export (a verbatim upstream defect, not a scrape "
                            "artifact) -- rural/urban classification per the 2023 USDA "
                            "RUCC (2013 codes for Connecticut). NULL in V1/V2, which "
                            "predate this column."),
            NestedField(4, "age_adjusted_rate_per_100_000", StringType(),
                        doc="The published death rate, age-adjusted to the 2000 US "
                            "standard population. NULL whenever the cell is suppressed "
                            "or withheld."),
            NestedField(5, "lower_ci_rate", StringType(),
                        doc="95% CI lower bound, or the literal sentinel '*' when the "
                            "rate is suppressed -- landed as text so the sentinel "
                            "survives verbatim; TRY_CAST in transform turns it into a "
                            "clean NULL."),
            NestedField(6, "upper_ci_rate", StringType(),
                        doc="95% CI upper bound, or '*' when suppressed (see "
                            "lower_ci_rate)."),
            NestedField(7, "ci_rank", StringType(),
                        doc="SCP's CI*Rank statistic; only populated for 'By State' rows "
                            "from the 2026-05-28+ scrape. Landed for reference, not "
                            "derived."),
            NestedField(8, "lower_ci_rank", StringType(),
                        doc="Lower bound of ci_rank's interval; see ci_rank."),
            NestedField(9, "upper_ci_rank", StringType(),
                        doc="Upper bound of ci_rank's interval; see ci_rank."),
            NestedField(10, "average_annual_count", StringType(),
                        doc="Average annual death count for the period, or NULL when "
                            "suppressed. A small rounded value (e.g. '3') is not itself "
                            "evidence of suppression -- SCP rounds counts of 16+ that "
                            "are still below reporting precision; only "
                            "suppression_reason / a NULL rate means suppressed."),
            NestedField(11, "recent_trend", StringType(),
                        doc="SCP's trend call ('stable'/'rising'/'falling'), or a text "
                            "marker: '*' (trend not computable/suppressed) or "
                            "'[P1 note]' (state-law withholding). Landed verbatim; both "
                            "markers become trend=NULL in measure.observation."),
            NestedField(12, "recent_5_year_trend_in_rate", StringType(),
                        doc="Average Annual Percent Change (AAPC) point estimate, or "
                            "the '*'/'[P1 note]' markers (see recent_trend). Landed for "
                            "reference, not derived."),
            NestedField(13, "lower_ci_trend_in_rate", StringType(),
                        doc="AAPC's 95% CI lower bound; see recent_5_year_trend_in_rate."),
            NestedField(14, "upper_ci_trend_in_rate", StringType(),
                        doc="AAPC's 95% CI upper bound; see recent_5_year_trend_in_rate."),
            NestedField(15, "year", StringType(),
                        doc="SCP's own period label -- always the literal constant "
                            "'Latest 5-year average' in every row of every vintage "
                            "(verified); carries no real year. See scp.py's module "
                            "docstring for where the real period comes from."),
            NestedField(16, "sex", StringType(),
                        doc="'Both Sexes' | 'Male' | 'Female', as published."),
            NestedField(17, "stage", StringType(),
                        doc="'All Stages' | 'Late Stage (Regional & Distant)', as "
                            "published. Absent from V3 (NULL on every V3 row) -- SCP "
                            "dropped this dimension from mortality starting with the "
                            "2026-08-24 scrape."),
            NestedField(18, "race", StringType(),
                        doc="Source-native race/ethnicity category, as published (6 "
                            "values; see the scraper's select_options.json)."),
            NestedField(19, "cancer", StringType(),
                        doc="SCP's cancer-site category label, as published (23 values, "
                            "including combined categories like 'Colon & Rectum' -- see "
                            "select_options.json and scp.py's SEER bridge)."),
            NestedField(20, "areatype", StringType(),
                        doc="SCP's query-mode flag ('By County'/'By State'); NOT "
                            "reliable for a row's own geographic level -- constant "
                            "'By County' even on the national aggregate row in V1. See "
                            "locale_type."),
            NestedField(21, "age", StringType(),
                        doc="Source-native age group, as published (7 values; see "
                            "select_options.json)."),
            NestedField(22, "state_fips", StringType(),
                        doc="2-digit state FIPS code, or '00' for the national row."),
            NestedField(23, "measurement", StringType(),
                        doc="Topic marker, constant 'mortality' for this table."),
            NestedField(24, "locale_type", StringType(),
                        doc="SCP's own geography-type tag ('county'/'state'/'national'/"
                            "'other'); NOT reliable for a row's own geographic level -- "
                            "known to misclassify real counties (Louisiana parishes, "
                            "Alaska boroughs, DC, Puerto Rico) as 'other'. Geo level is "
                            "derived from fips's own shape instead (scp.py's module "
                            "docstring)."),
            NestedField(25, "_extracted_at", StringType(),
                        doc="ISO timestamp the scraper recorded for this row's capture."),
            NestedField(26, "url", StringType(),
                        doc="The exact statecancerprofiles.cancer.gov query URL the "
                            "scraper captured this row from."),
            NestedField(27, "suppression_reason", StringType(),
                        doc="'suppressed_small_count' | 'withheld_state_law', or NULL "
                            "when reported. Absent from V1/V2 (NULL on every row there) "
                            "-- those vintages carry no suppression marker at all "
                            "(scp.py's module docstring); V3's own two values are the "
                            "complete enum found in the real files -- an unmapped value "
                            "raises SystemExit in transform rather than a guess."),
            NestedField(28, "locale", StringType(),
                        doc="Plain county/state name, footnote markers stripped "
                            "(compare reported_locale)."),
            NestedField(29, "state", StringType(),
                        doc="Full state name, or NULL for a county-level row (the "
                            "state is implied by fips)."),
            NestedField(30, "scp_vintage", StringType(), required=True,
                        doc="Which vintage this row belongs to: 'V1' | 'V2' | 'V3' "
                            "(scp.py's module docstring). Raw is replaced wholesale per "
                            "value of this column."),
            NestedField(31, "landed_in", StringType(), required=True,
                        doc="cancerOnIce release that landed this row."),
        ),
        sort_by=("scp_vintage", "fips", "cancer", "sex"),
        comment="State Cancer Profiles mortality, landed verbatim and whole, one row per "
                "(geography, cancer site, sex, age, race[, stage]) query cell, per "
                "vintage (SPEC.md § Sources -- first tranche, the flagship, #27). This is "
                "a REPUBLISHED SCRAPE, not the original archive: statecancerprofiles."
                "cancer.gov (NCI/CDC) has no API, bulk download or archive, so "
                "seandavi/state-cancer-profile-scraper scrapes it and republishes each "
                "captured vintage as a versioned Zenodo deposit, read here over anonymous "
                "HTTPS (#65). The underlying content is a U.S. Government work, public "
                "domain under 17 U.S.C. Sec 105; not endorsed by NCI/CDC.",
    ),

    "raw.scp__risk": TableDef(
        schema=Schema(
            NestedField(1, "reported_locale", StringType(),
                        doc="County/state name exactly as SCP renders it, including any "
                            "footnote markers glued on; see locale/state for the plain "
                            "name."),
            NestedField(2, "fips", StringType(),
                        doc="5-digit FIPS code: a real county code for the District of "
                            "Columbia/Puerto Rico rows only (BRFSS risk factors are not "
                            "tabulated for ordinary US counties), a state code padded "
                            "'<state>000', or the national sentinel '00000'."),
            NestedField(3, "percent", StringType(),
                        doc="The published BRFSS prevalence percent. NULL when "
                            "suppressed."),
            NestedField(4, "lower_ci_percent", StringType(),
                        doc="95% CI lower bound, or NULL when suppressed."),
            NestedField(5, "upper_ci_percent", StringType(),
                        doc="95% CI upper bound, or NULL when suppressed."),
            NestedField(6, "respondents", StringType(),
                        doc="BRFSS survey respondent count underlying this estimate."),
            NestedField(7, "topic", StringType(),
                        doc="Short topic-group code, e.g. 'smoke', 'colorec', 'women', "
                            "'men', 'alcohol', 'dietex', 'vaccine'."),
            NestedField(8, "topic_label", StringType(),
                        doc="Human-readable topic group, e.g. 'Smoking', 'Colorectal "
                            "Screening'."),
            NestedField(9, "risk", StringType(),
                        doc="Short code for the specific risk/screening measure, e.g. "
                            "'v505'."),
            NestedField(10, "risk_label", StringType(),
                        doc="Human-readable label for the measure, e.g. 'Binge drinking "
                            "(4+ drinks on one occasion for women, 5+ ... ), ages 21+'."),
            NestedField(11, "race", StringType(),
                        doc="Source-native race/ethnicity category, as published -- a "
                            "DIFFERENT vocabulary than incidence/mortality's race column "
                            "(includes a verbatim upstream misspelling, 'Asian /Pacifice "
                            "Islander (Non-Hispanic)'); never harmonised."),
            NestedField(12, "sex", StringType(),
                        doc="'Both Sexes' | 'Male' | 'Female', as published."),
            NestedField(13, "datatype", StringType(),
                        doc="Estimation method tag; always 'Direct Estimates' in the "
                            "landed file."),
            NestedField(14, "statefips_query", StringType(),
                        doc="The state FIPS value SCP's own query form used to produce "
                            "this row (not necessarily this row's own geography)."),
            NestedField(15, "state_fips", StringType(),
                        doc="2-digit state FIPS code, or '00' for the national row."),
            NestedField(16, "locale_type", StringType(),
                        doc="SCP's own geography-type tag; real county-level rows exist "
                            "only for DC/Puerto Rico (BRFSS has no ordinary county "
                            "estimates) -- see scp.py's module docstring ponytail note "
                            "on why this topic isn't derived yet."),
            NestedField(17, "_extracted_at", StringType(),
                        doc="ISO timestamp the scraper recorded for this row's capture."),
            NestedField(18, "url", StringType(),
                        doc="The exact statecancerprofiles.cancer.gov query URL the "
                            "scraper captured this row from."),
            NestedField(19, "suppression_reason", StringType(),
                        doc="'suppressed_small_count', or NULL when reported (the "
                            "complete enum found in the real V3 risk file; an unmapped "
                            "value would raise SystemExit if this topic were derived)."),
            NestedField(20, "model_based_percent3", StringType(),
                        doc="A separate model-based estimate variant published "
                            "alongside the direct estimate; landed for reference, not "
                            "derived."),
            NestedField(21, "scp_vintage", StringType(), required=True,
                        doc="Which vintage this row belongs to; always 'V3' -- risk is "
                            "not published in V1/V2 (scp.py's module docstring). Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(22, "landed_in", StringType(), required=True,
                        doc="cancerOnIce release that landed this row."),
        ),
        sort_by=("scp_vintage", "fips", "topic", "sex"),
        comment="State Cancer Profiles screening & risk factors (BRFSS-derived), landed "
                "verbatim and whole, V3 only -- not published in V1/V2 (SPEC.md § "
                "Sources, #27). Land-only in this PR: not yet derived into "
                "measure.observation (scp.py's module docstring ponytail note -- its "
                "geography and race vocabulary need their own investigation before "
                "deriving it safely). This is a REPUBLISHED SCRAPE, not the original "
                "archive: statecancerprofiles.cancer.gov (NCI/CDC) has no API, bulk "
                "download or archive, so seandavi/state-cancer-profile-scraper scrapes "
                "it and republishes each captured vintage as a versioned Zenodo deposit, "
                "read here over anonymous HTTPS (#65). The underlying content is a U.S. "
                "Government work, public domain under 17 U.S.C. Sec 105; not endorsed "
                "by NCI/CDC.",
    ),

    # --- raw: census acs ---
    # ACS 5-year, the Cancer InFocus indicator subset (#29).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.
    **{
        f"raw.acs__{_level}": TableDef(
            schema=Schema(
                NestedField(1, "geo_id", StringType(), required=True,
                            doc="Canonical geo_id: 'county:<5-digit FIPS>' or "
                                "'tract:<11-digit FIPS>', matching the raw table's own level."),
                NestedField(2, "acs_year", IntegerType(), required=True,
                            doc="The 5-year release's end year (e.g. 2023 for the 2019-2023 "
                                "release) -- the version column raw is scoped and overwritten by."),
                NestedField(3, "landed_in", StringType(), required=True,
                            doc="The cancerOnIce release whose ingest landed these rows."),
                NestedField(4, "B01003_E001", StringType(), doc="ACS estimate (count): Total (Total Population)."),
                NestedField(5, "B01003_M001", StringType(), doc="Margin of error (90% confidence) for the paired B01003_E001 estimate."),
                NestedField(6, "B09001_E001", StringType(), doc="ACS estimate (count): Total (Population Under 18 Years by Age)."),
                NestedField(7, "B09001_M001", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E001 estimate."),
                NestedField(8, "B09001_E002", StringType(), doc="ACS estimate (count): Total > In households (Population Under 18 Years by Age)."),
                NestedField(9, "B09001_M002", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E002 estimate."),
                NestedField(10, "B09001_E003", StringType(), doc="ACS estimate (count): Total > In households > Under 3 years (Population Under 18 Years by Age)."),
                NestedField(11, "B09001_M003", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E003 estimate."),
                NestedField(12, "B09001_E004", StringType(), doc="ACS estimate (count): Total > In households > 3 and 4 years (Population Under 18 Years by Age)."),
                NestedField(13, "B09001_M004", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E004 estimate."),
                NestedField(14, "B09001_E005", StringType(), doc="ACS estimate (count): Total > In households > 5 years (Population Under 18 Years by Age)."),
                NestedField(15, "B09001_M005", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E005 estimate."),
                NestedField(16, "B09001_E006", StringType(), doc="ACS estimate (count): Total > In households > 6 to 8 years (Population Under 18 Years by Age)."),
                NestedField(17, "B09001_M006", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E006 estimate."),
                NestedField(18, "B09001_E007", StringType(), doc="ACS estimate (count): Total > In households > 9 to 11 years (Population Under 18 Years by Age)."),
                NestedField(19, "B09001_M007", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E007 estimate."),
                NestedField(20, "B09001_E008", StringType(), doc="ACS estimate (count): Total > In households > 12 to 14 years (Population Under 18 Years by Age)."),
                NestedField(21, "B09001_M008", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E008 estimate."),
                NestedField(22, "B09001_E009", StringType(), doc="ACS estimate (count): Total > In households > 15 to 17 years (Population Under 18 Years by Age)."),
                NestedField(23, "B09001_M009", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E009 estimate."),
                NestedField(24, "B09001_E010", StringType(), doc="ACS estimate (count): Total > In group quarters (Population Under 18 Years by Age)."),
                NestedField(25, "B09001_M010", StringType(), doc="Margin of error (90% confidence) for the paired B09001_E010 estimate."),
                NestedField(26, "B09020_E001", StringType(), doc="ACS estimate (count): Total (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(27, "B09020_M001", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E001 estimate."),
                NestedField(28, "B09020_E002", StringType(), doc="ACS estimate (count): Total > In households (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(29, "B09020_M002", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E002 estimate."),
                NestedField(30, "B09020_E003", StringType(), doc="ACS estimate (count): Total > In households > In family households (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(31, "B09020_M003", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E003 estimate."),
                NestedField(32, "B09020_E004", StringType(), doc="ACS estimate (count): Total > In households > In family households > Householder (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(33, "B09020_M004", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E004 estimate."),
                NestedField(34, "B09020_E005", StringType(), doc="ACS estimate (count): Total > In households > In family households > Householder > Male (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(35, "B09020_M005", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E005 estimate."),
                NestedField(36, "B09020_E006", StringType(), doc="ACS estimate (count): Total > In households > In family households > Householder > Female (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(37, "B09020_M006", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E006 estimate."),
                NestedField(38, "B09020_E007", StringType(), doc="ACS estimate (count): Total > In households > In family households > Spouse (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(39, "B09020_M007", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E007 estimate."),
                NestedField(40, "B09020_E008", StringType(), doc="ACS estimate (count): Total > In households > In family households > Parent (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(41, "B09020_M008", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E008 estimate."),
                NestedField(42, "B09020_E009", StringType(), doc="ACS estimate (count): Total > In households > In family households > Parent-in-law (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(43, "B09020_M009", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E009 estimate."),
                NestedField(44, "B09020_E010", StringType(), doc="ACS estimate (count): Total > In households > In family households > Other relatives (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(45, "B09020_M010", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E010 estimate."),
                NestedField(46, "B09020_E011", StringType(), doc="ACS estimate (count): Total > In households > In family households > Nonrelatives (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(47, "B09020_M011", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E011 estimate."),
                NestedField(48, "B09020_E012", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(49, "B09020_M012", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E012 estimate."),
                NestedField(50, "B09020_E013", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(51, "B09020_M013", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E013 estimate."),
                NestedField(52, "B09020_E014", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Male (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(53, "B09020_M014", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E014 estimate."),
                NestedField(54, "B09020_E015", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Male > Living alone (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(55, "B09020_M015", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E015 estimate."),
                NestedField(56, "B09020_E016", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Male > Not living alone (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(57, "B09020_M016", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E016 estimate."),
                NestedField(58, "B09020_E017", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Female (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(59, "B09020_M017", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E017 estimate."),
                NestedField(60, "B09020_E018", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Female > Living alone (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(61, "B09020_M018", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E018 estimate."),
                NestedField(62, "B09020_E019", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Householder > Female > Not living alone (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(63, "B09020_M019", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E019 estimate."),
                NestedField(64, "B09020_E020", StringType(), doc="ACS estimate (count): Total > In households > In nonfamily households > Nonrelatives (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(65, "B09020_M020", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E020 estimate."),
                NestedField(66, "B09020_E021", StringType(), doc="ACS estimate (count): Total > In group quarters (Relationship by Household Type (Including Living Alone) for the Population 65 Years and Over)."),
                NestedField(67, "B09020_M021", StringType(), doc="Margin of error (90% confidence) for the paired B09020_E021 estimate."),
                NestedField(68, "B03002_E001", StringType(), doc="ACS estimate (count): Total (Hispanic or Latino Origin by Race)."),
                NestedField(69, "B03002_M001", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E001 estimate."),
                NestedField(70, "B03002_E002", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino (Hispanic or Latino Origin by Race)."),
                NestedField(71, "B03002_M002", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E002 estimate."),
                NestedField(72, "B03002_E003", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > White alone (Hispanic or Latino Origin by Race)."),
                NestedField(73, "B03002_M003", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E003 estimate."),
                NestedField(74, "B03002_E004", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Black or African American alone (Hispanic or Latino Origin by Race)."),
                NestedField(75, "B03002_M004", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E004 estimate."),
                NestedField(76, "B03002_E005", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > American Indian and Alaska Native alone (Hispanic or Latino Origin by Race)."),
                NestedField(77, "B03002_M005", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E005 estimate."),
                NestedField(78, "B03002_E006", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Asian alone (Hispanic or Latino Origin by Race)."),
                NestedField(79, "B03002_M006", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E006 estimate."),
                NestedField(80, "B03002_E007", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Native Hawaiian and Other Pacific Islander alone (Hispanic or Latino Origin by Race)."),
                NestedField(81, "B03002_M007", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E007 estimate."),
                NestedField(82, "B03002_E008", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Some other race alone (Hispanic or Latino Origin by Race)."),
                NestedField(83, "B03002_M008", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E008 estimate."),
                NestedField(84, "B03002_E009", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Two or more races (Hispanic or Latino Origin by Race)."),
                NestedField(85, "B03002_M009", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E009 estimate."),
                NestedField(86, "B03002_E010", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Two or more races > Two races including Some other race (Hispanic or Latino Origin by Race)."),
                NestedField(87, "B03002_M010", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E010 estimate."),
                NestedField(88, "B03002_E011", StringType(), doc="ACS estimate (count): Total > Not Hispanic or Latino > Two or more races > Two races excluding Some other race, and three or more races (Hispanic or Latino Origin by Race)."),
                NestedField(89, "B03002_M011", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E011 estimate."),
                NestedField(90, "B03002_E012", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino (Hispanic or Latino Origin by Race)."),
                NestedField(91, "B03002_M012", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E012 estimate."),
                NestedField(92, "B03002_E013", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > White alone (Hispanic or Latino Origin by Race)."),
                NestedField(93, "B03002_M013", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E013 estimate."),
                NestedField(94, "B03002_E014", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Black or African American alone (Hispanic or Latino Origin by Race)."),
                NestedField(95, "B03002_M014", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E014 estimate."),
                NestedField(96, "B03002_E015", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > American Indian and Alaska Native alone (Hispanic or Latino Origin by Race)."),
                NestedField(97, "B03002_M015", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E015 estimate."),
                NestedField(98, "B03002_E016", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Asian alone (Hispanic or Latino Origin by Race)."),
                NestedField(99, "B03002_M016", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E016 estimate."),
                NestedField(100, "B03002_E017", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Native Hawaiian and Other Pacific Islander alone (Hispanic or Latino Origin by Race)."),
                NestedField(101, "B03002_M017", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E017 estimate."),
                NestedField(102, "B03002_E018", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Some other race alone (Hispanic or Latino Origin by Race)."),
                NestedField(103, "B03002_M018", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E018 estimate."),
                NestedField(104, "B03002_E019", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Two or more races (Hispanic or Latino Origin by Race)."),
                NestedField(105, "B03002_M019", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E019 estimate."),
                NestedField(106, "B03002_E020", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Two or more races > Two races including Some other race (Hispanic or Latino Origin by Race)."),
                NestedField(107, "B03002_M020", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E020 estimate."),
                NestedField(108, "B03002_E021", StringType(), doc="ACS estimate (count): Total > Hispanic or Latino > Two or more races > Two races excluding Some other race, and three or more races (Hispanic or Latino Origin by Race)."),
                NestedField(109, "B03002_M021", StringType(), doc="Margin of error (90% confidence) for the paired B03002_E021 estimate."),
                NestedField(110, "B15003_E001", StringType(), doc="ACS estimate (count): Total (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(111, "B15003_M001", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E001 estimate."),
                NestedField(112, "B15003_E002", StringType(), doc="ACS estimate (count): Total > No schooling completed (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(113, "B15003_M002", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E002 estimate."),
                NestedField(114, "B15003_E003", StringType(), doc="ACS estimate (count): Total > Nursery school (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(115, "B15003_M003", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E003 estimate."),
                NestedField(116, "B15003_E004", StringType(), doc="ACS estimate (count): Total > Kindergarten (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(117, "B15003_M004", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E004 estimate."),
                NestedField(118, "B15003_E005", StringType(), doc="ACS estimate (count): Total > 1st grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(119, "B15003_M005", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E005 estimate."),
                NestedField(120, "B15003_E006", StringType(), doc="ACS estimate (count): Total > 2nd grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(121, "B15003_M006", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E006 estimate."),
                NestedField(122, "B15003_E007", StringType(), doc="ACS estimate (count): Total > 3rd grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(123, "B15003_M007", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E007 estimate."),
                NestedField(124, "B15003_E008", StringType(), doc="ACS estimate (count): Total > 4th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(125, "B15003_M008", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E008 estimate."),
                NestedField(126, "B15003_E009", StringType(), doc="ACS estimate (count): Total > 5th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(127, "B15003_M009", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E009 estimate."),
                NestedField(128, "B15003_E010", StringType(), doc="ACS estimate (count): Total > 6th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(129, "B15003_M010", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E010 estimate."),
                NestedField(130, "B15003_E011", StringType(), doc="ACS estimate (count): Total > 7th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(131, "B15003_M011", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E011 estimate."),
                NestedField(132, "B15003_E012", StringType(), doc="ACS estimate (count): Total > 8th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(133, "B15003_M012", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E012 estimate."),
                NestedField(134, "B15003_E013", StringType(), doc="ACS estimate (count): Total > 9th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(135, "B15003_M013", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E013 estimate."),
                NestedField(136, "B15003_E014", StringType(), doc="ACS estimate (count): Total > 10th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(137, "B15003_M014", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E014 estimate."),
                NestedField(138, "B15003_E015", StringType(), doc="ACS estimate (count): Total > 11th grade (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(139, "B15003_M015", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E015 estimate."),
                NestedField(140, "B15003_E016", StringType(), doc="ACS estimate (count): Total > 12th grade, no diploma (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(141, "B15003_M016", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E016 estimate."),
                NestedField(142, "B15003_E017", StringType(), doc="ACS estimate (count): Total > Regular high school diploma (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(143, "B15003_M017", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E017 estimate."),
                NestedField(144, "B15003_E018", StringType(), doc="ACS estimate (count): Total > GED or alternative credential (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(145, "B15003_M018", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E018 estimate."),
                NestedField(146, "B15003_E019", StringType(), doc="ACS estimate (count): Total > Some college, less than 1 year (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(147, "B15003_M019", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E019 estimate."),
                NestedField(148, "B15003_E020", StringType(), doc="ACS estimate (count): Total > Some college, 1 or more years, no degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(149, "B15003_M020", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E020 estimate."),
                NestedField(150, "B15003_E021", StringType(), doc="ACS estimate (count): Total > Associate's degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(151, "B15003_M021", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E021 estimate."),
                NestedField(152, "B15003_E022", StringType(), doc="ACS estimate (count): Total > Bachelor's degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(153, "B15003_M022", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E022 estimate."),
                NestedField(154, "B15003_E023", StringType(), doc="ACS estimate (count): Total > Master's degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(155, "B15003_M023", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E023 estimate."),
                NestedField(156, "B15003_E024", StringType(), doc="ACS estimate (count): Total > Professional school degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(157, "B15003_M024", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E024 estimate."),
                NestedField(158, "B15003_E025", StringType(), doc="ACS estimate (count): Total > Doctorate degree (Educational Attainment for the Population 25 Years and Over)."),
                NestedField(159, "B15003_M025", StringType(), doc="Margin of error (90% confidence) for the paired B15003_E025 estimate."),
                NestedField(160, "B19013_E001", StringType(), doc="ACS estimate (count): Median household income in the past 12 months (in 2023 inflation-adjusted dollars) (Median Household Income in the Past 12 Months (in 2023 Inflation-Adjusted Dollars))."),
                NestedField(161, "B19013_M001", StringType(), doc="Margin of error (90% confidence) for the paired B19013_E001 estimate."),
                NestedField(162, "C17002_E001", StringType(), doc="ACS estimate (count): Total (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(163, "C17002_M001", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E001 estimate."),
                NestedField(164, "C17002_E002", StringType(), doc="ACS estimate (count): Total > Under .50 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(165, "C17002_M002", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E002 estimate."),
                NestedField(166, "C17002_E003", StringType(), doc="ACS estimate (count): Total > .50 to .99 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(167, "C17002_M003", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E003 estimate."),
                NestedField(168, "C17002_E004", StringType(), doc="ACS estimate (count): Total > 1.00 to 1.24 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(169, "C17002_M004", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E004 estimate."),
                NestedField(170, "C17002_E005", StringType(), doc="ACS estimate (count): Total > 1.25 to 1.49 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(171, "C17002_M005", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E005 estimate."),
                NestedField(172, "C17002_E006", StringType(), doc="ACS estimate (count): Total > 1.50 to 1.84 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(173, "C17002_M006", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E006 estimate."),
                NestedField(174, "C17002_E007", StringType(), doc="ACS estimate (count): Total > 1.85 to 1.99 (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(175, "C17002_M007", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E007 estimate."),
                NestedField(176, "C17002_E008", StringType(), doc="ACS estimate (count): Total > 2.00 and over (Ratio of Income to Poverty Level in the Past 12 Months)."),
                NestedField(177, "C17002_M008", StringType(), doc="Margin of error (90% confidence) for the paired C17002_E008 estimate."),
                NestedField(178, "B23025_E001", StringType(), doc="ACS estimate (count): Total (Employment Status for the Population 16 Years and Over)."),
                NestedField(179, "B23025_M001", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E001 estimate."),
                NestedField(180, "B23025_E002", StringType(), doc="ACS estimate (count): Total > In labor force (Employment Status for the Population 16 Years and Over)."),
                NestedField(181, "B23025_M002", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E002 estimate."),
                NestedField(182, "B23025_E003", StringType(), doc="ACS estimate (count): Total > In labor force > Civilian labor force (Employment Status for the Population 16 Years and Over)."),
                NestedField(183, "B23025_M003", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E003 estimate."),
                NestedField(184, "B23025_E004", StringType(), doc="ACS estimate (count): Total > In labor force > Civilian labor force > Employed (Employment Status for the Population 16 Years and Over)."),
                NestedField(185, "B23025_M004", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E004 estimate."),
                NestedField(186, "B23025_E005", StringType(), doc="ACS estimate (count): Total > In labor force > Civilian labor force > Unemployed (Employment Status for the Population 16 Years and Over)."),
                NestedField(187, "B23025_M005", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E005 estimate."),
                NestedField(188, "B23025_E006", StringType(), doc="ACS estimate (count): Total > In labor force > Armed Forces (Employment Status for the Population 16 Years and Over)."),
                NestedField(189, "B23025_M006", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E006 estimate."),
                NestedField(190, "B23025_E007", StringType(), doc="ACS estimate (count): Total > Not in labor force (Employment Status for the Population 16 Years and Over)."),
                NestedField(191, "B23025_M007", StringType(), doc="Margin of error (90% confidence) for the paired B23025_E007 estimate."),
                NestedField(192, "B25044_E001", StringType(), doc="ACS estimate (count): Total (Tenure by Vehicles Available)."),
                NestedField(193, "B25044_M001", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E001 estimate."),
                NestedField(194, "B25044_E002", StringType(), doc="ACS estimate (count): Total > Owner occupied (Tenure by Vehicles Available)."),
                NestedField(195, "B25044_M002", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E002 estimate."),
                NestedField(196, "B25044_E003", StringType(), doc="ACS estimate (count): Total > Owner occupied > No vehicle available (Tenure by Vehicles Available)."),
                NestedField(197, "B25044_M003", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E003 estimate."),
                NestedField(198, "B25044_E004", StringType(), doc="ACS estimate (count): Total > Owner occupied > 1 vehicle available (Tenure by Vehicles Available)."),
                NestedField(199, "B25044_M004", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E004 estimate."),
                NestedField(200, "B25044_E005", StringType(), doc="ACS estimate (count): Total > Owner occupied > 2 vehicles available (Tenure by Vehicles Available)."),
                NestedField(201, "B25044_M005", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E005 estimate."),
                NestedField(202, "B25044_E006", StringType(), doc="ACS estimate (count): Total > Owner occupied > 3 vehicles available (Tenure by Vehicles Available)."),
                NestedField(203, "B25044_M006", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E006 estimate."),
                NestedField(204, "B25044_E007", StringType(), doc="ACS estimate (count): Total > Owner occupied > 4 vehicles available (Tenure by Vehicles Available)."),
                NestedField(205, "B25044_M007", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E007 estimate."),
                NestedField(206, "B25044_E008", StringType(), doc="ACS estimate (count): Total > Owner occupied > 5 or more vehicles available (Tenure by Vehicles Available)."),
                NestedField(207, "B25044_M008", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E008 estimate."),
                NestedField(208, "B25044_E009", StringType(), doc="ACS estimate (count): Total > Renter occupied (Tenure by Vehicles Available)."),
                NestedField(209, "B25044_M009", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E009 estimate."),
                NestedField(210, "B25044_E010", StringType(), doc="ACS estimate (count): Total > Renter occupied > No vehicle available (Tenure by Vehicles Available)."),
                NestedField(211, "B25044_M010", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E010 estimate."),
                NestedField(212, "B25044_E011", StringType(), doc="ACS estimate (count): Total > Renter occupied > 1 vehicle available (Tenure by Vehicles Available)."),
                NestedField(213, "B25044_M011", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E011 estimate."),
                NestedField(214, "B25044_E012", StringType(), doc="ACS estimate (count): Total > Renter occupied > 2 vehicles available (Tenure by Vehicles Available)."),
                NestedField(215, "B25044_M012", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E012 estimate."),
                NestedField(216, "B25044_E013", StringType(), doc="ACS estimate (count): Total > Renter occupied > 3 vehicles available (Tenure by Vehicles Available)."),
                NestedField(217, "B25044_M013", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E013 estimate."),
                NestedField(218, "B25044_E014", StringType(), doc="ACS estimate (count): Total > Renter occupied > 4 vehicles available (Tenure by Vehicles Available)."),
                NestedField(219, "B25044_M014", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E014 estimate."),
                NestedField(220, "B25044_E015", StringType(), doc="ACS estimate (count): Total > Renter occupied > 5 or more vehicles available (Tenure by Vehicles Available)."),
                NestedField(221, "B25044_M015", StringType(), doc="Margin of error (90% confidence) for the paired B25044_E015 estimate."),
                NestedField(222, "B25002_E001", StringType(), doc="ACS estimate (count): Total (Occupancy Status)."),
                NestedField(223, "B25002_M001", StringType(), doc="Margin of error (90% confidence) for the paired B25002_E001 estimate."),
                NestedField(224, "B25002_E002", StringType(), doc="ACS estimate (count): Total > Occupied (Occupancy Status)."),
                NestedField(225, "B25002_M002", StringType(), doc="Margin of error (90% confidence) for the paired B25002_E002 estimate."),
                NestedField(226, "B25002_E003", StringType(), doc="ACS estimate (count): Total > Vacant (Occupancy Status)."),
                NestedField(227, "B25002_M003", StringType(), doc="Margin of error (90% confidence) for the paired B25002_E003 estimate."),
                NestedField(228, "B25070_E001", StringType(), doc="ACS estimate (count): Total (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(229, "B25070_M001", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E001 estimate."),
                NestedField(230, "B25070_E002", StringType(), doc="ACS estimate (count): Total > Less than 10.0 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(231, "B25070_M002", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E002 estimate."),
                NestedField(232, "B25070_E003", StringType(), doc="ACS estimate (count): Total > 10.0 to 14.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(233, "B25070_M003", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E003 estimate."),
                NestedField(234, "B25070_E004", StringType(), doc="ACS estimate (count): Total > 15.0 to 19.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(235, "B25070_M004", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E004 estimate."),
                NestedField(236, "B25070_E005", StringType(), doc="ACS estimate (count): Total > 20.0 to 24.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(237, "B25070_M005", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E005 estimate."),
                NestedField(238, "B25070_E006", StringType(), doc="ACS estimate (count): Total > 25.0 to 29.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(239, "B25070_M006", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E006 estimate."),
                NestedField(240, "B25070_E007", StringType(), doc="ACS estimate (count): Total > 30.0 to 34.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(241, "B25070_M007", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E007 estimate."),
                NestedField(242, "B25070_E008", StringType(), doc="ACS estimate (count): Total > 35.0 to 39.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(243, "B25070_M008", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E008 estimate."),
                NestedField(244, "B25070_E009", StringType(), doc="ACS estimate (count): Total > 40.0 to 49.9 percent (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(245, "B25070_M009", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E009 estimate."),
                NestedField(246, "B25070_E010", StringType(), doc="ACS estimate (count): Total > 50.0 percent or more (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(247, "B25070_M010", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E010 estimate."),
                NestedField(248, "B25070_E011", StringType(), doc="ACS estimate (count): Total > Not computed (Gross Rent as a Percentage of Household Income in the Past 12 Months)."),
                NestedField(249, "B25070_M011", StringType(), doc="Margin of error (90% confidence) for the paired B25070_E011 estimate."),
                NestedField(250, "C16002_E001", StringType(), doc="ACS estimate (count): Total (Household Language by Household Limited English Speaking Status)."),
                NestedField(251, "C16002_M001", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E001 estimate."),
                NestedField(252, "C16002_E002", StringType(), doc="ACS estimate (count): Total > English only (Household Language by Household Limited English Speaking Status)."),
                NestedField(253, "C16002_M002", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E002 estimate."),
                NestedField(254, "C16002_E003", StringType(), doc="ACS estimate (count): Total > Spanish (Household Language by Household Limited English Speaking Status)."),
                NestedField(255, "C16002_M003", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E003 estimate."),
                NestedField(256, "C16002_E004", StringType(), doc="ACS estimate (count): Total > Spanish > Limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(257, "C16002_M004", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E004 estimate."),
                NestedField(258, "C16002_E005", StringType(), doc="ACS estimate (count): Total > Spanish > Not a limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(259, "C16002_M005", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E005 estimate."),
                NestedField(260, "C16002_E006", StringType(), doc="ACS estimate (count): Total > Other Indo-European languages (Household Language by Household Limited English Speaking Status)."),
                NestedField(261, "C16002_M006", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E006 estimate."),
                NestedField(262, "C16002_E007", StringType(), doc="ACS estimate (count): Total > Other Indo-European languages > Limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(263, "C16002_M007", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E007 estimate."),
                NestedField(264, "C16002_E008", StringType(), doc="ACS estimate (count): Total > Other Indo-European languages > Not a limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(265, "C16002_M008", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E008 estimate."),
                NestedField(266, "C16002_E009", StringType(), doc="ACS estimate (count): Total > Asian and Pacific Island languages (Household Language by Household Limited English Speaking Status)."),
                NestedField(267, "C16002_M009", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E009 estimate."),
                NestedField(268, "C16002_E010", StringType(), doc="ACS estimate (count): Total > Asian and Pacific Island languages > Limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(269, "C16002_M010", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E010 estimate."),
                NestedField(270, "C16002_E011", StringType(), doc="ACS estimate (count): Total > Asian and Pacific Island languages > Not a limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(271, "C16002_M011", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E011 estimate."),
                NestedField(272, "C16002_E012", StringType(), doc="ACS estimate (count): Total > Other languages (Household Language by Household Limited English Speaking Status)."),
                NestedField(273, "C16002_M012", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E012 estimate."),
                NestedField(274, "C16002_E013", StringType(), doc="ACS estimate (count): Total > Other languages > Limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(275, "C16002_M013", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E013 estimate."),
                NestedField(276, "C16002_E014", StringType(), doc="ACS estimate (count): Total > Other languages > Not a limited English speaking household (Household Language by Household Limited English Speaking Status)."),
                NestedField(277, "C16002_M014", StringType(), doc="Margin of error (90% confidence) for the paired C16002_E014 estimate."),
                NestedField(278, "B19083_E001", StringType(), doc="ACS estimate (count): Gini Index (Gini Index of Income Inequality)."),
                NestedField(279, "B19083_M001", StringType(), doc="Margin of error (90% confidence) for the paired B19083_E001 estimate."),
            ),
            comment="US Census ACS 5-year Summary File detailed tables (B01003, B09001, B09020, "
                    "B03002, B15003, B19013, C17002, B23025, B25044, B25002, B25070, C16002, "
                    "B19083), landed verbatim per (level, acs_year): every estimate and margin-of-"
                    "error variable of each requested table, as the jam-value strings ACS "
                    "publishes (see census_acs.py for the sentinel meanings and the CIF-indicator "
                    "decision). Public domain (U.S. Government work, 17 U.S.C. Sec 105).",
        )
        for _level in ("county", "tract")
    },

    # --- raw: cdc places tract ---
    # CDC PLACES tract releases (#30). Own column contract (places.py's
    # TRACT_COLUMNS): CountyFIPS/CountyName are new; LocationName duplicates
    # LocationID (both hold the 11-digit tract FIPS) rather than naming
    # anything, an upstream quirk verified against the real files. Crude
    # prevalence only -- derives into the SAME `PLACES:<MeasureId>:crude`
    # measure.definition rows county's crude variant already asserts.
    "raw.places__tract": TableDef(
        schema=Schema(
            NestedField(1, "Year", StringType(), required=True,
                        doc="BRFSS survey year this row's estimate is based on, e.g. '2022'."),
            NestedField(2, "StateAbbr", StringType(), doc="Two-letter state postal abbreviation."),
            NestedField(3, "StateDesc", StringType(), doc="State name."),
            NestedField(4, "CountyName", StringType(),
                        doc="County name the tract belongs to. Not present in the county-data "
                            "contract (raw.places__county), which IS the county."),
            NestedField(5, "CountyFIPS", StringType(), required=True,
                        doc="5-digit county FIPS code (leading zero kept) the tract belongs to."),
            NestedField(6, "LocationName", StringType(),
                        doc="The 11-digit tract FIPS, verbatim -- an upstream quirk: unlike the "
                            "county file, where LocationName is a human name, here it duplicates "
                            "LocationID rather than naming anything."),
            NestedField(7, "DataSource", StringType(),
                        doc="Survey the estimate is modeled from, e.g. 'BRFSS'."),
            NestedField(8, "Category", StringType(),
                        doc="Measure category, e.g. 'Health Outcomes', 'Disability'."),
            NestedField(9, "Measure", StringType(),
                        doc="Full measure description including its universe, e.g. 'Current "
                            "cigarette smoking among adults'."),
            NestedField(10, "Data_Value_Unit", StringType(), doc="Unit of Data_Value, e.g. '%'."),
            NestedField(11, "Data_Value_Type", StringType(),
                        doc="Always 'Crude prevalence' in the tract data -- PLACES publishes no "
                            "age-adjusted variant at tract grain (verified via the Socrata SODA "
                            "API against every landed release)."),
            NestedField(12, "Data_Value", StringType(),
                        doc="The published estimate, unparsed; empty (NULL) when suppressed -- "
                            "see Data_Value_Footnote. Every row is 'reported' in the releases "
                            "actually landed here (places.py module docstring)."),
            NestedField(13, "Data_Value_Footnote_Symbol", StringType(),
                        doc="Footnote marker on Data_Value, or NULL."),
            NestedField(14, "Data_Value_Footnote", StringType(),
                        doc="Footnote text explaining a missing Data_Value, or NULL."),
            NestedField(15, "Low_Confidence_Limit", StringType(),
                        doc="95% CI lower bound, unparsed."),
            NestedField(16, "High_Confidence_Limit", StringType(),
                        doc="95% CI upper bound, unparsed."),
            NestedField(17, "TotalPopulation", StringType(),
                        doc="Total population of the tract, per the source's own population "
                            "estimate. NOT this measure's denominator, same as the county table."),
            NestedField(18, "TotalPop18plus", StringType(),
                        doc="Adult (18+) population of the tract, per the source's own population "
                            "estimate. Not used as a denominator, same as the county table."),
            NestedField(19, "Geolocation", StringType(),
                        doc="Tract centroid as a WKT POINT string."),
            NestedField(20, "LocationID", StringType(), required=True,
                        doc="11-digit tract FIPS code (leading zero kept)."),
            NestedField(21, "CategoryID", StringType(), doc="Short code for Category."),
            NestedField(22, "MeasureId", StringType(), required=True,
                        doc="Short code for Measure, e.g. 'CSMOKING' -- the same code space as "
                            "the county table; derived observations reuse the county crude "
                            "measure_id rather than minting a tract-specific one."),
            NestedField(23, "DataValueTypeID", StringType(), doc="Always 'CrdPrv' in tract data."),
            NestedField(24, "Short_Question_Text", StringType(),
                        doc="Short label for Measure, e.g. 'Current Smoking'."),
            NestedField(25, "places_release", StringType(), required=True,
                        doc="The PLACES tract-data release year this row came from, e.g. '2025' "
                            "(a key of places.TRACT_RELEASES). Raw is replaced wholesale per "
                            "value of this column."),
        ),
        sort_by=("places_release", "LocationID", "MeasureId", "DataValueTypeID"),
        comment="CDC PLACES tract-data release, landed verbatim and whole: one row per "
                "(tract, measure) model-based small-area estimate, crude prevalence only "
                "(SPEC.md § Sources — first tranche; #30). Public domain.",
    ),

    # --- raw: fda mqsa ---
    # FDA MQSA certified mammography facilities (#36).
    # A weekly, unlabeled snapshot with no header row -- see fda_mqsa.py's
    # module docstring for the field list FDA's own page publishes and the
    # verification that no id column exists.
    "raw.fda__mqsa_facilities": TableDef(
        schema=Schema(
            NestedField(1, "Facility Name", StringType(), required=True,
                        doc="Facility's own name. Normalised and hashed into facility.site.facility_id "
                            "(no id column exists in this file -- see fda_mqsa.py); also facility.site.name."),
            NestedField(2, "Address 1", StringType(), doc="Street address line 1; part of facility.site.address and facility_id."),
            NestedField(3, "Address 2", StringType(), doc="Street address line 2, often NULL; part of facility.site.address and facility_id."),
            NestedField(4, "Address 3", StringType(), doc="Street address line 3, often NULL; part of facility.site.address and facility_id."),
            NestedField(5, "City", StringType(), doc="Part of facility.site.address and facility_id; also facility.site.attributes_json's 'city' key."),
            NestedField(6, "State", StringType(), required=True,
                        doc="USPS state/territory/military abbreviation; part of facility.site.address "
                            "and facility_id; also facility.site.attributes_json's 'state' key."),
            NestedField(7, "Zip Code", StringType(), required=True,
                        doc="5-digit or ZIP+4; only the first 5 digits feed facility_id (a ZIP+4 changing "
                            "alone must not look like a different facility). Also facility.site.address "
                            "and attributes_json's 'zip' key."),
            NestedField(8, "Phone", StringType(), doc="As published by FDA; not used downstream (excluded from facility_id -- see fda_mqsa.py)."),
            NestedField(9, "Fax", StringType(), doc="As published by FDA; not used downstream."),
            NestedField(10, "retrieved_on", StringType(), required=True,
                        doc="Date this weekly-refreshed dump was fetched, ISO YYYY-MM-DD -- the version "
                            "axis for this source (it publishes no edition label). Raw is replaced "
                            "wholesale per value of this column."),
            NestedField(11, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("retrieved_on", "State", "Facility Name", "Address 1"),
        comment="FDA MQSA certified mammography facility list, landed verbatim and whole, replaced "
                "weekly (SPEC.md § Sources — first tranche). Public domain; FDA's site-wide website "
                "policy states 'the contents of the FDA website ... are not copyrighted. They are in "
                "the public domain and may be republished, reprinted and otherwise used freely by "
                "anyone without the need to obtain permission from FDA' "
                "(https://www.fda.gov/about-fda/about-website/website-policies, checked 2026-09-18).",
    ),

    # --- raw: epa sdwis ---
    # EPA SDWIS Federal (via ECHO's SDWA bulk download): public water systems,
    # their violations, and the counties/areas they serve, refreshed quarterly
    # (SPEC.md § Sources — second tranche). Public domain (17 U.S.C. § 105).
    # ORG_NAME/ADMIN_NAME/EMAIL_ADDR/PHONE_NUMBER/PHONE_EXT_NUMBER/FAX_NUMBER/
    # ALT_PHONE_NUMBER/ADDRESS_LINE1/ADDRESS_LINE2 are excluded from
    # raw.sdwis__pub_water_systems -- see epa_sdwis.py's module docstring.
    "raw.sdwis__pub_water_systems": TableDef(
        schema=Schema(
            NestedField(1, "SUBMISSIONYEARQUARTER", StringType(), required=True,
                        doc="Fiscal year and quarter of this quarterly SDWIS snapshot, e.g. "
                            "'2026Q2' -- this source's version axis. Raw is replaced wholesale "
                            "per value of this column."),
            NestedField(2, "PWSID", StringType(), required=True,
                        doc="Public water system id: a two-letter state, territory, or numeric "
                            "EPA-region code, followed by seven digits, e.g. 'CT1680051'."),
            NestedField(3, "PWS_NAME", StringType(), doc="Water system name."),
            NestedField(4, "PRIMACY_AGENCY_CODE", StringType(),
                        doc="Two-character state/territory code, or a numeric EPA Region code, "
                            "of the agency with primary enforcement responsibility."),
            NestedField(5, "EPA_REGION", StringType(), doc="EPA Region number."),
            NestedField(6, "SEASON_BEGIN_DATE", StringType(),
                        doc="Opening month/day of a seasonal system's service period."),
            NestedField(7, "SEASON_END_DATE", StringType(),
                        doc="Closing month/day of a seasonal system's service period."),
            NestedField(8, "PWS_ACTIVITY_CODE", StringType(),
                        doc="Current activity status: A active, I inactive, N never operated, "
                            "M merged/consolidated, P potential future system. A current-snapshot "
                            "field, not a per-year history -- see module docstring."),
            NestedField(9, "PWS_DEACTIVATION_DATE", StringType(),
                        doc="Date the system was reported closed/deactivated."),
            NestedField(10, "PWS_TYPE_CODE", StringType(),
                        doc="CWS community, TNCWS transient non-community, NTNCWS non-transient "
                            "non-community, or NP."),
            NestedField(11, "DBPR_SCHEDULE_CAT_CODE", StringType(),
                        doc="Stage 2 Disinfectant Byproducts Rule monitoring category."),
            NestedField(12, "CDS_ID", StringType(), doc="Combined distribution system id."),
            NestedField(13, "GW_SW_CODE", StringType(), doc="Groundwater or surface water source."),
            NestedField(14, "LT2_SCHEDULE_CAT_CODE", StringType(),
                        doc="Long Term 2 Enhanced Surface Water Treatment Rule category."),
            NestedField(15, "OWNER_TYPE_CODE", StringType(),
                        doc="Ownership: F federal, L local, M public/private, N native American, "
                            "P private, S state."),
            NestedField(16, "POPULATION_SERVED_COUNT", StringType(),
                        doc="Estimated average daily population served, as of this snapshot -- "
                            "not a per-year historical figure (module docstring)."),
            NestedField(17, "POP_CAT_2_CODE", StringType(), doc="Population-size category (2 tiers)."),
            NestedField(18, "POP_CAT_3_CODE", StringType(), doc="Population-size category (3 tiers)."),
            NestedField(19, "POP_CAT_4_CODE", StringType(), doc="Population-size category (4 tiers)."),
            NestedField(20, "POP_CAT_5_CODE", StringType(), doc="Population-size category (5 tiers)."),
            NestedField(21, "POP_CAT_11_CODE", StringType(), doc="Population-size category (11 tiers)."),
            NestedField(22, "PRIMACY_TYPE", StringType(),
                        doc="State, tribal, territorial, or EPA-direct primacy regulation."),
            NestedField(23, "PRIMARY_SOURCE_CODE", StringType(),
                        doc="Primary water source, e.g. GW, SW, GU, GWP, SWP."),
            NestedField(24, "IS_GRANT_ELIGIBLE_IND", StringType(), doc="Grant eligibility indicator."),
            NestedField(25, "IS_WHOLESALER_IND", StringType(), doc="Wholesaler status indicator."),
            NestedField(26, "IS_SCHOOL_OR_DAYCARE_IND", StringType(),
                        doc="School-or-daycare service-area indicator."),
            NestedField(27, "SERVICE_CONNECTIONS_COUNT", StringType(),
                        doc="Number of service connections."),
            NestedField(28, "SUBMISSION_STATUS_CODE", StringType(),
                        doc="Reported/unreported/rejected submission status."),
            NestedField(29, "CITY_NAME", StringType(), doc="City."),
            NestedField(30, "ZIP_CODE", StringType(), doc="USPS ZIP code."),
            NestedField(31, "COUNTRY_CODE", StringType(), doc="Two-character country code."),
            NestedField(32, "FIRST_REPORTED_DATE", StringType(), doc="First reported date for the system."),
            NestedField(33, "LAST_REPORTED_DATE", StringType(), doc="Last reported date for the system."),
            NestedField(34, "STATE_CODE", StringType(),
                        doc="USPS state abbreviation of the system's address -- a mailing "
                            "address, not necessarily the state of the geography it serves (see "
                            "raw.sdwis__geographic_areas); can be NULL or non-US for a handful of "
                            "systems."),
            NestedField(35, "SOURCE_WATER_PROTECTION_CODE", StringType(),
                        doc="Source water protection implementation status."),
            NestedField(36, "SOURCE_PROTECTION_BEGIN_DATE", StringType(),
                        doc="Date source water protection was substantially implemented."),
            NestedField(37, "OUTSTANDING_PERFORMER", StringType(),
                        doc="Outstanding-performer criteria compliance."),
            NestedField(38, "OUTSTANDING_PERFORM_BEGIN_DATE", StringType(),
                        doc="Date outstanding-performer criteria was met."),
            NestedField(39, "REDUCED_RTCR_MONITORING", StringType(),
                        doc="Reduced Revised Total Coliform Rule monitoring frequency."),
            NestedField(40, "REDUCED_MONITORING_BEGIN_DATE", StringType(),
                        doc="Start date of reduced monitoring."),
            NestedField(41, "REDUCED_MONITORING_END_DATE", StringType(),
                        doc="End date of reduced monitoring."),
            NestedField(42, "SEASONAL_STARTUP_SYSTEM", StringType(),
                        doc="Seasonal pressurization/startup status."),
            NestedField(43, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("SUBMISSIONYEARQUARTER", "PWSID"),
        comment="EPA SDWIS Federal public water systems, landed verbatim and whole (minus nine "
                "individual columns -- ORG_NAME, ADDRESS_LINE1/2 and six admin-contact fields -- "
                "excluded -- see epa_sdwis.py). One row per PWSID, replaced wholesale per "
                "SUBMISSIONYEARQUARTER (SPEC.md § Sources — second tranche). Public domain "
                "(17 U.S.C. § 105).",
    ),

    "raw.sdwis__geographic_areas": TableDef(
        schema=Schema(
            NestedField(1, "SUBMISSIONYEARQUARTER", StringType(), required=True,
                        doc="This source's version axis, e.g. '2026Q2'. Raw is replaced "
                            "wholesale per value of this column."),
            NestedField(2, "PWSID", StringType(), required=True, doc="Public water system id."),
            NestedField(3, "GEO_ID", StringType(),
                        doc="EPA's own system-generated id for this (PWSID, area) record -- "
                            "unrelated to cancerOnIce's own geo_id convention used elsewhere in "
                            "this catalog."),
            NestedField(4, "AREA_TYPE_CODE", StringType(),
                        doc="TR tribal, CN county, ZC zip code, CT city, IR Indian reservation, "
                            "or NULL/unknown. Only CN rows are used to derive county measures."),
            NestedField(5, "TRIBAL_CODE", StringType(),
                        doc="EPA code for the Indian reservation or Alaska Native village."),
            NestedField(6, "STATE_SERVED", StringType(),
                        doc="State the facility is serving, per EPA's own documentation -- empty "
                            "on every CN (county) row observed in the real 2026Q2 download; "
                            "epa_sdwis.py derives the state from PWSID's own two-letter prefix "
                            "instead (see its module docstring)."),
            NestedField(7, "ANSI_ENTITY_CODE", StringType(),
                        doc="ANSI/FIPS county entity code, 3-digit zero-padded. Paired with the "
                            "state implied by PWSID's prefix, via raw.sdwis__ref_ansi_areas, to "
                            "build a county FIPS code. NULL on non-county rows; on some CN rows "
                            "it is populated but does not match any real county for that state "
                            "(bad upstream data) -- excluded and reported, never guessed."),
            NestedField(8, "ZIP_CODE_SERVED", StringType(), doc="ZIP code served, on ZC rows."),
            NestedField(9, "CITY_SERVED", StringType(), doc="City name served, on CT rows."),
            NestedField(10, "COUNTY_SERVED", StringType(),
                        doc="County name as reported; sometimes NULL even when ANSI_ENTITY_CODE "
                            "is populated. Not used for the FIPS derivation, kept for reference."),
            NestedField(11, "LAST_REPORTED_DATE", StringType(), doc="Last reported date."),
            NestedField(12, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("SUBMISSIONYEARQUARTER", "PWSID", "GEO_ID"),
        comment="EPA SDWIS Federal: every county/city/zip/tribal area each public water system "
                "reports serving, landed verbatim and whole, replaced wholesale per "
                "SUBMISSIONYEARQUARTER (SPEC.md § Sources — second tranche). A system serving "
                "several counties has one row per county. Public domain (17 U.S.C. § 105).",
    ),

    "raw.sdwis__violations_enforcement": TableDef(
        schema=Schema(
            NestedField(1, "SUBMISSIONYEARQUARTER", StringType(), required=True,
                        doc="This source's version axis, e.g. '2026Q2'. Raw is replaced "
                            "wholesale per value of this column."),
            NestedField(2, "PWSID", StringType(), required=True, doc="Public water system id."),
            NestedField(3, "VIOLATION_ID", StringType(),
                        doc="System-generated violation id; NULL on enforcement-only rows with "
                            "no associated violation. (PWSID, VIOLATION_ID) identifies one "
                            "violation but is NOT this table's row key: a violation with several "
                            "enforcement actions repeats across several rows sharing the same "
                            "(PWSID, VIOLATION_ID) and a different ENFORCEMENT_ID -- verified "
                            "against the real 2026Q2 file, whose 15,432,737 rows compress to "
                            "5,514,271 distinct (PWSID, VIOLATION_ID) pairs. Deriving a count of "
                            "violations must count DISTINCT (PWSID, VIOLATION_ID), never rows."),
            NestedField(4, "FACILITY_ID", StringType(), doc="Facility id within the water system."),
            NestedField(5, "COMPL_PER_BEGIN_DATE", StringType(), doc="Compliance period begin date."),
            NestedField(6, "COMPL_PER_END_DATE", StringType(), doc="Compliance period end date."),
            NestedField(7, "NON_COMPL_PER_BEGIN_DATE", StringType(),
                        doc="Noncompliance period begin date -- the violation-year axis "
                            "epa_sdwis.py derives measure.observation's period_start/period_end "
                            "from. Verified constant within a (PWSID, VIOLATION_ID) group."),
            NestedField(8, "NON_COMPL_PER_END_DATE", StringType(), doc="Noncompliance period end date."),
            NestedField(9, "PWS_DEACTIVATION_DATE", StringType(), doc="System deactivation date, if any."),
            NestedField(10, "VIOLATION_CODE", StringType(), doc="Violation type code."),
            NestedField(11, "VIOLATION_CATEGORY_CODE", StringType(),
                        doc="Category: TT, MRDL, MCL, MR, MON, RPT, etc."),
            NestedField(12, "IS_HEALTH_BASED_IND", StringType(),
                        doc="'Y' for a health-based violation, 'N' or NULL otherwise. "
                            "epa_sdwis.py's derived measure counts 'Y' only. Verified constant "
                            "within a (PWSID, VIOLATION_ID) group."),
            NestedField(13, "CONTAMINANT_CODE", StringType(), doc="Contaminant code."),
            NestedField(14, "VIOL_MEASURE", StringType(), doc="Analytical result value, unparsed."),
            NestedField(15, "UNIT_OF_MEASURE", StringType(), doc="Unit of VIOL_MEASURE."),
            NestedField(16, "FEDERAL_MCL", StringType(), doc="Federal maximum contaminant level exceeded."),
            NestedField(17, "STATE_MCL", StringType(), doc="State maximum contaminant level exceeded."),
            NestedField(18, "IS_MAJOR_VIOL_IND", StringType(), doc="Major/minor designation for MR violations."),
            NestedField(19, "SEVERITY_IND_CNT", StringType(), doc="Severity count for certain DBPR/IESWTR violations."),
            NestedField(20, "CALCULATED_RTC_DATE", StringType(), doc="Date the system returned to compliance."),
            NestedField(21, "VIOLATION_STATUS", StringType(),
                        doc="Resolved, Archived, Addressed, or Unaddressed."),
            NestedField(22, "PUBLIC_NOTIFICATION_TIER", StringType(), doc="Public notification tier."),
            NestedField(23, "CALCULATED_PUB_NOTIF_TIER", StringType(), doc="Calculated public notification tier."),
            NestedField(24, "VIOL_ORIGINATOR_CODE", StringType(), doc="F federal, H state, R reporting agency, S state."),
            NestedField(25, "SAMPLE_RESULT_ID", StringType(), doc="Reporting jurisdiction's sample-result id."),
            NestedField(26, "CORRECTIVE_ACTION_ID", StringType(), doc="Corrective action id."),
            NestedField(27, "RULE_CODE", StringType(), doc="National Drinking Water rule code."),
            NestedField(28, "RULE_GROUP_CODE", StringType(), doc="Rule group code."),
            NestedField(29, "RULE_FAMILY_CODE", StringType(), doc="Rule family code."),
            NestedField(30, "VIOL_FIRST_REPORTED_DATE", StringType(), doc="Date the violation was first reported."),
            NestedField(31, "VIOL_LAST_REPORTED_DATE", StringType(), doc="Date the violation was last reported."),
            NestedField(32, "ENFORCEMENT_ID", StringType(),
                        doc="Id of one enforcement action; several can exist per (PWSID, "
                            "VIOLATION_ID) -- see VIOLATION_ID's doc."),
            NestedField(33, "ENFORCEMENT_DATE", StringType(), doc="Enforcement action date."),
            NestedField(34, "ENFORCEMENT_ACTION_TYPE_CODE", StringType(), doc="Enforcement action type."),
            NestedField(35, "ENF_ACTION_CATEGORY", StringType(), doc="Formal, Informal, or Resolving."),
            NestedField(36, "ENF_ORIGINATOR_CODE", StringType(), doc="F federal, H state, R reporting agency, S state."),
            NestedField(37, "ENF_FIRST_REPORTED_DATE", StringType(), doc="Date the enforcement action was first reported."),
            NestedField(38, "ENF_LAST_REPORTED_DATE", StringType(), doc="Date the enforcement action was last reported."),
            NestedField(39, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("SUBMISSIONYEARQUARTER", "PWSID", "VIOLATION_ID"),
        comment="EPA SDWIS Federal violation+enforcement records, landed verbatim and whole, "
                "replaced wholesale per SUBMISSIONYEARQUARTER (SPEC.md § Sources — second "
                "tranche). Grain is (violation, enforcement action) -- see VIOLATION_ID's doc "
                "for why a violation count must dedupe. Millions of rows; landed in batches "
                "(epa_sdwis.py, ported from bioc-on-ice's ncbi.py::_land). Public domain "
                "(17 U.S.C. § 105).",
    ),

    "raw.sdwis__ref_ansi_areas": TableDef(
        schema=Schema(
            NestedField(1, "ANSI_STATE_CODE", StringType(), required=True,
                        doc="2-digit ANSI/FIPS state code, zero-padded, e.g. '09' for Connecticut."),
            NestedField(2, "ANSI_ENTITY_CODE", StringType(), required=True,
                        doc="3-digit ANSI/FIPS county entity code, zero-padded, e.g. '001'."),
            NestedField(3, "ANSI_NAME", StringType(), doc="County (or equivalent) name."),
            NestedField(4, "STATE_CODE", StringType(), required=True,
                        doc="USPS state postal abbreviation, e.g. 'CT'. Joined against "
                            "raw.sdwis__geographic_areas' PWSID prefix in epa_sdwis.py, since "
                            "that table's own STATE_SERVED column is empty on county rows."),
            NestedField(5, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("ANSI_STATE_CODE", "ANSI_ENTITY_CODE"),
        comment="EPA's own ANSI/FIPS county reference table, bundled in the same SDWA bulk "
                "download -- the authority epa_sdwis.py uses to turn a (PWSID prefix, "
                "ANSI_ENTITY_CODE) pair into a real county FIPS code, and to catch the ones that "
                "do not correspond to any real county (SPEC.md's 'report the unmatched rate "
                "honestly' rule). Not itself versioned per quarter; replaced wholesale each "
                "release, the same pattern as raw.census__state_fips. Public domain "
                "(17 U.S.C. § 105).",
    ),

    # --- raw: fcc broadband ---
    # FCC Broadband Data Collection (#54).
    # The table declaration(s) goes directly under this comment block.
    # Leave this marker and the blank lines around it untouched so
    # independent branches merge cleanly.

    # --- raw: bls laus ---
    # BLS Local Area Unemployment Statistics, county (#94). Landed whole per
    # retrieval-date vintage (bls_laus.py module docstring); scoped by laus_vintage.
    "raw.bls__laus_county": TableDef(
        schema=Schema(
            NestedField(1, "series_id", StringType(), required=True,
                        doc="BLS series id, 'LAUCN<5-digit FIPS>00000000<measure_code>' -- "
                            "e.g. 'LAUCN010010000000003' is Autauga County AL (01001), measure "
                            "03. Every row of this file carries the 'LAUCN' county prefix "
                            "(verified against the full real file, 3,225 areas)."),
            NestedField(2, "year", StringType(), required=True, doc="4-digit calendar year."),
            NestedField(3, "period", StringType(), required=True,
                        doc="'M01'-'M12' for a calendar month, 'M13' for the annual average "
                            "(la.period, landed in raw.bls__laus_measure's sibling la.period is "
                            "not landed separately -- its 13 values are exactly M01-M13, not "
                            "worth a lookup table of its own)."),
            NestedField(4, "value", StringType(),
                        doc="The published value, unparsed. A literal '-' means not available "
                            "(footnote_codes explains why); never silently cast to a number."),
            NestedField(5, "footnote_codes", StringType(),
                        doc="FK to raw.bls__laus_footnote.footnote_code; NULL when the value "
                            "carries no footnote."),
            NestedField(6, "laus_vintage", StringType(), required=True,
                        doc="Retrieval-date vintage (SPEC.md's versioning model: BLS revises "
                            "prior months in place, so the flat file's version axis is when it "
                            "was retrieved, not a label BLS publishes). Raw is replaced wholesale "
                            "per value of this column; several retrievals with identical bytes "
                            "are one vintage (bls_laus.py skips landing a repeat)."),
            NestedField(7, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("laus_vintage", "series_id", "year", "period"),
        comment="BLS LAUS county-level (area_type_code='F') monthly + annual-average data, "
                "landed verbatim and whole from la.data.64.County. Public domain (BLS is a "
                "federal agency).",
    ),

    "raw.bls__laus_area": TableDef(
        schema=Schema(
            NestedField(1, "area_type_code", StringType(), required=True,
                        doc="BLS area classification letter. 'F' is county/county-equivalent -- "
                            "the only level this module derives from; other codes (state, "
                            "metropolitan area, balance-of-state, etc.) are landed for reference "
                            "but not used."),
            NestedField(2, "area_code", StringType(), required=True,
                        doc="'CN' + 2-digit state FIPS + 3-digit county FIPS + 8 zeros for a "
                            "county-equivalent area, e.g. 'CN0100100000000' (Autauga County, AL)."),
            NestedField(3, "area_text", StringType(), required=True, doc="Human-readable area name."),
            NestedField(4, "display_level", StringType(), doc="BLS display grouping, as published."),
            NestedField(5, "selectable", StringType(), doc="'T'/'F', as published."),
            NestedField(6, "sort_sequence", StringType(), doc="BLS display sort order, as published."),
            NestedField(7, "laus_vintage", StringType(), required=True, doc="See raw.bls__laus_county."),
            NestedField(8, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("laus_vintage", "area_type_code", "area_code"),
        comment="BLS LAUS area code lookup (la.area), landed verbatim and whole per vintage.",
    ),

    "raw.bls__laus_series": TableDef(
        schema=Schema(
            NestedField(1, "series_id", StringType(), required=True, doc="See raw.bls__laus_county."),
            NestedField(2, "area_type_code", StringType(), required=True, doc="See raw.bls__laus_area."),
            NestedField(3, "area_code", StringType(), required=True, doc="FK raw.bls__laus_area."),
            NestedField(4, "measure_code", StringType(), required=True, doc="FK raw.bls__laus_measure."),
            NestedField(5, "seasonal", StringType(), required=True,
                        doc="'S' seasonally adjusted, 'U' not. Every county-level series is 'U' "
                            "(verified against the full real file) -- LAUS publishes no "
                            "seasonally-adjusted county series."),
            NestedField(6, "srd_code", StringType(), doc="BLS state/regional division code, as published."),
            NestedField(7, "series_title", StringType(), required=True, doc="Human-readable series title."),
            NestedField(8, "footnote_codes", StringType(), doc="Footnote(s) on the series overall, if any."),
            NestedField(9, "begin_year", StringType(), doc="First year this series has data."),
            NestedField(10, "begin_period", StringType(), doc="First period this series has data."),
            NestedField(11, "end_year", StringType(), doc="Last year this series has data, as of this vintage."),
            NestedField(12, "end_period", StringType(), doc="Last period this series has data, as of this vintage."),
            NestedField(13, "laus_vintage", StringType(), required=True, doc="See raw.bls__laus_county."),
            NestedField(14, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("laus_vintage", "series_id"),
        comment="BLS LAUS series identification lookup (la.series), landed verbatim and whole "
                "per vintage. Not read by this module's derive step -- the county data file's own "
                "series_id already encodes area + measure (raw.bls__laus_county's doc) -- landed "
                "for reference and for a future series-title/date-range lookup need.",
    ),

    "raw.bls__laus_measure": TableDef(
        schema=Schema(
            NestedField(1, "measure_code", StringType(), required=True,
                        doc="2-digit measure code, e.g. '03' = unemployment rate."),
            NestedField(2, "measure_text", StringType(), required=True, doc="Human-readable measure name."),
            NestedField(3, "laus_vintage", StringType(), required=True, doc="See raw.bls__laus_county."),
            NestedField(4, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("laus_vintage", "measure_code"),
        comment="BLS LAUS measure code lookup (la.measure), landed verbatim and whole per vintage.",
    ),

    "raw.bls__laus_footnote": TableDef(
        schema=Schema(
            NestedField(1, "footnote_code", StringType(), required=True,
                        doc="Single-letter footnote code, e.g. 'N' = not available."),
            NestedField(2, "footnote_text", StringType(), required=True, doc="Footnote text, as published."),
            NestedField(3, "laus_vintage", StringType(), required=True, doc="See raw.bls__laus_county."),
            NestedField(4, "landed_in", StringType(), required=True,
                        doc="The cancerOnIce release whose ingest landed these rows."),
        ),
        sort_by=("laus_vintage", "footnote_code"),
        comment="BLS LAUS footnote code lookup (la.footnote), landed verbatim and whole per "
                "vintage -- the real, complete set of sentinel/reliability codes this source "
                "publishes (bls_laus.py's FOOTNOTE_TEXT mirrors it and SystemExits on a code "
                "not in this table).",
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
        properties={"comment": d.comment,
                    TableProperties.PARQUET_ROW_GROUP_LIMIT: str(ROW_GROUP_ROWS),
                    **d.properties}))
    return _evolve(table, d, identifier)


def _evolve(table, d, identifier):
    """Add columns the declaration has gained since the table was created,
    reconcile Iceberg identifier fields when the declared business key changed,
    and (issue #120) bring a live table's row-group-limit property and
    declared sort order up to date — both are safe to set in place and cheap
    to check, so this runs on every call rather than only when a column or key
    changed.

    Without the column-add a new column in a TableDef never reaches a live
    table, and the cast in merge.write fails on it. Only optional columns can
    be added: existing rows read NULL for them. A new *required* column has no
    value for the rows already there, so that is a rebuild and this refuses it
    rather than guessing.

    Identifier fields follow the same rule, one level up: Iceberg requires an
    identifier field to be `required` (pyiceberg validates this at schema
    construction), so a business-key change is only reconcilable in place when
    every field in the new key is already required on the live table —
    `update_schema().set_identifier_fields(...)` handles exactly that case.
    When a newly-keyed field is still optional live (e.g. #78's
    provenance.release, whose source_version predates being part of the key),
    pyiceberg cannot verify no existing row is actually NULL there, so
    promoting it to required is refused rather than forced — see
    docs/DEPLOY.md for the rebuild this needs instead.

    ponytail: column additions and identifier-field reconciliation only. A
    changed type, a dropped column or a changed doc string is left alone —
    handle those when one actually happens.
    """
    live_schema = table.schema()
    live = {f.name for f in live_schema.fields}
    missing = [f for f in d.schema.fields if f.name not in live]
    required = [f.name for f in missing if f.required]
    if required:
        raise ValueError(f"{identifier}: declared required column(s) {required} are not in the "
                         "live table; Iceberg cannot add a required column to existing rows — "
                         "rebuild the table")

    if table.properties.get(TableProperties.PARQUET_ROW_GROUP_LIMIT) != str(ROW_GROUP_ROWS):
        with table.transaction() as txn:
            txn.set_properties({TableProperties.PARQUET_ROW_GROUP_LIMIT: str(ROW_GROUP_ROWS)})

    if d.sort_by:
        # Compare by column name, not by SortOrder equality: a table created
        # before #120 has no sort order (or an older declared one), and this is
        # what upgrades it in place. Reader-facing metadata only -- see
        # TableDef.sort_by; merge.overwrite's ORDER BY is the real work.
        current = [live_schema.find_field(f.source_id).name for f in table.sort_order().fields]
        if current != list(d.sort_by):
            with table.update_sort_order() as update:
                for col in d.sort_by:
                    update.asc(col, IdentityTransform())

    declared_key = set(d.business_key)
    live_key = {f.name for f in live_schema.fields if f.field_id in live_schema.identifier_field_ids}
    key_changed = bool(declared_key) and declared_key != live_key
    if not missing and not key_changed:
        return table

    if key_changed:
        not_required = sorted(n for n in declared_key
                              if n in live and not live_schema.find_field(n).required)
        if not_required:
            raise ValueError(
                f"{identifier}: business key changed to {sorted(declared_key)}, but "
                f"{not_required} is still optional on the live table. Iceberg identifier "
                f"fields must be required, and this cannot verify no existing row is NULL "
                f"there, so it refuses to promote it in place — see docs/DEPLOY.md for the "
                f"rebuild.")

    with table.update_schema() as update:
        for f in missing:
            update.add_column(f.name, f.field_type, doc=f.doc)
        if key_changed:
            update.set_identifier_fields(*declared_key)
    # `table` is updated in place by the commit above (pyiceberg sets
    # table.metadata directly); returning it explicitly fixes a latent bug
    # where create() got None back from here whenever a column was added.
    return table
