"""HTTP client for the simulated bank microservices, with retries and idempotent writes."""

from __future__ import annotations

import contextlib
import contextvars
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx

SERVICES: tuple[str, ...] = ("accounts", "cards", "disputes", "risk", "payments")

# Per-request retry tally. ``retry_count`` on the client is a lifetime total (shared, used by tests); this
# contextvar isolates the retries made while handling one request even when several requests share one client
# (Phase 7 fix of the Phase 3 cross-session accounting limitation). It is None unless a retry_scope is active.
_request_retries: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "bank_request_retries", default=None
)


@contextlib.contextmanager
def retry_scope() -> Iterator[Callable[[], int]]:
    """Count retries made inside this scope only (contextvar-isolated across threads/async tasks)."""
    box = [0]
    token = _request_retries.set(box)
    try:
        yield lambda: box[0]
    finally:
        _request_retries.reset(token)


class ServiceError(Exception):
    """A bank service call failed. ``code`` comes from the service's ``{"detail": {"code", "message"}}`` body."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class BankClient:
    """Routes calls to the right service, retries 5xx/transport errors, and makes writes idempotent.

    Each write call gets one Idempotency-Key that is reused on every retry, so a write whose response was lost
    (an "after" fault) is applied exactly once.
    """

    def __init__(
        self,
        http: httpx.Client,
        service_urls: dict[str, str] | None = None,
        retries: int = 3,
        backoff_s: float = 0.2,
    ) -> None:
        unknown = set(service_urls or {}) - set(SERVICES)
        if unknown:
            raise ValueError(f"unknown service(s) in service_urls: {sorted(unknown)}")
        if retries < 0 or backoff_s < 0:
            raise ValueError("retries and backoff_s must be >= 0")
        self.http = http
        self.service_urls = {name: url.rstrip("/") for name, url in (service_urls or {}).items()}
        self.retries = retries
        self.backoff_s = backoff_s
        self.retry_count = 0

    def get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body, idempotency_key=str(uuid.uuid4()))

    def patch(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("PATCH", path, json=body, idempotency_key=str(uuid.uuid4()))

    def url_for(self, path: str) -> str:
        """Absolute URL when the path's service has a configured base URL, else the path (httpx base_url applies)."""
        service = path.split("/", 2)[1] if path.startswith("/") else ""
        if service not in SERVICES:
            raise ValueError(f"cannot route path {path!r}: expected a /<service>/... prefix, one of {SERVICES}")
        base = self.service_urls.get(service)
        return f"{base}{path}" if base else path

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        url = self.url_for(path)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        last_error = "no attempt made"
        for attempt in range(self.retries + 1):
            if attempt:
                self.retry_count += 1
                box = _request_retries.get()
                if box is not None:
                    box[0] += 1
                if self.backoff_s:
                    time.sleep(self.backoff_s * 2 ** (attempt - 1))
            try:
                response = self.http.request(method, url, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise _service_error(response)
            return response.json()
        raise ServiceError(
            503, "UNAVAILABLE", f"{method} {path} failed after {self.retries + 1} attempts ({last_error})"
        )


def _service_error(response: httpx.Response) -> ServiceError:
    try:
        detail = response.json()["detail"]
        return ServiceError(response.status_code, str(detail["code"]), str(detail["message"]))
    except (ValueError, KeyError, TypeError):
        return ServiceError(response.status_code, f"HTTP_{response.status_code}", response.text[:200])
