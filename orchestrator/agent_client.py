"""
Layer 2 of the architecture: invoke the Foundry agent.

Uses the Foundry Agent Service (``azure-ai-projects`` 2.x, GA ``v1`` REST API):

* The agent (a *prompt agent*) is created/versioned by ``scripts/provision_agent.py``.
* Conversation state lives server-side in a **Conversation**
  (``openai_client.conversations.create()``), so multi-turn memory is handled
  by the service — the client only keeps the conversation id.
* Each turn is a **Responses** call that references the agent by name via
  ``extra_body={"agent_reference": {...}}``. The agent decides whether to call
  the Azure AI Search tool (RAG) and/or the OpenAPI tool (order status API).

Authentication is keyless (Entra ID). The caller needs the **Azure AI User**
role on the Foundry project (or resource).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from openai import APIError, OpenAI

logger = logging.getLogger("contoso.agent")

AGENT_TIMEOUT_SECONDS = 120.0


class AgentInvocationError(RuntimeError):
    """Raised when the agent run fails or returns no usable output."""


class ContentFilterBlockedError(AgentInvocationError):
    """Raised when the model deployment's content filter (Guardrails) blocks the prompt or completion."""


_CONTENT_FILTER_MARKERS = (
    "content_filter",
    "content management policy",
    "responsibleaipolicyviolation",
)


def _filter_entries(source: Any) -> list[Any]:
    """The ``content_filters`` list from an openai APIError (``.body``), a run error dict, or a wrapper dict."""
    body = getattr(source, "body", source)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    filters = _get(body, "content_filters") or []
    return [filters] if isinstance(filters, dict) else list(filters)


def _triggered_label(category: str, verdict: Any) -> Optional[str]:
    if not (_get(verdict, "filtered") or _get(verdict, "detected")):
        return None
    severity = _get(verdict, "severity")
    return f"{category} ({severity})" if severity and severity != "safe" else category


def _content_filter_summary(source: Any) -> str:
    """Name the categories that tripped the content filter, e.g. "jailbreak" or "violence (medium)"."""
    triggered: list[str] = []
    for entry in _filter_entries(source):
        results = _get(entry, "content_filter_results") or {}
        if not isinstance(results, dict):
            continue
        for category, verdict in results.items():
            label = _triggered_label(category, verdict)
            if label and label not in triggered:
                triggered.append(label)
    return ", ".join(triggered) or "category not reported"


def _describe_api_error(exc: Exception) -> str:
    """Add an actionable hint to common auth / not-found failures from the Foundry endpoint."""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return (
            f"{exc} -> HTTP {status}: your identity needs the 'Azure AI User' role on the Foundry project "
            "(check the project's Access control (IAM), or ask an admin). Run 'az login' if the token is missing."
        )
    if status == 400 and "tool_user_error" in str(exc):
        return (
            f"{exc} -> an agent tool call failed (e.g. the OpenAPI tool got a non-2xx response), which aborts "
            "the whole run. Make the API return HTTP 200 for business errors such as 'not found'."
        )
    if status == 400 and "unsupported parameter" in str(exc).lower():
        return (
            f"{exc} -> the model deployment rejects a parameter set on the agent. For 'temperature', "
            "clear AGENT_TEMPERATURE in .env and re-run 'python -m scripts.provision_agent'."
        )
    if status == 404:
        return (
            f"{exc} -> HTTP 404: check FOUNDRY_PROJECT_ENDPOINT and that AGENT_NAME exists "
            "(run 'python -m scripts.provision_agent')."
        )
    return str(exc)


def _one_line(exc: Exception) -> str:
    """First line of an Azure error without the trailing docs link, e.g. for a 403 'missing permission'."""
    lines = str(exc).strip().splitlines() or [type(exc).__name__]
    first = lines[0].split(" Please refer to ")[0]
    status = getattr(exc, "status_code", None)
    return f"HTTP {status}: {first}" if status else first


def _is_content_filter(code: Any, message: Any) -> bool:
    haystack = f"{code or ''} {message or ''}".lower()
    return any(marker in haystack for marker in _CONTENT_FILTER_MARKERS)


