"""Offline tests for scripts/evaluate_quality.py (no Azure, no LLM judge)."""

import json

import pytest

from orchestrator.config import POLICY_PATH
from scripts.evaluate_quality import (
    CASES_PATH,
    EvalRow,
    build_rows,
    evaluate_row,
    extract_facts,
    order_context,
    unsupported_facts,
)

POLICY = POLICY_PATH.read_text(encoding="utf-8")
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _outcome(case_id, reply, tools, stage="completed", category="RAG"):
    return {
        "case_id": case_id,
        "category": category,
        "reply": reply,
        "stage": stage,
        "tool_calls": tools,
    }


def test_extract_facts_finds_checkable_tokens():
    text = "Pay $19.99 or €75, AUD 24.95, a 15% fee (POL-SHP-002); order CON-500101 for CUST-10001, CX1Z99A0001."
    assert extract_facts(text) == [
        "$19.99",
        "€75",
        "AUD 24.95",
        "15%",
        "POL-SHP-002",
        "CON-500101",
        "CUST-10001",
        "CX1Z99A0001",
    ]


def test_unsupported_facts_flags_invented_values_only():
    reply = "Standard shipping is $19.99 (POL-SHP-002), but express is $24.99 under POL-SHP-099."
    assert unsupported_facts(reply, POLICY, "Is shipping free to Alaska for $120?") == [
        "$24.99",
        "POL-SHP-099",
    ]


def test_values_from_the_question_are_not_fabrications():
    assert unsupported_facts("A $120 order still pays $19.99.", POLICY, "My order is $120") == []


def test_order_context_mirrors_the_function_store():
    records = json.loads(order_context(["CUST-10001", "CUST-99999"]))
    assert records[0]["trackingNumber"] == "CX1Z99A0001"
    assert {
        "found": False,
        "customerId": "CUST-99999",
        "error": "OrderNotFound",
    } in records


def test_build_rows_picks_context_from_the_tools_used():
    report = [
        _outcome("R4", "No, $19.99 (POL-SHP-002).", ["azure_ai_search_call"]),
        _outcome(
            "T1",
            "Shipped, CX1Z99A0001.",
            ["openapi_call:contoso_order_status_getOrderStatus"],
            category="TOOL",
        ),
        _outcome("T2", "Please share your Customer ID.", [], category="TOOL"),
        _outcome("S1", "blocked", [], stage="input_safety", category="SAFETY"),
    ]
    rows = {row.case_id: row for row in build_rows(report, CASES, POLICY)}
    assert set(rows) == {"R4", "T1", "T2"}  # blocked S1 is skipped
    assert rows["R4"].context_sources == ["policy_handbook"]
    assert rows["T1"].context_sources == ["order_api"] and "CX1Z99A0001" in rows["T1"].context
    assert rows["T2"].context == "" and "Where is my order?" in rows["T2"].query


def test_evaluate_row_offline_detects_fabrication():
    row = EvalRow(
        "R4",
        "RAG",
        "Is shipping free to Alaska?",
        "It costs $9.99 (POL-SHP-002).",
        POLICY,
        ["p"],
    )
    result = evaluate_row(row, None, 3)
    assert not result.passed and result.unsupported_facts == ["$9.99"] and result.facts_checked == 2


def _judge(key, score):
    def call(**kwargs):
        return {key: score, f"{key}_reason": f"{key} reason"}

    return call


@pytest.mark.parametrize("grounded,relevant,ok", [(5, 4, True), (2, 5, False), (5, 1, False)])
def test_evaluate_row_applies_judge_threshold(grounded, relevant, ok):
    row = EvalRow("R1", "RAG", "Return window?", "30 days (POL-RET-001).", POLICY, ["p"])
    judges = (_judge("groundedness", grounded), _judge("relevance", relevant))
    result = evaluate_row(row, judges, 3)
    assert result.passed is ok
    assert result.groundedness == grounded and result.relevance == relevant
    assert result.groundedness_reason == "groundedness reason"


def test_judge_errors_fail_the_row_without_crashing():
    def broken(**kwargs):
        raise RuntimeError("judge offline")

    row = EvalRow("R1", "RAG", "q", "30 days (POL-RET-001).", POLICY, ["p"])
    result = evaluate_row(row, (broken, broken), 3)
    assert not result.passed and any("judge error" in note for note in result.notes)


def test_rows_without_context_skip_groundedness():
    calls = []

    def grounded(**kwargs):
        calls.append(kwargs)
        return {"groundedness": 5}

    row = EvalRow("T2", "TOOL", "Where is my order?", "Please share your Customer ID.", "", [])
    result = evaluate_row(row, (grounded, _judge("relevance", 5)), 3)
    assert result.passed and calls == [] and result.groundedness is None and result.relevance == 5


def test_low_relevance_on_an_intended_refusal_does_not_fail():
    row = EvalRow(
        "F2",
        "FALLBACK",
        "Write me Python code",
        "I can only help with Contoso orders.",
        "",
        [],
        "refuse",
    )
    result = evaluate_row(row, (_judge("groundedness", 5), _judge("relevance", 1)), 3)
    assert result.passed and any("informational" in note for note in result.notes)


def test_build_rows_marks_refusal_cases_from_the_exam_expectations():
    report = [
        _outcome("F2", "I can only help with Contoso orders.", [], category="FALLBACK"),
        _outcome("T2", "Please share your Customer ID.", [], category="TOOL"),
    ]
    rows = {row.case_id: row for row in build_rows(report, CASES, POLICY)}
    assert rows["F2"].expected_behaviour == "refuse"
    assert rows["T2"].expected_behaviour == "clarify"


def test_low_relevance_on_a_clarifying_question_does_not_fail():
    row = EvalRow(
        "T3",
        "TOOL",
        "Check order 12345",
        "Please give a Customer ID like CUST-12345.",
        "",
        [],
        "clarify",
    )
    result = evaluate_row(row, (_judge("groundedness", 5), _judge("relevance", 2)), 3)
    assert result.passed and any("asking for valid input" in note for note in result.notes)


def test_low_relevance_on_a_normal_answer_still_fails():
    row = EvalRow("R1", "RAG", "Return window?", "Hello!", POLICY, ["p"])
    result = evaluate_row(row, (_judge("groundedness", 5), _judge("relevance", 2)), 3)
    assert not result.passed


def test_expected_behaviour_values_are_known():
    for case in CASES:
        assert case.get("expected_behaviour", "answer") in {
            "answer",
            "clarify",
            "refuse",
        }, case["id"]
