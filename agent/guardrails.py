"""Layered guardrail engine that runs on every proposed tool call.

L1 allow-list -> L2 strict validation -> L3 ownership -> (reads: allow) -> L4 business policy -> L5 action budget
-> confirm. L6 (the human confirmation itself) is enforced by the agent graph; this engine only returns CONFIRM.
The engine never raises: every outcome, including internal errors, is a GuardResult (failing closed).
Block messages are written for the model and never contain another customer's data.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from agent.client import ServiceError
from agent.tools import AGENT_ALLOWLIST, REGISTRY, Ctx, ToolSpec
from bank_services.data import DISPUTE_WINDOW_DAYS

MAX_REPORTED_ERRORS = 4

# Argument name -> (resource label, lookup path, owner field in the lookup response).
OWNED_RESOURCES: dict[str, tuple[str, str, str]] = {
    "card_id": ("card", "/cards/v1/cards/{}", "user_id"),
    "transaction_id": ("transaction", "/accounts/v1/transactions/{}", "owner_user_id"),
    "dispute_id": ("dispute", "/disputes/v1/disputes/{}", "user_id"),
}


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True)
class GuardResult:
    decision: Decision
    tool: str
    args: Any = None  # validated + normalized (JSON-safe) when guards are on; raw when guards are off
    layer: str | None = None  # set on blocks: "L1".."L5"
    code: str | None = None  # set on blocks
    message: str = ""  # model-facing explanation for blocks
    summary: str | None = None  # human-readable action description for CONFIRM
    latency_ms: float = 0.0


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    max_writes_per_session: int = 6


class _Blocked(Exception):
    def __init__(self, layer: str | None, code: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer
        self.code = code
        self.message = message


class GuardrailEngine:
    def __init__(self, config: GuardConfig | None = None) -> None:
        self.config = config or GuardConfig()

    def check(self, name: Any, raw_args: Any, ctx: Ctx, writes_so_far: int = 0) -> GuardResult:
        started = time.perf_counter()
        tool = "<invalid>"
        try:
            tool = _tool_label(name)
            if self.config.enabled:
                result = self._check(name, tool, raw_args, ctx, writes_so_far)
            else:
                result = _check_disabled(name, tool, raw_args)
        except _Blocked as blocked:
            result = GuardResult(Decision.BLOCK, tool, layer=blocked.layer, code=blocked.code, message=blocked.message)
        except Exception:  # fail closed on anything unexpected
            result = GuardResult(
                Decision.BLOCK,
                tool,
                code="GUARD_ERROR",
                message="The action could not be checked and was not performed. Do not retry it.",
            )
        return dataclasses.replace(result, latency_ms=(time.perf_counter() - started) * 1000)

    def _check(self, name: Any, tool: str, raw_args: Any, ctx: Ctx, writes_so_far: int) -> GuardResult:
        # L1: allow-list (also catches hallucinated tools and admin tools)
        if not isinstance(name, str) or name not in AGENT_ALLOWLIST:
            raise _Blocked("L1", "NOT_ALLOWLISTED", f"Tool '{tool}' is not available. Use only the tools provided.")
        spec = REGISTRY[name]

        # L2: strict validation
        args = _validate(spec, raw_args)
        normalized = args.model_dump(mode="json")

        # L3: every referenced card / transaction / dispute must belong to the session customer
        resources = _authorize(args, ctx)
        if spec.kind == "read":
            return GuardResult(Decision.ALLOW, tool, args=normalized)

        # L4: business policy, checked before the customer is ever asked
        _check_policy(spec, args, resources, ctx)

        # L5: per-session action budget
        if writes_so_far >= self.config.max_writes_per_session:
            raise _Blocked(
                "L5",
                "ACTION_BUDGET_EXCEEDED",
                f"This session already made {writes_so_far} changes (limit {self.config.max_writes_per_session}). "
                "Do not attempt more changes; tell the customer to contact support for anything else.",
            )
        return GuardResult(Decision.CONFIRM, tool, args=normalized, summary=_summary(spec, args, resources))


def _check_disabled(name: Any, tool: str, raw_args: Any) -> GuardResult:
    """Ablation mode: no checks at all. Only a tool that does not exist is refused."""
    if isinstance(name, str) and name in REGISTRY:
        return GuardResult(Decision.ALLOW, tool, args=raw_args)
    raise _Blocked(None, "UNKNOWN_TOOL", f"Tool '{tool}' does not exist.")


# ---- L2 -----------------------------------------------------------------------------------------------------------


def _validate(spec: ToolSpec, raw_args: Any) -> BaseModel:
    if not isinstance(raw_args, Mapping):
        raise _Blocked("L2", "INVALID_ARGS", f"Arguments for {spec.name} must be a JSON object.")
    try:
        return spec.args_model.model_validate(dict(raw_args))
    except ValidationError as exc:
        errors = exc.errors()
        problems = "; ".join(_format_error(err) for err in errors[:MAX_REPORTED_ERRORS])
        if len(errors) > MAX_REPORTED_ERRORS:
            problems += f"; and {len(errors) - MAX_REPORTED_ERRORS} more"
        raise _Blocked(
            "L2", "INVALID_ARGS", f"Invalid arguments for {spec.name}: {problems}. Fix the arguments and try again."
        ) from None


def _format_error(err: Mapping[str, Any]) -> str:
    location = ".".join(_clip(str(part), 40) for part in err.get("loc", ())) or "arguments"
    return f"{location}: {_clip(str(err.get('msg', 'invalid value')), 160)}"


# ---- L3 -----------------------------------------------------------------------------------------------------------


def _not_found_message(label: str) -> str:
    # Identical for NOT_OWNER and NOT_FOUND so errors never reveal whether another customer's id exists.
    return f"No such {label} for this customer. Look IDs up with a list tool instead of guessing."


def _authorize(args: BaseModel, ctx: Ctx) -> dict[str, dict[str, Any]]:
    """Look up every referenced resource and require that the session customer owns it. Fails closed."""
    found: dict[str, dict[str, Any]] = {}
    for arg_name, (label, path, owner_field) in OWNED_RESOURCES.items():
        value = getattr(args, arg_name, None)
        if value is None:
            continue
        try:
            resource = ctx.client.get(path.format(value))
        except ServiceError as exc:
            if exc.status == 404:
                raise _Blocked("L3", "NOT_FOUND", _not_found_message(label)) from None
            raise _lookup_failed("L3", label) from None
        except Exception:
            raise _lookup_failed("L3", label) from None
        if not isinstance(resource, dict) or resource.get(owner_field) != ctx.user_id:
            raise _Blocked("L3", "NOT_OWNER", _not_found_message(label))
        found[arg_name] = resource
    return found


def _lookup_failed(layer: str, what: str) -> _Blocked:
    return _Blocked(
        layer,
        "LOOKUP_FAILED",
        f"Could not verify the {what} because a bank service is unavailable. The action was not performed; "
        "tell the customer to try again shortly.",
    )


# ---- L4 -----------------------------------------------------------------------------------------------------------


def _check_policy(spec: ToolSpec, args: Any, resources: dict[str, dict[str, Any]], ctx: Ctx) -> None:
    if spec.name in ("freeze_card", "unfreeze_card"):
        card = resources["card_id"]
        ending = f"Card ending {card['last4']}"
        if card["status"] == "reported_stolen":
            raise _Blocked(
                "L4",
                "CARD_REPORTED_STOLEN",
                f"{ending} was reported stolen and is permanently blocked; it cannot be frozen or unfrozen. "
                "Point the customer to their replacement card (see list_cards).",
            )
        if spec.name == "freeze_card" and card["status"] == "frozen":
            raise _Blocked("L4", "CARD_ALREADY_FROZEN", f"{ending} is already frozen. No action is needed.")
        if spec.name == "unfreeze_card" and card["status"] != "frozen":
            raise _Blocked(
                "L4", "CARD_NOT_FROZEN", f"{ending} is not frozen (status: {card['status']}). No action is needed."
            )

    elif spec.name == "create_dispute":
        txn = resources["transaction_id"]
        if txn["age_days"] > DISPUTE_WINDOW_DAYS:
            raise _Blocked(
                "L4",
                "DISPUTE_WINDOW_EXPIRED",
                f"This charge is {txn['age_days']} days old; disputes must be opened within {DISPUTE_WINDOW_DAYS} "
                "days. Explain this to the customer instead of retrying.",
            )
        charge = Decimal(str(txn["amount"]))
        if args.amount is not None and args.amount > charge:
            raise _Blocked(
                "L4",
                "AMOUNT_EXCEEDS_TRANSACTION",
                f"The dispute amount {_usd(args.amount)} is more than the {_usd(charge)} charge. "
                "Use an amount up to the charge, or omit amount to dispute the full charge.",
            )
        disputes = _policy_lookup(ctx, f"/disputes/v1/customers/{ctx.user_id}/disputes", "disputes")
        for dispute in disputes:
            if dispute["transaction_id"] == txn["id"] and dispute["status"] != "closed":
                raise _Blocked(
                    "L4",
                    "DUPLICATE_DISPUTE",
                    f"Dispute {dispute['id']} is already open for this charge (status: {dispute['status']}). "
                    "Use get_dispute_status instead of opening another dispute.",
                )

    elif spec.name == "flag_transaction":
        txn = resources["transaction_id"]
        flags = _policy_lookup(ctx, f"/risk/v1/customers/{ctx.user_id}/flags", "flags")
        if any(flag["transaction_id"] == txn["id"] for flag in flags):
            raise _Blocked(
                "L4", "ALREADY_FLAGGED", "This transaction is already flagged as suspicious. No action is needed."
            )


def _policy_lookup(ctx: Ctx, path: str, key: str) -> list[dict[str, Any]]:
    try:
        return list(ctx.client.get(path)[key])
    except Exception:
        raise _lookup_failed("L4", f"existing {key}") from None


# ---- confirmation summary -----------------------------------------------------------------------------------------


def _summary(spec: ToolSpec, args: Any, resources: dict[str, dict[str, Any]]) -> str:
    if spec.name in ("freeze_card", "unfreeze_card"):
        card = resources["card_id"]
        card_text = f"your {card['type']} card ending {card['last4']}"
        if spec.name == "freeze_card":
            return f"Freeze {card_text} (reason: {args.reason})"
        return f"Unfreeze {card_text}"
    txn = resources["transaction_id"]
    charge = Decimal(str(txn["amount"]))
    charge_text = f"{_usd(charge)} charge from {_clip(txn['merchant'], 60)} on {txn['posted_at'][:10]}"
    if spec.name == "create_dispute":
        amount = args.amount if args.amount is not None else charge
        return f"Dispute {_usd(amount)} of the {charge_text} (reason: {args.reason})"
    if spec.name == "flag_transaction":
        return f"Flag the {charge_text} as suspicious"
    raise ValueError(f"no summary for {spec.name}")


# ---- helpers ------------------------------------------------------------------------------------------------------


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _clip(text: str, limit: int) -> str:
    clipped = "".join(ch if ch.isprintable() else "?" for ch in text[:limit])
    return clipped + "..." if len(text) > limit else clipped


def _tool_label(name: Any) -> str:
    return _clip(name, 64) if isinstance(name, str) else f"<{type(name).__name__}>"
