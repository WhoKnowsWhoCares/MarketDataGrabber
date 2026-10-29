"""Replay-движок бэктеста (P5.1): прогон 7 типов сигналов по полной истории gold-витрины.

Отличие от signals.detect_signals (рантайм, только последние бары):
    replay идёт по ВСЕМ барам витрины подряд и на каждом баре T вычисляет те же
    7 условий с порогами из signals.py. Сигнал на баре T использует только бары <= T
    (витрина lookahead-free by construction).
Для каждого сигнала считаются forward returns по ТОРГОВЫМ барам (последовательность
баров инструмента, не календарь): fwd_ret_N = close(T+N)/close(T) - 1, N = 1, 5, 20;
если T+N за концом истории -> NULL.
Сообщение не формируется — это анализ, не рассылка.
"""
from __future__ import annotations

from mdgrabber.signals import (
    GOLD_TABLE,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    VOLUME_SPIKE_RATIO,
    _q,
)

BACKTEST_TABLE = "market_data_aux.signal_backtest"
FWD_HORIZONS = (1, 5, 20)
BATCH_SIZE = 1000


def replay_signals(client, instruments: dict[str, str]) -> list[dict]:
    """Replay 7 типов сигналов по полной истории витрины для каждого инструмента.

    instruments: {instrument_uid: ticker}.
    Возвращает список {instrument_uid, ticker, signal_type, bar_date, value,
    fwd_ret_1d, fwd_ret_5d, fwd_ret_20d}.
    """
    if not instruments:
        return []

    uids = ", ".join(_q(uid) for uid in instruments)
    sql = f"""
    SELECT instrument_uid, bar_date, close, volume_ratio, rsi14, ma20, ma50
    FROM {GOLD_TABLE}
    WHERE instrument_uid IN ({uids})
    ORDER BY instrument_uid, bar_date
    """
    rows = list(client.query(sql).named_results())

    by_uid: dict[str, list[dict]] = {}
    for row in rows:
        by_uid.setdefault(row["instrument_uid"], []).append(row)

    out: list[dict] = []
    for uid, bars in by_uid.items():
        ticker = instruments.get(uid, uid)
        bars.sort(key=lambda r: r["bar_date"])  # старые -> новые
        prev: dict | None = None  # предыдущий ВАЛИДНЫЙ бар (rsi/ma не NULL)
        for i, bar in enumerate(bars):
            rsi = bar.get("rsi14")
            vr = bar.get("volume_ratio")
            ma20 = bar.get("ma20")
            ma50 = bar.get("ma50")
            if rsi is None or ma20 is None or ma50 is None:
                continue  # бар без индикаторов пропущен
            date_str = _date_str(bar["bar_date"])

            if rsi >= RSI_OVERBOUGHT:
                out.append(_row(uid, ticker, "rsi_overbought", date_str, rsi, bars, i))
            elif rsi <= RSI_OVERSOLD:
                out.append(_row(uid, ticker, "rsi_oversold", date_str, rsi, bars, i))

            if vr is not None and vr >= VOLUME_SPIKE_RATIO:
                out.append(_row(uid, ticker, "volume_spike", date_str, vr, bars, i))

            if prev is not None:
                _detect_crosses(uid, ticker, prev, bar, date_str, bars, i, out)

            prev = bar
    return out


def persist_backtest(client, rows: list[dict]) -> None:
    """INSERT пачками; идемпотентность обеспечивает ReplacingMergeTree по ключу ORDER BY."""
    if not rows:
        return
    for start in range(0, len(rows), BATCH_SIZE):
        client.command(_insert_sql(rows[start:start + BATCH_SIZE]))


def _row(uid: str, ticker: str, signal_type: str, bar_date: str, value: float,
         bars: list[dict], i: int) -> dict:
    return {
        "instrument_uid": uid,
        "ticker": ticker,
        "signal_type": signal_type,
        "bar_date": bar_date,
        "value": float(value),
        "fwd_ret_1d": _fwd_ret(bars, i, 1),
        "fwd_ret_5d": _fwd_ret(bars, i, 5),
        "fwd_ret_20d": _fwd_ret(bars, i, 20),
    }


def _detect_crosses(uid, ticker, prev, last, date_str, bars, i, out):
    # close x MA20
    prev_below = prev["close"] < prev["ma20"]
    prev_above = prev["close"] > prev["ma20"]
    last_ge = last["close"] >= last["ma20"]
    last_le = last["close"] <= last["ma20"]
    if prev_below and last_ge:
        out.append(_row(uid, ticker, "ma_cross_up", date_str, 0, bars, i))
    elif prev_above and last_le:
        out.append(_row(uid, ticker, "ma_cross_down", date_str, 0, bars, i))

    # MA20 x MA50
    prev_below = prev["ma20"] < prev["ma50"]
    prev_above = prev["ma20"] > prev["ma50"]
    last_ge = last["ma20"] >= last["ma50"]
    last_le = last["ma20"] <= last["ma50"]
    if prev_below and last_ge:
        out.append(_row(uid, ticker, "golden_cross", date_str, 0, bars, i))
    elif prev_above and last_le:
        out.append(_row(uid, ticker, "death_cross", date_str, 0, bars, i))


def _fwd_ret(bars: list[dict], i: int, n: int) -> float | None:
    """close(T+n)/close(T) - 1 по торговым барам; NULL если T+n за концом истории."""
    j = i + n
    if j >= len(bars):
        return None
    c0, cj = bars[i]["close"], bars[j]["close"]
    if c0 is None or cj is None or c0 == 0:
        return None
    return cj / c0 - 1.0


def _date_str(d) -> str:
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


def _insert_sql(rows: list[dict]) -> str:
    values = ",\n".join(
        f"({_q(r['instrument_uid'])}, {_q(r['ticker'])}, {_q(r['signal_type'])}, "
        f"{_q(str(r['bar_date']))}, {float(r['value'])}, "
        f"{_lit(r['fwd_ret_1d'])}, {_lit(r['fwd_ret_5d'])}, {_lit(r['fwd_ret_20d'])})"
        for r in rows
    )
    return (
        f"INSERT INTO {BACKTEST_TABLE} "
        f"(instrument_uid, ticker, signal_type, bar_date, value, "
        f"fwd_ret_1d, fwd_ret_5d, fwd_ret_20d) VALUES\n"
        f"{values}"
    )


def _lit(v) -> str:
    return "NULL" if v is None else repr(float(v))
