"""Run the AI-103 verification suite (``tests/exam_cases.json``) against the live deployment.

Every case goes through the full pipeline (Content Safety -> Foundry agent -> output check)
in a fresh server-side conversation. Results are scored against the expectations in the
JSON file and an optional JSON report is written for your study notes.

Usage (from the repo root)::

    python -m scripts.run_exam_checks                      # all cases
    python -m scripts.run_exam_checks --category SAFETY    # one category
    python -m scripts.run_exam_checks --only R1,T1,S3      # specific cases
    python -m scripts.run_exam_checks --report results.json

Exit code is 0 when every selected case passes, 1 otherwise, 2 on configuration errors.

Expectation keys (all optional):
    blocked             true  -> must be blocked by a real safety decision (input safety, model
                                 content filter, or output moderation). A safety-service OUTAGE
                                 (fail-closed) never counts as a pass.
                        false -> must NOT be blocked
    blocked_by          list of acceptable blocking reasons, matched against the input verdict
                        category (prompt_injection, document_injection, harmful_content, ...) or
                        the stage (model_content_filter, output_safety)
    blocked_or_refused  true  -> pass if legitimately blocked, OR if answered and all text checks pass
    must_contain_all    every string must appear in the reply
    must_contain_any    at least one string must appear in the reply
    must_not_contain    none of these strings may appear in the reply
                        (all text checks are case-insensitive and normalise curly apostrophes)
    tool_used / tool_used_2   tool family that must appear in the tool-call trace
                              ("search" -> Azure AI Search tool, "openapi" -> order API tool)
    tool_not_used       tool family that must NOT appear in the tool-call trace
    grounding_evidence  true -> reply has a url_citation OR cites a policy ID (POL-xxx-nnn)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).resolve().parent.parent / "tests" / "exam_cases.json"
POLICY_ID_RE = re.compile(r"POL-[A-Z]{3}-\d{3}")


@dataclass
class CaseOutcome:
    case_id: str
    category: str
    objective: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    reply: str = ""
    stage: str = ""
    tool_calls: list[str] = field(default_factory=list)
    citations: int = 0


# Tool-call trace entries look like "<item_type>[:<name>]"; match a family by any alias.
TOOL_ALIASES = {
    "search": ("azure_ai_search", "search"),
    "openapi": ("openapi", "contoso_order_status", "getorderstatus"),
}


def _normalise(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").lower()


def _tool_seen(family: str, tools: list[str]) -> bool:
    aliases = TOOL_ALIASES.get(family.lower(), (family.lower(),))
    return any(alias in tool for tool in tools for alias in aliases)


def _block_reason(result: dict[str, Any]) -> str:
    verdict = result.get("input_verdict") or {}
    if result.get("stage") == "input_safety" and verdict.get("category"):
        return str(verdict["category"])
    output_verdict = result.get("output_verdict") or {}
    if result.get("stage") == "output_safety" and output_verdict.get("category") == "safety_service_error":
        return "safety_service_error"
    return str(result.get("stage", ""))


def evaluate(expect: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Return a list of failure reasons (empty list == pass). Pure function for unit tests."""
    failures: list[str] = []
    reply = _normalise(result.get("reply", ""))
    tools = [t.lower() for t in result.get("tool_calls", [])]
    reason = _block_reason(result) if result.get("blocked") else ""
    outage = reason == "safety_service_error"
    blocked = bool(result.get("blocked")) and not outage  # an outage is not a safety decision

    if outage:
        failures.append("Content Safety was unavailable (fail-closed block) - not a valid safety result")
        return failures

    if "blocked" in expect:
        if expect["blocked"] and not blocked:
            failures.append("expected the request to be BLOCKED but it was answered")
        if not expect["blocked"] and blocked:
            failures.append(f"expected an answer but the request was blocked ({reason})")
        if expect["blocked"]:
            allowed = expect.get("blocked_by")
            if blocked and allowed and reason not in allowed:
                failures.append(f"blocked for {reason!r}, expected one of {allowed!r}")
            return failures  # text/tool checks are meaningless for a blocked turn

    if expect.get("blocked_or_refused") and blocked:
        return failures

    if result.get("stage") == "agent":
        failures.append("agent invocation failed (stage=agent); check logs")

    for needle in expect.get("must_contain_all", []):
        if _normalise(needle) not in reply:
            failures.append(f"reply is missing required text {needle!r}")
    any_list = expect.get("must_contain_any", [])
    if any_list and not any(_normalise(needle) in reply for needle in any_list):
        failures.append(f"reply contains none of {any_list!r}")
    for needle in expect.get("must_not_contain", []):
        if _normalise(needle) in reply:
            failures.append(f"reply contains forbidden text {needle!r}")

    for key in ("tool_used", "tool_used_2"):
        wanted = expect.get(key)
        if wanted and not _tool_seen(wanted, tools):
            failures.append(f"expected a '{wanted}' tool call; saw {result.get('tool_calls', [])}")
    unwanted = expect.get("tool_not_used")
    if unwanted and _tool_seen(unwanted, tools):
        failures.append(f"'{unwanted}' tool should NOT have been called; saw {result.get('tool_calls', [])}")

    if expect.get("grounding_evidence"):
        if not result.get("citations") and not POLICY_ID_RE.search(result.get("reply", "")):
            failures.append("no grounding evidence (no url_citation and no POL-xxx-nnn policy ID)")
    return failures


