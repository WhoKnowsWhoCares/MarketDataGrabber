"""Торговый клиент T-Invest API (sandbox).

Фаза P6.1: только sandbox (paper trading). Live-режим будет добавлен позже
отдельным флагом — сейчас trade_mode != "sandbox" запрещён на уровне клиента.

Критично (проверено живьём):
- host sandbox: https://sandbox-invest-public-api.tinkoff.ru/rest
- units/nano/quantity/price — СТРОКИ в JSON ("150"); числовые значения
  дают code 3 (ошибка парсинга);
- цена лимитки должна быть в биржевых лимитах (далёкая цена → code 3, 30099).
"""
from __future__ import annotations

import httpx

from .config import Settings

SANDBOX_REST = "https://sandbox-invest-public-api.tinkoff.ru/rest"
PREFIX = "/tinkoff.public.invest.api.contract.v1"

_DIRECTIONS = {"buy": "ORDER_DIRECTION_BUY", "sell": "ORDER_DIRECTION_SELL"}


class TradeError(RuntimeError):
    """Ошибка торгового API: код и сообщение из ответа."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"code={code}: {message}")


class TradeClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        if settings.trade_mode != "sandbox":
            raise RuntimeError("live trading not enabled")
        self._account_id = settings.t_invest_sandbox_account_id
        self._http = httpx.Client(
            base_url=settings.trade_base_url,
            timeout=30.0,
            verify=settings.tls_verify,
            transport=transport,
        )
        self._headers = {
            "Authorization": f"Bearer {settings.t_invest_sandbox_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        r = self._http.post(f"{PREFIX}.{path}" if "." not in path else path, json=payload, headers=self._headers)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and "code" in body:
            raise TradeError(int(body["code"]), str(body.get("message", "")))
        if r.status_code >= 400:
            raise TradeError(r.status_code, r.text[:200])
        return body

    # --- SandboxService ------------------------------------------------------
    def get_accounts(self) -> dict:
        return self._post("SandboxService/GetSandboxAccounts", {})

    def ensure_account(self) -> str:
        """Вернуть accountId первого sandbox-аккаунта; если аккаунтов нет — открыть."""
        accs = self.get_accounts().get("accounts", [])
        if accs:
            return accs[0]["id"]
        return self._post("SandboxService/OpenSandboxAccount", {})["accountId"]

    def pay_in(self, amount_units: int) -> dict:
        return self._post(
            "SandboxService/SandboxPayIn",
            {
                "accountId": self._account_id,
                "amount": {
                    "currency": "rub",
                    "units": str(amount_units),
                    "nano": 0,
                },
            },
        )

    # --- Orders ---------------------------------------------------------------
    def _order_payload(
        self,
        instrument_id: str,
        lots: int,
        direction: str,
        order_type: str,
        price: dict | None = None,
    ) -> dict:
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction должен быть buy/sell, получено: {direction}")
        payload: dict = {
            "instrumentId": instrument_id,
            "quantity": str(lots),
            "direction": _DIRECTIONS[direction],
            "accountId": self._account_id,
            "orderType": order_type,
        }
        if price is not None:
            payload["price"] = price
        return payload

    def place_market_order(self, instrument_id: str, lots: int, direction: str) -> dict:
        return self._post(
            "SandboxService/PostSandboxOrder",
            self._order_payload(instrument_id, lots, direction, "ORDER_TYPE_MARKET"),
        )

    def place_limit_order(
        self, instrument_id: str, lots: int, price_units: int, price_nano: int, direction: str
    ) -> dict:
        return self._post(
            "SandboxService/PostSandboxOrder",
            self._order_payload(
                instrument_id,
                lots,
                direction,
                "ORDER_TYPE_LIMIT",
                price={"units": str(price_units), "nano": price_nano},
            ),
        )

    def get_orders(self) -> dict:
        return self._post("SandboxService/GetSandboxOrders", {"accountId": self._account_id})

    def cancel_order(self, order_id: str) -> dict:
        return self._post(
            "SandboxService/CancelSandboxOrder",
            {"accountId": self._account_id, "orderId": order_id},
        )

    # --- Operations -----------------------------------------------------------
    def get_portfolio(self) -> dict:
        return self._post("OperationsService/GetPortfolio", {"accountId": self._account_id})
