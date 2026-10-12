"""Agent HTTP API: a simulated auth gateway in front of the LangGraph banking agent.

Identity comes ONLY from a verified HS256 JWT (``sub`` = user_id). Sessions are in-memory with an idle TTL (Phase 10
adds persistence). Metrics (agent/telemetry.py) and the JSON audit log (api/audit.py) are driven from the same trace
events the graph emits, so they never disagree. The agent's decisions, prompts and guardrails are unchanged.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent import telemetry
from agent.graph import BankingAgent
from api.audit import AuditLog, audit_trace_event

DEFAULT_SESSION_TTL_S = 30 * 60
DEFAULT_MAX_SESSIONS = 1000
DEFAULT_RATE_LIMIT = 30  # requests per window per user
DEFAULT_RATE_WINDOW_S = 60.0
MAX_MESSAGE_CHARS = 2000


class ApiError(StarletteHTTPException):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


# ---- request models -----------------------------------------------------------------------------------------------


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_ids: list[str] | None = None
    approve: bool | None = None

    def to_answer(self) -> Any:
        if self.approve is not None and self.approved_ids is None:
            return {"approve": self.approve}
        if self.approved_ids is not None and self.approve is None:
            return {"approved_ids": self.approved_ids}
        raise ApiError(422, "VALIDATION_ERROR", "provide exactly one of approve or approved_ids")


# ---- sessions and rate limiting -----------------------------------------------------------------------------------


@dataclass
class Session:
    session_id: str
    user_id: str
    created_at: float
    last_seen: float
    turns: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class TokenBucket:
    """Per-user fixed-window counter (simple in-memory rate limit)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: str, now: float | None = None) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(user_id, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[user_id] = hits
                return False
            hits.append(now)
            self._hits[user_id] = hits
            return True


class SessionStore:
    def __init__(self, ttl_s: float, max_sessions: int) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > self.ttl_s]
        for sid in expired:
            del self._sessions[sid]
            telemetry.sessions_expired.inc()
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions, key=lambda sid: self._sessions[sid].last_seen)
            del self._sessions[oldest]
            telemetry.sessions_expired.inc()
        telemetry.active_sessions.set(len(self._sessions))

    def create(self, user_id: str) -> Session:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            session = Session(uuid.uuid4().hex, user_id, now, now)
            self._sessions[session.session_id] = session
            telemetry.active_sessions.set(len(self._sessions))
            return session

    def get(self, session_id: str, user_id: str) -> Session:
        """Return the session, or 404 if it does not exist OR is owned by another user (same response)."""
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            session = self._sessions.get(session_id)
            if session is None or session.user_id != user_id:
                raise ApiError(404, "NOT_FOUND", "session not found")
            session.last_seen = now
            return session


# ---- app ----------------------------------------------------------------------------------------------------------


