# MarketDataGrabber

🇷🇺 Русский | [🇬🇧 English](README.md)

Личная система сбора и анализа рыночных данных через **T-Invest API (Т-Банк)**: накопление истории, аналитические витрины, торговые сигналы, Telegram-бот, Grafana-дашборд и MCP-сервер для доступа AI-агентов.

> Решения о сделках принимает человек. Система готовит данные, отчёты и сигналы.

## Возможности

- **Сбор данных** — минутки (бэкофилл из годовых архивов + ежедневный инкремент), дневные свечи за 6 лет, события (дивиденды, экспирации, отчётность, календарь сессий MOEX), фундаментал (54 поля: мультипликаторы, дивиденды, капитализация)
- **Хранение** — ClickHouse: bronze (сырые данные, ReplacingMergeTree, идемпотентно) → gold-витрина `mart_daily_metrics` (close, ret 1/5/20d, RSI14, ATR14, MA20/50, volume_ratio, gap_pct) + представления 5m/1h
- **Дашборд Grafana** — 13 панелей в 6 рядах: свежесть данных, DQ, состояние инструментов, тренд+MA, объёмы, RSI-зоны, события, мультипликаторы; селектор инструмента
- **Telegram-бот** — `/digest` (утренний дайджест), `/status` (свежесть), `/subscribe` (фильтры сигналов), ops/market-разделение уведомлений
- **Сигналы** — 7 типов: RSI-экстремумы, объёмные спайки (×2), close↔MA20 кроссы, golden/death cross (MA20↔MA50); доставка подписчикам по фильтрам + аннотации на графиках
- **Калибровка сигналов** — набор откалиброван по бэктесту за 6 лет (2026-10-29): доставляются ma_cross_up, death_cross, ma_cross_down, rsi_oversold; rsi_overbought/golden_cross/volume_spike детектируются, но не доставляются
- **MCP-сервер** — 5 read-only инструментов над витринами для AI-агентов (ключи, аудит)
- **DQ + watchdog** — ежесуточные проверки качества данных и ежечасный контроль свежести с алертами в Telegram

## Архитектура

```
T-Invest API ──► bronze (ClickHouse) ──► gold-витрины ──► Telegram │ Grafana │ MCP
                 candles_1m / candles_1d  mart_daily_metrics       дайджест, сигналы
                 events                   v_candles_5m / v_candles_1h
                 fundamentals_snapshots
aux: etl_runs │ dq_metrics │ signals │ mcp_keys │ mcp_audit │ quarantine
```

Docker Compose, профили: `core` (clickhouse), `obs` (grafana), `bot` (redis, telegram-bot), `jobs` (cron-задачи в контейнере), `mcp` (FastMCP-сервер).

## Быстрый старт

```bash
cp .env.example .env          # токены: T_INVEST_READ_TOKEN, TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, CLICKHOUSE_*, GRAFANA_ADMIN_*
docker compose --profile core --profile obs --profile bot --profile jobs up -d
python3 -m venv .venv && . .venv/bin/activate && pip install -e .[dev]
bash db/apply_schema.sh

# бэкофилл истории
PYTHONPATH=src .venv/bin/python scripts/backfill_1m.py
PYTHONPATH=src .venv/bin/python scripts/backfill_1d.py

# бота, графану и cron поднимает compose + install_cron.sh; дашборд: http://localhost:3000
```

## Расписание (cron, Europe/Moscow)

| Время | Задача | Скрипт |
|---|---|---|
| 00:15 пн–пт | инкремент 1m + 1d | `scripts/increment_candles.py` |
| 00:35 ежедневно | gold-витрины | `scripts/run_marts.py` |
| 00:40 ежедневно | сигналы → Telegram + аннотации | `scripts/run_signals.py` |
| 00:45 ежедневно | DQ-проверки | `transforms/run_dq.py` |
| 06:30 ежедневно | фундаментал-снапшоты | `scripts/collect_fundamentals.py` |
| вс 04:00 | события и календарь | `scripts/collect_events.py` |
| ежечасно | watchdog (свежесть/DQ) | `scripts/watchdog.py` |
| 09:10 ежедневно | Telegram-дайджест | `scripts/daily_digest.py` |

Установка: `bash deploy/install_cron.sh` (идемпотентно, трогает только строки с маркером `# MDG:auto`). Все задачи выполняются внутри контейнера `mdg-jobs`.

## MCP API (для агентов)

```
endpoint:  http://<host>:8900/mcp        (Streamable HTTP)
auth:      X-API-Key: <ключ из market_data_aux.mcp_keys>
```

| Инструмент | Возвращает |
|---|---|
| `list_instruments()` | тикеры, uid, figi, asset_uid |
| `get_daily_metrics(ticker, days)` | дневные метрики витрины |
| `get_latest_status(ticker)` | последний бар: close, ret, RSI, положение к MA |
| `get_events(ticker?)` | ближайшие события (без trading_day) |
| `get_fundamentals(ticker)` | P/E, P/B, форвардная дивидендная доходность |

Ключи: `PYTHONPATH=src python scripts/mcp_key.py generate --owner <имя>`. Все запросы только на чтение (отдельный CH-пользователь `mdg_ro`; единственное исключение — запись вызовов в `mcp_audit`).

## Universe

5 инструментов: SBER, GAZP (акции), CNYRUBF, IMOEXF (фьючерсы), IBZ6 (биткоин-фьючерс) — `config/instruments.json`.

## Разработка

```bash
.venv/bin/python -m pytest tests/ -q     # 107 тестов
bash -n deploy/install_cron.sh           # синтаксис установщика cron
```

Структура:

```
src/mdgrabber/     пакет: config, ch (ClickHouse), tinvest (API), notify (Telegram),
                   bot (aiogram), users (подписки), signals (детекция), report (дайджест),
                   mcp_tools/mcp_server/mcp_asgi (MCP), mcp_auth, logging
scripts/           сбор, бэкофилл, марты, сигналы, watchdog, дайджест, smoke-проверки
transforms/        SQL-витрины + run_dq.py
db/                schema.sql, ro_user.sql, apply-скрипты
grafana/           provisioning datasource + дашборд (как код)
deploy/            crontab.mdg + install_cron.sh
docs/              SPEC.md, Визуализация.md, mockups/
tests/             pytest (моки, без живых API)
```

## Спецификация

Полная спека фаз P0–P7: [`docs/SPEC.md`](docs/SPEC.md). Реестр графиков: [`docs/Визуализация.md`](docs/Визуализация.md).

## Статус

- ✅ **P0–P4**: инфраструктура, бэкофилл, инкремент+витрины, дайджест+бот+дашборд, MCP, cron-расписание, сигналы
- ⬜ **P5** бэктест сигналов · **P6** полуавтомат-исполнение + live-стриминг · **P7** веб-интерфейс
