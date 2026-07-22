"""Cloud-only Alpaca historical SIP adapter; credentials never cross the API boundary."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx


class MarketDataError(RuntimeError):
    pass


class AlpacaHistoricalMarketData:
    BASE_URL = "https://data.alpaca.markets/v2/stocks"
    NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Alpaca credentials are required")
        self._headers = {
            "APCA-API-KEY-ID": api_key.strip(),
            "APCA-API-SECRET-KEY": api_secret.strip(),
        }
        self._client = client or httpx.Client(timeout=30)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _validate(
        symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[str, ...]:
        normalized = tuple(sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()}))
        if not normalized:
            raise ValueError("at least one symbol is required")
        if (
            start_utc.tzinfo is None
            or start_utc.utcoffset() != timedelta(0)
            or end_utc.tzinfo is None
            or end_utc.utcoffset() != timedelta(0)
            or end_utc <= start_utc
        ):
            raise ValueError("market-data interval must be timezone-aware UTC")
        return normalized

    def _pages(
        self,
        endpoint: str,
        *,
        symbols: tuple[str, ...],
        start_utc: datetime,
        end_utc: datetime,
        timeframe: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        normalized = self._validate(symbols, start_utc, end_utc)
        params: dict[str, str | int] = {
            "symbols": ",".join(normalized),
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "feed": "sip",
            "limit": 10_000,
            "sort": "asc",
        }
        if timeframe is not None:
            params.update({"timeframe": timeframe, "adjustment": "split"})
        rows: list[dict[str, object]] = []
        while True:
            try:
                response = self._client.get(
                    f"{self.BASE_URL}/{endpoint}", params=params, headers=self._headers
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise MarketDataError("Alpaca market-data request failed") from exc
            if not isinstance(payload, dict):
                raise MarketDataError("Alpaca market-data response was invalid")
            groups = payload.get(endpoint, {})
            if not isinstance(groups, dict):
                raise MarketDataError("Alpaca market-data response was invalid")
            for symbol, values in groups.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    if isinstance(value, dict):
                        rows.append({"symbol": str(symbol).upper(), **value})
            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = str(token)
        return tuple(rows)

    def bars(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        raw = self._pages(
            "bars", symbols=symbols, start_utc=start_utc, end_utc=end_utc, timeframe="1Min"
        )
        return tuple(
            {
                "symbol": row["symbol"],
                "ts_utc": row.get("t"),
                "open": row.get("o"),
                "high": row.get("h"),
                "low": row.get("l"),
                "close": row.get("c"),
                "volume": row.get("v"),
                "trade_count": row.get("n"),
                "vwap": row.get("vw"),
                "source": "cloud.alpaca.market_data",
                "feed": "sip",
                "adjustment": "split_adjusted",
            }
            for row in raw
        )

    def quotes(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        raw = self._pages(
            "quotes", symbols=symbols, start_utc=start_utc, end_utc=end_utc
        )
        return tuple(
            {
                "symbol": row["symbol"],
                "ts_utc": row.get("t"),
                "bid_price": row.get("bp"),
                "ask_price": row.get("ap"),
                "bid_size": row.get("bs"),
                "ask_size": row.get("as"),
                "bid_exchange": row.get("bx"),
                "ask_exchange": row.get("ax"),
                "conditions": row.get("c") if isinstance(row.get("c"), list) else [],
                "tape": row.get("z"),
                "source": "cloud.alpaca.market_data",
                "feed": "sip",
            }
            for row in raw
        )

    def news(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        self._validate(("AAPL",), start_utc, end_utc)
        params: dict[str, str | int] = {
            "start": start_utc.isoformat(),
            "end": end_utc.isoformat(),
            "sort": "asc",
            "limit": 50,
            "include_content": "false",
        }
        rows: list[dict[str, object]] = []
        while True:
            try:
                response = self._client.get(
                    self.NEWS_URL, params=params, headers=self._headers
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise MarketDataError("Alpaca news request failed") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("news"), list):
                raise MarketDataError("Alpaca news response was invalid")
            rows.extend(item for item in payload["news"] if isinstance(item, dict))
            token = payload.get("next_page_token")
            if not token:
                return tuple(rows)
            params["page_token"] = str(token)