@dataclass(frozen=True)
class Citation:
    title: str
    url: str

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url}


@dataclass(frozen=True)
class AgentReply:
    text: str
    response_id: str
    citations: list[Citation] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "response_id": self.response_id,
            "citations": [c.as_dict() for c in self.citations],
            "tool_calls": list(self.tool_calls),
            "usage": dict(self.usage),
        }


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute from an SDK object or a key from a dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _raise_for_status(response: Any) -> None:
    """Translate failed / incomplete run states into typed exceptions."""
    status = _get(response, "status")
    if status == "failed":
        error = _get(response, "error")
        code, message = _get(error, "code"), _get(error, "message", error)
        if _is_content_filter(code, message):
            raise ContentFilterBlockedError(
                f"Blocked by the deployment content filter ({_content_filter_summary(error)})"
            )
        raise AgentInvocationError(f"Agent run failed: {message}")
    if status == "incomplete":
        details = _get(response, "incomplete_details")
        reason = _get(details, "reason", details)
        if reason == "content_filter":
            raise ContentFilterBlockedError("Completion stopped by the deployment content filter.")
        logger.warning("Agent response incomplete: %s", reason)


# Raw citation markers emitted by the Azure AI Search tool, e.g. "【4:0†source】".
_CITATION_MARKER_RE = re.compile(r"\s?【[^】]*】")


class _CitationRegistry:
    """Numbers unique source URLs in order of first appearance across the whole reply."""

    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}
        self.citations: list[Citation] = []

    def number_for(self, url: str, title: str) -> int:
        if url not in self._numbers:
            self.citations.append(Citation(title=title or url, url=url))
            self._numbers[url] = len(self.citations)
        return self._numbers[url]


def _render_citations(part: Any, registry: _CitationRegistry) -> str:
    """Replace 【…】 markers with [n] (via annotation offsets when present) and register their sources."""
    text: str = _get(part, "text", "") or ""
    spans: list[tuple[int, int, int]] = []
    for annotation in _get(part, "annotations", []) or []:
        url = _get(annotation, "url", "") or ""
        if _get(annotation, "type") != "url_citation" or not url:
            continue
        number = registry.number_for(url, _get(annotation, "title", "") or "")
        start, end = _get(annotation, "start_index"), _get(annotation, "end_index")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
            spans.append((start, end, number))
    for start, end, number in sorted(spans, reverse=True):  # right-to-left keeps earlier offsets valid
        text = f"{text[:start]} [{number}]{text[end:]}"
    return _CITATION_MARKER_RE.sub("", text).replace("  [", " [").strip()


def _message_parts(item: Any) -> list[Any]:
    """The ``output_text`` content parts of a message output item."""
    return [part for part in (_get(item, "content", []) or []) if _get(part, "type") == "output_text"]


def _tool_call_label(item: Any) -> str:
    # e.g. azure_ai_search_call, openapi_call, function_call, mcp_call ...
    item_type = _get(item, "type", "")
    name = _get(item, "name")
    return f"{item_type}:{name}" if name else item_type


def _usage(response: Any) -> dict[str, int]:
    usage_obj = _get(response, "usage")
    values = {key: _get(usage_obj, key) for key in ("input_tokens", "output_tokens", "total_tokens")}
    return {key: value for key, value in values.items() if isinstance(value, int)}


def parse_response(response: Any) -> AgentReply:
    """Convert an OpenAI ``Response`` object into an :class:`AgentReply`.

    Kept as a pure function so it can be unit-tested without Azure.
    """
    _raise_for_status(response)

    texts: list[str] = []
    registry = _CitationRegistry()
    tool_calls: list[str] = []
    for item in _get(response, "output", []) or []:
        item_type = _get(item, "type", "")
        if item_type == "message":
            texts.extend(_render_citations(part, registry) for part in _message_parts(item))
        elif item_type and item_type != "reasoning":
            tool_calls.append(_tool_call_label(item))

    fallback = _CITATION_MARKER_RE.sub("", _get(response, "output_text", "") or "")
    text = "\n".join(t for t in texts if t).strip() or fallback.strip()
    if not text:
        raise AgentInvocationError("Agent returned no text output.")

    return AgentReply(
        text=text,
        response_id=_get(response, "id", "") or "",
        citations=registry.citations,
        tool_calls=tool_calls,
        usage=_usage(response),
    )


