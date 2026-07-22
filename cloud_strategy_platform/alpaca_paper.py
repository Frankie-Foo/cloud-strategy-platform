"""Cloud-only adapter pinned permanently to Alpaca Paper Trading."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class BrokerWritesDisabledError(BrokerError):
    pass


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PaperAccount(FrozenModel):
    status: str
    account_blocked: bool
    trading_blocked: bool
    equity: str
    last_equity: str
    buying_power: str


class PaperPosition(FrozenModel):
    symbol: str
    qty: str
    side: str
    market_value: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class BrokerOrder(FrozenModel):
    id: str
    client_order_id: str
    symbol: str
    qty: int = Field(gt=0)
    filled_qty: str
    status: str

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class PaperOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    side: Literal["buy"] = "buy"
    order_type: Literal["market", "limit"] = "market"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    limit_price: str | None = None
    take_profit_price: str = Field(min_length=1)
    stop_loss_price: str = Field(min_length=1)

    def broker_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "order_class": "bracket",
            "take_profit": {"limit_price": self.take_profit_price},
            "stop_loss": {"stop_price": self.stop_loss_price},
        }
        if self.order_type == "limit":
            if self.limit_price is None:
                raise ValueError("limit order requires limit_price")
            payload["limit_price"] = self.limit_price
        elif self.limit_price is not None:
            raise ValueError("market order cannot include limit_price")
        return payload


class PaperCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    side: Literal["sell"] = "sell"
    order_type: Literal["market"] = "market"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False

    def broker_payload(self) -> dict[str, object]:
        return self.model_dump()


class AlpacaPaperBroker:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        writes_enabled: bool,
        client: httpx.Client | None = None,
    ):
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Alpaca credentials are required")
        self.writes_enabled = writes_enabled
        self._headers = {
            "APCA-API-KEY-ID": api_key.strip(),
            "APCA-API-SECRET-KEY": api_secret.strip(),
        }
        self._client = client or httpx.Client(timeout=20)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_error:
            raise BrokerError(f"paper broker request failed: HTTP {response.status_code}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{PAPER_BASE_URL}{path}",
                headers=self._headers,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise BrokerError("paper broker request failed") from exc
        self._raise(response)
        return response

    def get_account(self) -> PaperAccount:
        return PaperAccount.model_validate(self._request("GET", "/v2/account").json())

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        response = self._client.get(
            f"{PAPER_BASE_URL}/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            headers=self._headers,
        )
        if response.status_code == 404:
            return None
        self._raise(response)
        return BrokerOrder.model_validate(response.json())

    def list_positions(self) -> tuple[PaperPosition, ...]:
        payload = self._request("GET", "/v2/positions").json()
        if not isinstance(payload, list):
            raise BrokerError("paper positions response was invalid")
        return tuple(PaperPosition.model_validate(item) for item in payload)

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        payload = self._request(
            "GET", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500}
        ).json()
        if not isinstance(payload, list):
            raise BrokerError("paper orders response was invalid")
        return tuple(BrokerOrder.model_validate(item) for item in payload)

    def cancel_order(self, order_id: str) -> bool:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        response = self._client.delete(
            f"{PAPER_BASE_URL}/v2/orders/{order_id}", headers=self._headers
        )
        if response.status_code == 404:
            return False
        self._raise(response)
        return True

    def _submit(self, request: PaperOrderRequest | PaperCloseRequest) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        existing = self.get_order_by_client_id(request.client_order_id)
        if existing is not None:
            return existing
        response = self._client.post(
            f"{PAPER_BASE_URL}/v2/orders",
            headers=self._headers,
            json=request.broker_payload(),
        )
        if response.status_code == 422:
            existing = self.get_order_by_client_id(request.client_order_id)
            if existing is not None:
                return existing
        self._raise(response)
        return BrokerOrder.model_validate(response.json())

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        return self._submit(request)

    def submit_close_order_idempotent(self, request: PaperCloseRequest) -> BrokerOrder:
        return self._submit(request)
