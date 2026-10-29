"""Движок сигналов (P4.1): детекция по gold-витрине + дедуп в market_data_aux.signals.

7 типов сигналов:
    rsi_overbought  — rsi14 >= 70 (последний бар)
    rsi_oversold    — rsi14 <= 30
    volume_spike    — volume_ratio >= 2.0
    ma_cross_up     — вчера close < MA20, сегодня close >= MA20
    ma_cross_down   — вчера close > MA20, сегодня close <= MA20
    golden_cross    — вчера MA20 < MA50, сегодня MA20 >= MA50
    death_cross     — вчера MA20 > MA50, сегодня MA20 <= MA50
"""
from __future__ import annotations

GOLD_TABLE = "market_data_gold.mart_daily_metrics"
SIGNALS_TABLE = "market_data_aux.signals"

RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
VOLUME_SPIKE_RATIO = 2.0
DEDUP_WINDOW_DAYS = 30

# Калибровка по бэктесту 2026-10-29 (6 лет, 2774 сигнала, win-rate 5d / avg 5d):
# ma_cross_up 57.1%/+0.49%; death_cross 43.2%/-1.15% (avg20d -3.11%); ma_cross_down -0.28%;
# rsi_oversold 51.1%/+0.41%; rsi_overbought +0.37%/+1.04% 20d (моментум); golden_cross -0.64%; volume_spike -0.36%.
SIGNAL_CALIBRATION = {
    "ma_cross_up":    {"deliver": True,  "importance": 2},
    "death_cross":    {"deliver": True,  "importance": 3},
    "ma_cross_down":  {"deliver": True,  "importance": 1},
    "rsi_oversold":   {"deliver": True,  "importance": 1},
    "rsi_overbought": {"deliver": False, "importance": 0},
    "golden_cross":   {"deliver": False, "importance": 0},
    "volume_spike":   {"deliver": False, "importance": 0},
}

_DEFAULT_CALIBRATION = {"deliver": False, "importance": 0}


def calibration(signal_type: str) -> dict:
    """Калибровка типа сигнала {deliver, importance}; неизвестный тип — не доставлять."""
    return SIGNAL_CALIBRATION.get(signal_type, dict(_DEFAULT_CALIBRATION))


