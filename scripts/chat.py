"""Interactive chat with the banking agent, for manual testing and demos.

    python -m scripts.chat --user u_1001 [--show-trace] [--record llm_requests.jsonl]

The model comes from LLM_BASE_URL / LLM_MODEL (e.g. Ollama: http://localhost:11434/v1, qwen2.5:7b). The bank runs
in-process unless BANK_BASE_URL (or <NAME>_SERVICE_URL) points at running services.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

import httpx

from agent.client import BankClient
from agent.config import Settings
from agent.graph import BankingAgent, TurnResult
from agent.guardrails import GuardConfig
from agent.llm import OpenAICompatLLM


def build_client(settings: Settings) -> BankClient:
    if settings.bank_base_url or settings.service_urls:
        http: httpx.Client = httpx.Client(base_url=settings.bank_base_url or "http://localhost:9000", timeout=10.0)
    else:
        from fastapi.testclient import TestClient

        from bank_services.app import create_app
        from bank_services.store import BankStore

        http = TestClient(create_app(BankStore()))
    return BankClient(http, settings.service_urls, retries=settings.http_retries, backoff_s=settings.http_backoff_s)


def format_event(event: dict[str, Any]) -> str:
    kind = event["type"]
    if kind == "tool_call":
        outcome = event["decision"] + (f" {event['code']} ({event['layer']})" if event["code"] else "")
        return f"  [guard] {event['name']} -> {outcome}"
    if kind == "confirmation":
        return f"  [confirm] approved {len(event['approved'])}/{len(event['requested'])}"
    if kind == "execution":
        status = "ok" if event["ok"] else f"error {event.get('error')}"
        return f"  [exec] {event['name']} -> {status} (retries {event['retries']}, {event['latency_s']:.2f}s)"
    if kind == "llm":
        error = f" ERROR {event['error']}" if "error" in event else ""
        return (
            f"  [llm] {event['latency_s']:.2f}s, tokens in/out {event['input_tokens']}/{event['output_tokens']}, "
            f"tool calls {event['n_tool_calls']}{error}"
        )
    return f"  [{kind}] {event}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="u_1001", help="customer to sign in as (u_1001 .. u_1006)")
    parser.add_argument("--show-trace", action="store_true", help="print guard decisions and other trace events")
    parser.add_argument("--record", default=None, help="append every LLM request to this JSONL file")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    agent = BankingAgent(
        OpenAICompatLLM.from_settings(settings, record_path=args.record),
        build_client(settings),
        GuardConfig(settings.guardrails_enabled, settings.max_writes_per_session),
        settings.max_agent_steps,
    )
    thread_id = f"chat-{uuid.uuid4().hex[:8]}"
    guards = "on" if settings.guardrails_enabled else "OFF (ablation)"
    print(f"model {settings.llm_model} @ {settings.llm_base_url} | user {args.user} | guardrails {guards}")
    print("Type 'quit' to exit.\n")

    seen = 0

    def show_trace() -> None:
        nonlocal seen
        trace = agent.state(thread_id, args.user).get("trace", [])
        if args.show_trace:
            for event in trace[seen:]:
                print(format_event(event))
        seen = len(trace)

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text.lower() in {"quit", "exit"}:
            return 0
        result: TurnResult = agent.send(thread_id, args.user, text)
        show_trace()
        while result.confirmation:
            approved = []
            for action in result.confirmation["actions"]:
                answer = input(f"  confirm: {action['summary']}? [y/N] ").strip().lower()
                if answer in {"y", "yes"}:
                    approved.append(action["action_id"])
            result = agent.resume(thread_id, args.user, {"approved_ids": approved})
            show_trace()
        print(f"bot> {result.reply}\n")


if __name__ == "__main__":
    raise SystemExit(main())
