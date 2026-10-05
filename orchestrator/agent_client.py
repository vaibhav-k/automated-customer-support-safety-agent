"""Layer 2 of the architecture: invoke the Foundry agent.

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
from dataclasses import dataclass, field
from typing import Any, Optional

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from openai import APIError, OpenAI

logger = logging.getLogger("contoso.agent")


class AgentInvocationError(RuntimeError):
    """Raised when the agent run fails or returns no usable output."""


class ContentFilterBlockedError(AgentInvocationError):
    """Raised when the model deployment's content filter (Guardrails) blocks the prompt or completion."""


_CONTENT_FILTER_MARKERS = (
    "content_filter",
    "content management policy",
    "responsibleaipolicyviolation",
)


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


def parse_response(response: Any) -> AgentReply:
    """Convert an OpenAI ``Response`` object into an :class:`AgentReply`.

    Kept as a pure function so it can be unit-tested without Azure.
    """
    status = _get(response, "status")
    if status == "failed":
        error = _get(response, "error")
        code, message = _get(error, "code"), _get(error, "message", error)
        if _is_content_filter(code, message):
            raise ContentFilterBlockedError(f"Blocked by the deployment content filter: {message}")
        raise AgentInvocationError(f"Agent run failed: {message}")
    if status == "incomplete":
        details = _get(response, "incomplete_details")
        reason = _get(details, "reason", details)
        if reason == "content_filter":
            raise ContentFilterBlockedError("Completion stopped by the deployment content filter.")
        logger.warning("Agent response incomplete: %s", reason)

    texts: list[str] = []
    citations: list[Citation] = []
    tool_calls: list[str] = []
    seen_urls: set[str] = set()

    for item in _get(response, "output", []) or []:
        item_type = _get(item, "type", "")
        if item_type == "message":
            for part in _get(item, "content", []) or []:
                if _get(part, "type") != "output_text":
                    continue
                texts.append(_get(part, "text", "") or "")
                for annotation in _get(part, "annotations", []) or []:
                    if _get(annotation, "type") != "url_citation":
                        continue
                    url = _get(annotation, "url", "") or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        citations.append(Citation(title=_get(annotation, "title", "") or url, url=url))
        elif item_type and item_type != "reasoning":
            # e.g. azure_ai_search_call, openapi_call, function_call, mcp_call ...
            name = _get(item, "name")
            tool_calls.append(f"{item_type}:{name}" if name else item_type)

    text = "\n".join(t for t in texts if t).strip() or (_get(response, "output_text", "") or "").strip()
    if not text:
        raise AgentInvocationError("Agent returned no text output.")

    usage_obj = _get(response, "usage")
    usage = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _get(usage_obj, key)
        if isinstance(value, int):
            usage[key] = value

    return AgentReply(
        text=text,
        response_id=_get(response, "id", "") or "",
        citations=citations,
        tool_calls=tool_calls,
        usage=usage,
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
    ) -> None:
        if not project_endpoint:
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT is required")
        if not agent_name:
            raise ValueError("AGENT_NAME is required")
        self._agent_name = agent_name
        self._project: Optional[AIProjectClient] = None
        if openai_client is None:
            self._project = AIProjectClient(endpoint=project_endpoint, credential=credential)
            openai_client = self._project.get_openai_client()
        self._openai = openai_client

    @property
    def agent_name(self) -> str:
        return self._agent_name

    def start_conversation(self) -> str:
        try:
            conversation = self._openai.conversations.create()
        except (APIError, AzureError) as exc:
            raise AgentInvocationError(f"Could not create conversation: {exc}") from exc
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
                extra_body={
                    "agent_reference": {
                        "name": self._agent_name,
                        "type": "agent_reference",
                    }
                },
            )
        except APIError as exc:
            if _is_content_filter(getattr(exc, "code", None), str(exc)):
                raise ContentFilterBlockedError(f"Blocked by the deployment content filter: {exc}") from exc
            raise AgentInvocationError(f"Agent call failed: {exc}") from exc
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
