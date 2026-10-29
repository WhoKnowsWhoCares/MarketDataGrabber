# MarketDataGrabber

🇬🇧 English | [🇷🇺 Русский](README.ru.md)

A personal market-data collection and analytics system built on the **T-Invest API (T-Bank)**: history accumulation, analytical marts, trading signals, a Telegram bot, a Grafana dashboard, and an MCP server for AI-agent access.

> Trading decisions are made by a human. The system prepares data, reports, and signals.

## Features

- **Data collection** — minute candles (backfill from yearly archives + daily incremental), 6 years of daily candles, events (dividends, expiries, earnings, MOEX session calendar), fundamentals (54 fields: multiples, dividends, market cap)
- **Storage** — ClickHouse: bronze (raw data, ReplacingMergeTree, idempotent) → the `mart_daily_metrics` gold mart (close, ret 1/5/20d, RSI14, ATR14, MA20/50, volume_ratio, gap_pct) + 5m/1h views
- **Grafana dashboard** — 13 panels in 6 rows: data freshness, DQ, instrument state cards, trend+MA, volumes, RSI zones, events, multiples; instrument selector
- **Telegram bot** — `/digest` (morning digest), `/status` (freshness), `/subscribe` (signal filters), ops/market notification split
- **Signals** — 7 types: RSI extremes, volume spikes (×2), close↔MA20 crossovers, golden/death cross (MA20↔MA50); delivery to subscribers with per-user filters + chart annotations
- **MCP server** — 5 read-only tools over the marts for AI agents (API keys, audit trail)
- **DQ + watchdog** — daily data-quality checks and hourly freshness monitoring with Telegram alerts

## Architecture

```
T-Invest API ──► bronze (ClickHouse) ──► gold marts ──► Telegram │ Grafana │ MCP
                 candles_1m / candles_1d  mart_daily_metrics      digest, signals
                 events                   v_candles_5m / v_candles_1h
                 fundamentals_snapshots
aux: etl_runs │ dq_metrics │ signals │ mcp_keys │ mcp_audit │ quarantine
```

Docker Compose profiles: `core` (clickhouse), `obs` (grafana), `bot` (redis, telegram-bot), `jobs` (cron tasks in a container), `mcp` (FastMCP server).

## Quick start

```bash
cp .env.example .env          # tokens: T_INVEST_READ_TOKEN, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, CLICKHOUSE_*, GRAFANA_ADMIN_*
docker compose --profile core --profile obs --profile bot --profile jobs up -d
python3 -m venv .venv && . .venv/bin/activate && pip install -e .[dev]
bash db/apply_schema.sh

# backfill history
PYTHONPATH=src .venv/bin/python scripts/backfill_1m.py
PYTHONPATH=src .venv/bin/python scripts/backfill_1d.py

# bot, Grafana and cron come up via compose + install_cron.sh; dashboard: http://localhost:3000
```

## Schedule (cron, Europe/Moscow)

| Time | Task | Script |
|---|---|---|
| 00:15 Mon–Fri | 1m + 1d incremental | `scripts/increment_candles.py` |
| 00:35 daily | gold marts | `scripts/run_marts.py` |
| 00:40 daily | signals → Telegram + annotations | `scripts/run_signals.py` |
| 00:45 daily | DQ checks | `transforms/run_dq.py` |
| 06:30 daily | fundamentals snapshots | `scripts/collect_fundamentals.py` |
| Sun 04:00 | events and calendar | `scripts/collect_events.py` |
| hourly | watchdog (freshness/DQ) | `scripts/watchdog.py` |
| 09:10 daily | Telegram digest | `scripts/daily_digest.py` |

Install: `bash deploy/install_cron.sh` (idempotent, only touches lines tagged `# MDG:auto`). All tasks run inside the `mdg-jobs` container.

## MCP API (for agents)

```
endpoint:  http://<host>:8900/mcp        (Streamable HTTP)
auth:      X-API-Key header, key stored in market_data_aux.mcp_keys
```

| Tool | Returns |
|---|---|
| `list_instruments()` | tickers, uid, figi, asset_uid |
| `get_daily_metrics(ticker, days)` | daily mart metrics |
| `get_latest_status(ticker)` | latest bar: close, ret, RSI, position vs MA |
| `get_events(ticker?)` | upcoming events (trading_day excluded) |
| `get_fundamentals(ticker)` | P/E, P/B, forward dividend yield |

Keys: `PYTHONPATH=src python scripts/mcp_key.py generate --owner <name>`. All queries are read-only (a dedicated ClickHouse user `mdg_ro`; the only exception is writing calls to `mcp_audit`).

## Universe

5 instruments: SBER, GAZP (stocks), CNYRUBF, IMOEXF (futures), IBZ6 (bitcoin futures) — `config/instruments.json`.

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # 107 tests
bash -n deploy/install_cron.sh           # cron installer syntax check
```

Layout:

```
src/mdgrabber/     package: config, ch (ClickHouse), tinvest (API), notify (Telegram),
                   bot (aiogram), users (subscriptions), signals (detection), report (digest),
                   mcp_tools/mcp_server/mcp_asgi (MCP), mcp_auth, logging
scripts/           collection, backfill, marts, signals, watchdog, digest, smoke checks
transforms/        SQL marts + run_dq.py
db/                schema.sql, ro_user.sql, apply scripts
grafana/           datasource provisioning + dashboard as code
deploy/            crontab.mdg + install_cron.sh
docs/              SPEC.md, Визуализация.md (chart registry, RU), mockups/
tests/             pytest (mocked, no live APIs)
```

## Specification

Full phase spec (P0–P7): [`docs/SPEC.md`](docs/SPEC.md). Chart registry: [`docs/Визуализация.md`](docs/Визуализация.md) (Russian).

## Status

- ✅ **P0–P4**: infrastructure, backfill, incremental+marts, digest+bot+dashboard, MCP, cron schedule, signals
- ⬜ **P5** signal backtesting · **P6** semi-automatic execution + live streaming · **P7** web UI
