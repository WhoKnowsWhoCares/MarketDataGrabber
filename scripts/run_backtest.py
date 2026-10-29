#!/usr/bin/env python3
"""Статистика бэктеста + Telegram-отчёт (P5.2).

Фазы (без флагов = обе):
    --replay  replay_signals + persist_backtest по полной истории gold-витрины
              (идемпотентно: ReplacingMergeTree(signal_backtest) по ORDER BY-ключу);
    --report  статистика по signal_backtest одним SELECT, расчёт в Python
              (statistics.mean/median/quantiles) и Telegram-отчёт админу
              (категория ops, parse_mode='HTML'). --no-send — dry-режим: только лог.
Статистика по каждому signal_type (по всем инструментам вместе и отдельно по
тикетам для топ-3 типов по count): count, win_rate_5d (доля fwd_ret_5d > 0),
mean/median fwd_ret_1d/5d/20d (в %), p10/p90 для 5d. NULL fwd не участвуют в
статистике (их количество выводится отдельно: «нет данных N»).
Запись о прогоне — market_data_aux.etl_runs (transform='backtest').

Использование:
    set -a; source .env; set +a
    PYTHONPATH=src .venv/bin/python scripts/run_backtest.py [--replay] [--report] [--no-send]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.backtest import BACKTEST_TABLE, persist_backtest, replay_signals  # noqa: E402
from mdgrabber.ch import get_client  # noqa: E402
from mdgrabber.config import Settings  # noqa: E402
from mdgrabber.logging import get_logger, setup_logging  # noqa: E402
from mdgrabber.notify import send_telegram  # noqa: E402

log = get_logger("run_backtest")

ETL_RUNS_TABLE = "market_data_aux.etl_runs"
FWD_COLS = ("fwd_ret_1d", "fwd_ret_5d", "fwd_ret_20d")


def load_instruments(path: Path) -> dict[str, str]:
    """config/instruments.json {ticker: {uid, ...}} -> {uid: ticker}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {v["uid"]: k for k, v in data.items() if v.get("uid")}


# ---------- статистика ----------

def _pct(vals: list[float]) -> list[float]:
    return [v * 100.0 for v in vals]


def _quantiles(vals: list[float]) -> tuple[float, float] | None:
    """(p10, p90) в %; None если точек меньше двух."""
    if len(vals) < 2:
        return None
    q = statistics.quantiles(vals, n=10)
    return q[0], q[7]


def _mean_median(vals: list[float]) -> tuple[float, float]:
    return statistics.mean(vals), statistics.median(vals)


def group_stats(rows: list[dict]) -> dict:
    """Статистика по набору строк signal_backtest (уже отфильтрованному по группе).

    rows: [{fwd_ret_1d, fwd_ret_5d, fwd_ret_20d, ...}]. NULL fwd исключены из
    расчётов; missing = число строк с NULL fwd_ret_5d («нет данных»).
    """
    fwd1 = [r["fwd_ret_1d"] for r in rows if r["fwd_ret_1d"] is not None]
    fwd5 = [r["fwd_ret_5d"] for r in rows if r["fwd_ret_5d"] is not None]
    fwd20 = [r["fwd_ret_20d"] for r in rows if r["fwd_ret_20d"] is not None]
    missing = sum(1 for r in rows if r["fwd_ret_5d"] is None)

    stats: dict = {
        "count": len(rows),
        "missing": missing,
        "win_rate_5d": (sum(1 for v in fwd5 if v > 0) / len(fwd5)) if fwd5 else None,
        "p10_p90_5d": _quantiles(_pct(fwd5)),
    }
    for name, vals in (("1d", fwd1), ("5d", fwd5), ("20d", fwd20)):
        if vals:
            mean, median = _mean_median(_pct(vals))
            stats[f"mean_{name}"] = mean
            stats[f"median_{name}"] = median
        else:
            stats[f"mean_{name}"] = None
            stats[f"median_{name}"] = None
    return stats


def compute_stats(rows: list[dict]) -> dict[str, dict]:
    """Статистика по каждому signal_type (все инструменты вместе).

    rows: строки из signal_backtest одним SELECT.
    Возвращает {signal_type: stats}.
    """
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["signal_type"], []).append(r)
    return {st: group_stats(rs) for st, rs in sorted(by_type.items())}


def _fmt(v, suffix: str = "") -> str:
    return "—" if v is None else f"{v:.2f}{suffix}"


def _fmt_qp(qp: tuple[float, float] | None) -> tuple[str, str]:
    return ("—", "—") if qp is None else (f"{qp[0]:.2f}", f"{qp[1]:.2f}")


TABLE_HEADER = ("тип", "N", "win5d", "mean1d", "med1d", "mean5d", "med5d",
                "mean20d", "med20d", "p10_5d", "p90_5d", "нетДанных")


