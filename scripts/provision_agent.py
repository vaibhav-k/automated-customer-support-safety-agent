"""
Create a new version of the Contoso support agent in Azure AI Foundry.

The agent definition combines:
* ``agent/system_prompt.txt``  -> instructions (persona, guardrails, grounding, fallbacks)
* Azure AI Search tool         -> RAG over the ``contoso-policy-index`` (vector + semantic hybrid)
* OpenAPI tool                 -> ``agent/openapi_spec.json`` pointing at the Azure Function

Usage (from the repo root)::

    python -m scripts.provision_agent --dry-run   # print the definition, no Azure calls
    python -m scripts.provision_agent             # create a new agent version

Running it again creates a new *version* of the same agent name, which is how
prompt/tool changes are rolled out (and rolled back) in Foundry.

Required role for the identity running this script: **Azure AI User** (or higher,
e.g. Azure AI Project Manager) on the Foundry project.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential

from orchestrator.config import (
    OPENAPI_SPEC_PATH,
    SYSTEM_PROMPT_PATH,
    ConfigError,
    Settings,
)

logger = logging.getLogger("contoso.provision")

OPENAPI_TOOL_NAME = "contoso_order_status"
OPENAPI_TOOL_DESCRIPTION = (
    "Look up the live status of a Contoso customer's order (Processing, Shipped, Delivered, "
    "Cancelled, ReturnInitiated, Refunded) by customerId in the format CUST-12345."
)
DRY_RUN_PLACEHOLDER = "<resolved-at-runtime>"


def prepare_openapi_spec(spec: dict[str, Any], base_url: str, use_connection_auth: bool) -> dict[str, Any]:
    """Point the spec at the deployed Function App and align security with the auth mode."""
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(
            "ORDER_API_BASE_URL must be a public https:// URL (e.g. https://<app>.azurewebsites.net/api); "
            "the Foundry service cannot reach localhost"
        )
    prepared = copy.deepcopy(spec)
    prepared["servers"] = [
        {
            "url": base_url.rstrip("/"),
            "description": "Contoso Order API (Azure Functions)",
        }
    ]
    if not use_connection_auth:
        # Anonymous auth: the tool must not advertise a security requirement it cannot satisfy.
        prepared.pop("security", None)
        prepared.get("components", {}).pop("securitySchemes", None)
        for path_item in prepared.get("paths", {}).values():
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.pop("security", None)
    return prepared


def build_definition(
    *,
    model: str,
    instructions: str,
    temperature: Optional[float],
    search_connection_id: str,
    index_name: str,
    top_k: int,
    openapi_spec: dict[str, Any],
    order_api_connection_id: Optional[str],
):
    from azure.ai.projects.models import (
        AISearchIndexResource,
        AzureAISearchQueryType,
        AzureAISearchTool,
        AzureAISearchToolResource,
        OpenApiAnonymousAuthDetails,
        OpenApiAuthDetails,
        OpenApiFunctionDefinition,
        OpenApiProjectConnectionAuthDetails,
        OpenApiProjectConnectionSecurityScheme,
        OpenApiTool,
        PromptAgentDefinition,
    )

    search_tool = AzureAISearchTool(
        azure_ai_search=AzureAISearchToolResource(
            indexes=[
                AISearchIndexResource(
                    project_connection_id=search_connection_id,
                    index_name=index_name,
                    query_type=AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
                    top_k=top_k,
                )
            ]
        )
    )
    auth: OpenApiAuthDetails
    if order_api_connection_id:
        auth = OpenApiProjectConnectionAuthDetails(
            security_scheme=OpenApiProjectConnectionSecurityScheme(project_connection_id=order_api_connection_id)
        )
    else:
        auth = OpenApiAnonymousAuthDetails()
    openapi_tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name=OPENAPI_TOOL_NAME,
            description=OPENAPI_TOOL_DESCRIPTION,
            spec=openapi_spec,
            auth=auth,
        )
    )
    # Only send temperature when configured: reasoning models return HTTP 400
    # "Unsupported parameter: 'temperature'" at run time if it is set.
    if temperature is None:
        return PromptAgentDefinition(model=model, instructions=instructions, tools=[search_tool, openapi_tool])
    return PromptAgentDefinition(
        model=model,
        instructions=instructions,
        temperature=temperature,
        tools=[search_tool, openapi_tool],
    )


@dataclass(frozen=True)
class _Inputs:
    """Everything needed to build the agent definition, loaded and validated up front."""

    settings: Settings
    instructions: str
    spec: dict[str, Any]
    use_connection: bool


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision the Contoso support agent in Foundry.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the definition without calling Azure.",
    )
    return parser.parse_args(argv)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Keep our INFO messages but silence per-request HTTP/credential logging from the Azure SDKs.
    for noisy in ("azure", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_inputs(dry_run: bool) -> _Inputs:
    """Load settings, instructions, and the prepared OpenAPI spec. Raises ConfigError on any problem."""
    settings = Settings.from_env()
    required = [
        "agent_name",
        "model_deployment_name",
        "search_index_name",
        "order_api_base_url",
    ]
    if not dry_run:
        required += ["foundry_project_endpoint", "search_connection_name"]
    settings.require(*required)

    use_connection = bool(settings.order_api_connection_name)
    raw_spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    try:
        spec = prepare_openapi_spec(raw_spec, settings.required_str("order_api_base_url"), use_connection)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    instructions = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return _Inputs(settings, instructions, spec, use_connection)


def _definition(inputs: _Inputs, search_connection_id: str, order_api_connection_id: Optional[str]):
    s = inputs.settings
    return build_definition(
        model=s.model_deployment_name,
        instructions=inputs.instructions,
        temperature=s.agent_temperature,
        search_connection_id=search_connection_id,
        index_name=s.search_index_name,
        top_k=s.search_top_k,
        openapi_spec=inputs.spec,
        order_api_connection_id=order_api_connection_id,
    )


def _dry_run(inputs: _Inputs) -> int:
    order_id = DRY_RUN_PLACEHOLDER if inputs.use_connection else None
    definition = _definition(inputs, DRY_RUN_PLACEHOLDER, order_id)
    payload = {
        "agent_name": inputs.settings.agent_name,
        "definition": definition.as_dict(),
    }
    print(json.dumps(payload, indent=2))
    return 0


def _log_http_error(exc: HttpResponseError) -> None:
    if exc.status_code in (401, 403):
        logger.error(
            "HTTP %s from Foundry: your identity lacks the 'Azure AI User' role on the Foundry project "
            "(project > Access control (IAM)); ask an admin to grant it. Details: %s",
            exc.status_code,
            exc.message,
        )
    elif exc.status_code == 404:
        logger.error(
            "HTTP 404 from Foundry: check FOUNDRY_PROJECT_ENDPOINT (Foundry > project > Overview). Details: %s",
            exc.message,
        )
    else:
        logger.error(
            "Foundry rejected the agent definition (HTTP %s): %s",
            exc.status_code,
            exc.message,
        )


def _resolve_connections(project: AIProjectClient, inputs: _Inputs) -> tuple[str, Optional[str]]:
    """Return (search connection id, order API connection id or None). Raises ResourceNotFoundError."""
    s = inputs.settings
    search_id = project.connections.get(s.required_str("search_connection_name")).id
    order_id = (
        project.connections.get(s.required_str("order_api_connection_name")).id if inputs.use_connection else None
    )
    return search_id, order_id


def _provision(inputs: _Inputs) -> int:
    endpoint = inputs.settings.required_str("foundry_project_endpoint")
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    try:
        search_id, order_id = _resolve_connections(project, inputs)
        agent = project.agents.create_version(
            agent_name=inputs.settings.agent_name,
            definition=_definition(inputs, search_id, order_id),
            description="Contoso customer support agent with RAG (Azure AI Search) and order-status tool.",
            metadata={"project": "ai-103-catch-all", "owner": "customer-support"},
        )
        logger.info("Agent ready: name=%s version=%s id=%s", agent.name, agent.version, agent.id)
        print(json.dumps({"name": agent.name, "version": agent.version, "id": agent.id}))
        return 0
    except ResourceNotFoundError as exc:
        print(
            "Project connection not found. Check AZURE_SEARCH_CONNECTION_NAME / "
            f"ORDER_API_CONNECTION_NAME in Foundry > Management center > Connected resources. ({exc.message})",
            file=sys.stderr,
        )
        return 2
    except ClientAuthenticationError as exc:
        logger.exception(
            "Could not get an Entra ID token (run 'az login' and 'az account set'): %s",
            exc.message,
        )
        return 1
    except HttpResponseError as exc:
        _log_http_error(exc)
        return 1
    except AzureError as exc:  # DNS / network / malformed endpoint
        logger.exception("Could not reach the Foundry project endpoint %s: %s", endpoint, exc)
        return 1
    finally:
        project.close()
        credential.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging()
    try:
        inputs = _load_inputs(args.dry_run)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return _dry_run(inputs) if args.dry_run else _provision(inputs)


if __name__ == "__main__":
    sys.exit(main())
