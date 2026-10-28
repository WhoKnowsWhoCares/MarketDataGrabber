"""Тесты для scripts/run_marts.py (мок-клиент ClickHouse, без живого CH)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_marts import (  # noqa: E402
    execute_transform,
    list_transforms,
    main,
    record_run,
    split_statements,
)


class MockClient:
    def __init__(self):
        self.commands: list[str] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def query(self, sql: str):  # не используется в этих тестах
        raise AssertionError("query не должен вызываться в тестах раннера")


SQL_THREE = """
-- Комментарий-шапка
CREATE TABLE IF NOT EXISTS t1 (x UInt8) ENGINE = MergeTree ORDER BY x;

-- чисто-комментарный блок между стейтментами


INSERT INTO t1 VALUES (1);

INSERT INTO t1 VALUES (2);
"""


def test_split_statements_three_in_order():
    stmts = split_statements(SQL_THREE)
    assert len(stmts) == 3
    assert stmts[0].startswith("CREATE TABLE")
    assert stmts[1].startswith("INSERT INTO t1 VALUES (1)")
    assert stmts[2].startswith("INSERT INTO t1 VALUES (2)")


def test_execute_transform_executes_in_order_and_records(tmp_path):
    client = MockClient()
    path = tmp_path / "mart_x.sql"
    path.write_text(SQL_THREE, encoding="utf-8")

    ok = execute_transform(client, path)

    assert ok is True
    # 3 стейтмента + 1 INSERT в etl_runs
    assert len(client.commands) == 4
    assert client.commands[0].startswith("CREATE TABLE")
    assert client.commands[1].startswith("INSERT INTO t1 VALUES (1)")
    assert client.commands[2].startswith("INSERT INTO t1 VALUES (2)")
    assert "etl_runs" in client.commands[3]
    assert "'ok'" in client.commands[3]


def test_split_skips_comments_and_blanks_only():
    sql = """
    -- только комментарий


    SELECT 1;
    """
    stmts = split_statements(sql)
    assert stmts == ["SELECT 1"]


def test_execute_transform_stops_on_error_and_records_failed(tmp_path):
    client = MockClient()

    def fail_on_insert(sql: str) -> None:
        if sql.startswith("INSERT INTO t1 VALUES (1)"):
            raise RuntimeError("boom")
        client.commands.append(sql)

    client.command = fail_on_insert
    path = tmp_path / "mart_bad.sql"
    path.write_text(SQL_THREE, encoding="utf-8")

    ok = execute_transform(client, path)
    assert ok is False
    # первый стейтмент прошёл, второй упал → запись etl_runs со статусом failed
    assert len(client.commands) == 2
    assert "'failed'" in client.commands[1]


def test_record_run_values():
    client = MockClient()
    record_run(
        client, "mart_x", statements=2, failed=1,
        started_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        status="failed",
    )
    assert len(client.commands) == 1
    assert "'failed'" in client.commands[0]
    assert "mart_x" in client.commands[0]
    assert "market_data_aux.etl_runs" in client.commands[0]


def test_list_transforms_skips_underscore(tmp_path):
    (tmp_path / "a_mart.sql").write_text("SELECT 1;")
    (tmp_path / "_helper.sql").write_text("SELECT 2;")
    (tmp_path / "b_mart.sql").write_text("SELECT 3;")
    (tmp_path / "notes.txt").write_text("not sql")
    files = list_transforms(tmp_path)
    assert [p.name for p in files] == ["a_mart.sql", "b_mart.sql"]


def test_dry_run_does_not_call_command(tmp_path):
    (tmp_path / "m1.sql").write_text("SELECT 1;\nSELECT 2;")

    import scripts.run_marts as rm

    original_get_client = rm.get_client
    original_dir = rm.TRANSFORMS_DIR
    rm.TRANSFORMS_DIR = tmp_path

    def no_client(settings):
        raise AssertionError("get_client не должен вызываться в dry-run")

    rm.get_client = no_client
    try:
        rc = main(["--dry-run"])
    finally:
        rm.get_client = original_get_client
        rm.TRANSFORMS_DIR = original_dir
    assert rc == 0


def test_only_selects_single_file(tmp_path):
    (tmp_path / "m1.sql").write_text("SELECT 1;")
    (tmp_path / "m2.sql").write_text("SELECT 2;")

    import scripts.run_marts as rm

    client = MockClient()
    original = (rm.TRANSFORMS_DIR, rm.get_client)
    rm.TRANSFORMS_DIR = tmp_path
    rm.get_client = lambda settings: client
    try:
        rc = main(["--only", "m2"])
    finally:
        rm.TRANSFORMS_DIR, rm.get_client = original
    assert rc == 0
    # 1 стейтмент m2 + 1 запись etl_runs; m1 не выполнялся
    assert len(client.commands) == 2
    assert client.commands[0].startswith("SELECT 2")
    assert "etl_runs" in client.commands[1]


def test_only_unknown_file_returns_error(tmp_path):
    import scripts.run_marts as rm

    original = rm.TRANSFORMS_DIR
    rm.TRANSFORMS_DIR = tmp_path
    try:
        rc = main(["--only", "nope"])
    finally:
        rm.TRANSFORMS_DIR = original
    assert rc == 2
