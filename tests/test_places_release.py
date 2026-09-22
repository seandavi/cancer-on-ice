"""PLACES through the shared release contract, offline (#160 step 1).

Reuses `test_places.py`'s handcrafted 2024/2025 fixtures. Each release is
ingested through the current product path (`places.ingest` into a tmp local
Iceberg catalog) with the Arrow tables it hands to `merge` captured on the
way, so the same transform output feeds both sides of the comparison:

  product   -- what `merge.merge` / `merge.write` actually landed;
  candidate -- what cdsci-lake's `plan_scd2_release` does with it under the
               SPEC.md writer scopes (`contracts.observation_scope`), applied
               to an in-memory DuckDB history and built into a `LocalDirStore`
               release.

Nothing here writes to a live table, the network, Iceberg on R2, or a
Frozen DuckLake. The candidate history lives in DuckDB memory only.

Needs cdsci-lake: `uv pip install -e ../cdsci-lake` (README § Develop); skipped otherwise.
"""

import duckdb
import pyarrow as pa
import pytest

pytest.importorskip("cdsci.lake")

from cdsci.lake.history import (
    CompleteScope,
    SCD2Policy,
    plan_scd2_release,
)
from cdsci.lake.publish.builder import (
    LocalDirStore,
    build_release,
    finalize_release,
)
from cdsci.lake.publish.release import (
    ArtifactStatus,
    ReleaseCandidate,
    SourceAssetVersion,
)
from cdsci.lake.publish.verify import verify_release
from pyiceberg.expressions import EqualTo
from test_places import (  # noqa: F401 -- fixtures
    DATA_2025,
    REL1,
    REL2,
    csv2024,
    rows,
    write,
)

from canceronice import contracts, merge, places, schemas

# The definition writer's scope. The product's interim #76 fix (places.py) narrows
# this to the ids a release asserts; the candidate keeps the source-wide scope and
# hands the planner a complete definition state instead (see `_definitions`).
DEFINITION_SCOPE = CompleteScope("source = 'PLACES'")
TABLES = ("measure.observation", "measure.definition")
CANDIDATE_RELEASE = REL2

# 2025 with MAMMOUSE's Short_Question_Text (-> measure.definition.label) revised:
# the one attribute change the planner must record as a closed + reopened interval.
DATA_2025_RELABELLED = [r.replace(",Mammography,", ",Mammography use,") for r in DATA_2025]
assert sum(a != b for a, b in zip(DATA_2025, DATA_2025_RELABELLED)) == 1


@pytest.fixture
def csv2025_relabelled(tmp_path):
    return write(tmp_path / "tiny_places_2025_relabelled.csv", DATA_2025_RELABELLED)


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


def _plan(con, identifier, history, incoming, release, scope):
    return plan_scd2_release(
        con, con.table(history), con.from_arrow(incoming), release=release, scope=scope,
        policy=SCD2Policy(business_key=_key(identifier)), release_key=contracts.release_key)


def _at(con, table, release):
    """Rows of history `table` valid at `release` -- the release_snapshot materialization.
    Release labels are zero-padded YYYY.MM[.NN] by design (schemas.py provenance.release
    doc), so the SQL string comparison is the declared ordering, not an accident."""
    return con.sql(f"SELECT * FROM {table} WHERE valid_from <= '{release}' "
                   f"AND (valid_to IS NULL OR valid_to > '{release}')")


def _dangling(con, release, definitions="hist_definition"):
    """observation.measure_id values valid at `release` with no definition valid at
    `release` -- the domain FK gate; must be empty for anything that finalizes."""
    obs = _at(con, "hist_observation", release).sql_query()
    defs = _at(con, definitions, release).sql_query()
    return {r[0] for r in con.sql(f"""
        SELECT DISTINCT o.measure_id FROM ({obs}) o
        LEFT JOIN ({defs}) d USING (measure_id)
        WHERE d.measure_id IS NULL""").fetchall()}


def _definitions(con, new, old):
    """The complete definition state a release asserts under `DEFINITION_SCOPE`: what
    this PLACES edition publishes, plus every earlier-edition definition it does not
    re-publish -- because the earlier edition's observations stay live (a new release
    never retires an earlier one, SPEC.md § Measures) and must keep resolving. This
    is what the product's interim #76 fix converges to (every id ever asserted);
    recorded as the shape #76 needs, not decided here."""
    con.register("d_new", new)
    con.register("d_old", old)
    return con.sql("SELECT * FROM d_new UNION ALL SELECT * FROM d_old "
                   "WHERE measure_id NOT IN (SELECT measure_id FROM d_new)").to_arrow_table()