def _q(s: str) -> str:
    """SQL-литерал строки с экранированием."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def detect_signals(
    client,
    instruments: dict[str, str],
    lookback: int = 3,
) -> list[dict]:
    """Детектит сигналы по последним `lookback` барам витрины для каждого инструмента.

    instruments: {instrument_uid: ticker}.
    Для кроссов нужны предпоследний и последний бар (lookback >= 2).
    Возвращает список {instrument_uid, ticker, signal_type, bar_date, value, message}.
    """
    if lookback < 2:
        lookback = 2

    if not instruments:
        return []

    uids = ", ".join(_q(uid) for uid in instruments)
    sql = f"""
    SELECT instrument_uid, bar_date, close, volume_ratio, rsi14, ma20, ma50
    FROM {GOLD_TABLE}
    WHERE instrument_uid IN ({uids}) AND rsi14 IS NOT NULL
    ORDER BY instrument_uid, bar_date DESC
    LIMIT {int(lookback)} BY instrument_uid
    """
    rows = list(client.query(sql).named_results())

    by_uid: dict[str, list[dict]] = {}
    for row in rows:
        by_uid.setdefault(row["instrument_uid"], []).append(row)

    signals: list[dict] = []
    for uid, bars in by_uid.items():
        ticker = instruments.get(uid, uid)
        bars.sort(key=lambda r: r["bar_date"])  # старые -> новые
        last = bars[-1]
        bar_date = last["bar_date"]
        date_str = bar_date.strftime("%Y-%m-%d") if hasattr(bar_date, "strftime") else str(bar_date)

        rsi = last.get("rsi14")
        vr = last.get("volume_ratio")
        if rsi is not None and rsi >= RSI_OVERBOUGHT:
            signals.append(_make(uid, ticker, "rsi_overbought", date_str, rsi,
                                 f"🔴 {ticker}: RSI {rsi:.1f} — перекупленность"))
        elif rsi is not None and rsi <= RSI_OVERSOLD:
            signals.append(_make(uid, ticker, "rsi_oversold", date_str, rsi,
                                 f"🟢 {ticker}: RSI {rsi:.1f} — перепроданность"))

        if vr is not None and vr >= VOLUME_SPIKE_RATIO:
            signals.append(_make(uid, ticker, "volume_spike", date_str, vr,
                                 f"📊 {ticker}: объём ×{vr:.1f} от среднего"))

        if len(bars) >= 2:
            prev = bars[-2]
            _detect_crosses(uid, ticker, prev, last, date_str, signals)

    return signals


def _make(uid: str, ticker: str, signal_type: str, bar_date, value: float, message: str) -> dict:
    return {
        "instrument_uid": uid,
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": float(value),
        "message": message,
        "importance": calibration(signal_type)["importance"],
    }


def _detect_crosses(uid, ticker, prev, last, date_str, signals):
    # close x MA20
    prev_below = prev["close"] < prev["ma20"]
    prev_above = prev["close"] > prev["ma20"]
    last_ge = last["close"] >= last["ma20"]
    last_le = last["close"] <= last["ma20"]
    if prev_below and last_ge:
        signals.append(_make(uid, ticker, "ma_cross_up", date_str, 0,
                             f"📈 {ticker}: close пересёк MA20 вверх"))
    elif prev_above and last_le:
        signals.append(_make(uid, ticker, "ma_cross_down", date_str, 0,
                             f"📉 {ticker}: close пересёк MA20 вниз"))

    # MA20 x MA50
    prev_below = prev["ma20"] < prev["ma50"]
    prev_above = prev["ma20"] > prev["ma50"]
    last_ge = last["ma20"] >= last["ma50"]
    last_le = last["ma20"] <= last["ma50"]
    if prev_below and last_ge:
        signals.append(_make(uid, ticker, "golden_cross", date_str, 0,
                             f"🟡 {ticker}: GOLDEN CROSS — MA20 вышла выше MA50"))
    elif prev_above and last_le:
        signals.append(_make(uid, ticker, "death_cross", date_str, 0,
                             f"🟠 {ticker}: DEATH CROSS — MA20 ушла ниже MA50"))


def persist_new(client, signals: list[dict]) -> list[dict]:
    """Дедуп + вставка. Читает существующие (instrument_uid, signal_type, bar_date)
    за последние 30 дней, вставляет только новые, возвращает вставленные."""
    if not signals:
        return []

    existing: set[tuple[str, str, str]] = set()
    result = client.query(
        f"""
        SELECT DISTINCT instrument_uid, signal_type, bar_date
        FROM {SIGNALS_TABLE}
        WHERE bar_date >= today() - {DEDUP_WINDOW_DAYS}
        """
    )
    for row in result.named_results():
        d = row["bar_date"]
        existing.add(
            (row["instrument_uid"], row["signal_type"],
             d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d))
        )

    fresh = [
        s for s in signals
        if (s["instrument_uid"], s["signal_type"], str(s["bar_date"])) not in existing
    ]
    if fresh:
        client.command(_insert_sql(fresh, sent=0))
    return fresh


def mark_sent(client, signals: list[dict]) -> None:
    """UPDATE sent=1: в Replacing это вставка тех же строк (ключи ORDER BY) с sent=1."""
    if not signals:
        return
    client.command(_insert_sql(signals, sent=1))


def fetch_unsent(client) -> list[dict]:
    """Сигналы, сохранённые, но ещё не доставленные (sent=0).

    Рантайм доставляет именно их — это делает систему самовосстанавливающейся:
    сигналы, persist'нутые dry-run'ом или при упавшей доставке, уходят при
    следующем живом запуске.
    """
    rows = client.query(
        f"SELECT instrument_uid, ticker, signal_type, bar_date, value, message "
        f"FROM {SIGNALS_TABLE} FINAL WHERE sent = 0"
    ).named_results()
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "instrument_uid": r["instrument_uid"],
                "ticker": r["ticker"],
                "signal_type": r["signal_type"],
                "bar_date": r["bar_date"],
                "value": float(r["value"]),
                "message": r["message"],
            }
        )
    return out


def _insert_sql(signals: list[dict], sent: int) -> str:
    values = ",\n".join(
        f"({_row_ts()}, {_q(s['instrument_uid'])}, {_q(s['ticker'])}, "
        f"{_q(s['signal_type'])}, {_q(str(s['bar_date']))}, {float(s['value'])}, "
        f"{_q(s['message'])}, {int(sent)})"
        for s in signals
    )
    return (
        f"INSERT INTO {SIGNALS_TABLE} "
        f"(ts, instrument_uid, ticker, signal_type, bar_date, value, message, sent) VALUES\n"
        f"{values}"
    )


def _row_ts() -> str:
    return "now()"
