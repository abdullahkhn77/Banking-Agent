# Phase 2 — Implementation Summary

**Status:** complete · **Commit:** "Phase 2: bank client, tool registry, guardrail engine" · **Date:** 2026-10-10

Phase 2 builds everything between a model's proposed tool call and the bank:
- an HTTP client for the five services
- the agent's tool registry, with OpenAI function schemas
- the layered guardrail engine (L1–L5)

There is no LangGraph, LLM or prompt code. L6 (human confirmation) is enforced by the graph in phase 3. Here the
engine only returns a `confirm` decision.

## 1. Modules

| File | Lines | Responsibility |
|---|---|---|
| `agent/client.py` | 104 | `BankClient`, `ServiceError`: routing, retries, idempotent writes |
| `agent/tools.py` | 432 | Argument models, `Ctx`, `ToolSpec`, 14 executors, `REGISTRY`, `AGENT_ALLOWLIST`, `openai_tools()` |
| `agent/guardrails.py` | 286 | `Decision`, `GuardResult`, `GuardConfig`, `GuardrailEngine` |

## 2. Bank client (`agent/client.py`)

- **Routing:** `BankClient(http, service_urls=None, retries=3, backoff_s=0.2)` sends each call to its service based on
  the path's first segment (`/accounts`, `/cards`, `/disputes`, `/risk`, `/payments`).
  - If that service has a base URL configured, the request goes there.
  - Otherwise it uses the httpx client's `base_url`, which is how FastAPI `TestClient` works.
  - A path that can't be routed, or an unknown service name in `service_urls`, raises `ValueError`.
- **4xx:** raises `ServiceError(status, code, message)` immediately, parsed from `{"detail": {"code", "message"}}`.
  It is never retried. A body in an unexpected format becomes code `HTTP_<status>`.
- **5xx and transport errors:** retried up to `retries` times (so 4 attempts by default), sleeping
  `backoff_s * 2**i` between attempts: 0.2 s, 0.4 s, 0.8 s. After the last attempt it raises
  `ServiceError(503, "UNAVAILABLE", …)`. `retry_count` counts every retry the client has made.
- **Idempotency:** each `post`/`patch` call creates **one** `Idempotency-Key` (a UUID4) and sends the same key on
  every retry. A write whose response was lost (an "after" fault) is therefore applied exactly once. `get` sends no
  key and drops `None` query parameters.

## 3. Tool registry (`agent/tools.py`)

**Identity.** `Ctx(user_id, client)` carries the customer's id from the authenticated session.
`Ctx.primary_account()` looks up and caches the customer's account. No tool, admin tools included, has a
user or customer argument, and a test enforces this.

**Argument models**
- The base model uses pydantic v2 with `extra="forbid"` and `str_strip_whitespace=True`.
- **IDs:** `card_id ^card_[a-z0-9_]{3,40}$` · `transaction_id ^txn_[a-z0-9_]{3,40}$` · `dispute_id ^dsp_[0-9]{3,10}$`.
  Pydantic checks these with the Rust regex engine, where `$` really means end of string, so a trailing `\n` is
  rejected.
- **Amounts:** `Decimal` with `gt=0`, `le=100000`, `decimal_places=2`.
  - The check is exact: `15.999`, `0.1+0.2`, NaN, infinity and booleans are all rejected, while `"10.50"` and
    `Decimal("1.230")` are accepted.
  - The model sees amounts as a plain `number` in the schema. In the validated arguments the engine stores, they
    are floats.
- **Text fields** (`details` up to 300 characters, flag `reason` 3–200) reject control characters (Unicode category
  Cc).

**Tools**

| Kind | Tools |
|---|---|
| read (7) | `get_accounts` · `list_cards` · `list_transactions(days 1–180=30, merchant_contains?, card_id?, limit 1–50=25)` · `get_transaction` · `analyze_spending(days 7–180=90)` · `list_disputes` · `get_dispute_status` |
| write (4) | `freeze_card(card_id, reason=temporary)` · `unfreeze_card` · `create_dispute(transaction_id, reason, amount?, details?)` · `flag_transaction(transaction_id, reason)` |
| admin (3, never allow-listed) | `transfer_funds(to_account, amount, from_account?)` · `close_account(account_id?)` · `update_contact_email(email)`. Their argument models are deliberately loose (unknown fields are ignored) |

**Outputs.** Executors return short dicts scoped to `ctx.user_id`:
- transactions: `{transaction_id, date "YYYY-MM-DD HH:MM", amount, merchant, country, card_id, memo_untrusted}`,
  where `memo_untrusted` is the description cut to 240 characters
- cards: `{card_id, type, last4, status}`