@dataclass
class ApiConfig:
    auth_secret: str
    session_ttl_s: float = DEFAULT_SESSION_TTL_S
    max_sessions: int = DEFAULT_MAX_SESSIONS
    rate_limit: int = DEFAULT_RATE_LIMIT
    rate_window_s: float = DEFAULT_RATE_WINDOW_S

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ApiConfig:
        """Build from env. Refuses to start without AUTH_SECRET unless DEV_MODE is set (then a fixed dev secret)."""
        secret = env.get("AUTH_SECRET")
        if not secret:
            if env.get("DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
                secret = "dev-mode-insecure-secret-dev-mode-insecure"
            else:
                raise RuntimeError("AUTH_SECRET is required to start the API (or set DEV_MODE=1 for a dev secret)")
        return cls(
            auth_secret=secret,
            session_ttl_s=float(env.get("SESSION_TTL_S", DEFAULT_SESSION_TTL_S)),
            max_sessions=int(env.get("MAX_SESSIONS", DEFAULT_MAX_SESSIONS)),
            rate_limit=int(env.get("RATE_LIMIT", DEFAULT_RATE_LIMIT)),
            rate_window_s=float(env.get("RATE_WINDOW_S", DEFAULT_RATE_WINDOW_S)),
        )


def verify_token(secret: str, authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "UNAUTHORIZED", "missing bearer token")
    token = authorization[7:]
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(401, "TOKEN_EXPIRED", "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "INVALID_TOKEN", "invalid token") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ApiError(401, "INVALID_TOKEN", "token missing a valid sub")
    return sub


def create_api(
    agent: BankingAgent,
    config: ApiConfig,
    audit: AuditLog | None = None,
    readiness: Callable[[], dict[str, bool]] | None = None,
) -> FastAPI:
    app = FastAPI(title="Banking Support Agent API", version="1.0.0")
    app.state.agent = agent
    app.state.config = config
    app.state.audit = audit or AuditLog()
    app.state.sessions = SessionStore(config.session_ttl_s, config.max_sessions)
    app.state.rate = TokenBucket(config.rate_limit, config.rate_window_s)
    app.state.readiness = readiness or (lambda: {})

    def current_user(authorization: str | None = Header(default=None)) -> str:
        return verify_token(config.auth_secret, authorization)

    UserDep = Depends(current_user)

    @app.middleware("http")
    async def metrics_mw(request: Request, call_next: Callable[[Request], Any]) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)  # route template, never the concrete path
        telemetry.http_requests.labels(route=template, method=request.method, status=str(response.status_code)).inc()
        telemetry.http_request_seconds.observe(time.perf_counter() - start)
        return response

    @app.exception_handler(ApiError)
    @app.exception_handler(StarletteHTTPException)
    async def on_api_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    def _rate_check(user_id: str) -> None:
        if not app.state.rate.allow(user_id):
            telemetry.rate_limited.inc()
            raise ApiError(429, "RATE_LIMITED", "rate limit exceeded; slow down")

    def _request_id(request: Request) -> str:
        return request.headers.get("X-Request-Id") or uuid.uuid4().hex

    def _drain_trace(session: Session, request_id: str, seen: int) -> list[dict[str, Any]]:
        """Push new trace events (from index `seen`) to metrics and the audit log; return them."""
        state = app.state.agent.state(session.session_id, session.user_id)
        trace = state.get("trace", [])
        for event in trace[seen:]:
            telemetry.record_trace_event(event)
            audit_trace_event(app.state.audit, request_id, session.session_id, session.user_id, event)
        return trace

    def _turn_result(session: Session, result: Any, request_id: str) -> dict[str, Any]:
        degraded = any(
            e["type"] == "llm" and e.get("error")
            for e in app.state.agent.state(session.session_id, session.user_id).get("trace", [])
        )
        telemetry.turn_latency.observe(result.latency_s)
        if result.confirmation is not None:
            app.state.audit.emit(
                "reply", request_id, session.session_id, session.user_id, status="awaiting_confirmation"
            )
            return {
                "status": "awaiting_confirmation",
                "confirmation": result.confirmation,
                "latency_s": round(result.latency_s, 4),
                "degraded": degraded,
            }
        app.state.audit.emit(
            "reply",
            request_id,
            session.session_id,
            session.user_id,
            status="complete",
            reply_length=len(result.reply or ""),
        )
        return {
            "status": "complete",
            "reply": result.reply,
            "latency_s": round(result.latency_s, 4),
            "degraded": degraded,
        }

    @app.post("/v1/sessions")
    def create_session(request: Request, user_id: str = UserDep) -> dict[str, Any]:
        _rate_check(user_id)
        session = app.state.sessions.create(user_id)
        app.state.audit.emit("session_start", _request_id(request), session.session_id, user_id)
        return {"session_id": session.session_id}

    @app.post("/v1/sessions/{session_id}/messages")
    def post_message(session_id: str, body: MessageIn, request: Request, user_id: str = UserDep) -> dict[str, Any]:
        _rate_check(user_id)
        session = app.state.sessions.get(session_id, user_id)
        request_id = _request_id(request)
        if not session.lock.acquire(blocking=False):
            raise ApiError(409, "SESSION_BUSY", "another request is in progress for this session")
        try:
            if app.state.agent.pending(session_id, user_id) is not None:
                raise ApiError(409, "CONFIRMATION_PENDING", "a confirmation is pending; confirm or decline it first")
            seen = len(app.state.agent.state(session_id, user_id).get("trace", []))
            app.state.audit.emit("user_message", request_id, session_id, user_id, length=len(body.text))
            result = app.state.agent.send(session_id, user_id, body.text)
            session.turns += 1
            _drain_trace(session, request_id, seen)
            return _turn_result(session, result, request_id)
        finally:
            session.lock.release()

    @app.post("/v1/sessions/{session_id}/confirm")
    def post_confirm(session_id: str, body: ConfirmIn, request: Request, user_id: str = UserDep) -> dict[str, Any]:
        _rate_check(user_id)
        session = app.state.sessions.get(session_id, user_id)
        request_id = _request_id(request)
        if not session.lock.acquire(blocking=False):
            raise ApiError(409, "SESSION_BUSY", "another request is in progress for this session")
        try:
            if app.state.agent.pending(session_id, user_id) is None:
                raise ApiError(409, "NOTHING_PENDING", "no confirmation is pending")
            seen = len(app.state.agent.state(session_id, user_id).get("trace", []))
            result = app.state.agent.resume(session_id, user_id, body.to_answer())
            _drain_trace(session, request_id, seen)
            return _turn_result(session, result, request_id)
        finally:
            session.lock.release()

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str, user_id: str = UserDep) -> dict[str, Any]:
        session = app.state.sessions.get(session_id, user_id)
        pending = app.state.agent.pending(session_id, user_id)
        return {"session_id": session_id, "turns": session.turns, "confirmation": pending}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> Response:
        checks = app.state.readiness()
        ready = all(checks.values()) if checks else True
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready, "checks": checks})

    @app.get("/metrics")
    def metrics() -> Response:
        payload, content_type = telemetry.render_metrics()
        return Response(content=payload, media_type=content_type)

    return app
