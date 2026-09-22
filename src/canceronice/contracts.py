"""Shared release contracts (cdsci-lake `TableContract`) for the PLACES slice (#160).

`schemas.py` stays the single source of table structure: every column here is
DERIVED from its `TableDef` (name, type, doc, required-ness), never copied by
hand, and `tests/test_places_release.py` asserts the two stay equal. This
module only adds what the Iceberg schema cannot say and cdsci-lake's contract
can: the temporal model by name, FK namespaces, closed enums, and what a NULL
means (SPEC.md § Suppression is a value, not a NULL).

Lives beside `schemas.py` rather than inside it so the base package keeps
importing without cdsci-lake installed (not declared in pyproject.toml at all
until it publishes: `uv pip install -e ../cdsci-lake`, README § Develop);
nothing in the ingest path imports this module.

**Temporal model.** Both tables are `scd2_release` (issue #160's working
assumption). SPEC.md § Versioning model carries biocOnIce's row-history model
over unchanged for `measure.observation` (`valid_from`/`valid_to` = the
cancerOnIce release that first/last served the row), which IS `scd2_release`.
For `measure.definition`, SPEC.md lists no validity columns and `schemas.py`
declares it "a lookup table, not versioned in place"; the contract below adds
`valid_from`/`valid_to` so the release model can carry definition history --
the thing #76 (ISOLATION -> LONELINESS) needs and the interim in-place fix in
`places.py` cannot give. That is a working assumption pending #19/#76, not a
decision taken here; the difference from `schemas.py` is deliberate and
reported, not drifted into.

Domain values (namespaces, enums, suppression meaning, licence) are this
repository's; cdsci-lake supplies the dataclasses and the mechanics only.
"""

from cdsci.lake.contracts import (
    ColumnContract,
    DatasetContract,
    TableContract,
    TemporalModel,
)
from cdsci.lake.history import CompleteScope
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
)

from . import places, schemas

# Iceberg primitive -> cdsci-lake's canonical Arrow type string.
_ARROW = {StringType: "string", IntegerType: "int32", LongType: "int64",
          DoubleType: "double", BooleanType: "bool"}

# What NULL means, per column, where a NULL is a statement and not an absence.
# The suppression invariant lives on `value`: a NULL there is never "unknown".
_OBSERVATION = {
    "measure_id": dict(identifier_namespace="measure.definition"),
    "geo_id": dict(identifier_namespace="geography.unit"),
    "stratum_id": dict(identifier_namespace="measure.stratum"),
    "value": dict(null_meaning="value_status != 'reported': the source suppressed or did not "
                               "publish this cell (SPEC.md Acceptance C). Never a number."),
    "lower": dict(null_meaning="No interval published, or the cell is not 'reported'."),
    "upper": dict(null_meaning="No interval published, or the cell is not 'reported'."),
    "interval_level": dict(null_meaning="No interval published."),
    "numerator": dict(null_meaning="Not published by the source."),
    "denominator": dict(null_meaning="Not published by the source."),
    "value_status": dict(enum=schemas.VALUE_STATUSES),
    "reliability_flag": dict(null_meaning="No reliability flag published."),
    "trend": dict(null_meaning="No trend call published."),
    "valid_from": dict(identifier_namespace="provenance.release"),
    "valid_to": dict(identifier_namespace="provenance.release",
                     null_meaning="Current version -- not unknown."),
}

_DEFINITION = {
    "measure_id": dict(identifier_namespace="measure.definition"),
    "rate_basis": dict(enum=("per_100000", "per_1000000", "percent", "count", "index", "sum")),
    "age_adjustment": dict(null_meaning="Not age-adjusted."),
    "method": dict(enum=("direct", "model_based", "survey_direct", "derived")),
    "cancer_site_code": dict(identifier_namespace="measure.cancer_site",
                             null_meaning="Not cancer-site-specific."),
    "valid_from": dict(identifier_namespace="provenance.release"),
    "valid_to": dict(identifier_namespace="provenance.release",
                     null_meaning="Current version -- not unknown."),
}

