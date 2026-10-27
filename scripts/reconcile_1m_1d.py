#!/usr/bin/env python3
"""Сверка candles_1m ↔ candles_1d на пересечении истории + отчёт о дырах.

Агрегирует минутки (FINAL, дедуп ReplacingMergeTree) по датам и сравнивает
с дневками: close с допуском 0.5%, объём — для проверки конвенции вечерней
сессии (относятся ли минуты после 18:50 MSK к следующему дню в 1d-данных).
Затем: будни за последние 12 месяцев без дневной свечи («дыры»).
Результат печатается и одним сообщением уходит в Telegram (category='ops').

Использование:
    set -a; source .env; set +a; .venv/bin/python scripts/reconcile_1m_1d.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mdgrabber.ch import get_client
from mdgrabber.config import Settings
from mdgrabber.notify import send_telegram

CLOSE_TOL = 0.005      # 0.5% на close из-за округлений
VOL_TOL = 0.001        # 0.1% на объём (он целый, допускаем только rounding-шум)
EVENING_UTC = 15 * 3600 + 50 * 60  # 18:50 MSK = 15:50 UTC, секунды от начала суток
TOP_N = 10


def vol_match(a: int, b: int) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b <= VOL_TOL


def fetch_1m_agg(ch) -> dict[tuple[str, date], dict]:
    rows = ch.query(
        """
        SELECT instrument_uid, toDate(bar_time) AS d,
               argMin(open, bar_time)  AS o, max(high) AS h,
               min(low) AS l, argMax(close, bar_time) AS c,
               sum(volume) AS v_all,
               sumIf(volume, bar_time < toDateTime(toDate(bar_time), 'UTC') + 57000) AS v_main
        FROM market_data.candles_1m FINAL
        GROUP BY instrument_uid, d
        """
    ).result_rows
    out = {}
    for uid, d, o, h, l, c, v_all, v_main in rows:
        out[(uid, d)] = {
            "open": o, "high": h, "low": l, "close": c,
            "v_all": int(v_all), "v_main": int(v_main),
            "v_evening": int(v_all) - int(v_main),
        }
    return out


def fetch_1d(ch) -> dict[tuple[str, date], dict]:
    rows = ch.query(
        """
        SELECT instrument_uid, bar_date, open, high, low, close, volume
        FROM market_data.candles_1d FINAL
        """
    ).result_rows
    return {
        (uid, d): {"open": o, "high": h, "low": l, "close": c, "volume": int(v)}
        for uid, d, o, h, l, c, v in rows
    }


def main() -> int:
    settings = Settings.load()
    ch = get_client(settings)

    agg = fetch_1m_agg(ch)
    day = fetch_1d(ch)
    uids = sorted({u for u, _ in agg} | {u for u, _ in day})
    # обратная карта uid→тикер для читаемого вывода
    import json
    inv = json.loads((ROOT / "config" / "instruments.json").read_text())
    uid2ticker = {v.get("uid", ""): k for k, v in inv.items()}

    lines = ["reconcile_1m_1d: сверка минуток и дневок"]

    # --- 1. Сверка close + конвенция вечерней сессии -------------------------
    for uid in uids:
        ticker = uid2ticker.get(uid, uid[:8])
        m_dates = {d for u, d in agg if u == uid}
        d_dates = {d for u, d in day if u == uid}
        common = sorted(m_dates & d_dates)
        if not common:
            lines.append(f"{ticker}: пересечений нет (1m: {len(m_dates)} дн., 1d: {len(d_dates)} дн.)")
            continue

        matched = 0
        diffs: list[tuple[float, date, float, float]] = []
        h_same = h_next = 0
        for d in common:
            a, b = agg[(uid, d)], day[(uid, d)]
            if b["close"]:
                rel = abs(a["close"] - b["close"]) / b["close"]
                if rel <= CLOSE_TOL:
                    matched += 1
                else:
                    diffs.append((rel, d, b["close"], a["close"]))
            # конвенция: H_same — вечерние минуты этого же дня в 1d этого же дня
            if vol_match(a["v_all"], b["volume"]):
                h_same += 1
            # H_next — вечерние минуты предыдущего дня отнесены к текущему дню
            prev = agg.get((uid, d - timedelta(days=1)))
            if prev and vol_match(a["v_main"] + prev["v_evening"], b["volume"]):
                h_next += 1

        pct = 100.0 * matched / len(common)
        diffs.sort(reverse=True)
        print(f"\n{ticker}: общих дней {len(common)}, close совпадает (±0.5%): {matched} ({pct:.1f}%)")
        for rel, d, c1d, c1m in diffs[:TOP_N]:
            print(f"  расхождение {d}: 1d.close={c1d} vs 1m-агрегат={c1m} ({rel * 100:.2f}%)")
        # распределение: у каких долей дней вечерние минуты в 1d-данных — того же дня
        h_same_pct = 100.0 * h_same / len(common)
        h_next_pct = 100.0 * h_next / len(common)
        verdict = (
            "вечерние минуты (после 18:50 MSK) относятся К ТОМУ ЖЕ дню в 1d-данных"
            if h_same_pct >= h_next_pct
            else "вечерние минуты (после 18:50 MSK) относятся К СЛЕДУЮЩЕМУ дню в 1d-данных"
        )
        print(f"  конвенция: H_same={h_same_pct:.1f}% дней, H_next={h_next_pct:.1f}% дней → {verdict}")

        lines.append(
            f"{ticker}: общих дней {len(common)}, close совпадает {pct:.1f}%, "
            f"расхождений {len(diffs)}; объём: same-day {h_same_pct:.0f}% / next-day {h_next_pct:.0f}% "
            f"→ {verdict}"
        )

    # --- 2. Отчёт о дырах: будни за 12 мес без дневной свечи ------------------
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=365)
    lines.append("")
    lines.append("Дыры (будни за 12 мес без дневной свечи):")
    all_gaps = 0
    for uid in uids:
        ticker = uid2ticker.get(uid, uid[:8])
        have = {d for u, d in day if u == uid and start <= d <= today}
        gaps = []
        d = start
        while d <= today:
            if d.weekday() < 5 and d not in have:
                gaps.append(d)
            d += timedelta(days=1)
        all_gaps += len(gaps)
        shown = ", ".join(g.isoformat() for g in gaps[:15])
        more = f" … (+{len(gaps) - 15})" if len(gaps) > 15 else ""
        print(f"\n{ticker}: дыр {len(gaps)}: {shown}{more}")
        lines.append(f"{ticker}: {len(gaps)}: {shown}{more}" if gaps else f"{ticker}: 0")

    # --- 3. Одно сообщение в Telegram -----------------------------------------
    try:
        send_telegram(settings, "\n".join(lines), category="ops")
        print("\nTelegram: отчёт отправлен")
    except Exception as e:  # noqa: BLE001
        print(f"\nWARN: Telegram недоступен: {e} — отчёт только в логе")

    return 0 if (agg or day) else 1


if __name__ == "__main__":
    sys.exit(main())
