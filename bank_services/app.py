"""FastAPI app for the five simulated bank microservices: accounts, cards, disputes, risk, payments.

Services enforce domain rules but trust the caller's identity (no auth). Authorization is the agent layer's job.
Run all services in one process, or one per process with ``SERVICE=<name> uvicorn bank_services.app:app``.
Each process holds its own in-memory store, so cross-service consistency only exists in single-process mode.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bank_services.data import DISPUTE_WINDOW_DAYS, DisputeReason, FreezeReason
from bank_services.risk import detect_anomalies
from bank_services.store import STORE, BankStore, FaultMode, Response

SERVICES: tuple[str, ...] = ("accounts", "cards", "disputes", "risk", "payments")
MAX_TRANSACTION_LIMIT = 200
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# ---- errors -------------------------------------------------------------------------------------------------------


class BankError(Exception):
    """Domain error rendered as ``{"detail": {"code": ..., "message": ...}}``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def body(self) -> dict[str, Any]:
        return {"detail": {"code": self.code, "message": self.message}}


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=BankError(status_code, code, message).body())


async def _bank_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BankError)
    return _error(exc.status_code, exc.code, exc.message)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors())
    return _error(422, "VALIDATION_ERROR", problems)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    return _error(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))


# ---- shared helpers -----------------------------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def get_store(request: Request) -> BankStore:
    return request.app.state.store


