"""PLACES through the shared release contract, offline (#160 step 1).

Reuses `test_places.py`'s handcrafted 2024/2025 fixtures. Each release is
ingested through the current product path (`places.ingest` into a tmp local
Iceberg catalog) with the Arrow tables it hands to `merge` captured on the
way, so the same transform output feeds both sides of the comparison:

  product   -- what `merge.merge` / `merge.write` actually landed;
  candidate -- what cdsci-lake's `plan_scd2_release` does with it under a
               `source = 'PLACES'` scope, applied to an in-memory DuckDB
               history and built into a `LocalDirStore` release.

Nothing here writes to a live table, the network, Iceberg on R2, or a
Frozen DuckLake. The candidate history lives in DuckDB memory only.
"""

import duckdb
import pyarrow as pa
import pytest
from cdsci.lake.history import CompleteScope, SCD2Policy, plan_scd2_release
from cdsci.lake.publish.builder import LocalDirStore, build_release, finalize_release
from cdsci.lake.publish.release import ArtifactStatus, ReleaseCandidate, SourceAssetVersion
from cdsci.lake.publish.verify import verify_release
from pyiceberg.expressions import EqualTo
from test_places import REL1, REL2, csv2024, csv2025, rows  # noqa: F401 -- fixtures

from canceronice import contracts, merge, places, schemas

SCOPE = CompleteScope("source = 'PLACES'")
TABLES = ("measure.observation", "measure.definition")
CANDIDATE_RELEASE = REL2


def _key(identifier):
    return schemas.TABLES[identifier].business_key


def _hist(identifier):
    return "hist_" + identifier.split(".")[1]


def _incoming_schema(contract):
    validity = ("valid_from", "valid_to")
    return pa.schema([f for f in contract.arrow_schema() if f.name not in validity])


@pytest.fixture
def captured(monkeypatch):
    """The Arrow tables `places.transform` hands to `merge`, keyed by table name --
    the transform's own output, before any Iceberg write, cast to the contract."""
    got = {}
    real_write, real_merge = merge.write, merge.merge

    def write(cat, identifier, arrow, overwrite_filter):
        got[identifier] = arrow
        return real_write(cat, identifier, arrow, overwrite_filter)

    def mrg(cat, identifier, incoming, release, scope, **kw):
        got[identifier] = incoming
        return real_merge(cat, identifier, incoming, release, scope, **kw)

    monkeypatch.setattr(merge, "write", write)
    monkeypatch.setattr(merge, "merge", mrg)
    return got


def _apply(con, table, key, plan):
    assert plan.rejections == ()
    assert plan.to_replace_draft == ()  # no same-release correction in this narrative
    for r in plan.to_close:
        where = " AND ".join(f"{c} = ?" for c in (*key, "valid_from"))
        con.execute(f"UPDATE {table} SET valid_to = ? WHERE {where} AND valid_to IS NULL",
                    [r["valid_to"], *[r[c] for c in key], r["valid_from"]])
    for r in plan.to_open:
        cols = ", ".join(r)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(r))})",
                    list(r.values()))


def _plan(con, identifier, incoming, release):
    return plan_scd2_release(
        con, con.table(_hist(identifier)), con.from_arrow(incoming), release=release,
        scope=SCOPE, policy=SCD2Policy(business_key=_key(identifier)),
        release_key=contracts.release_key)


def _at(con, identifier, release):
    """Rows valid at `release` -- the release_snapshot materialization. Release labels
    are zero-padded YYYY.MM[.NN] by design (schemas.py provenance.release doc), so the
    SQL string comparison is the declared ordering, not an accident."""
    return con.sql(f"SELECT * FROM {_hist(identifier)} WHERE valid_from <= '{release}' "
                   f"AND (valid_to IS NULL OR valid_to > '{release}')")


