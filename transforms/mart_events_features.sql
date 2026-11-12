-- ============================================================
-- mart_events_features: «дней до/после события» на инструменто-день.
-- Зерно = (instrument_uid, bar_date) из mart_daily_metrics (торговые дни).
-- Point-in-time: days_to_* — ближайшее событие с expected_date >= bar_date;
-- days_since_* — последнее с expected_date <= bar_date. Macro-события
-- (macro:ru:cbr, macro:us:fomc) расширяются на все инструменты universe.
-- Идемпотентно: ReplacingMergeTree по (instrument_uid, bar_date).
-- ============================================================

CREATE DATABASE IF NOT EXISTS market_data_gold;

CREATE TABLE IF NOT EXISTS market_data_gold.mart_events_features
(
    instrument_uid      LowCardinality(String),
    bar_date            Date,
    days_to_dividend    Nullable(Int32),
    days_to_report      Nullable(Int32),
    days_to_expiry      Nullable(Int32),
    days_to_cbr         Nullable(Int32),
    days_to_fomc        Nullable(Int32),
    days_since_dividend Nullable(Int32),
    days_since_report   Nullable(Int32),
    days_since_expiry   Nullable(Int32),
    days_since_cbr      Nullable(Int32),
    days_since_fomc     Nullable(Int32),
    computed_at         DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(bar_date)
ORDER BY (instrument_uid, bar_date);

INSERT INTO market_data_gold.mart_events_features
(
    instrument_uid, bar_date,
    days_to_dividend, days_to_report, days_to_expiry, days_to_cbr, days_to_fomc,
    days_since_dividend, days_since_report, days_since_expiry, days_since_cbr, days_since_fomc
)
WITH
    -- Инструменто-дни из витрины метрик (торговые дни, окно как в mart_daily_metrics)
    days AS (
        SELECT instrument_uid, bar_date
        FROM market_data_gold.mart_daily_metrics FINAL
        WHERE bar_date >= (
            SELECT max(bar_date) - INTERVAL 600 DAY
            FROM market_data_gold.mart_daily_metrics FINAL
        )
    ),
    -- Все события с датой: инструментальные как есть, macro — на все инструменты
    inst_events AS (
        SELECT instrument_uid, event_type AS kind, expected_date
        FROM market_data.events FINAL
        WHERE expected_date IS NOT NULL
          AND instrument_uid != ''
          AND event_type IN ('dividend', 'report', 'expiry')
        UNION ALL
        SELECT i.instrument_uid, 'cbr' AS kind, e.expected_date
        FROM market_data.events e FINAL
        CROSS JOIN (SELECT DISTINCT instrument_uid FROM days) AS i
        WHERE e.instrument_uid = 'macro:ru:cbr' AND e.expected_date IS NOT NULL
        UNION ALL
        SELECT i.instrument_uid, 'fomc' AS kind, e.expected_date
        FROM market_data.events e FINAL
        CROSS JOIN (SELECT DISTINCT instrument_uid FROM days) AS i
        WHERE e.instrument_uid = 'macro:us:fomc' AND e.expected_date IS NOT NULL
    ),
    -- Привязка событий к каждому инструменто-дню
    joined AS (
        SELECT
            d.instrument_uid,
            d.bar_date,
            e.kind,
            toInt32(dateDiff('day', d.bar_date, e.expected_date)) AS dd
        FROM days AS d
        INNER JOIN inst_events AS e ON e.instrument_uid = d.instrument_uid
    )
SELECT
    instrument_uid,
    bar_date,
    if(countIf(kind = 'dividend' AND dd >= 0) > 0,
       minIf(dd, kind = 'dividend' AND dd >= 0), toInt32(NULL)) AS days_to_dividend,
    if(countIf(kind = 'report' AND dd >= 0) > 0,
       minIf(dd, kind = 'report' AND dd >= 0), toInt32(NULL)) AS days_to_report,
    if(countIf(kind = 'expiry' AND dd >= 0) > 0,
       minIf(dd, kind = 'expiry' AND dd >= 0), toInt32(NULL)) AS days_to_expiry,
    if(countIf(kind = 'cbr' AND dd >= 0) > 0,
       minIf(dd, kind = 'cbr' AND dd >= 0), toInt32(NULL)) AS days_to_cbr,
    if(countIf(kind = 'fomc' AND dd >= 0) > 0,
       minIf(dd, kind = 'fomc' AND dd >= 0), toInt32(NULL)) AS days_to_fomc,
    if(countIf(kind = 'dividend' AND dd <= 0) > 0,
       minIf(-dd, kind = 'dividend' AND dd <= 0), toInt32(NULL)) AS days_since_dividend,
    if(countIf(kind = 'report' AND dd <= 0) > 0,
       minIf(-dd, kind = 'report' AND dd <= 0), toInt32(NULL)) AS days_since_report,
    if(countIf(kind = 'expiry' AND dd <= 0) > 0,
       minIf(-dd, kind = 'expiry' AND dd <= 0), toInt32(NULL)) AS days_since_expiry,
    if(countIf(kind = 'cbr' AND dd <= 0) > 0,
       minIf(-dd, kind = 'cbr' AND dd <= 0), toInt32(NULL)) AS days_since_cbr,
    if(countIf(kind = 'fomc' AND dd <= 0) > 0,
       minIf(-dd, kind = 'fomc' AND dd <= 0), toInt32(NULL)) AS days_since_fomc
FROM joined
GROUP BY instrument_uid, bar_date;