@pytest.fixture
def lake(cat, csv2024, csv2025_relabelled, captured):  # noqa: F811 -- imported fixtures
    """Product state after both releases, plus the candidate history planned from the
    same transform output. Returns (con, plans, incoming, product_live_rows_by_table).
    `r1_definition` is a copy of the definition history as of REL1, for the #76 probe."""
    con = duckdb.connect()
    for identifier in TABLES:
        con.register("_empty", contracts.PLACES.tables[identifier].arrow_schema().empty_table())
        con.execute(f"CREATE TABLE {_hist(identifier)} AS SELECT * FROM _empty")

    plans = {}
    incoming = {}
    for release, places_release, url in ((REL1, "2024", csv2024),
                                         (REL2, "2025", csv2025_relabelled)):
        places.ingest(cat, release, places_release=places_release, url=url)
        incoming[places_release] = {
            t: captured[t].cast(_incoming_schema(contracts.PLACES.tables[t])) for t in TABLES}

    # R1: the 2024 release, every key new.
    plans[(REL1, "measure.observation")] = _plan(
        con, "measure.observation", "hist_observation", incoming["2024"]["measure.observation"],
        REL1, contracts.observation_scope("2024"))
    plans[(REL1, "measure.definition")] = _plan(
        con, "measure.definition", "hist_definition", incoming["2024"]["measure.definition"],
        REL1, DEFINITION_SCOPE)
    for t in TABLES:
        _apply(con, _hist(t), _key(t), plans[(REL1, t)])
    con.execute("CREATE TABLE r1_definition AS SELECT * FROM hist_definition")

    # R2: observations under the SPEC scope (source, source_release) -- the 2025
    # edition's complete state, which cannot touch 2024's rows; definitions as the
    # complete PLACES definition state (`_definitions`).
    plans[(REL2, "measure.observation")] = _plan(
        con, "measure.observation", "hist_observation", incoming["2025"]["measure.observation"],
        REL2, contracts.observation_scope("2025"))
    plans[(REL2, "measure.definition")] = _plan(
        con, "measure.definition", "hist_definition",
        _definitions(con, incoming["2025"]["measure.definition"],
                     incoming["2024"]["measure.definition"]),
        REL2, DEFINITION_SCOPE)
    for t in TABLES:
        _apply(con, _hist(t), _key(t), plans[(REL2, t)])

    product = {t: [r for r in rows(cat, t, row_filter=EqualTo("source", "PLACES"))
                   if r.get("valid_to") is None] for t in TABLES}
    return con, plans, incoming, product


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
    assert "source_release is in the key" in contracts.OBSERVATION.grain
    # SPEC names no coordinate system on these two tables (lat/lon live on geography.unit)
    assert all(c.coordinate_system is None for c in contracts.OBSERVATION.columns)
    assert contracts.PLACES.required_artifacts == {"parquet"}


def test_added_validity_fields_continue_the_tabledef_ids():
    ids = [f.field_id for f in contracts._validity(schemas.TABLES["measure.definition"].schema.fields)]
    assert ids == [11, 12]


def test_observation_scope_is_per_source_release_and_allowlisted():
    assert contracts.observation_scope("2025").predicate_sql == \
        "source = 'PLACES' AND source_release = '2025'"
    with pytest.raises(ValueError):
        contracts.observation_scope("2025' OR 1=1 --")


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
    # containment: every per-release observation scope, and the definition scope, is
    # inside `source = 'PLACES'`
    assert con.sql(f"SELECT count(*) FROM {t} WHERE NOT (source = 'PLACES')").fetchone()[0] == 0


def test_two_releases_plan_and_satisfy_scd2_postconditions(lake):
    con, plans, _, _ = lake
    r1o, r1d = plans[(REL1, "measure.observation")], plans[(REL1, "measure.definition")]
    assert (r1o.inserted, r1o.changed, r1o.retired) == (6, 0, 0)
    assert (r1d.inserted, r1d.changed, r1d.retired) == (4, 0, 0)

    # 2025's four rows are all new keys (source_release is in the key); under its own
    # scope the plan cannot see, let alone retire, 2024's six.
    r2o = plans[(REL2, "measure.observation")]
    assert (r2o.unchanged, r2o.inserted, r2o.changed, r2o.retired) == (0, 4, 0, 0)
    r2d = plans[(REL2, "measure.definition")]
    assert (r2d.unchanged, r2d.inserted, r2d.changed, r2d.retired) == (3, 2, 1, 0)
    for t in TABLES:
        _postconditions(con, t)


