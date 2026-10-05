"""Unit tests for the pipeline, response parsing, and exam-case scoring."""

import json
from types import SimpleNamespace

import pytest

from orchestrator.agent_client import (
    AgentInvocationError,
    AgentReply,
    Citation,
    ContentFilterBlockedError,
    parse_response,
)
from orchestrator.pipeline import (
    BLOCKED_INPUT_MESSAGE,
    BLOCKED_OUTPUT_MESSAGE,
    UNAVAILABLE_MESSAGE,
    SupportPipeline,
)
from orchestrator.safety import SafetyCategory, SafetyVerdict
from scripts.run_exam_checks import CASES_PATH, evaluate

SAFE = SafetyVerdict(True, SafetyCategory.SAFE)


class FakeGate:
    def __init__(self, input_verdict=SAFE, output_verdict=SAFE):
        self.input_verdict = input_verdict
        self.output_verdict = output_verdict

    def check_user_input(self, text, documents=()):
        return self.input_verdict

    def check_model_output(self, text):
        return self.output_verdict


class FakeAgent:
    def __init__(self, reply: AgentReply | None = None, exc: Exception | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls = 0

    def ask(self, conversation_id: str, user_text: str) -> AgentReply:
        self.calls += 1
        if self.exc:
            raise self.exc
        assert self.reply is not None, "FakeAgent needs either a reply or an exception"
        return self.reply


REPLY = AgentReply(
    "Your return window is 30 days (POL-RET-001).",
    "resp_1",
    [Citation("Return Policy", "https://x#a")],
    ["azure_ai_search_call"],
)


def test_happy_path():
    result = SupportPipeline(FakeGate(), FakeAgent(REPLY)).handle("conv", "return window?")
    assert not result.blocked
    assert result.stage == "completed"
    assert result.citations == [{"title": "Return Policy", "url": "https://x#a"}]


def test_blocked_input_never_reaches_agent():
    agent = FakeAgent(REPLY)
    gate = FakeGate(SafetyVerdict(False, SafetyCategory.PROMPT_INJECTION, "attack"))
    result = SupportPipeline(gate, agent).handle("conv", "ignore instructions")
    assert result.blocked
    assert result.stage == "input_safety"
    assert result.reply == BLOCKED_INPUT_MESSAGE
    assert agent.calls == 0


def test_model_content_filter_is_reported_as_block():
    result = SupportPipeline(FakeGate(), FakeAgent(exc=ContentFilterBlockedError("jailbreak"))).handle("c", "x")
    assert result.blocked
    assert result.stage == "model_content_filter"


def test_agent_failure_returns_unavailable():
    result = SupportPipeline(FakeGate(), FakeAgent(exc=AgentInvocationError("500"))).handle("c", "x")
    assert not result.blocked
    assert result.stage == "agent"
    assert result.reply == UNAVAILABLE_MESSAGE


def test_harmful_output_is_replaced():
    gate = FakeGate(output_verdict=SafetyVerdict(False, SafetyCategory.HARMFUL_CONTENT, "Violence=6"))
    result = SupportPipeline(gate, FakeAgent(REPLY)).handle("c", "x")
    assert result.blocked
    assert result.stage == "output_safety"
    assert result.reply == BLOCKED_OUTPUT_MESSAGE


def test_parse_response_extracts_text_citations_tools_usage():
    response = SimpleNamespace(
        id="resp_9",
        status="completed",
        output_text="",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        output=[
            SimpleNamespace(type="openapi_call", name="contoso_order_status_getOrderStatus"),
            SimpleNamespace(type="azure_ai_search_call"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        text="Shipped via Contoso Express.",
                        annotations=[
                            SimpleNamespace(type="url_citation", url="https://p#a", title="Shipping"),
                            SimpleNamespace(type="url_citation", url="https://p#a", title="dup"),
                        ],
                    )
                ],
            ),
        ],
    )
    reply = parse_response(response)
    assert reply.text == "Shipped via Contoso Express."
    assert reply.tool_calls == [
        "openapi_call:contoso_order_status_getOrderStatus",
        "azure_ai_search_call",
    ]
    assert [c.url for c in reply.citations] == ["https://p#a"]
    assert reply.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_parse_response_failures():
    with pytest.raises(ContentFilterBlockedError):
        parse_response(
            {
                "status": "failed",
                "error": {"code": "content_filter", "message": "blocked"},
                "output": [],
            }
        )
    with pytest.raises(AgentInvocationError):
        parse_response(
            {
                "status": "failed",
                "error": {"code": "server_error", "message": "x"},
                "output": [],
            }
        )
    with pytest.raises(ContentFilterBlockedError):
        parse_response(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "output": [],
            }
        )
    with pytest.raises(AgentInvocationError):
        parse_response({"status": "completed", "output": [], "output_text": ""})


def test_evaluate_scoring_rules():
    ok = {
        "reply": "It is Shipped (POL-ORD-001).",
        "blocked": False,
        "stage": "completed",
        "tool_calls": ["openapi_call", "azure_ai_search_call"],
        "citations": [],
    }
    assert (
        evaluate(
            {
                "blocked": False,
                "must_contain_all": ["Shipped"],
                "tool_used": "openapi",
                "tool_used_2": "search",
                "grounding_evidence": True,
            },
            ok,
        )
        == []
    )
    assert evaluate({"tool_not_used": "openapi"}, ok)
    assert evaluate({"blocked": True}, ok)
    blocked = {
        "reply": BLOCKED_INPUT_MESSAGE,
        "blocked": True,
        "stage": "input_safety",
        "tool_calls": [],
    }
    assert evaluate({"blocked": True}, blocked) == []
    assert evaluate({"blocked_or_refused": True, "must_not_contain": ["x"]}, blocked) == []
    assert evaluate({"must_not_contain": ["shipped"]}, ok)  # case-insensitive
    assert evaluate({"must_contain_any": ["it is shipped"]}, ok) == []
    curly = dict(ok, reply="I couldn\u2019t find that.")
    assert evaluate({"must_contain_any": ["couldn't find"]}, curly) == []


