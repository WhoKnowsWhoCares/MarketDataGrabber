-- ============================================================
-- mart_daily_metrics: идемпотентная пересборка последних 400 торговых дней
-- ReplacingMergeTree дедуплицирует по (instrument_uid, bar_date) —
-- просто вставляем свежие данные для окна, повторный запуск безопасен.
-- ============================================================

-- Вставка пересчитанных метрик за последние ~600 календарных дней
-- (покрывает 400 торговых дней с запасом)
INSERT INTO market_data_gold.mart_daily_metrics
WITH
    -- База: дневные свечи FINAL с окном 600 дней назад от максимума
    base AS (
        SELECT
            instrument_uid,
            bar_date,
            open,
            high,
            low,
            close,
            volume,
            -- Предыдущий close для расчёта gap и ret_1d
            lagInFrame(close) OVER (PARTITION BY instrument_uid ORDER BY bar_date) AS prev_close,
            -- Для RSI: приросты
            close - lagInFrame(close) OVER (PARTITION BY instrument_uid ORDER BY bar_date) AS change_1d
        FROM market_data.candles_1d FINAL
        WHERE bar_date >= (
            SELECT max(bar_date) - INTERVAL 2500 DAY
            FROM market_data.candles_1d FINAL
        )
    ),
    -- Расчёт True Range для ATR14
    tr_calc AS (
        SELECT
            *,
            GREATEST(
                high - low,
                ABS(high - prev_close),
                ABS(low - prev_close)
            ) AS true_range,
            CASE WHEN change_1d > 0 THEN change_1d ELSE 0 END AS gain,
            CASE WHEN change_1d < 0 THEN -change_1d ELSE 0 END AS loss
        FROM base
    ),
    -- Оконные агрегаты
    windowed AS (
        SELECT
            instrument_uid,
            bar_date,
            close,
            volume,
            open,
            high,
            low,
            prev_close,
            -- Доходности
            CASE WHEN prev_close > 0 THEN (close / prev_close - 1) * 100 ELSE NULL END AS ret_1d,
            -- Скользящие средние (SMA через окно)
            avg(close) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
            avg(close) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) AS ma50,
            avg(volume) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS volume_ma20,
            -- ATR14: средний True Range за 14 дней
            avg(true_range) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS atr14,
            -- Range %: (high-low)/close*100
            CASE WHEN close > 0 THEN (high - low) / close * 100 ELSE NULL END AS range_pct,
            -- Gap %: (open/prev_close-1)*100
            CASE WHEN prev_close > 0 THEN (open / prev_close - 1) * 100 ELSE NULL END AS gap_pct,
            -- RSI14 компоненты: SMA gain/loss за 14 дней
            avg(gain) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain_14,
            avg(loss) OVER (PARTITION BY instrument_uid ORDER BY bar_date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss_14
        FROM tr_calc
    ),
    -- Финальные метрики с ret_5d, ret_20d, volume_ratio, rsi14
    final_metrics AS (
        SELECT
            instrument_uid,
            bar_date,
            close,
            ret_1d,
            -- ret_5d: (close / close_5d_ago - 1) * 100
            CASE
                WHEN lagInFrame(close, 5) OVER (PARTITION BY instrument_uid ORDER BY bar_date) > 0
                THEN (close / lagInFrame(close, 5) OVER (PARTITION BY instrument_uid ORDER BY bar_date) - 1) * 100
                ELSE NULL
            END AS ret_5d,
            -- ret_20d: (close / close_20d_ago - 1) * 100
            CASE
                WHEN lagInFrame(close, 20) OVER (PARTITION BY instrument_uid ORDER BY bar_date) > 0
                THEN (close / lagInFrame(close, 20) OVER (PARTITION BY instrument_uid ORDER BY bar_date) - 1) * 100
                ELSE NULL
            END AS ret_20d,
            volume,
            volume_ma20,
            CASE WHEN volume_ma20 > 0 THEN volume / volume_ma20 ELSE NULL END AS volume_ratio,
            atr14,
            range_pct,
            gap_pct,
            ma20,
            ma50,
            -- RSI14 = 100 - 100 / (1 + avg_gain / avg_loss)
            -- Fix: handle avg_loss_14 = 0 to avoid division by zero
            CASE
                WHEN avg_loss_14 = 0 AND avg_gain_14 > 0 THEN 100
                WHEN avg_loss_14 = 0 AND avg_gain_14 = 0 THEN 50
                ELSE 100 - 100 / (1 + avg_gain_14 / avg_loss_14)
            END AS rsi14
        FROM windowed
        WHERE prev_close IS NOT NULL  -- пропускаем первый день у каждого инструмента
    )
SELECT
    instrument_uid,
    bar_date,
    close,
    ret_1d,
    ret_5d,
    ret_20d,
    volume,
    volume_ma20,
    volume_ratio,
    atr14,
    range_pct,
    gap_pct,
    ma20,
    ma50,
    rsi14
FROM final_metrics
WHERE bar_date >= (
    SELECT max(bar_date) - INTERVAL 2500 DAY
    FROM market_data.candles_1d FINAL
)
ORDER BY instrument_uid, bar_date;