@pytest.fixture
def lake(cat, csv2024, csv2025, captured):  # noqa: F811 -- imported fixtures
    """Product state after both releases, plus the candidate history planned from the
    same transform output. Returns (con, plans, product_live_rows_by_table)."""
    con = duckdb.connect()
    for identifier in TABLES:
        con.register("_empty", contracts.PLACES.tables[identifier].arrow_schema().empty_table())
        con.execute(f"CREATE TABLE {_hist(identifier)} AS SELECT * FROM _empty")

    plans = {}
    incoming = {}
    for release, places_release, url in ((REL1, "2024", csv2024), (REL2, "2025", csv2025)):
        places.ingest(cat, release, places_release=places_release, url=url)
        incoming[places_release] = {
            t: captured[t].cast(_incoming_schema(contracts.PLACES.tables[t])) for t in TABLES}

    # R1: the 2024 release, every key new.
    for t in TABLES:
        plans[(REL1, t)] = _plan(con, t, incoming["2024"][t], REL1)
        _apply(con, _hist(t), _key(t), plans[(REL1, t)])

    # R2: scope is `source = 'PLACES'`, so incoming must be PLACES' COMPLETE state.
    # Observations: 2024 rows are still asserted (a new release never retires an
    # earlier one, SPEC.md § Measures), so incoming carries both releases.
    # Definitions: what the 2025 release itself asserts -- the #76 shape, where
    # ISOLATION is gone and LONELINESS has taken its slot.
    both = pa.concat_tables([incoming["2024"]["measure.observation"],
                             incoming["2025"]["measure.observation"]])
    plans[(REL2, "measure.observation")] = _plan(con, "measure.observation", both, REL2)
    plans[(REL2, "measure.definition")] = _plan(
        con, "measure.definition", incoming["2025"]["measure.definition"], REL2)
    for t in TABLES:
        _apply(con, _hist(t), _key(t), plans[(REL2, t)])

    product = {t: [r for r in rows(cat, t, row_filter=EqualTo("source", "PLACES"))
                   if r.get("valid_to") is None] for t in TABLES}
    return con, plans, product


# --- 1. contracts derive from schemas.py, not copied ---

def test_contract_columns_equal_tabledef_columns():
    for identifier, contract in contracts.PLACES.tables.items():
        d = schemas.TABLES[identifier]
        expected = [(f.name, f.doc, not f.required) for f in d.schema.fields]
        if not any(n == "valid_from" for n, *_ in expected):
            expected += [("valid_from", schemas.VALID_FROM, False),
                         ("valid_to", schemas.VALID_TO, True)]
        assert [(c.name, c.description, c.nullable) for c in contract.columns] == expected
        assert contract.primary_key == (*d.business_key, "valid_from")
        assert contract.sort_by == d.sort_by
        assert contract.temporal_model.value == "scd2_release"
        assert all(c.description for c in contract.columns)

    obs = {c.name: c for c in contracts.OBSERVATION.columns}
    assert obs["measure_id"].identifier_namespace == "measure.definition"
    assert obs["geo_id"].identifier_namespace == "geography.unit"
    assert obs["stratum_id"].identifier_namespace == "measure.stratum"
    assert obs["value_status"].enum == schemas.VALUE_STATUSES
    assert obs["value"].null_meaning and "reported" in obs["value"].null_meaning
    assert obs["valid_to"].null_meaning
    assert obs["geo_vintage"].arrow_type == "int32" and obs["value"].arrow_type == "double"
    # SPEC names no coordinate system on these two tables (lat/lon live on geography.unit)
    assert all(c.coordinate_system is None for c in contracts.OBSERVATION.columns)


# --- 2. release candidate: §11.4 postconditions and the #76 evidence ---

def _postconditions(con, identifier):
    key = ", ".join(_key(identifier))
    t = _hist(identifier)
    assert con.sql(f"SELECT count(*) FROM (SELECT {key} FROM {t} WHERE valid_to IS NULL "
                   f"GROUP BY {key} HAVING count(*) > 1)").fetchone()[0] == 0
    assert con.sql(f"SELECT count(*) FROM (SELECT {key}, valid_from FROM {t} "
                   f"GROUP BY {key}, valid_from HAVING count(*) > 1)").fetchone()[0] == 0
    assert con.sql(f"""
        SELECT count(*) FROM (
            SELECT valid_to, lead(valid_from) OVER (PARTITION BY {key} ORDER BY valid_from) AS nxt
            FROM {t}) WHERE nxt IS NOT NULL AND (valid_to IS NULL OR valid_to > nxt)
    """).fetchone()[0] == 0
    assert con.sql(f"SELECT count(*) FROM {t} WHERE NOT ({SCOPE.predicate_sql})").fetchone()[0] == 0


