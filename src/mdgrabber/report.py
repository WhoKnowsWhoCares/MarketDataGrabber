"""Ежедневный Telegram-дайджест по universe из gold-витрин.

Генератор отчёта — переиспользуемая функция: бот (P3.2) вызовет её же по
команде. Данные: mart_daily_metrics (метрики T-дня), events (ближайшие
дивиденды/отчёты/экспирации), fundamentals_snapshots (P/E).

Today = max(bar_date) в витрине — данные T-день, «сегодня» от календаря
может отличаться (выходные/праздники).
"""
from __future__ import annotations

import json
from datetime import date

from .ch import get_client
from .config import REPO_ROOT, Settings

# Telegram-лимит 4096 символов; держим запас.
MAX_LEN = 3500
# Ближайших событий на инструмент в дайджесте.
EVENTS_PER_INSTRUMENT = 2


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def _rsi_status(rsi: float | None) -> str:
    if rsi is None:
        return "—"
    if rsi > 70:
        return "перекуплен"
    if rsi < 30:
        return "перепродан"
    return "нейтрально"


def _load_instruments() -> dict[str, dict]:
    path = REPO_ROOT / "config" / "instruments.json"
    with open(path) as f:
        return json.load(f)


def _fetch_metrics(client, today: date) -> dict[str, dict]:
    rows = client.query(
        "SELECT instrument_uid, close, ret_1d, ret_5d, ret_20d, volume_ratio, "
        "rsi14, gap_pct "
        "FROM market_data_gold.mart_daily_metrics FINAL "
        "WHERE bar_date = {today:Date}",
        parameters={"today": today},
    )
    return {
        r["instrument_uid"]: {
            "close": r["close"],
            "ret_1d": r["ret_1d"],
            "ret_5d": r["ret_5d"],
            "ret_20d": r["ret_20d"],
            "volume_ratio": r["volume_ratio"],
            "rsi14": r["rsi14"],
            "gap_pct": r["gap_pct"],
        }
        for r in rows.named_results()
    }


def _fetch_pe(client, asset_uids: set[str]) -> dict[str, float]:
    """Последний P/E TTM по каждому asset_uid (фундаментал есть только у акций)."""
    if not asset_uids:
        return {}
    placeholders = ", ".join(f"{i!r}" for i in sorted(asset_uids))
    rows = client.query(
        f"""
        SELECT asset_uid, JSONExtractFloat(payload, 'peRatioTtm') AS pe
        FROM market_data.fundamentals_snapshots
        WHERE asset_uid IN ({placeholders})
          AND snapshot_ts = (
              SELECT max(snapshot_ts) FROM market_data.fundamentals_snapshots
          )
        """
    )
    return {r["asset_uid"]: r["pe"] for r in rows.named_results() if r["pe"] > 0}


def _fetch_events(client, today: date, uids: set[str]) -> dict[str, list[tuple]]:
    """Ближайшие события с expected_date >= today, по instrument_uid."""
    if not uids:
        return {}
    placeholders = ", ".join(f"{i!r}" for i in sorted(uids))
    rows = client.query(
        f"""
        SELECT instrument_uid, expected_date, event_type
        FROM market_data.events
        WHERE instrument_uid IN ({placeholders})
          AND expected_date >= {{today:Date}}
          AND event_type IN ('dividend', 'report', 'expiry')
        ORDER BY expected_date
        """,
        parameters={"today": today},
    )
    out: dict[str, list[tuple]] = {}
    for r in rows.named_results():
        out.setdefault(r["instrument_uid"], []).append(
            (r["expected_date"], r["event_type"])
        )
    return out


def _instrument_block(
    ticker: str,
    m: dict,
    pe: float | None,
    events: list[tuple],
    with_events: bool = True,
) -> str:
    lines = [
        f"<b>{ticker}</b> — {_close_str(m)} | "
        f"{_fmt_pct(m.get('ret_1d'))} 1d, {_fmt_pct(m.get('ret_5d'))} 5d, "
        f"{_fmt_pct(m.get('ret_20d'))} 20d"
    ]
    vr = m.get("volume_ratio")
    vr_str = "—" if vr is None else f"×{vr:.2f}"
    if vr is not None and (vr > 2 or vr < 0.5):
        vr_str = f"⚠{vr_str}"
    rsi = m.get("rsi14")
    rsi_str = "—" if rsi is None else f"{rsi:.1f} ({_rsi_status(rsi)})"
    gap = _fmt_pct(m.get("gap_pct"))
    lines.append(f"RSI14 {rsi_str} | Vol {vr_str} | Gap {gap}")
    if pe is not None:
        lines.append(f"P/E TTM {pe:.2f}")
    if with_events:
        for d, etype in events[:EVENTS_PER_INSTRUMENT]:
            lines.append(f"📅 {d} {etype} {ticker}")
    return "\n".join(lines)


def _close_str(m: dict) -> str:
    c = m.get("close")
    return "—" if c is None else f"{c:.2f}"


def build_digest(settings: Settings) -> str:
    """Собрать текст ежедневного дайджеста по universe (≤ MAX_LEN символов)."""
    instruments = _load_instruments()
    client = get_client(settings)

    today_row = client.query(
        "SELECT max(bar_date) AS d FROM market_data_gold.mart_daily_metrics"
    )
    today = list(today_row.named_results())[0]["d"]

    metrics = _fetch_metrics(client, today)
    asset_by_ticker = {
        t: cfg["asset_uid"] for t, cfg in instruments.items() if cfg.get("asset_uid")
    }
    pe_by_asset = _fetch_pe(client, set(asset_by_ticker.values()))
    events_by_uid = _fetch_events(
        client, today, {cfg["uid"] for cfg in instruments.values() if "uid" in cfg}
    )

    header = f"📊 Ежедневный дайджест — данные за {today}"

    def block_for(ticker: str, cfg: dict, with_events: bool) -> str:
        uid = cfg.get("uid", "")
        m = metrics.get(uid)
        if m is None:
            return f"<b>{ticker}</b> — нет данных за {today}"
        pe = pe_by_asset.get(asset_by_ticker.get(ticker, ""))
        events = events_by_uid.get(uid, []) if with_events else []
        return _instrument_block(ticker, m, pe, events, with_events=with_events)

    blocks = [block_for(t, cfg, with_events=True) for t, cfg in instruments.items()]
    # Переполнение → режем список событий, потом жёсткий лимит.
    if _total_len(header, blocks) > MAX_LEN:
        blocks = [block_for(t, cfg, with_events=False) for t, cfg in instruments.items()]
    text = header + "\n\n" + "\n\n".join(blocks)
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1] + "…"
    return text


def _total_len(header: str, blocks: list[str]) -> int:
    return len(header) + 2 + sum(len(b) + 2 for b in blocks)
