"""CLI entry point: ``python -m orchestrator`` (interactive) or ``python -m orchestrator -q "..."``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import ExitStack
from pathlib import Path

from azure.identity import DefaultAzureCredential

from .agent_client import SupportAgentClient
from .config import ConfigError, Settings
from .pipeline import SupportPipeline
from .safety import ContentSafetyGate


def build_pipeline(settings: Settings, credential, stack: ExitStack) -> tuple[SupportPipeline, SupportAgentClient]:
    """Construct the gate, agent client, and pipeline; every resource is registered on ``stack`` for cleanup."""
    settings.require("foundry_project_endpoint", "agent_name", "content_safety_endpoint")
    gate = ContentSafetyGate(
        settings.content_safety_endpoint,  # type: ignore[arg-type]
        credential,
        harm_severity_threshold=settings.harm_severity_threshold,
        fail_closed=settings.safety_fail_closed,
        max_input_chars=settings.max_input_chars,
    )
    stack.callback(gate.close)
    agent = SupportAgentClient(settings.foundry_project_endpoint, settings.agent_name, credential)  # type: ignore[arg-type]
    stack.callback(agent.close)
    return SupportPipeline(gate, agent), agent


def _print_result(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.as_dict(), indent=2))
        return
    print(f"\nAgent> {result.reply}")
    if result.citations:
        print("  Sources: " + "; ".join(c["title"] for c in result.citations))
    meta = [f"stage={result.stage}"]
    if result.blocked and result.input_verdict and not result.input_verdict.allowed:
        meta.append(f"blocked_by={result.input_verdict.category.value}")
    if result.tool_calls:
        meta.append("tools=" + ",".join(result.tool_calls))
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
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.doc and not args.query:
        parser.error("--doc requires --query")

    with ExitStack() as stack:
        try:
            documents = _read_documents(args.doc)
            settings = Settings.from_env()
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            stack.callback(credential.close)
            pipeline, agent = build_pipeline(settings, credential, stack)
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