def test_two_releases_plan_and_satisfy_scd2_postconditions(lake):
    con, plans, _ = lake
    r1o, r1d = plans[(REL1, "measure.observation")], plans[(REL1, "measure.definition")]
    assert (r1o.inserted, r1o.changed, r1o.retired) == (6, 0, 0)
    assert (r1d.inserted, r1d.changed, r1d.retired) == (4, 0, 0)

    r2o = plans[(REL2, "measure.observation")]
    assert (r2o.unchanged, r2o.inserted, r2o.changed, r2o.retired) == (6, 4, 0, 0)
    for t in TABLES:
        _postconditions(con, t)

    # join at release R: exactly one definition version per observation measure_id at R1
    assert con.sql(f"""
        SELECT count(*) FROM ({_at(con, 'measure.observation', REL1).sql_query()}) o
        LEFT JOIN ({_at(con, 'measure.definition', REL1).sql_query()}) d USING (measure_id)
        WHERE d.measure_id IS NULL
    """).fetchone()[0] == 0


def test_76_evidence_dropped_definition_is_retired_while_its_2024_observation_stays_live(lake):
    """EVIDENCE for #76, not a decision. Under scd2_release with scope source='PLACES'
    and the 2025 release asserting only its own definitions, the planner CLOSES
    the definitions 2025 no longer carries at valid_to=REL2. The 2024 observation
    referencing PLACES:ISOLATION:crude is untouched and still current, so at REL2
    its FK no longer resolves to a definition valid at REL2 (it does at REL1)."""
    con, plans, _ = lake
    r2d = plans[(REL2, "measure.definition")]
    assert (r2d.unchanged, r2d.inserted, r2d.changed, r2d.retired) == (2, 2, 0, 2)
    assert {r["measure_id"] for r in r2d.to_open} == {"PLACES:LONELINESS:crude",
                                                        "PLACES:LPA:age_adjusted"}
    # CSMOKING:age_adjusted is retired only because the 2025 FIXTURE omits it (the
    # real 2025 file carries it); ISOLATION:crude is the real #76 rename.
    assert {(r["measure_id"], r["valid_from"], r["valid_to"]) for r in r2d.to_close} == {
        ("PLACES:ISOLATION:crude", REL1, REL2), ("PLACES:CSMOKING:age_adjusted", REL1, REL2)}

    iso_def = con.sql("SELECT valid_from, valid_to FROM hist_definition "
                      "WHERE measure_id = 'PLACES:ISOLATION:crude'").fetchall()
    assert iso_def == [(REL1, REL2)]  # one closed interval, never deleted
    iso_obs = con.sql("SELECT source_release, valid_from, valid_to FROM hist_observation "
                      "WHERE measure_id = 'PLACES:ISOLATION:crude'").fetchall()
    assert iso_obs == [("2024", REL1, None)]  # still current at REL2

    def dangling(release):
        obs = _at(con, "measure.observation", release).sql_query()
        defs = _at(con, "measure.definition", release).sql_query()
        return {r[0] for r in con.sql(f"""
            SELECT DISTINCT o.measure_id FROM ({obs}) o
            LEFT JOIN ({defs}) d USING (measure_id)
            WHERE d.measure_id IS NULL""").fetchall()}
    assert dangling(REL1) == set()
    assert dangling(REL2) == {"PLACES:ISOLATION:crude", "PLACES:CSMOKING:age_adjusted"}


def test_observation_scope_evidence_per_release_incoming_retires_the_earlier_release(lake):
    """Why the R2 observation plan above carries both releases: with the packet's
    `source = 'PLACES'` scope, an incoming of 2025 rows alone reads as '2024 is gone'
    and closes all six 2024 rows. SPEC.md's merge scope `(source, source_release)`
    is what the product uses (`places.transform`); under that scope the same
    2025-only incoming touches nothing from 2024. Reported, not decided here."""
    con, _, _ = lake
    only_2025 = con.sql("SELECT * EXCLUDE (valid_from, valid_to) FROM hist_observation "
                        "WHERE source_release = '2025'").to_arrow_table()
    con.execute("CREATE TABLE probe AS SELECT * FROM hist_observation")
    key = _key("measure.observation")
    wide = plan_scd2_release(
        con, con.table("probe"), con.from_arrow(only_2025), release="2026.10", scope=SCOPE,
        policy=SCD2Policy(business_key=key), release_key=contracts.release_key)
    assert (wide.unchanged, wide.retired) == (4, 6)
    assert {r["source_release"] for r in wide.to_close} == {"2024"}

    narrow = plan_scd2_release(
        con, con.table("probe"), con.from_arrow(only_2025), release="2026.10",
        scope=CompleteScope("source = 'PLACES' AND source_release = '2025'"),
        policy=SCD2Policy(business_key=key), release_key=contracts.release_key)
    assert (narrow.unchanged, narrow.retired) == (4, 0)


# --- 3. cancer gates over candidate rows ---