def test_every_observation_measure_id_resolves_in_definition_at_the_same_release(lake):
    """Domain FK gate: at every release R, each observation.measure_id valid at R has
    a definition valid at R -- and exactly one version of it."""
    con, _, _, _ = lake
    for release in (REL1, REL2):
        assert _dangling(con, release) == set()
        obs, defs = (_at(con, t, release).sql_query() for t in ("hist_observation",
                                                                  "hist_definition"))
        assert con.sql(f"""
            SELECT count(*) FROM (SELECT DISTINCT measure_id FROM ({obs})) o
            JOIN ({defs}) d USING (measure_id)
        """).fetchone()[0] == con.sql(f"SELECT count(DISTINCT measure_id) FROM ({obs})"
                                      ).fetchone()[0]


def test_definition_attribute_change_closes_and_reopens_one_key(lake):
    con, plans, _, _ = lake
    assert plans[(REL2, "measure.definition")].changed == 1
    assert con.sql("SELECT label, valid_from, valid_to FROM hist_definition "
                   "WHERE measure_id = 'PLACES:MAMMOUSE:age_adjusted' ORDER BY valid_from"
                   ).fetchall() == [("Mammography", REL1, REL2), ("Mammography use", REL2, None)]


def test_76_evidence_dropped_definition_is_retired_while_its_2024_observation_stays_live(lake):
    """EVIDENCE for #76, not a decision -- and never finalized. Under scd2_release with
    scope source='PLACES' and the 2025 release asserting ONLY its own definitions,
    the planner CLOSES the definitions 2025 no longer carries at valid_to=REL2. The
    2024 observation referencing PLACES:ISOLATION:crude is untouched and still
    current (its scope is per source_release), so at REL2 its FK no longer resolves
    to a definition valid at REL2 (it does at REL1). `_definitions` is what the
    finalizing candidate does instead."""
    con, _, incoming, _ = lake
    r2d = _plan(con, "measure.definition", "r1_definition",
                incoming["2025"]["measure.definition"], REL2, DEFINITION_SCOPE)
    assert (r2d.unchanged, r2d.inserted, r2d.changed, r2d.retired) == (1, 2, 1, 2)
    assert {r["measure_id"] for r in r2d.to_open} == {
        "PLACES:LONELINESS:crude", "PLACES:LPA:age_adjusted", "PLACES:MAMMOUSE:age_adjusted"}
    # CSMOKING:age_adjusted is retired only because the 2025 FIXTURE omits it (the
    # real 2025 file carries it); ISOLATION:crude is the real #76 rename; MAMMOUSE
    # closes because its label changed, not because it was dropped.
    assert {(r["measure_id"], r["valid_from"], r["valid_to"]) for r in r2d.to_close} == {
        ("PLACES:ISOLATION:crude", REL1, REL2), ("PLACES:CSMOKING:age_adjusted", REL1, REL2),
        ("PLACES:MAMMOUSE:age_adjusted", REL1, REL2)}
    _apply(con, "r1_definition", _key("measure.definition"), r2d)

    iso_def = con.sql("SELECT valid_from, valid_to FROM r1_definition "
                      "WHERE measure_id = 'PLACES:ISOLATION:crude'").fetchall()
    assert iso_def == [(REL1, REL2)]  # one closed interval, never deleted
    iso_obs = con.sql("SELECT source_release, valid_from, valid_to FROM hist_observation "
                      "WHERE measure_id = 'PLACES:ISOLATION:crude'").fetchall()
    assert iso_obs == [("2024", REL1, None)]  # still current at REL2

    assert _dangling(con, REL1, "r1_definition") == set()
    assert _dangling(con, REL2, "r1_definition") == {"PLACES:ISOLATION:crude",
                                                     "PLACES:CSMOKING:age_adjusted"}