**Exports**
- `REGISTRY`, `READ_TOOLS`, `WRITE_TOOLS`, `ADMIN_TOOLS`, and `AGENT_ALLOWLIST` (read + write)
- `openai_tools(allowlist)`: the OpenAI schemas for the allow-listed tools only, in registry order
- `ToolSpec.openai_schema()`: removes pydantic's `title` keys, but keeps a property that is itself named `title`
- `ToolSpec.invoke(ctx, args)`: checks the arguments against the tool's own model, then runs the tool
- `ToolSpec.key_arg`: names the resource each write acts on

## 4. Guardrail engine (`agent/guardrails.py`)

`check(name, raw_args, ctx, writes_so_far) -> GuardResult` with fields `decision`, `tool`, `args`, `layer`, `code`,
`message`, `summary` and `latency_ms`. The layers run in this order:

1. **L1 allow-list:** any name not in `AGENT_ALLOWLIST` → `NOT_ALLOWLISTED`. This covers admin tools, made-up tools
   and names that aren't strings.
2. **L2 validation:** the arguments must be a dict and must pass the tool's model, otherwise `INVALID_ARGS`. The
   message lists up to 4 field errors, plus "and N more". Field names in the message are cut to 40 characters and
   unprintable characters are replaced.
3. **L3 ownership:** every `card_id`, `transaction_id` or `dispute_id` in the arguments is looked up, including the
   `card_id` filter on `list_transactions`. It must belong to `ctx.user_id`.
   - Codes: `NOT_OWNER` and `NOT_FOUND` stay distinct for metrics, but the model gets the **same message** for both.
   - Any other lookup failure gives `LOOKUP_FAILED` (fail closed).
   - Reads that pass this layer → `allow`.
4. **L4 policy (writes only):** `CARD_REPORTED_STOLEN`, `CARD_ALREADY_FROZEN`, `CARD_NOT_FROZEN`,
   `DISPUTE_WINDOW_EXPIRED`, `AMOUNT_EXCEEDS_TRANSACTION`, `DUPLICATE_DISPUTE` and `ALREADY_FLAGGED`.
   - Transaction age comes from the service's `age_days`. The window constant is imported from `bank_services.data`.
   - Duplicate and already-flagged checks list the customer's existing disputes and flags. If that lookup fails →
     `LOOKUP_FAILED`.
5. **L5 budget:** `writes_so_far >= max_writes_per_session` (default 6) → `ACTION_BUDGET_EXCEEDED`.
6. **Otherwise → `confirm`**, with a summary built from the looked-up data. For example:
   - "Freeze your debit card ending 4417 (reason: lost)"
   - "Dispute $15.99 of the $15.99 charge from StreamFlix on 2026-09-25 (reason: duplicate)"
   - "Flag the $2,450.00 charge from Elektro-Mag Bucuresti on 2026-09-29 as suspicious"

**Stored arguments.** `args` holds the validated arguments from proposal time, in JSON-safe form: whitespace stripped,
defaults filled in, amounts as floats. A test checks that `REGISTRY[tool].invoke(ctx, result.args)` performs exactly
the action that was confirmed.

**Ablation mode** (`GuardConfig(enabled=False)`) runs no checks at all. Any tool in `REGISTRY` is allowed with its raw
arguments, including admin tools, cross-customer calls and writes, with no confirmation. Only a name not in `REGISTRY`
→ `UNKNOWN_TOOL`.

**Never raises.** Any unexpected exception becomes a block with `GUARD_ERROR`. Latency is measured with
`perf_counter` and stored on every result.

**Block messages** are written for the model and never contain another customer's data. For example:
- "No such card for this customer. Look IDs up with a list tool instead of guessing."
- "Card ending 7790 was reported stolen and is permanently blocked; it cannot be frozen or unfrozen. Point the
  customer to their replacement card (see list_cards)."

The full code table is in `CLAUDE.md`.

## 5. Design decisions

| Decision | Why |
|---|---|
| L2 runs before any backend call | IDs are checked against their regex before they're put into a URL, so path traversal never reaches a service. A test confirms that L1/L2 blocks make zero HTTP requests |
| Same message for NOT_OWNER and NOT_FOUND | The model can't use error messages to find out whether another customer's id exists. The codes stay separate so metrics can tell them apart |
| Policy uses service data, not the agent's clock | The window check uses the service's `age_days`, which is based on the fixed `NOW`. The agent never computes dates |
| Fail closed (`LOOKUP_FAILED`, `GUARD_ERROR`) | When ownership or policy can't be verified, the action doesn't happen |
| `Decimal` for amounts, floats in the stored arguments | The 2-decimal check is exact, and the stored arguments stay JSON-safe for the phase 3 graph state and audit log |
| `ToolSpec.invoke` always uses the tool's own model | That model is the tool's type contract, not part of the guardrail engine. Whether the phase 5 ablation should skip it is still open |
| Latency summary excludes disabled-mode checks and separates lookup paths | Disabled checks don't do any work, and L1/L2 blocks never call the backend. Mixing them in would make the numbers look misleadingly fast |

## 6. Verification

Everything below was run locally on 2026-10-10.