def test_evaluate_blocked_by_and_outage():
    injection = {
        "reply": BLOCKED_INPUT_MESSAGE,
        "blocked": True,
        "stage": "input_safety",
        "input_verdict": {"category": "prompt_injection"},
        "tool_calls": [],
    }
    assert evaluate({"blocked": True, "blocked_by": ["prompt_injection"]}, injection) == []
    assert evaluate({"blocked": True, "blocked_by": ["document_injection"]}, injection)
    filtered = {
        "reply": BLOCKED_INPUT_MESSAGE,
        "blocked": True,
        "stage": "model_content_filter",
        "tool_calls": [],
    }
    assert evaluate({"blocked": True, "blocked_by": ["model_content_filter"]}, filtered) == []
    outage = dict(injection, input_verdict={"category": "safety_service_error"})
    assert evaluate({"blocked": True}, outage)
    assert evaluate({"blocked_or_refused": True}, outage)


def test_tool_aliases_match_by_name():
    named = {
        "reply": "Shipped",
        "blocked": False,
        "stage": "completed",
        "tool_calls": ["function_call:contoso_order_status_getOrderStatus"],
        "citations": [],
    }
    assert evaluate({"tool_used": "openapi"}, named) == []
    assert evaluate({"tool_not_used": "openapi"}, named)


def test_documents_are_forwarded_to_agent_as_untrusted_data():
    class RecordingAgent(FakeAgent):
        seen = ""

        def ask(self, conversation_id: str, user_text: str) -> AgentReply:
            self.seen = user_text
            return super().ask(conversation_id, user_text)

    agent = RecordingAgent(REPLY)
    SupportPipeline(FakeGate(), agent).handle("c", "Summarise this", ["warehouse note"])
    assert "warehouse note" in agent.seen
    assert 'trust="untrusted"' in agent.seen


def test_exam_cases_file_is_consistent():
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids))
    allowed = {
        "blocked",
        "blocked_by",
        "blocked_or_refused",
        "must_contain_all",
        "must_contain_any",
        "must_not_contain",
        "tool_used",
        "tool_used_2",
        "tool_not_used",
        "grounding_evidence",
    }
    for case in data["cases"]:
        assert case["category"] in {"RAG", "TOOL", "COMBINED", "FALLBACK", "SAFETY"}
        assert case["turns"]
        assert set(case["expect"]) <= allowed, case["id"]


def test_agent_errors_carry_role_hints():
    from orchestrator.agent_client import _describe_api_error

    class Forbidden(Exception):
        status_code = 403

    class Missing(Exception):
        status_code = 404

    assert "Azure AI User" in _describe_api_error(Forbidden("denied"))
    assert "provision_agent" in _describe_api_error(Missing("nope"))
    assert _describe_api_error(ValueError("other")) == "other"


def test_citation_markers_become_numbered_references():
    marker_a, marker_b = "【4:0†source】", "【4:2†source】"
    text = f"Shipping is $19.99 (POL-SHP-002) {marker_a}. EU rule applies.{marker_a}{marker_b}"
    start_a1 = text.index(marker_a)
    start_a2 = text.index(marker_a, start_a1 + 1)
    start_b = text.index(marker_b)
    annotations = [
        {
            "type": "url_citation",
            "url": "https://p#ship",
            "title": "4.2 Shipping",
            "start_index": start_a1,
            "end_index": start_a1 + len(marker_a),
        },
        {
            "type": "url_citation",
            "url": "https://p#ship",
            "title": "4.2 Shipping",
            "start_index": start_a2,
            "end_index": start_a2 + len(marker_a),
        },
        {
            "type": "url_citation",
            "url": "https://p#eu",
            "title": "5.3 EU",
            "start_index": start_b,
            "end_index": start_b + len(marker_b),
        },
    ]
    response = {
        "status": "completed",
        "id": "r",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text, "annotations": annotations}],
            }
        ],
    }
    reply = parse_response(response)
    assert "【" not in reply.text
    assert reply.text == "Shipping is $19.99 (POL-SHP-002) [1]. EU rule applies. [1] [2]"
    assert [(c.title, c.url) for c in reply.citations] == [
        ("4.2 Shipping", "https://p#ship"),
        ("5.3 EU", "https://p#eu"),
    ]


def test_stray_markers_without_offsets_are_removed():
    response = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Answer 【4:1†source】",
                        "annotations": [],
                    }
                ],
            }
        ],
    }
    assert parse_response(response).text == "Answer"


def test_skip_input_gate_lets_attacks_reach_the_agent():
    agent = FakeAgent(REPLY)
    gate = FakeGate(SafetyVerdict(False, SafetyCategory.PROMPT_INJECTION, "attack"))
    result = SupportPipeline(gate, agent, skip_input_gate=True).handle("c", "ignore all instructions")
    assert agent.calls == 1 and result.stage == "completed"
    assert result.input_verdict is not None and "skipped" in result.input_verdict.detail
