"""CLI entry point: ``python -m orchestrator`` (interactive) or ``python -m orchestrator -q "..."``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import ExitStack
from pathlib import Path

from .agent_client import SupportAgentClient
from .auth import make_credential
from .config import ConfigError, Settings
from .pipeline import SupportPipeline
from .safety import ContentSafetyGate
from .telemetry import TracingHandle, TracingMode, configure_from_settings

TRACE_CHOICES = [mode.value for mode in TracingMode]


def setup_tracing(settings: Settings, credential, stack: ExitStack, mode_override: str | None) -> TracingHandle:
    """Configure tracing (before any client exists) and flush it on exit."""
    handle = configure_from_settings(settings, credential, mode_override=mode_override)
    stack.callback(handle.shutdown)
    return handle


def build_pipeline(
    settings: Settings,
    credential,
    stack: ExitStack,
    *,
    skip_input_gate: bool = False,
    tracing: TracingHandle | None = None,
) -> tuple[SupportPipeline, SupportAgentClient]:
    """Construct the gate, agent client, and pipeline; every resource is registered on ``stack`` for cleanup."""
    settings.require("foundry_project_endpoint", "agent_name", "content_safety_endpoint")
    gate = ContentSafetyGate(
        settings.content_safety_endpoint,  # type: ignore[arg-type]
        credential,
        harm_severity_threshold=settings.harm_severity_threshold,
        fail_closed=settings.safety_fail_closed,
        max_input_chars=settings.max_input_chars,
        api_key=settings.content_safety_api_key,
    )
    stack.callback(gate.close)
    agent = SupportAgentClient(
        settings.foundry_project_endpoint,  # type: ignore[arg-type]
        settings.agent_name,
        credential,
        resolve_agent_id=bool(tracing and tracing.enabled),
    )
    stack.callback(agent.close)
    return SupportPipeline(gate, agent, skip_input_gate=skip_input_gate), agent


def _print_result(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.as_dict(), indent=2))
        return
    print(f"\nAgent> {result.reply}")
    for number, citation in enumerate(result.citations, start=1):
        print(f"  [{number}] {citation['title']} - {citation['url']}")
    meta = [f"stage={result.stage}"]
    if result.blocked and result.input_verdict and not result.input_verdict.allowed:
        meta.append(f"blocked_by={result.input_verdict.category.value}")
    if result.tool_calls:
        meta.append("tools=" + ",".join(result.tool_calls))
    if result.trace_id:
        meta.append(f"trace={result.trace_id}")
    print("  [" + " ".join(meta) + "]\n")


def _read_documents(paths: list[Path]) -> list[str]:
    documents = []
    for path in paths:
        if not path.is_file():
            raise ConfigError(f"Attached document not found: {path}")
        documents.append(path.read_text(encoding="utf-8"))
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contoso Customer Support & Safety Agent")
    parser.add_argument("-q", "--query", help="Ask a single question and exit.")
    parser.add_argument(
        "--doc",
        type=Path,
        action="append",
        default=[],
        help="Attach a text file to the --query turn (repeatable). Checked by Prompt Shields "
        "for indirect attacks before the agent sees it.",
    )
    parser.add_argument("--json", action="store_true", help="Print full pipeline results as JSON.")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging for this app's own modules.",
    )
    parser.add_argument(
        "--trace-http",
        action="store_true",
        help="Also log Azure SDK / HTTP / credential details (very noisy).",
    )
    parser.add_argument(
        "--trace",
        choices=TRACE_CHOICES,
        default=None,
        help="OpenTelemetry tracing: console (span tree on stderr) or azure_monitor (Application Insights / "
        "Foundry Tracing). Overrides TRACING_MODE.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.trace_http else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        logging.getLogger("contoso").setLevel(logging.DEBUG)
    if args.doc and not args.query:
        parser.error("--doc requires --query")

    with ExitStack() as stack:
        try:
            documents = _read_documents(args.doc)
            settings = Settings.from_env()
            credential = make_credential()
            stack.callback(credential.close)
            tracing = setup_tracing(settings, credential, stack, args.trace)
            pipeline, agent = build_pipeline(settings, credential, stack, tracing=tracing)
        except ConfigError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
        except (ValueError, OSError) as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2

        try:
            conversation_id = agent.start_conversation()
            stack.callback(agent.end_conversation, conversation_id)
            if args.query:
                _print_result(pipeline.handle(conversation_id, args.query, documents), args.json)
                return 0

            print("Contoso Support Agent — type 'exit' to quit.\n")
            while True:
                try:
                    user_text = input("You> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if user_text.lower() in {"exit", "quit"}:
                    break
                if user_text:
                    _print_result(pipeline.handle(conversation_id, user_text), args.json)
            return 0
        except Exception as exc:  # top-level guard so the CLI never dumps a raw traceback
            logging.getLogger("contoso").debug("Fatal error", exc_info=True)
            print(f"Error: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