class SupportAgentClient:
    """Thin, production-friendly wrapper around a named Foundry prompt agent."""

    def __init__(
        self,
        project_endpoint: str,
        agent_name: str,
        credential: TokenCredential,
        *,
        openai_client: Optional[OpenAI] = None,
        resolve_agent_id: bool = False,
    ) -> None:
        if not project_endpoint:
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT is required")
        if not agent_name:
            raise ValueError("AGENT_NAME is required")
        self._agent_name = agent_name
        self._agent_id: Optional[str] = None
        self._project: Optional[AIProjectClient] = None
        if openai_client is None:
            self._project = AIProjectClient(endpoint=project_endpoint, credential=credential)
            # Bounded waits: the openai defaults (600 s, 2 retries) can block ~30 min and a retried
            # responses.create after a read timeout may append the user's turn to the conversation twice.
            openai_client = self._project.get_openai_client(timeout=AGENT_TIMEOUT_SECONDS, max_retries=1)
            if resolve_agent_id:
                self._agent_id = self._latest_version_id()
        self._openai = openai_client

    def _latest_version_id(self) -> Optional[str]:
        """Id of the agent's latest version. Sent in ``agent_reference`` so Foundry's Tracing view can
        correlate client-side traces with the agent; optional, so a lookup failure only costs that link.
        """
        if self._project is None:
            return None
        try:
            latest = self._project.agents.get(self._agent_name).versions.latest
        except AzureError as exc:
            logger.warning("Could not resolve agent id for trace correlation: %s", _one_line(exc))
            return None
        logger.info(
            "Agent %s latest version %s (id %s)",
            self._agent_name,
            latest.version,
            latest.id,
        )
        return latest.id

    def _agent_reference(self) -> dict[str, str]:
        reference = {"name": self._agent_name, "type": "agent_reference"}
        if self._agent_id:
            reference["id"] = self._agent_id
        return reference

    @property
    def agent_name(self) -> str:
        return self._agent_name

    def start_conversation(self) -> str:
        try:
            conversation = self._openai.conversations.create()
        except (APIError, AzureError) as exc:
            raise AgentInvocationError(f"Could not create conversation: {_describe_api_error(exc)}") from exc
        logger.info("Conversation created: %s", conversation.id)
        return conversation.id

    def end_conversation(self, conversation_id: str) -> None:
        try:
            self._openai.conversations.delete(conversation_id=conversation_id)
        except (APIError, AzureError) as exc:
            logger.warning("Could not delete conversation %s: %s", conversation_id, exc)

    def ask(self, conversation_id: str, user_text: str) -> AgentReply:
        try:
            response = self._openai.responses.create(
                conversation=conversation_id,
                input=user_text,
                extra_body={"agent_reference": self._agent_reference()},
            )
        except APIError as exc:
            if _is_content_filter(getattr(exc, "code", None), str(exc)):
                raise ContentFilterBlockedError(
                    f"Blocked by the deployment content filter ({_content_filter_summary(exc)})"
                ) from exc
            raise AgentInvocationError(f"Agent call failed: {_describe_api_error(exc)}") from exc
        except AzureError as exc:  # e.g. ClientAuthenticationError from the token provider
            raise AgentInvocationError(f"Agent call failed (Azure credential/transport): {exc}") from exc
        reply = parse_response(response)
        logger.info(
            "Agent reply %s tools=%s citations=%d usage=%s",
            reply.response_id,
            reply.tool_calls,
            len(reply.citations),
            reply.usage,
        )
        return reply

    def close(self) -> None:
        self._openai.close()
        if self._project is not None:
            self._project.close()