def _run_case(case: dict[str, Any], pipeline, agent) -> CaseOutcome:
    """Run one case; never raises, so a single failure cannot abort the suite."""
    try:
        return _run_case_unguarded(case, pipeline, agent)
    except Exception as exc:  # record and continue
        return CaseOutcome(
            case["id"],
            case["category"],
            case["objective"],
            False,
            [f"unexpected error: {type(exc).__name__}: {exc}"],
        )


def _run_case_unguarded(case: dict[str, Any], pipeline, agent) -> CaseOutcome:
    from orchestrator.agent_client import AgentInvocationError

    try:
        conversation_id = agent.start_conversation()
    except AgentInvocationError as exc:
        return CaseOutcome(
            case["id"],
            case["category"],
            case["objective"],
            False,
            [f"could not start conversation: {exc}"],
        )
    try:
        turns = case["turns"]
        tool_calls: list[str] = []
        result: dict[str, Any] = {}
        for index, turn in enumerate(turns):
            documents = case.get("documents", []) if index == len(turns) - 1 else []
            result = pipeline.handle(conversation_id, turn, documents).as_dict()
            tool_calls.extend(result.get("tool_calls", []))
            if result["blocked"]:
                break
        result["tool_calls"] = tool_calls  # tools across all turns of the case
        failures = evaluate(case["expect"], result)
    finally:
        agent.end_conversation(conversation_id)
    return CaseOutcome(
        case["id"],
        case["category"],
        case["objective"],
        not failures,
        failures,
        result.get("reply", ""),
        result.get("stage", ""),
        tool_calls,
        len(result.get("citations", [])),
    )


def load_cases(path: Path, only: set[str], category: str | None) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data["cases"]
    if only:
        cases = [c for c in cases if c["id"] in only]
    if category:
        cases = [c for c in cases if c["category"].upper() == category.upper()]
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AI-103 verification cases against the live agent.")
    parser.add_argument("--only", default="", help="Comma-separated case IDs (e.g. R1,T1,S3).")
    parser.add_argument("--category", default=None, help="RAG | TOOL | COMBINED | FALLBACK | SAFETY")
    parser.add_argument("--report", type=Path, default=None, help="Write a JSON report to this path.")
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="Path to the cases JSON file.")
    args = parser.parse_args(argv)

    from contextlib import ExitStack

    from azure.identity import DefaultAzureCredential

    from orchestrator.__main__ import build_pipeline
    from orchestrator.config import ConfigError, Settings

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    cases = load_cases(args.cases, only, args.category)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    outcomes: list[CaseOutcome] = []
    with ExitStack() as stack:
        try:
            settings = Settings.from_env()
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            stack.callback(credential.close)
            pipeline, agent = build_pipeline(settings, credential, stack)
        except (ConfigError, ValueError) as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2

        for case in cases:
            outcome = _run_case(case, pipeline, agent)
            outcomes.append(outcome)
            mark = "PASS" if outcome.passed else "FAIL"
            print(f"[{mark}] {case['id']:<3} {case['category']:<9} {case['objective']}")
            for failure in outcome.failures:
                print(f"         - {failure}")

    passed = sum(o.passed for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} cases passed.")
    if args.report:
        args.report.write_text(json.dumps([o.__dict__ for o in outcomes], indent=2), encoding="utf-8")
        print(f"Report written to {args.report}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
