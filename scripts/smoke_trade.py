"""Smoke торгового клиента на sandbox. Запуск: .venv/bin/python scripts/smoke_trade.py

Цепочка: аккаунты → портфель → market BUY 1 SBER → лимитка на 1% ниже рынка →
отмена. НЕ платит pay-in, НЕ открывает аккаунт (уже существует с 100K RUB).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdgrabber.config import Settings
from mdgrabber.tinvest import TInvestClient, money_to_float
from mdgrabber.trade import TradeClient

SBER_FIGI = "BBG004730N88"


def last_sber_price(read: TInvestClient) -> tuple[int, int] | None:
    """Текущая цена SBER (close последней минутной свечи) → (units, nano)."""
    candles = read.get_candles(
        read.share_by("SBER")["uid"],
        "CANDLE_INTERVAL_1_MIN",
        "2026-10-29T09:50:00+03:00",
        "2026-10-29T23:59:00+03:00",
    )
    if not candles:
        return None
    px = candles[-1].close * 0.99  # на 1% ниже рынка — внутри биржевых лимитов
    units = int(px)
    nano = int(round((px - units) * 1e9))
    return units, nano


def main() -> int:
    s = Settings.load()
    trade = TradeClient(s)  # guard: trade_mode != "sandbox" → RuntimeError

    accs = trade.get_accounts().get("accounts", [])
    print(f"sandbox accounts: {[a.get('id') for a in accs]}")

    pf = trade.get_portfolio()
    positions = [
        (p.get("figi"), money_to_float(p.get("quantity"))) for p in pf.get("positions", [])
    ]
    cash = money_to_float(pf.get("totalAmountCurrencies")) or money_to_float(
        pf.get("totalAmountPositions")
    )
    print(f"cash: {cash}")
    print(f"positions: {positions}")

    fill = trade.place_market_order(SBER_FIGI, 1, "buy")
    status = fill.get("executionReportStatus")
    print(f"market BUY 1 SBER: orderId={fill.get('orderId')} status={status} "
          f"lotsExecuted={fill.get('lotsExecuted')}")
    if fill.get("orderId") is None:
        print("SMOKE: FAIL (нет orderId)")
        return 1

    px = last_sber_price(TInvestClient(s))
    if px is None:
        print("SMOKE: FAIL (не получили цену SBER для лимитки)")
        return 1
    units, nano = px
    print(f"limit price (market -1%): {units}.{nano:09d}")
    lim = trade.place_limit_order(SBER_FIGI, 1, units, nano, "buy")
    lim_id = lim.get("orderId")
    print(f"limit BUY: orderId={lim_id} status={lim.get('executionReportStatus')}")

    if lim_id:
        cancelled = trade.cancel_order(lim_id)
        print(f"cancel: {cancelled.get('time', 'ok')}")
        orders = trade.get_orders().get("orders", [])
        still = [o.get("orderId") for o in orders if o.get("orderId") == lim_id]
        print(f"заявка после cancel в списке: {bool(still)}")

    print("SMOKE: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