def test_observation_scope_evidence_source_wide_incoming_retires_the_earlier_release(lake):
    """Why `contracts.observation_scope` is per source_release: with a source-wide
    `source = 'PLACES'` scope, an incoming of 2025 rows alone reads as '2024 is gone'
    and closes all six 2024 rows. Under SPEC.md's `(source, source_release)` scope
    -- what `places.transform` uses -- the same incoming touches nothing from 2024."""
    con, _, incoming, _ = lake
    only_2025 = incoming["2025"]["measure.observation"]
    con.execute("CREATE TABLE probe AS SELECT * FROM hist_observation")
    wide = _plan(con, "measure.observation", "probe", only_2025, "2026.10",
                 CompleteScope("source = 'PLACES'"))
    assert (wide.unchanged, wide.retired) == (4, 6)
    assert {r["source_release"] for r in wide.to_close} == {"2024"}

    narrow = _plan(con, "measure.observation", "probe", only_2025, "2026.10",
                   contracts.observation_scope("2025"))
    assert (narrow.unchanged, narrow.retired) == (4, 0)


# --- 3. cancer gates over candidate rows ---

def test_candidate_rows_pass_cancer_gates(lake):
    con, _, _, _ = lake
    obs = _at(con, "hist_observation", CANDIDATE_RELEASE).to_arrow_table().to_pylist()
    assert len(obs) == 10

    # no suppressed cell reads as a number, both directions, closed enum: every
    # numeric column is NULL on every non-reported row
    numeric = ("value", "lower", "upper", "numerator", "denominator")
    assert all(o["value_status"] in schemas.VALUE_STATUSES for o in obs)
    assert all(o[c] is None for o in obs if o["value_status"] != "reported" for c in numeric)
    assert all(o["value"] is not None for o in obs if o["value_status"] == "reported")
    lpa = next(o for o in obs if o["measure_id"] == "PLACES:LPA:age_adjusted")
    assert lpa["value_status"] == "suppressed_small_count"
    assert sum(o["value_status"] != "reported" for o in obs) == 1  # the fixture's one

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

    # licence gate: every source in the candidate is cleared against the literal
    # allowlist, no table is licence-unknown. SPEC.md § Licence gate makes
    # aggregate-only a pre-ingest assessment, not a column, so nothing to assert here.
    assert {o["source"] for o in obs} == {"PLACES"}
    defs = _at(con, "hist_definition", CANDIDATE_RELEASE).to_arrow_table().to_pylist()
    assert {d["source"] for d in defs} == {"PLACES"}
    assert all(t.license == "public-domain" for t in contracts.PLACES.tables.values())


# --- 4. build, verify, finalize; parity with the product ---

def test_candidate_builds_verifies_and_matches_product_rows(lake, tmp_path):
    con, _, _, product = lake
    assert _dangling(con, CANDIDATE_RELEASE) == set()  # never finalize a dangling FK
    store = LocalDirStore(root=tmp_path / "store")
    candidate = ReleaseCandidate(
        dataset=contracts.PLACES.id, release=CANDIDATE_RELEASE, run_id="test-run",
        built_at="2026-09-22T00:00:00Z",
        destination=f"local://{contracts.PLACES.id}/{CANDIDATE_RELEASE}",
        tables=TABLES, status=ArtifactStatus.STAGED,
        source_asset_versions=tuple(
            SourceAssetVersion(ref="lake.raw.places__county", version=v) for v in ("2024", "2025")),
    )
    tables = {t: _at(con, _hist(t), CANDIDATE_RELEASE) for t in TABLES}
    manifest = build_release(candidate, tables, store, contract=contracts.PLACES)
    # `contract=` arms the required_artifacts gate (design §11.5 #8); without it the
    # gate never runs and an artifact-less manifest finalizes.
    report = verify_release(store, contracts.PLACES.id, CANDIDATE_RELEASE, manifest=manifest,
                            contract=contracts.PLACES)
    assert report.passed, [c for c in report.checks if not c.passed]
    finalize_release(store, manifest, report, contract=contracts.PLACES)
    assert verify_release(store, contracts.PLACES.id, CANDIDATE_RELEASE,
                          contract=contracts.PLACES).passed  # cold reload

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

    # definitions: the product's interim #76 fix keeps every id ever asserted, with the
    # latest text; the candidate at REL2 carries the same six, same labels.
    assert counts["measure.definition"] == len(product["measure.definition"]) == 6
    built = duckdb.sql(f"SELECT * FROM read_parquet('{parquet}/measure.definition/data/*.parquet')"
                       ).to_arrow_table().to_pylist()
    assert {(d["measure_id"], d["label"]) for d in built} == {
        (d["measure_id"], d["label"]) for d in product["measure.definition"]}
