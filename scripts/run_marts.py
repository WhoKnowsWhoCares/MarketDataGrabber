#!/usr/bin/env python3
"""Идемпотентный раннер gold-мартов: применяет transforms/*.sql к ClickHouse.

Читает все transforms/*.sql (кроме служебных с префиксом `_`) по алфавиту,
сплитит на стейтменты по `;\\n` и выполняет каждый через client.command().
После выполнения каждого файла пишет запись в market_data_aux.etl_runs.
Повторный запуск безопасен: марты идемпотентны (ReplacingMergeTree/CREATE
OR REPLACE), вставки дедуплицируются.

Использование:
    set -a; source .env; set +a
    PYTHONPATH=src .venv/bin/python scripts/run_marts.py [--only NAME] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.logging import get_logger, setup_logging

log = get_logger("run_marts")

TRANSFORMS_DIR = ROOT / "transforms"
ETL_RUNS_TABLE = "market_data_aux.etl_runs"


def list_transforms(directory: Path | None = None) -> list[Path]:
    """Все *.sql кроме служебных с префиксом `_`, отсортированные по имени."""
    directory = directory or TRANSFORMS_DIR
    return sorted(
        p for p in directory.glob("*.sql") if not p.name.startswith("_")
    )


def split_statements(sql_text: str) -> list[str]:
    """Сплит по `;` на границах строк; пустые и чисто-комментарные куски — мимо.

    Строчные комментарии `-- ...` вырезаются: как отдельные строки (кусок из
    одних комментариев не выполняется), так и внутри стейтмента (в CH уходят
    только сами стейтменты).
    """
    statements: list[str] = []
    for chunk in sql_text.split(";\n"):
        lines = [
            stripped
            for line in chunk.splitlines()
            if (stripped := re.sub(r"--.*$", "", line).strip())
        ]
        if not lines:
            continue
        statements.append("\n".join(lines).rstrip(";").rstrip())
    return statements


def _code_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — версию не нашли, не критично
        return ""


def record_run(
    client,
    transform: str,
    statements: int,
    failed: int,
    started_at: datetime,
    status: str,
) -> None:
    """Запись о прогоне одного transform-файла в market_data_aux.etl_runs."""
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    start_str = started_at.strftime("%Y-%m-%d %H:%M:%S")
    finish_str = finished.strftime("%Y-%m-%d %H:%M:%S")
    client.command(
        f"""
        INSERT INTO {ETL_RUNS_TABLE}
            (transform, date_from, date_to, rows_in, rows_out, errors,
             status, started_at, finished_at, code_version)
        VALUES
            ('{transform}', NULL, NULL, 0, {statements}, {failed},
             '{status}', '{start_str}', '{finish_str}', '{_code_version()}')
        """
    )


def execute_transform(client, path: Path) -> bool:
    """Выполняет все стейтменты файла; False при первой ошибке (файл прерывается)."""
    statements = split_statements(path.read_text(encoding="utf-8"))
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    log.info("transform_start", file=path.name, statements=len(statements))
    for idx, stmt in enumerate(statements, start=1):
        try:
            client.command(stmt)
        except Exception as exc:  # noqa: BLE001 — фиксируем и падаем
            log.error("statement_failed", file=path.name, index=idx, error=str(exc))
            record_run(client, path.stem, idx - 1, 1, started_at, "failed")
            return False
    record_run(client, path.stem, len(statements), 0, started_at, "ok")
    log.info("transform_done", file=path.name, statements=len(statements))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Идемпотентный раннер мартов")
    parser.add_argument(
        "--only", metavar="NAME",
        help="выполнить один файл (имя с или без .sql)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="напечатать план и не выполнять ничего",
    )
    args = parser.parse_args(argv)

    setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    files = list_transforms()
    if args.only:
        wanted = args.only.removesuffix(".sql")
        files = [p for p in files if p.stem == wanted]
        if not files:
            log.error("only_not_found", name=args.only)
            return 2

    plan = {p.name: len(split_statements(p.read_text(encoding="utf-8"))) for p in files}
    log.info("plan", files=plan, dry_run=args.dry_run)
    if args.dry_run:
        return 0

    client = get_client(Settings.load())
    ok = True
    for path in files:
        ok = execute_transform(client, path) and ok
    log.info("run_finished", ok=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
