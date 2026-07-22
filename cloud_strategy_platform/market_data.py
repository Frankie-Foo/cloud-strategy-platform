"""The repository-local Alpaca SIP protocol; no Broker or order capability exists."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

SIP_STREAM_URL = "wss://stream.data.alpaca.markets/v2/sip"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]*$")


class SipProtocolError(RuntimeError):
    pass


class WebSocketLike(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SipQuote(FrozenModel):
    event_type: str = "quote"
    symbol: str
    ts_utc: datetime
    bid_price: float = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_price: float = Field(ge=0)
    ask_size: int = Field(ge=0)
    feed: str = "sip"
    provenance: str

    @field_validator("ts_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("SIP event timestamps must be UTC")
        return value


class SipBar(FrozenModel):
    event_type: str = "bar"
    symbol: str
    ts_utc: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    vwap: float = Field(gt=0)
    feed: str = "sip"
    provenance: str

    @field_validator("ts_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("SIP event timestamps must be UTC")
        return value


SipEvent = SipQuote | SipBar


def _decode(frame: str | bytes) -> list[object]:
    try:
        value = json.loads(frame.decode() if isinstance(frame, bytes) else frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SipProtocolError("invalid JSON frame from SIP stream") from exc
    if not isinstance(value, list):
        raise SipProtocolError("SIP stream frame must be a JSON array")
    return value


def parse_market_data_frame(frame: str | bytes) -> tuple[SipEvent, ...]:
    events: list[SipEvent] = []
    for item in _decode(frame):
        if not isinstance(item, dict) or item.get("T") not in {"q", "b"}:
            continue
        symbol = str(item.get("S", "")).strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise SipProtocolError("SIP event has an invalid symbol")
        raw_timestamp = item.get("t")
        if not isinstance(raw_timestamp, str):
            raise SipProtocolError("SIP event is missing its timestamp")
        try:
            ts_utc = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            provenance = f"alpaca.sip.websocket@{ts_utc.isoformat()}"
            if item["T"] == "q":
                events.append(
                    SipQuote(
                        symbol=symbol,
                        ts_utc=ts_utc,
                        bid_price=item["bp"],
                        bid_size=item["bs"],
                        ask_price=item["ap"],
                        ask_size=item["as"],
                        provenance=provenance,
                    )
                )
            else:
                events.append(
                    SipBar(
                        symbol=symbol,
                        ts_utc=ts_utc,
                        open=item["o"],
                        high=item["h"],
                        low=item["l"],
                        close=item["c"],
                        volume=item["v"],
                        trade_count=item["n"],
                        vwap=item["vw"],
                        provenance=provenance,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise SipProtocolError("SIP event failed schema validation") from exc
    return tuple(events)


class AlpacaSipStream:
    def __init__(self, *, api_key: str, api_secret: str, symbols: tuple[str, ...]):
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not api_key.strip() or not api_secret.strip() or not normalized:
            raise ValueError("credentials and symbols are required")
        if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("symbol is invalid")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self.symbols = normalized

    async def _subscribe(self, socket: WebSocketLike) -> None:
        await socket.recv()
        await socket.send(
            json.dumps({"action": "auth", "key": self._api_key, "secret": self._api_secret})
        )
        authenticated = _decode(await socket.recv())
        if not any(
            isinstance(item, dict) and item.get("msg") == "authenticated"
            for item in authenticated
        ):
            raise SipProtocolError("SIP authentication failed")
        await socket.send(
            json.dumps(
                {"action": "subscribe", "bars": self.symbols, "quotes": self.symbols}
            )
        )
        await socket.recv()

    async def events(self) -> AsyncGenerator[SipEvent, None]:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        async for socket in connect(
            SIP_STREAM_URL,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_048_576,
            max_queue=16,
        ):
            await self._subscribe(cast(WebSocketLike, socket))
            try:
                async for frame in socket:
                    for event in parse_market_data_frame(frame):
                        yield event
            except ConnectionClosed:
                await asyncio.sleep(0)
                continue