def _table_rows(stats: dict[str, dict], label_key: str = "signal_type") -> list[tuple[str, ...]]:
    out = []
    for st, s in stats.items():
        p10, p90 = _fmt_qp(s["p10_p90_5d"])
        out.append((
            st, str(s["count"]), _fmt(s["win_rate_5d"]),
            _fmt(s["mean_1d"]), _fmt(s["median_1d"]),
            _fmt(s["mean_5d"]), _fmt(s["median_5d"]),
            _fmt(s["mean_20d"]), _fmt(s["median_20d"]),
            p10, p90, str(s["missing"]),
        ))
    return out


def _render_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i])
              for i in range(len(header))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths))]
    for r in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)))
    return "\n".join(lines)


def build_message(rows: list[dict]) -> str:
    """HTML-отчёт: заголовок + таблица по типам + блоки по тикерам топ-3 типов."""
    dates = sorted(str(r["bar_date"]) for r in rows)
    history = f"{dates[0]}..{dates[-1]}"

    stats = compute_stats(rows)
    table = _render_table(TABLE_HEADER, _table_rows(stats))

    lines = [f"📊 <b>Бэктест сигналов</b> (история: {history})", "", "<pre>", table, "</pre>"]

    top3 = sorted(stats.items(), key=lambda kv: kv[1]["count"], reverse=True)[:3]
    if top3:
        lines.append("")
        for st, _ in top3:
            subset = [r for r in rows if r["signal_type"] == st]
            by_ticker: dict[str, list[dict]] = {}
            for r in subset:
                by_ticker.setdefault(r["ticker"], []).append(r)
            tstats = {t: group_stats(rs) for t, rs in sorted(by_ticker.items())}
            ttable = _render_table(TABLE_HEADER, _table_rows(tstats))
            lines.append(f"<b>{st}</b> по тикерам:")
            lines.append("<pre>")
            lines.append(ttable)
            lines.append("</pre>")
    return "\n".join(lines)


# ---------- фазы ----------

def fetch_rows(client) -> list[dict]:
    """Все строки signal_backtest одним SELECT (FINAL — дедуп ReplacingMergeTree)."""
    sql = (
        f"SELECT ticker, signal_type, bar_date, fwd_ret_1d, fwd_ret_5d, fwd_ret_20d "
        f"FROM {BACKTEST_TABLE} FINAL"
    )
    return [dict(r) for r in client.query(sql).named_results()]


def do_replay(client, instruments_path: Path) -> int:
    """replay_signals + persist_backtest; возвращает число строк."""
    instruments = load_instruments(instruments_path)
    rows = replay_signals(client, instruments)
    persist_backtest(client, rows)
    log.info("replay_done", rows=len(rows))
    return len(rows)


def do_report(client, settings: Settings, no_send: bool) -> int:
    """Статистика + отчёт админу (категория ops); --no-send — только лог."""
    rows = fetch_rows(client)
    if not rows:
        log.info("no_backtest_rows")
        return 0
    text = build_message(rows)
    if no_send:
        log.info("dry_run_would_send", rows=len(rows))
    else:
        send_telegram(settings, text, category="ops", parse_mode="HTML")
        log.info("report_sent", rows=len(rows))
    return len(rows)


def record_run(client, status: str, rows: int, started_at: datetime) -> None:
    """Запись о прогоне в market_data_aux.etl_runs (transform='backtest')."""
    finished = datetime.now(timezone.utc).replace(microsecond=0)
    start_str = started_at.strftime("%Y-%m-%d %H:%M:%S")
    finish_str = finished.strftime("%Y-%m-%d %H:%M:%S")
    try:
        client.command(
            f"""
            INSERT INTO {ETL_RUNS_TABLE}
                (transform, date_from, date_to, rows_in, rows_out, errors,
                 status, started_at, finished_at, code_version)
            VALUES
                ('backtest', NULL, NULL, 0, {int(rows)}, 0,
                 '{status}', '{start_str}', '{finish_str}', '')
            """
        )
    except Exception as exc:  # noqa: BLE001 — запись телеметрии не валит рантайм
        log.warning("etl_run_record_failed", error=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Бэктест сигналов: replay + отчёт в Telegram")
    parser.add_argument("--replay", action="store_true", help="replay_signals + persist_backtest")
    parser.add_argument("--report", action="store_true", help="статистика + Telegram-отчёт админу")
    parser.add_argument("--no-send", action="store_true", help="dry-режим отчёта: не слать в Telegram")
    args = parser.parse_args(argv)

    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    started_at = datetime.now(timezone.utc).replace(microsecond=0)

    settings = Settings.load()
    client = get_client(settings)

    do_replay_phase = args.replay or not (args.replay or args.report)
    do_report_phase = args.report or not (args.replay or args.report)

    rows = 0
    if do_replay_phase:
        rows = do_replay(client, ROOT / "config" / "instruments.json")
    if do_report_phase:
        rows = do_report(client, settings, no_send=args.no_send)

    record_run(client, "ok", rows, started_at)
    log.info("run_finished", replay=do_replay_phase, report=do_report_phase, rows=rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