StoreDep = Annotated[BankStore, Depends(get_store)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


def _get(store: BankStore, collection: str, obj_id: str, label: str) -> dict[str, Any]:
    """Look up an object in the store (caller holds the lock) or raise 404 NOT_FOUND."""
    obj = store.state[collection].get(obj_id)
    if obj is None:
        raise BankError(404, "NOT_FOUND", f"{label} {obj_id} not found")
    return obj


def _read(store: BankStore, fn: Callable[[], Any]) -> Any:
    """Run a read under the lock and return a deep copy, so responses never alias live state."""
    with store.lock:
        return copy.deepcopy(fn())


def _write(store: BankStore, request: Request, key: str | None, operation: Callable[[], Response]) -> JSONResponse:
    """Apply a state change at most once per (method + path, Idempotency-Key)."""

    def guarded() -> Response:
        try:
            return operation()
        except BankError as exc:
            return exc.status_code, exc.body()

    status, body = store.run_idempotent(f"{request.method} {request.url.path}", key, guarded)
    return JSONResponse(status_code=status, content=body)


# ---- accounts -----------------------------------------------------------------------------------------------------

accounts_router = APIRouter(prefix="/accounts/v1", tags=["accounts"])


class ContactUpdate(StrictModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)


@accounts_router.get("/customers/{user_id}")
def get_customer(user_id: str, store: StoreDep) -> dict[str, Any]:
    return _read(store, lambda: _get(store, "customers", user_id, "customer"))


@accounts_router.get("/customers/{user_id}/accounts")
def list_customer_accounts(user_id: str, store: StoreDep) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        customer = _get(store, "customers", user_id, "customer")
        return {"user_id": user_id, "accounts": [store.state["accounts"][a] for a in customer["account_ids"]]}

    return _read(store, read)


@accounts_router.get("/accounts/{account_id}/transactions")
def list_transactions(
    account_id: str,
    store: StoreDep,
    days: Annotated[int, Query(ge=1, le=3650)] = 30,
    limit: Annotated[int, Query(ge=1)] = 50,
    merchant: str | None = None,
    card_id: str | None = None,
) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        _get(store, "accounts", account_id, "account")
        since = store.now - timedelta(days=days)
        txns = [
            t
            for t in store.account_transactions(account_id)
            if since <= datetime.fromisoformat(t["posted_at"]) <= store.now
        ]
        if merchant:
            needle = merchant.casefold()
            txns = [t for t in txns if needle in t["merchant"].casefold()]
        if card_id:
            txns = [t for t in txns if t["card_id"] == card_id]
        txns.sort(key=lambda t: (datetime.fromisoformat(t["posted_at"]), t["id"]), reverse=True)
        txns = txns[: min(limit, MAX_TRANSACTION_LIMIT)]
        return {"account_id": account_id, "days": days, "count": len(txns), "transactions": txns}

    return _read(store, read)


@accounts_router.get("/transactions/{txn_id}")
def get_transaction(txn_id: str, store: StoreDep) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        txn = _get(store, "transactions", txn_id, "transaction")
        return {
            **txn,
            "owner_user_id": store.owner_of_account(txn["account_id"]),
            "age_days": store.txn_age_days(txn),
        }

    return _read(store, read)


@accounts_router.post("/accounts/{account_id}/close")
def close_account(
    account_id: str, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        account = _get(store, "accounts", account_id, "account")
        if account["status"] == "closed":
            raise BankError(409, "ACCOUNT_ALREADY_CLOSED", f"account {account_id} is already closed")
        account["status"] = "closed"
        return 200, copy.deepcopy(account)

    return _write(store, request, idempotency_key, apply)


@accounts_router.patch("/customers/{user_id}/contact")
def update_contact(
    user_id: str, body: ContactUpdate, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        customer = _get(store, "customers", user_id, "customer")
        store.state["contact_changes"].append(
            {
                "user_id": user_id,
                "old_email": customer["email"],
                "new_email": body.email,
                "changed_at": store.now.isoformat(),
            }
        )
        customer["email"] = body.email
        return 200, copy.deepcopy(customer)

    return _write(store, request, idempotency_key, apply)


# ---- cards --------------------------------------------------------------------------------------------------------

cards_router = APIRouter(prefix="/cards/v1", tags=["cards"])


class FreezeRequest(StrictModel):
    reason: FreezeReason


def _ensure_not_stolen(card: dict[str, Any]) -> None:
    if card["status"] == "reported_stolen":
        raise BankError(409, "CARD_PERMANENTLY_BLOCKED", f"card {card['id']} was reported stolen and is blocked")


@cards_router.get("/customers/{user_id}/cards")
def list_customer_cards(user_id: str, store: StoreDep) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        _get(store, "customers", user_id, "customer")
        return {"user_id": user_id, "cards": [c for c in store.state["cards"].values() if c["user_id"] == user_id]}

    return _read(store, read)


@cards_router.get("/cards/{card_id}")
def get_card(card_id: str, store: StoreDep) -> dict[str, Any]:
    return _read(store, lambda: _get(store, "cards", card_id, "card"))


@cards_router.post("/cards/{card_id}/freeze")
def freeze_card(
    card_id: str, body: FreezeRequest, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        card = _get(store, "cards", card_id, "card")
        _ensure_not_stolen(card)
        if card["status"] == "frozen":
            raise BankError(409, "CARD_ALREADY_FROZEN", f"card {card_id} is already frozen")
        card["status"] = "frozen"
        card["status_reason"] = body.reason
        return 200, copy.deepcopy(card)

    return _write(store, request, idempotency_key, apply)


@cards_router.post("/cards/{card_id}/unfreeze")
def unfreeze_card(
    card_id: str, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        card = _get(store, "cards", card_id, "card")
        _ensure_not_stolen(card)
        if card["status"] != "frozen":
            raise BankError(409, "CARD_NOT_FROZEN", f"card {card_id} is not frozen")
        card["status"] = "active"
        card["status_reason"] = None
        return 200, copy.deepcopy(card)

    return _write(store, request, idempotency_key, apply)


# ---- disputes -----------------------------------------------------------------------------------------------------

disputes_router = APIRouter(prefix="/disputes/v1", tags=["disputes"])


class DisputeCreate(StrictModel):
    transaction_id: str = Field(min_length=1, max_length=64)
    reason: DisputeReason
    amount: float | None = Field(default=None, gt=0)
    details: str | None = Field(default=None, max_length=500)


@disputes_router.post("/disputes", status_code=201)
def create_dispute(
    body: DisputeCreate, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        txn = _get(store, "transactions", body.transaction_id, "transaction")
        age = store.txn_age_days(txn)
        if age > DISPUTE_WINDOW_DAYS:
            raise BankError(
                422,
                "DISPUTE_WINDOW_EXPIRED",
                f"transaction is {age} days old; disputes must be opened within {DISPUTE_WINDOW_DAYS} days",
            )
        amount = round(body.amount if body.amount is not None else txn["amount"], 2)
        if amount > txn["amount"]:
            raise BankError(
                422, "AMOUNT_EXCEEDS_TRANSACTION", f"dispute amount {amount} exceeds transaction amount {txn['amount']}"
            )
        for existing in store.state["disputes"].values():
            if existing["transaction_id"] == txn["id"] and existing["status"] != "closed":
                raise BankError(
                    409, "DUPLICATE_DISPUTE", f"dispute {existing['id']} is already open for transaction {txn['id']}"
                )
        dispute = {
            "id": store.next_id("dispute", "dsp"),
            "transaction_id": txn["id"],
            "account_id": txn["account_id"],
            "user_id": store.owner_of_account(txn["account_id"]),
            "reason": body.reason,
            "amount": amount,
            "details": body.details,
            "status": "open",
            "created_at": store.now.isoformat(),
        }
        store.state["disputes"][dispute["id"]] = dispute
        return 201, copy.deepcopy(dispute)

    return _write(store, request, idempotency_key, apply)


@disputes_router.get("/disputes/{dispute_id}")
def get_dispute(dispute_id: str, store: StoreDep) -> dict[str, Any]:
    return _read(store, lambda: _get(store, "disputes", dispute_id, "dispute"))


@disputes_router.get("/customers/{user_id}/disputes")
def list_customer_disputes(user_id: str, store: StoreDep) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        _get(store, "customers", user_id, "customer")
        disputes = [d for d in store.state["disputes"].values() if d["user_id"] == user_id]
        return {"user_id": user_id, "disputes": disputes}

    return _read(store, read)


# ---- risk ---------------------------------------------------------------------------------------------------------

risk_router = APIRouter(prefix="/risk/v1", tags=["risk"])


class FlagCreate(StrictModel):
    transaction_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


@risk_router.get("/accounts/{account_id}/anomalies")
def get_anomalies(account_id: str, store: StoreDep, days: Annotated[int, Query(ge=1, le=365)] = 90) -> dict[str, Any]:
    def read() -> list[dict[str, Any]]:
        _get(store, "accounts", account_id, "account")
        return store.account_transactions(account_id)

    txns = _read(store, read)
    return {"account_id": account_id, "days": days, "anomalies": detect_anomalies(txns, store.now, days)}


@risk_router.post("/flags", status_code=201)
def create_flag(
    body: FlagCreate, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        txn = _get(store, "transactions", body.transaction_id, "transaction")
        for existing in store.state["flags"].values():
            if existing["transaction_id"] == txn["id"]:
                raise BankError(
                    409, "ALREADY_FLAGGED", f"transaction {txn['id']} is already flagged ({existing['id']})"
                )
        flag = {
            "id": store.next_id("flag", "flg"),
            "transaction_id": txn["id"],
            "account_id": txn["account_id"],
            "user_id": store.owner_of_account(txn["account_id"]),
            "reason": body.reason,
            "created_at": store.now.isoformat(),
        }
        store.state["flags"][flag["id"]] = flag
        return 201, copy.deepcopy(flag)

    return _write(store, request, idempotency_key, apply)


@risk_router.get("/customers/{user_id}/flags")
def list_customer_flags(user_id: str, store: StoreDep) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        _get(store, "customers", user_id, "customer")
        return {"user_id": user_id, "flags": [f for f in store.state["flags"].values() if f["user_id"] == user_id]}

    return _read(store, read)


# ---- payments -----------------------------------------------------------------------------------------------------

payments_router = APIRouter(prefix="/payments/v1", tags=["payments"])


class TransferCreate(StrictModel):
    from_account: str = Field(min_length=1, max_length=64)
    to_account: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0)


@payments_router.post("/transfers", status_code=201)
def create_transfer(
    body: TransferCreate, request: Request, store: StoreDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    def apply() -> Response:
        account = _get(store, "accounts", body.from_account, "account")
        if account["status"] == "closed":
            raise BankError(409, "ACCOUNT_CLOSED", f"account {body.from_account} is closed")
        amount = round(body.amount, 2)
        if amount > account["balance"]:
            raise BankError(
                422, "INSUFFICIENT_FUNDS", f"balance {account['balance']} is less than transfer amount {amount}"
            )
        account["balance"] = round(account["balance"] - amount, 2)
        transfer = {
            "id": store.next_id("transfer", "trf"),
            "from_account": body.from_account,
            "to_account": body.to_account,
            "amount": amount,
            "currency": account["currency"],
            "status": "completed",
            "created_at": store.now.isoformat(),
        }
        store.state["transfers"][transfer["id"]] = transfer
        return 201, copy.deepcopy(transfer)

    return _write(store, request, idempotency_key, apply)


# ---- health + admin (never faulted) -------------------------------------------------------------------------------

system_router = APIRouter(tags=["system"])


@system_router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    return {"ok": True, "services": list(request.app.state.services)}


@system_router.post("/_admin/reset")
def admin_reset(
    store: StoreDep,
    fault_rate: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    latency_ms: Annotated[int, Query(ge=0, le=60_000)] = 0,
    seed: int = 0,
    fault_mode: FaultMode = "random",
) -> dict[str, Any]:
    store.reset(fault_rate=fault_rate, latency_ms=latency_ms, seed=seed, fault_mode=fault_mode)
    return {"ok": True, "fault_rate": fault_rate, "latency_ms": latency_ms, "seed": seed, "fault_mode": fault_mode}


@system_router.get("/_admin/snapshot")
def admin_snapshot(store: StoreDep) -> dict[str, Any]:
    return store.snapshot()


# ---- fault injection ----------------------------------------------------------------------------------------------


def _is_fault_exempt(path: str) -> bool:
    return path == "/healthz" or path.startswith("/_admin/")


class FaultInjectionMiddleware:
    """Adds optional latency, then injects 503s: "before" skips the handler, "after" runs it and drops the response.

    Both fault kinds return the same body, so a caller cannot tell whether the change was applied.
    """

    def __init__(self, app: ASGIApp, store: BankStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_fault_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        if self.store.latency_ms:
            await asyncio.sleep(self.store.latency_ms / 1000)
        fault = self.store.roll_fault()
        if fault is None:
            await self.app(scope, receive, send)
            return
        if fault == "after":

            async def discard(_: Message) -> None:
                return None

            await self.app(scope, receive, discard)
        response = _error(503, "UNAVAILABLE", "service temporarily unavailable; retry with the same Idempotency-Key")
        await response(scope, receive, send)


# ---- app factory --------------------------------------------------------------------------------------------------

ROUTERS: dict[str, APIRouter] = {
    "accounts": accounts_router,
    "cards": cards_router,
    "disputes": disputes_router,
    "risk": risk_router,
    "payments": payments_router,
}


def create_app(store: BankStore = STORE, services: Iterable[str] | None = None) -> FastAPI:
    """Build an app serving the requested services (default: all five) backed by ``store``."""
    requested = set(SERVICES if services is None else services)
    unknown = requested - set(SERVICES)
    if unknown:
        raise ValueError(f"unknown service(s): {sorted(unknown)}; expected any of {SERVICES}")
    if not requested:
        raise ValueError("at least one service is required")
    selected = tuple(name for name in SERVICES if name in requested)

    app = FastAPI(title="Simulated Bank", version="1.0.0")
    app.state.store = store
    app.state.services = selected
    for name in selected:
        app.include_router(ROUTERS[name])
    app.include_router(system_router)
    app.add_exception_handler(BankError, _bank_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_middleware(FaultInjectionMiddleware, store=store)
    return app


def services_from_env(value: str | None) -> tuple[str, ...] | None:
    """Parse the SERVICE env var: unset, empty or "all" -> all services; otherwise a name (or comma-separated names)."""
    if value is None or value.strip().lower() in ("", "all"):
        return None
    return tuple(name.strip() for name in value.split(",") if name.strip())


app = create_app(STORE, services_from_env(os.environ.get("SERVICE")))
