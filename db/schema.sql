-- ============================================================
-- MarketDataGrabber: bronze (сырьё) + служебные таблицы
-- ============================================================
CREATE DATABASE IF NOT EXISTS market_data;

-- Минутные свечи (атомарное зерно 1m). volume в лотах, как отдаёт API.
CREATE TABLE IF NOT EXISTS market_data.candles_1m (
    instrument_uid LowCardinality(String),
    bar_time       DateTime('UTC'),
    open           Float64,
    high           Float64,
    low            Float64,
    close          Float64,
    volume         UInt64,
    source         LowCardinality(String),   -- 'archive' | 'api'
    fetched_at     DateTime('UTC') DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY toYYYYMM(bar_time)
ORDER BY (instrument_uid, bar_time);

-- Дневные свечи (атомарное зерно 1d, независимое от 1m).
CREATE TABLE IF NOT EXISTS market_data.candles_1d (
    instrument_uid LowCardinality(String),
    bar_date       Date,
    open           Float64,
    high           Float64,
    low            Float64,
    close          Float64,
    volume         UInt64,
    source         LowCardinality(String),
    fetched_at     DateTime('UTC') DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY toYYYYMM(bar_date)
ORDER BY (instrument_uid, bar_date);

-- Снапшоты фундаментальных показателей (point-in-time история, точность Decimal не критична).
CREATE TABLE IF NOT EXISTS market_data.fundamentals_snapshots (
    snapshot_ts DateTime('UTC') DEFAULT now(),
    asset_uid   LowCardinality(String),
    payload     String   -- полный JSON ответа GetAssetFundamentals
) ENGINE = ReplacingMergeTree(snapshot_ts)
PARTITION BY toYYYYMM(snapshot_ts)
ORDER BY (asset_uid, snapshot_ts);

-- События: дивиденды, отчётности, экспирации, макро, календарь сессий.
CREATE TABLE IF NOT EXISTS market_data.events (
    event_type     LowCardinality(String),  -- dividend|report|expiry|macro|trading_day
    instrument_uid Nullable(String),        -- NULL для макрособытий
    event_time     Nullable(DateTime('UTC')),
    expected_date  Nullable(Date),
    actual         Nullable(Date),
    importance     UInt8 DEFAULT 0,
    source         LowCardinality(String),
    payload        String DEFAULT '',
    fetched_at     DateTime('UTC') DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
-- ifNull: nullable-колонки запрещены в ключах партиционирования и сортировки
-- (allow_nullable_key=0); NULL → epoch-значения (макрособытия без time/uid).
PARTITION BY toYYYYMM(ifNull(event_time, toDateTime('1970-01-01 00:00:00', 'UTC')))
ORDER BY (event_type, ifNull(instrument_uid, ''), ifNull(event_time, toDateTime('1970-01-01 00:00:00', 'UTC')));

-- ============================================================
-- Служебные таблицы (lineage, DQ, карантин)
-- ============================================================
CREATE DATABASE IF NOT EXISTS market_data_aux;

CREATE TABLE IF NOT EXISTS market_data_aux.etl_runs (
    run_id       UUID DEFAULT generateUUIDv4(),
    transform    LowCardinality(String),    -- имя шага/трансформа
    date_from    Nullable(Date),
    date_to      Nullable(Date),
    rows_in      UInt64 DEFAULT 0,
    rows_out     UInt64 DEFAULT 0,
    errors       UInt64 DEFAULT 0,
    status       LowCardinality(String),    -- running|ok|failed
    started_at   DateTime('UTC') DEFAULT now(),
    finished_at  Nullable(DateTime('UTC')),
    code_version String DEFAULT ''          -- git sha
) ENGINE = MergeTree
ORDER BY (transform, started_at);

CREATE TABLE IF NOT EXISTS market_data_aux.dq_metrics (
    run_ts  DateTime('UTC') DEFAULT now(),
    layer   LowCardinality(String),        -- bronze|silver|gold
    metric  LowCardinality(String),        -- напр. ohlc_violations, null_share
    value   Float64,
    details String DEFAULT ''
) ENGINE = MergeTree
ORDER BY (run_ts, layer, metric);

CREATE TABLE IF NOT EXISTS market_data_aux.quarantine (
    ts         DateTime('UTC') DEFAULT now(),
    table_name LowCardinality(String),
    reason     String,
    raw        String                     -- исходная строка как есть
) ENGINE = MergeTree
ORDER BY (ts, table_name);

-- ============================================================
-- MCP: ключи доступа и аудит вызовов
-- ============================================================
CREATE TABLE IF NOT EXISTS market_data_aux.mcp_keys (
    key_hash    FixedString(64),            -- SHA256 hex ключа (сам ключ не хранится)
    owner       LowCardinality(String),
    public_only UInt8 DEFAULT 1,
    revoked     UInt8 DEFAULT 0,
    created_at  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree
ORDER BY key_hash;

CREATE TABLE IF NOT EXISTS market_data_aux.mcp_audit (
    ts          DateTime DEFAULT now(),
    key_hash    FixedString(64),
    tool        LowCardinality(String),
    args        String DEFAULT '',
    rows        UInt32 DEFAULT 0,
    duration_ms UInt32 DEFAULT 0,
    ok          UInt8 DEFAULT 1
) ENGINE = MergeTree
ORDER BY (ts, key_hash)
TTL toDateTime(ts) + INTERVAL 90 DAY;

-- ============================================================
-- Сигналы: результат работы движка детекции (P4.1)
-- ============================================================
CREATE TABLE IF NOT EXISTS market_data_aux.signals (
    ts            DateTime DEFAULT now(),
    instrument_uid LowCardinality(String),
    ticker        LowCardinality(String),
    signal_type   LowCardinality(String),  -- rsi_overbought | rsi_oversold | volume_spike | ma_cross_up | ma_cross_down | golden_cross | death_cross
    bar_date      Date,
    value         Float64,
    message       String,
    sent          UInt8 DEFAULT 0          -- 1 = доставлено в Telegram (ставит рантайм)
) ENGINE = ReplacingMergeTree
ORDER BY (instrument_uid, signal_type, bar_date);

-- ============================================================
-- Бэктест сигналов (P5.1): replay 7 типов по полной истории витрины,
-- forward returns 1/5/20 торговых бара. Идемпотентно по ключу ORDER BY.
-- ============================================================
CREATE TABLE IF NOT EXISTS market_data_aux.signal_backtest (
    instrument_uid LowCardinality(String),
    ticker         LowCardinality(String),
    signal_type    LowCardinality(String),  -- те же 7 типов, что market_data_aux.signals
    bar_date       Date,
    value          Float64,                 -- rsi14 / volume_ratio / 0.0 для кроссов
    fwd_ret_1d     Nullable(Float64),
    fwd_ret_5d     Nullable(Float64),
    fwd_ret_20d    Nullable(Float64),       -- NULL если T+N за концом истории
    computed_at    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
ORDER BY (instrument_uid, signal_type, bar_date);