| Check | Result |
|---|---|
| `make lint` | `All checks passed!` · `28 files already formatted` |
| `make test` | **241 passed** in 1.08 s |
| Tests per file | client 16 · guardrails 89 · tools 32 · risk 29 · seed 25 · services 43 · smoke 7 |
| Ownership matrix (from the test run) | 63 calls: 8 own active cards → `confirm`; 55 other customers' cards → `NOT_OWNER` |
| Fuzz test (from the test run) | 200 seeded random inputs each against the enabled and disabled engine. None raised and none produced `GUARD_ERROR` |
| Mutation check (L3 owner comparison disabled) | First run: **9 tests failed**. It exposed one weak test (the "leaks nothing" test passed trivially because nothing was blocked), which I strengthened. Re-run: **10 failed**. Code restored; 241 pass |

**Guard check latency** (from the `make test` run: in-process `TestClient`, guards enabled, includes the HTTP lookups):

| Group | n | median | p95 |
|---|---|---|---|
| all enabled checks | 373 | 0.008 ms | 2.670 ms |
| allow | 22 | 0.011 ms | 2.650 ms |
| confirm | 26 | 2.368 ms | 4.244 ms |
| block at L1/L2 (no lookups) | 234 | 0.004 ms | 0.017 ms |
| block at L3–L5 (with lookups) | 91 | 0.403 ms | 3.172 ms |

These are test diagnostics, not production metrics:
- The overall median is mostly fuzz-generated L1/L2 blocks.
- Checks that call the backend take roughly 0.4–4 ms in-process.

Real latency over the network is measured in phase 7.

**What the tests cover**
- **Client:**
  - prefix routing with per-service URLs, also through `TestClient`
  - rejection of unroutable paths, and dropping of `None` query parameters
  - reads send no key; each write sends a new key, reused on its retries
  - 4xx not retried (both from a mock and from the real services)
  - 5xx and transport errors retried, then `UNAVAILABLE`; backoff doubles 0.2 → 0.4 → 0.8 s
  - an "after" or "before" fault followed by a retry applies a dispute exactly once
  - two "after" faults on a transfer still debit only once
- **Tools:**
  - allow-list contents, and no admin tool on it
  - every allow-listed schema validates against `openai.types.chat.ChatCompletionFunctionToolParam`, has no `title`
    keys, and has `additionalProperties: false`
  - no user or customer field in any tool
  - for all 6 customers, the read tools return only that customer's data
  - `memo_untrusted` is truncated to 240 characters
  - write and admin tools change the backend
- **Guardrails:**
  - L1: 4 disallowed names, plus names that aren't strings
  - L2: 23 attack or malformed cases, the 4-error limit, and no backend calls on L1/L2 blocks
  - L3: other customers' cards, transactions and disputes (including the `list_transactions` card filter);
    nonexistent ids; identical messages; no leaks; outage → `LOOKUP_FAILED`
  - L4: every policy code using the phase 1 planted cases; a failed policy lookup → `LOOKUP_FAILED`; L4 runs before L5
  - L5: 5 writes → confirm, 6 → block; the limit is configurable; reads don't count
  - happy paths for all 7 reads and all 4 writes, with the exact summary text
  - stored arguments are normalized and executable; checks have no side effects (snapshot unchanged)
  - ablation mode; the ownership matrix; fuzzing

**Not verified yet:** CI on GitHub. There's no remote configured.

## 7. Known limitations and follow-ups

1. **Only the 3 ID arguments get ownership checks.** These are the only kinds of resource the agent can reference.
   If a tool ever takes an account id, add it to `OWNED_RESOURCES`.
2. **Duplicate-dispute and already-flagged checks happen at proposal time.** The services enforce both again when the
   action runs, so a race between proposal and confirmation is still caught (it comes back as a 409 `ServiceError`).
3. **Phase 5 still has to decide one thing:** whether the ablation should run raw arguments without the tool's own
   model check (`ToolSpec.invoke`). For now, an invalid ID is rejected at execution even when guards are off.
4. **Docker is still broken locally** (see `docs/PHASE0_SUMMARY.md` §6). **No GitHub remote yet**, so CI hasn't run.

## 8. Definition of done (from `docs/PLAN.md`)

- [x] `BankClient` with prefix routing, 4xx never retried, 5xx retried, one Idempotency-Key per write reused on retries
- [x] Allow-list excludes payments and admin ops (test)
- [x] Identity comes from the session, never a tool argument (test)
- [x] L1–L5, each with unit tests for both allow and block paths
- [x] Guard result records which layer blocked and why
- [x] Model-facing block message (phase 3 wraps it in a ToolMessage)
- [x] Validated arguments captured at proposal time
- [x] Engine never raises (fuzz); fails closed; ablation mode available
- [x] Mutation check on L3 confirmed the tests catch a broken ownership check

**Next:** Phase 3: the LangGraph agent (agent → guard → confirm via `interrupt()` → execute), with retries, a step
limit and PAN redaction.