def test_candidate_rows_pass_cancer_gates(lake):
    con, _, _ = lake
    obs = _at(con, "measure.observation", CANDIDATE_RELEASE).to_arrow_table().to_pylist()
    assert len(obs) == 10

    # no suppressed cell reads as a number, both directions, closed enum
    assert all(o["value_status"] in schemas.VALUE_STATUSES for o in obs)
    assert all(o["value"] is None for o in obs if o["value_status"] != "reported")
    assert all(o["value"] is not None for o in obs if o["value_status"] == "reported")
    lpa = next(o for o in obs if o["measure_id"] == "PLACES:LPA:age_adjusted")
    assert lpa["value_status"] == "suppressed_small_count"
    assert (lpa["value"], lpa["lower"], lpa["upper"]) == (None, None, None)

    # three temporal axes: three distinct, populated columns on every row
    for o in obs:
        assert None not in (o["source_release"], o["period_start"], o["period_end"],
                            o["valid_from"])
        assert len({o["source_release"], o["period_start"], o["valid_from"]}) == 3
    denver = next(o for o in obs if o["geo_id"] == "county:08031" and o["source_release"] == "2025")
    assert (denver["period_start"], denver["source_release"], denver["valid_from"]) == (
        "2023", "2025", REL2)

    # geography carries a level and a vintage (§11.10 #4)
    assert all(o["geo_id"].split(":")[0] in ("nation", "county", "tract") for o in obs)
    assert {o["geo_vintage"] for o in obs} == {2020}

    # licence gate: every source in the candidate is cleared, no table is licence-unknown
    assert {o["source"] for o in obs} == {"PLACES"}
    defs = _at(con, "measure.definition", CANDIDATE_RELEASE).to_arrow_table().to_pylist()
    assert {d["source"] for d in defs} == {"PLACES"}
    assert all(t.license == contracts.PLACES_LICENSE != "unknown"
               for t in contracts.PLACES.tables.values())


# --- 4. build, verify, finalize; parity with the product ---

def test_candidate_builds_verifies_and_matches_product_rows(lake, tmp_path):
    con, _, product = lake
    store = LocalDirStore(root=tmp_path / "store")
    candidate = ReleaseCandidate(
        dataset=contracts.PLACES.id, release=CANDIDATE_RELEASE, run_id="test-run",
        built_at="2026-09-22T00:00:00Z",
        destination=f"local://{contracts.PLACES.id}/{CANDIDATE_RELEASE}",
        tables=TABLES, status=ArtifactStatus.STAGED,
        source_asset_versions=tuple(
            SourceAssetVersion(ref="lake.raw.places__county", version=v) for v in ("2024", "2025")),
    )
    tables = {t: _at(con, t, CANDIDATE_RELEASE) for t in TABLES}
    manifest = build_release(candidate, tables, store, contract=contracts.PLACES)
    report = verify_release(store, contracts.PLACES.id, CANDIDATE_RELEASE, manifest=manifest)
    assert report.passed, [c for c in report.checks if not c.passed]
    finalize_release(store, manifest, report)
    assert verify_release(store, contracts.PLACES.id, CANDIDATE_RELEASE).passed  # cold reload

    counts = {t.name: t.row_count for t in manifest.tables}
    # observations: identical to the product's live rows (business key, value, status,
    # validity), not just the same count
    assert counts["measure.observation"] == len(product["measure.observation"]) == 10
    parquet = tmp_path / "store" / contracts.PLACES.id / CANDIDATE_RELEASE / "tables"
    built = duckdb.sql(f"SELECT * FROM read_parquet('{parquet}/measure.observation/data/*.parquet')"
                       ).to_arrow_table().to_pylist()
    fields = (*_key("measure.observation"), "value", "value_status", "valid_from", "valid_to")
    assert {tuple(r[f] for f in fields) for r in built} == {
        tuple(r[f] for f in fields) for r in product["measure.observation"]}

    # definitions: the product's interim #76 fix keeps every id ever asserted (6);
    # the scd2_release candidate at REL2 carries only what 2025 asserts (4). The
    # difference IS the #76 question, recorded here rather than papered over.
    assert counts["measure.definition"] == 4 and len(product["measure.definition"]) == 6
    assert ({d["measure_id"] for d in product["measure.definition"]}
            - {d["measure_id"] for d in tables["measure.definition"].to_arrow_table().to_pylist()}
            == {"PLACES:ISOLATION:crude", "PLACES:CSMOKING:age_adjusted"})
