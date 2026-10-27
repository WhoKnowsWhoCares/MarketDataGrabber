"""REST-клиент T-Invest API (без SDK: gRPC-типы не нужны, REST покрывает всё).

Endpoint-пути: /rest/tinkoff.public.invest.api.contract.v1.<Service>/<Method>
(между v1 и именем сервиса — точка, весь префикс — один path-сегмент; со слэшем API отдаёт 404).
Лимиты: на 429 ждём Retry-After + 1 с (число попыток ограничено).
"""
from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass

import httpx

from .config import Settings

REST = "https://invest-public-api.tinkoff.ru/rest"
PREFIX = "/tinkoff.public.invest.api.contract.v1"
HISTORY = "https://invest-public-api.tinkoff.ru/history-data"


class TInvestError(RuntimeError):
    pass


def money_to_float(v: dict | None) -> float | None:
    """MoneyValue {"units": "291", "nano": 100000000} -> 291.1"""
    if v is None:
        return None
    units = int(v.get("units", 0))
    nano = int(v.get("nano", 0))
    return units + nano / 1e9


@dataclass
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int  # в лотах


class TInvestClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._verify = settings.tls_verify
        self._http = httpx.Client(
            base_url=REST, timeout=30.0, verify=self._verify, transport=transport
        )
        self._headers = {
            "Authorization": f"Bearer {settings.t_invest_read_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        last_err = ""
        for _ in range(retries):
            r = self._http.post(path, json=payload, headers=self._headers)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2")) + 1
                time.sleep(wait)
                last_err = f"429 после {retries} попыток"
                continue
            if r.status_code >= 400:
                raise TInvestError(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        raise TInvestError(f"{path}: {last_err}")

    # --- Сервисы -----------------------------------------------------------
    def get_accounts(self) -> dict:
        return self._post(f"{PREFIX}.UsersService/GetAccounts", {})

    def get_candles(
        self, instrument_uid: str, interval: str, from_iso: str, to_iso: str
    ) -> list[Candle]:
        d = self._post(
            f"{PREFIX}.MarketDataService/GetCandles",
            {
                "instrument_id": instrument_uid,
                "interval": interval,
                "from": from_iso,
                "to": to_iso,
            },
        )
        out = []
        for c in d.get("candles", []):
            out.append(
                Candle(
                    time=c["time"],
                    open=money_to_float(c["open"]),
                    high=money_to_float(c["high"]),
                    low=money_to_float(c["low"]),
                    close=money_to_float(c["close"]),
                    volume=int(c.get("volume", 0)),
                )
            )
        return out

    def share_by(self, ticker: str, class_code: str = "TQBR") -> dict:
        return self._post(
            f"{PREFIX}.InstrumentsService/ShareBy",
            {"id_type": "INSTRUMENT_ID_TYPE_TICKER", "class_code": class_code, "id": ticker},
        ).get("instrument", {})

    def futures_all(self) -> list[dict]:
        return self._post(f"{PREFIX}.InstrumentsService/Futures", {}).get("instruments", [])

    def get_trading_schedules(self, from_iso: str | None = None, to_iso: str | None = None) -> list[dict]:
        """Расписания площадок: [{exchange, days: [...]}]. Дни — camelCase:
        date, isTradingDay, startTime/endTime, intervals. Ограничение API:
        период from..to не длиннее 14 дней (или {} — ближайшие ~15 дней)."""
        payload: dict = {}
        if from_iso and to_iso:
            payload = {"from": from_iso, "to": to_iso}
        return self._post(f"{PREFIX}.InstrumentsService/TradingSchedules", payload).get(
            "exchanges", []
        )

    def get_dividends(self, uid: str, from_iso: str, to_iso: str) -> list[dict]:
        return self._post(
            f"{PREFIX}.InstrumentsService/GetDividends",
            {"instrument_id": uid, "from": from_iso, "to": to_iso},
        ).get("dividends", [])

    def get_asset_reports(self, uid: str) -> list[dict]:
        return self._post(
            f"{PREFIX}.InstrumentsService/GetAssetReports", {"instrument_id": uid}
        ).get("events", [])

    def get_fundamentals(self, asset_uids: list[str]) -> dict:
        return self._post(f"{PREFIX}.InstrumentsService/GetAssetFundamentals", {"assets": asset_uids})

    def get_assets(self) -> list[dict]:
        """Все активы (у каждого — instruments[] с instrument uid)."""
        return self._post(f"{PREFIX}.InstrumentsService/GetAssets", {}).get("assets", [])

    def get_asset_by(self, instrument_uid: str) -> dict:
        """instrument_uid → asset. ВНИМАНИЕ: asset.uid может отличаться от instrument_uid.
        Замечено: для части акций REST-GetAssetBy отвечает 404 «Asset not found»,
        хотя актив есть в GetAssets — вызывающий код должен уметь фолбэк."""
        return self._post(
            f"{PREFIX}.InstrumentsService/GetAssetBy",
            {"id_type": "INSTRUMENT_ID_TYPE_INSTRUMENT_ID", "id": instrument_uid},
        ).get("asset", {})

    def get_consensus_forecasts(self) -> dict:
        return self._post(f"{PREFIX}.InstrumentsService/GetConsensusForecasts", {})

    def history_zip(self, figi: str, year: int) -> bytes:
        """Годовой ZIP минутных свечей. ВАЖНО: параметр именно figi (uid даёт 400)."""
        with httpx.Client(timeout=120.0, verify=self._verify) as http:
            r = http.get(
                HISTORY,
                params={"figi": figi, "year": str(year)},
                headers={"Authorization": self._headers["Authorization"]},
            )
            if r.status_code != 200:
                raise TInvestError(f"history-data: HTTP {r.status_code}: {r.text[:200]}")
            return r.content

    @staticmethod
    def parse_history_zip(data: bytes) -> list[dict]:
        """CSV внутри ZIP: uid;utc;open;close;high;low;volume; (разделитель ';',
        без заголовка; запятая с заголовком тоже поддержана). Объём в лотах."""
        rows = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    text = f.read().decode("utf-8")
                for line in text.splitlines():
                    if not line or line.lower().startswith("uid"):
                        continue
                    sep = ";" if ";" in line else ","
                    parts = line.split(sep)
                    if len(parts) < 7:
                        continue
                    rows.append(
                        {
                            "uid": parts[0],
                            "utc": parts[1],
                            "open": float(parts[2]),
                            "close": float(parts[3]),
                            "high": float(parts[4]),
                            "low": float(parts[5]),
                            "volume": int(float(parts[6])),
                        }
                    )
        return rows
