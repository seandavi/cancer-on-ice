"""docs/examples/seerstat_join.sql has the only non-trivial logic among the
client docs (suppression detection via TRY_CAST, FIPS extraction via regexp)
-- worth one offline check. Runs the *actual* file against a local stub of
the three tables it joins, standing in for the real `ATTACH ... AS coi`.
"""

from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).parent.parent
SQL = (REPO_ROOT / "docs/examples/seerstat_join.sql").read_text()


def test_seerstat_join_handles_suppression_and_fips():
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS coi")
    con.execute("""
        CREATE SCHEMA coi.geography;
        CREATE TABLE coi.geography.unit (geo_id VARCHAR, name VARCHAR, level VARCHAR,
                                          vintage INTEGER, valid_to VARCHAR);
        INSERT INTO coi.geography.unit VALUES
            ('county:08001', 'Adams County', 'county', 2020, NULL),
            ('county:08003', 'Alamosa County', 'county', 2020, NULL),
            ('county:08013', 'Boulder County', 'county', 2020, NULL),
            ('county:08031', 'Denver County', 'county', 2020, NULL),
            ('county:08061', 'Kiowa County', 'county', 2020, NULL);

        CREATE SCHEMA coi.measure;
        CREATE TABLE coi.measure.observation (geo_id VARCHAR, source VARCHAR,
            measure_id VARCHAR, source_release VARCHAR, value DOUBLE,
            value_status VARCHAR, valid_to VARCHAR);
        INSERT INTO coi.measure.observation VALUES
            ('county:08001', 'PLACES', 'PLACES:MAMMOUSE:age_adjusted', '2025', 64.9, 'reported', NULL),
            ('county:08003', 'PLACES', 'PLACES:MAMMOUSE:age_adjusted', '2025', NULL, 'suppressed_small_count', NULL);

        CREATE SCHEMA coi.facility;
        CREATE TABLE coi.facility.site (geo_id VARCHAR, kind VARCHAR, valid_to VARCHAR);
        INSERT INTO coi.facility.site VALUES ('county:08001', 'fqhc', NULL), ('county:08001', 'fqhc', NULL);
    """)

    # Skip the real ATTACH/INSTALL block (the local `coi` stub replaces it) --
    # everything from the first numbered comment onward is the actual logic.
    body = SQL[SQL.index("-- 1. Read the export"):].replace(
        "docs/examples/seerstat_synthetic_export.csv",
        str(REPO_ROOT / "docs/examples/seerstat_synthetic_export.csv"),
    )
    create_stmt, select_stmt = body.split("-- 2. Join to lake context")
    con.execute(create_stmt)
    rows = con.sql("--" + select_stmt).fetchall()
    by_geo = {r[1]: r for r in rows}

    # Reported cell: real numbers pass through.
    reported = by_geo["county:08001"]
    assert reported[4] == 142  # seer_count
    assert reported[6] == "reported"  # seer_status
    assert reported[9] == 64.9  # mammo_screening_pct
    assert reported[11] == 2  # n_fqhc

    # Suppressed cell ('^' in the synthetic export): no number leaks through.
    suppressed = by_geo["county:08061"]
    assert suppressed[4] is None  # seer_count
    assert suppressed[5] is None  # seer_age_adj_rate
    assert suppressed[6] == "suppressed"
    assert suppressed[11] is None  # no fqhc rows stubbed for this county

    # FIPS extraction kept the leading zero.
    assert all(geo.startswith("county:0") for geo in by_geo)
