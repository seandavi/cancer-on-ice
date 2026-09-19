"""Offline tests for the core machinery: schema declarations, merge, and the
suppression invariant (SPEC.md Acceptance C). Uses the local sqlite warehouse
only, via the shared `cat` fixture in conftest.py.
"""

import dataclasses

import pyarrow as pa
import pytest
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField

from canceronice import merge, schemas

REL1, REL2 = "2026.08", "2026.09"


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_tables_create_from_declarations(cat):
    for identifier in schemas.TABLES:
        table = schemas.create(cat, identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"


def county(geo_id, fips, name, parent="state:08"):
    return dict(geo_id=geo_id, level="county", fips=fips, vintage=2020, name=name,
                parent_geo_id=parent, aland_m2=None, awater_m2=None,
                centroid_lat=None, centroid_lon=None, geometry_uri=None)


def test_geography_unit_merge_round_trip(cat):
    """Type-2 merge on geography.unit: added, retired and unchanged rows."""
    v1 = pa.Table.from_pylist([
        county("county:08031", "08031", "Denver County"),
        county("county:08013", "08013", "Boulder County"),
    ])
    merge.merge(cat, "geography.unit", v1, REL1, AlwaysTrue())

    v2 = pa.Table.from_pylist([
        county("county:08031", "08031", "Denver County"),   # unchanged
        county("county:08059", "08059", "Jefferson County"),  # new
        # 08013 dropped -> retired
    ])
    counts = merge.merge(cat, "geography.unit", v2, REL2, AlwaysTrue())

    versions = {(r["geo_id"], r["valid_from"], r["valid_to"]) for r in rows(cat, "geography.unit")}
    assert ("county:08031", REL1, None) in versions       # unchanged row, untouched
    assert ("county:08013", REL1, REL2) in versions        # retired at REL2
    assert ("county:08059", REL2, None) in versions        # added at REL2
    assert counts["written"] == 2  # new + retired
    assert counts["unchanged"] == 1


def observation(value_status, value):
    return dict(value_status=value_status, value=value)


def test_check_observations_rejects_suppressed_value_and_unknown_status():
    arrow = pa.Table.from_pylist([
        observation("reported", 1.0),
        observation("suppressed_small_count", 5.0),     # a suppressed row carrying a value
        observation("not_a_real_status", None),          # not in the closed enum
    ])
    with pytest.raises(ValueError, match="2 row"):
        merge.check_observations(arrow)


def test_check_observations_accepts_valid_rows():
    arrow = pa.Table.from_pylist([
        observation("reported", 1.0),
        observation("suppressed_reliability", None),
        observation("not_applicable", None),
    ])
    merge.check_observations(arrow)  # does not raise


def test_manifest_writes_a_provenance_release_row(cat):
    merge.manifest(cat, REL2, "test_source", "http://example.org/data", 42,
                   version="v1", method="release_number")
    row = next(r for r in rows(cat, "provenance.release") if r["source"] == "test_source")
    assert row["release"] == REL2
    assert row["row_count"] == 42
    assert row["source_version"] == "v1"
    assert row["version_method"] == "release_number"


def test_manifest_keeps_one_row_per_source_version(cat):
    """#78: several source releases can land under one catalog release; the
    manifest keyed on (release, source, source_version) keeps a row for each,
    rather than the last one landed overwriting the rest."""
    for version in ("2024", "2025", "2026"):
        merge.manifest(cat, REL1, "places", "http://example.org/places", 10,
                       version=version, method="release_number")
    landed = {r["source_version"]: r for r in rows(cat, "provenance.release")
             if r["source"] == "places"}
    assert set(landed) == {"2024", "2025", "2026"}
    assert all(r["release"] == REL1 for r in landed.values())


def test_manifest_same_version_twice_overwrites_its_own_row(cat):
    merge.manifest(cat, REL1, "places", "http://example.org/places", 10,
                   version="2024", method="release_number")
    merge.manifest(cat, REL1, "places", "http://example.org/places", 20,
                   version="2024", method="release_number")
    landed = [r for r in rows(cat, "provenance.release")
             if r["source"] == "places" and r["source_version"] == "2024"]
    assert len(landed) == 1
    assert landed[0]["row_count"] == 20


def test_evolve_refuses_to_promote_business_key_field_to_required(cat, monkeypatch):
    """#78 changed provenance.release's business key to include
    source_version, which Iceberg requires to be `required` as an identifier
    field. Simulates the real live table: created under the OLD declaration
    (business_key=(release, source), source_version optional) as the first
    live ingest actually did, then asked to evolve under the current one.
    _evolve should refuse rather than silently force the promotion."""
    live_def = schemas.TABLES["provenance.release"]
    old_fields = tuple(
        NestedField(f.field_id, f.name, f.field_type, required=False, doc=f.doc)
        if f.name == "source_version" else f
        for f in live_def.schema.fields
    )
    old_def = dataclasses.replace(live_def, schema=Schema(*old_fields),
                                  business_key=("release", "source"))

    monkeypatch.setitem(schemas.TABLES, "provenance.release", old_def)
    schemas.create(cat, "provenance.release")  # the pre-#78 live table

    monkeypatch.setitem(schemas.TABLES, "provenance.release", live_def)
    with pytest.raises(ValueError, match="source_version.*optional"):
        schemas.create(cat, "provenance.release")