# PLACES is public domain (places.py module docstring: Socrata metadata
# `"license": {"name": "Public Domain"}`, checked 2026-09-18). A source with
# no cleared licence never gets a contract (AGENTS.md: license:unknown is a
# hard stop).
PLACES_LICENSE = "public-domain"


def _columns(fields, overrides):
    unknown = set(overrides) - {f.name for f in fields}
    if unknown:
        raise ValueError(f"overrides for columns not in the TableDef: {sorted(unknown)}")
    return tuple(
        ColumnContract(f.name, _ARROW[type(f.field_type)], f.doc, nullable=not f.required,
                       **overrides.get(f.name, {}))
        for f in fields
    )


def _validity(fields):
    """`valid_from`/`valid_to` for a TableDef that has none (see module docstring),
    numbered after the TableDef's own ids."""
    nxt = max(f.field_id for f in fields) + 1
    return (NestedField(nxt, "valid_from", StringType(), required=True, doc=schemas.VALID_FROM),
            NestedField(nxt + 1, "valid_to", StringType(), doc=schemas.VALID_TO))


def _table(identifier, overrides, grain, description):
    d = schemas.TABLES[identifier]
    fields = tuple(d.schema.fields)
    if not any(f.name == "valid_from" for f in fields):
        fields += _validity(fields)
    return TableContract(
        name=identifier,
        description=description,
        grain=grain,
        primary_key=(*d.business_key, "valid_from"),  # row key, as TableDef.iceberg_schema
        temporal_model=TemporalModel.SCD2_RELEASE,
        owner="cancer-on-ice",
        license=PLACES_LICENSE,
        columns=_columns(fields, overrides),
        sort_by=d.sort_by,
        # ponytail: TableDef.partition_by ('source') is dropped -- cdsci-lake's
        # release builder refuses partition_by (single file per table, cdsci-lake#100);
        # partitioning is a pruning hint there, not a correctness mechanism.
    )


OBSERVATION = _table(
    "measure.observation", _OBSERVATION,
    grain="one row per (source, source_release, measure_id, geo_id, geo_vintage, period_start, "
          "period_end, stratum_id) and cancerOnIce validity interval; source_release is in "
          "the key, so a later PLACES edition's revised estimate arrives as a new key, never "
          "as a change to an earlier edition's row",
    description="Every PLACES-published number, one stacked long table (SPEC.md § Measures). "
                "Three distinct time axes: period_start/period_end (what the estimate "
                "describes), source_release (the PLACES edition that published it), "
                "valid_from/valid_to (the cancerOnIce releases that served it).",
)

DEFINITION = _table(
    "measure.definition", _DEFINITION,
    grain="one row per measure_id and cancerOnIce validity interval",
    description="One row per PLACES measure definition (SPEC.md § Measures), with release "
                "history: a definition a release stops asserting closes at that release "
                "rather than being deleted in place.",
)

PLACES = DatasetContract(
    id="canceronice-places",
    title="Catchment Lake, built on the cancerOnIce catalog: CDC PLACES slice",
    description="CDC PLACES county/tract model-based estimates as measure.observation, every "
                "landed PLACES release kept there, plus their measure.definition rows.",
    publisher="cancer-on-ice",
    tables={"measure.observation": OBSERVATION, "measure.definition": DEFINITION},
    # Parquet is the format-neutral release itself. "ducklake" joins this set in
    # #160 step 2, once cdsci-lake's M2 Frozen DuckLake adapter (mid-review) lands.
    required_artifacts=frozenset({"parquet"}),
)


def observation_scope(source_release):
    """The measure.observation writer scope, SPEC.md § Measures: `(source, source_release)`,
    so one PLACES edition's complete state never retires another edition's rows.
    Mirrors `places.transform`'s merge filter; `places.RELEASES` is the allowlist."""
    if source_release not in places.RELEASES:
        raise ValueError(f"unknown PLACES release {source_release!r}")
    return CompleteScope(f"source = 'PLACES' AND source_release = '{source_release}'")


def release_key(release):
    """cancerOnIce release 'YYYY.MM' or 'YYYY.MM.NN' -> ordered numeric tuple
    (schemas.py provenance.release doc). Never compare release labels as strings."""
    return tuple(int(p) for p in release.split("."))
