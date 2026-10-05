"""Score the agent's answers for groundedness, relevance and fabricated facts (AI-103: "Evaluate models and apps").

Reads the replies captured by ``run_exam_checks --report`` (so the agent is NOT called again) and scores every
answered case with two kinds of evaluator:

1. **Deterministic fabrication check** (always on, no model needed): every concrete fact token in the reply —
   currency amounts, percentages, policy IDs, order/customer IDs, tracking numbers — must appear in the grounding
   context (policy handbook and/or the order record the tool returned) or in the user's own question.
2. **AI-assisted judges** from ``azure-ai-evaluation`` (skipped with ``--offline``):
   ``GroundednessEvaluator`` (is the answer supported by the context?) and ``RelevanceEvaluator``
   (does it address the question?), each on a 1-5 scale with a pass threshold.

Usage (from the repo root)::

    python -m scripts.run_exam_checks --report exam_report.json    # capture replies first
    python -m scripts.evaluate_quality --offline                    # fabrication check only, no Azure calls
    python -m scripts.evaluate_quality                              # + groundedness & relevance judges
    python -m scripts.evaluate_quality --only R1,C1 --output eval.json

The judges need ``pip install -r requirements-eval.txt``, a chat deployment (``EVAL_MODEL_DEPLOYMENT_NAME``,
defaulting to ``FOUNDRY_MODEL_DEPLOYMENT_NAME``) and **Cognitive Services OpenAI User** on the Foundry resource.
Exit code: 0 when every scored case passes, 1 otherwise, 2 on configuration/input errors.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from orchestrator.config import POLICY_PATH, REPO_ROOT, ConfigError, Settings

logger = logging.getLogger("contoso.evaluate")

CASES_PATH = REPO_ROOT / "tests" / "exam_cases.json"
DEFAULT_REPORT = REPO_ROOT / "exam_report.json"
DEFAULT_OUTPUT = REPO_ROOT / "evaluation_report.json"
JUDGE_API_VERSION = "2024-10-21"

# Concrete, checkable facts. Anything matching these in a reply must be traceable to the context or the question.
FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "currency": re.compile(r"(?:[$€£]|AUD\s?)\d+(?:,\d{3})*(?:\.\d{1,2})?"),
    "percent": re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%"),
    "policy_id": re.compile(r"\bPOL-[A-Z]{3}-\d{3}\b"),
    "order_id": re.compile(r"\bCON-\d{6}\b"),
    "customer_id": re.compile(r"\bCUST-\d{5}\b"),
    "tracking": re.compile(r"\b[A-Z]{2}[0-9][A-Z0-9]{7,}\b"),
}
_ORDER_TOOL_MARKERS = ("openapi", "contoso_order_status", "getorderstatus")
_SEARCH_TOOL_MARKERS = ("azure_ai_search", "search")
INFORMATIONAL_RELEVANCE = {"refuse": "declining", "clarify": "asking for valid input"}
_REASONING_MODEL_HINTS = ("chat-latest", "gpt-5", "o1", "o3", "o4")


@dataclass
class EvalRow:
    case_id: str
    category: str
    query: str
    response: str
    context: str
    context_sources: list[str]
    # "answer" (default), "clarify" (ask for missing/invalid input) or "refuse" (decline is correct).
    expected_behaviour: str = "answer"


@dataclass
class EvalResult:
    case_id: str
    category: str
    passed: bool
    facts_checked: int = 0
    unsupported_facts: list[str] = field(default_factory=list)
    groundedness: Optional[float] = None
    groundedness_reason: str = ""
    relevance: Optional[float] = None
    relevance_reason: str = ""
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Building the evaluation rows
# --------------------------------------------------------------------------- #
def _normalise(text: str) -> str:
    return text.replace("’", "'").replace(" ", " ")


def _uses_tool(tool_calls: list[str], markers: tuple[str, ...]) -> bool:
    return any(marker in call.lower() for call in tool_calls for marker in markers)


def order_context(customer_ids: list[str]) -> str:
    """The order records the Function would have returned for these IDs (same deterministic mock store)."""
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import function_app  # the Function App module; its STORE is pure Python
    except ImportError:
        logger.warning("Could not import src/function_app.py; order facts cannot be verified.")
        return ""
    records = []
    for customer_id in dict.fromkeys(customer_ids):  # de-duplicate, keep order
        orders = function_app.STORE.get_orders(customer_id)
        if not orders:
            records.append({"found": False, "customerId": customer_id, "error": "OrderNotFound"})
        records.extend(function_app._serialize(order) for order in orders)
    return json.dumps(records, indent=2) if records else ""


def build_rows(report: list[dict[str, Any]], cases: list[dict[str, Any]], policy_text: str) -> list[EvalRow]:
    """One row per answered (not blocked) case, with the context the agent's tools could have seen."""
    turns_by_id = {case["id"]: case["turns"] for case in cases}
    behaviour_by_id = {case["id"]: case.get("expected_behaviour", "answer") for case in cases}
    rows: list[EvalRow] = []
    for outcome in report:
        if outcome.get("stage") != "completed" or not outcome.get("reply"):
            continue  # blocked or failed turns have nothing to score
        query = "\n".join(turns_by_id.get(outcome["case_id"], []))
        tools = list(outcome.get("tool_calls", []))
        parts: list[str] = []
        sources: list[str] = []
        if _uses_tool(tools, _ORDER_TOOL_MARKERS):
            records = order_context(FACT_PATTERNS["customer_id"].findall(query))
            if records:
                parts.append("Order status tool results:\n" + records)
                sources.append("order_api")
        if _uses_tool(tools, _SEARCH_TOOL_MARKERS):
            parts.append("Contoso Customer Policy Handbook:\n" + policy_text)
            sources.append("policy_handbook")
        rows.append(
            EvalRow(
                case_id=outcome["case_id"],
                category=outcome.get("category", ""),
                query=query,
                response=outcome["reply"],
                context="\n\n".join(parts),
                context_sources=sources,
                expected_behaviour=behaviour_by_id.get(outcome["case_id"], "answer"),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Deterministic fabrication check
# --------------------------------------------------------------------------- #
def extract_facts(text: str) -> list[str]:
    found: list[str] = []
    for pattern in FACT_PATTERNS.values():
        found.extend(match.strip() for match in pattern.findall(_normalise(text)))
    return list(dict.fromkeys(found))


def _canonical(fact: str) -> str:
    return re.sub(r"[\s,]", "", fact).lower()


def unsupported_facts(response: str, context: str, query: str) -> list[str]:
    """Facts stated in the response that appear neither in the grounding context nor in the question."""
    haystack = _canonical(_normalise(context + "\n" + query))
    return [fact for fact in extract_facts(response) if _canonical(fact) not in haystack]


# --------------------------------------------------------------------------- #
# AI-assisted judges (azure-ai-evaluation)
# --------------------------------------------------------------------------- #
Judge = Callable[..., dict[str, Any]]


def build_judges(settings: Settings) -> tuple[Judge, Judge]:
    """Create the Groundedness and Relevance evaluators (keyless, Entra ID)."""
    try:
        from azure.ai.evaluation import (
            AzureOpenAIModelConfiguration,
            GroundednessEvaluator,
            RelevanceEvaluator,
        )
    except ImportError as exc:
        raise ConfigError(
            "azure-ai-evaluation is not installed. Run 'pip install -r requirements-eval.txt' or use --offline."
        ) from exc

    from orchestrator.auth import make_credential

    deployment = settings.eval_model_deployment_name or settings.model_deployment_name
    model_config = AzureOpenAIModelConfiguration(
        azure_endpoint=settings.required_str("azure_openai_endpoint"),
        azure_deployment=deployment,
        api_version=JUDGE_API_VERSION,
    )
    reasoning = settings.eval_is_reasoning_model
    if reasoning is None:
        reasoning = any(hint in deployment.lower() for hint in _REASONING_MODEL_HINTS)
    credential = make_credential()
    logger.info("Judge model: %s (reasoning=%s)", deployment, reasoning)
    groundedness = GroundednessEvaluator(model_config, credential=credential, is_reasoning_model=reasoning)
    relevance = RelevanceEvaluator(model_config, credential=credential, is_reasoning_model=reasoning)
    return groundedness, relevance


def _score(result: dict[str, Any], key: str) -> tuple[Optional[float], str]:
    value = result.get(key, result.get(f"{key}_score"))
    reason = str(result.get(f"{key}_reason", "") or "")
    return (float(value) if isinstance(value, (int, float)) else None), reason


def evaluate_row(row: EvalRow, judges: Optional[tuple[Judge, Judge]], threshold: float) -> EvalResult:
    result = EvalResult(row.case_id, row.category, passed=True)
    if row.context:
        result.facts_checked = len(extract_facts(row.response))
        result.unsupported_facts = unsupported_facts(row.response, row.context, row.query)
        if result.unsupported_facts:
            result.passed = False
            result.notes.append("possible fabrication: facts not found in context or question")
    else:
        result.notes.append("no tool context (refusal / clarification); groundedness not applicable")

    if judges is None:
        return result
    groundedness, relevance = judges
    try:
        if row.context:
            result.groundedness, result.groundedness_reason = _score(
                groundedness(query=row.query, response=row.response, context=row.context),
                "groundedness",
            )
        result.relevance, result.relevance_reason = _score(
            relevance(query=row.query, response=row.response), "relevance"
        )
    except Exception as exc:  # one failed judge call must not abort the whole run
        result.passed = False
        result.notes.append(f"judge error: {type(exc).__name__}: {exc}")
        return result
    _apply_thresholds(row, result, threshold)
    return result


def _apply_thresholds(row: EvalRow, result: EvalResult, threshold: float) -> None:
    """Gate on the judge scores. Relevance is informational when the correct reply is *not* an answer.

    RelevanceEvaluator rewards answering the question, so a deliberate decline (out-of-scope request, prompt
    extraction attempt) or a clarifying question (missing/invalid customer ID) scores low by design, and the
    score swings between runs. Gating on it would reward the agent for guessing or complying.
    """
    if result.groundedness is not None and result.groundedness < threshold:
        result.passed = False
        result.notes.append(f"groundedness {result.groundedness:g} < threshold {threshold:g}")
    if result.relevance is None or result.relevance >= threshold:
        return
    if row.expected_behaviour in INFORMATIONAL_RELEVANCE:
        why = INFORMATIONAL_RELEVANCE[row.expected_behaviour]
        result.notes.append(f"relevance {result.relevance:g} is informational: {why} is the expected behaviour")
    else:
        result.passed = False
        result.notes.append(f"relevance {result.relevance:g} < threshold {threshold:g}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score exam replies for groundedness, relevance and fabrication.")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="JSON from run_exam_checks --report.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=CASES_PATH,
        help="Exam cases (for the original questions).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Where to write the evaluation JSON.",
    )
    parser.add_argument("--only", default="", help="Comma-separated case IDs to score (e.g. R1,C1).")
    parser.add_argument(
        "--threshold",
        type=float,
        default=3.0,
        help="Pass mark for the 1-5 judge scores.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Only the deterministic fabrication check.",
    )
    return parser.parse_args(argv)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"{path} not found. Run 'python -m scripts.run_exam_checks --report {path.name}' first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _print_result(result: EvalResult) -> None:
    def fmt(score: Optional[float]) -> str:
        return "  - " if score is None else f"{score:>4g}"

    mark = "PASS" if result.passed else "FAIL"
    facts = f"facts {result.facts_checked - len(result.unsupported_facts)}/{result.facts_checked} supported"
    judged = (
        ""
        if result.groundedness is None and result.relevance is None
        else (f"  grounded={fmt(result.groundedness)} relevant={fmt(result.relevance)}")
    )
    print(f"[{mark}] {result.case_id:<3} {result.category:<9} {facts}{judged}")
    for fact in result.unsupported_facts:
        print(f"         - unsupported fact: {fact}")
    for note in result.notes:
        if not note.startswith("possible fabrication"):
            print(f"         - {note}")


def _prepare(
    args: argparse.Namespace,
) -> tuple[list[EvalRow], Optional[tuple[Judge, Judge]]]:
    """Load report + cases, build rows, and create judges unless offline. Raises ConfigError."""
    report = _load_json(args.report)
    cases = _load_json(args.cases)["cases"]
    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    if only:
        report = [o for o in report if o["case_id"] in only]
    rows = build_rows(report, cases, POLICY_PATH.read_text(encoding="utf-8"))
    judges = None if args.offline else build_judges(Settings.from_env())
    return rows, judges


def _summarise(results: list[EvalResult], args: argparse.Namespace) -> int:
    for result in results:
        _print_result(result)
    passed = sum(r.passed for r in results)
    mode = "fabrication check only" if args.offline else f"judges + fabrication check, threshold {args.threshold:g}"
    print(f"\n{passed}/{len(results)} answered cases passed ({mode}).")
    args.output.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"Evaluation written to {args.output}")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
    for noisy in ("azure", "httpx", "httpx2", "openai", "promptflow"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        rows, judges = _prepare(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("No answered cases to score (blocked cases are skipped).", file=sys.stderr)
        return 2
    return _summarise([evaluate_row(row, judges, args.threshold) for row in rows], args)


if __name__ == "__main__":
    sys.exit(main())
