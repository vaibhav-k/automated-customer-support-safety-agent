"""Unit tests for chunking, index definition, OpenAPI spec, and agent definition (offline)."""

import json
from pathlib import Path

import pytest
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    SearchIndex,
)
from openapi_spec_validator import validate

from orchestrator.config import OPENAPI_SPEC_PATH, POLICY_PATH, SYSTEM_PROMPT_PATH
from scripts.ingest_policy import (
    VECTOR_PROFILE_NAME,
    _split_long,
    build_index,
    chunk_markdown,
)
from scripts.provision_agent import build_definition, prepare_openapi_spec


def _aoai_vectorizer_params(index: SearchIndex) -> AzureOpenAIVectorizerParameters:
    """Narrow the index's first vectorizer to the Azure OpenAI type (keeps type checkers happy)."""
    assert index.vector_search is not None
    assert index.vector_search.vectorizers is not None
    vectorizer = index.vector_search.vectorizers[0]
    assert isinstance(vectorizer, AzureOpenAIVectorizer)
    assert vectorizer.parameters is not None
    return vectorizer.parameters


def test_policy_chunks_are_heading_aware_and_bounded():
    chunks = chunk_markdown(POLICY_PATH.read_text(encoding="utf-8"), "contoso_policy.md", "https://p/x.md")
    assert len(chunks) >= 30
    assert len({c.id for c in chunks}) == len(chunks)
    assert all(len(c.content) <= 1800 + 200 for c in chunks)
    alaska = [c for c in chunks if "POL-SHP-002" in c.content]
    assert alaska
    assert "Alaska" in alaska[0].content
    assert alaska[0].title.startswith("4. Shipping Policy")
    assert alaska[0].url.startswith("https://p/x.md#")


def test_split_long_respects_limit():
    text = "\n\n".join(["word " * 100] * 10)
    pieces = _split_long(text, 1000, 100)
    assert len(pieces) > 1
    assert all(len(p) <= 1000 for p in pieces)
    with pytest.raises(ValueError):
        _split_long(text, 100, 100)


def test_index_definition_has_vectorizer_and_semantic_config():
    index = build_index(
        "contoso-policy-index",
        "https://aoai.openai.azure.com/",
        "text-embedding-3-small",
        "text-embedding-3-small",
        1536,
    )
    vector_field = next(f for f in index.fields if f.name == "content_vector")
    assert vector_field.vector_search_dimensions == 1536
    assert vector_field.vector_search_profile_name == VECTOR_PROFILE_NAME
    params = _aoai_vectorizer_params(index)
    assert params.resource_url == "https://aoai.openai.azure.com"
    assert params.api_key is None  # managed identity, keyless
    assert index.semantic_search is not None
    assert index.semantic_search.default_configuration_name


def test_index_vectorizer_key_fallback():
    index = build_index(
        "i",
        "https://aoai.openai.azure.com",
        "d",
        "text-embedding-3-large",
        3072,
        vectorizer_api_key="lab-key",
    )
    assert _aoai_vectorizer_params(index).api_key == "lab-key"
    assert next(f for f in index.fields if f.name == "content_vector").vector_search_dimensions == 3072


def test_settings_repr_masks_keys(monkeypatch):
    from orchestrator.config import Settings

    monkeypatch.setenv("AZURE_SEARCH_API_KEY", "super-secret-value")
    monkeypatch.setenv("AZURE_OPENAI_VECTORIZER_API_KEY", "another-secret")
    settings = Settings.from_env(env_file=Path("does-not-exist.env"))
    assert settings.search_api_key == "super-secret-value"
    assert "super-secret-value" not in repr(settings)
    assert "another-secret" not in repr(settings)


def test_openapi_spec_is_valid_openapi_3():
    spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    validate(spec)
    op = spec["paths"]["/orders/{customerId}/status"]["get"]
    assert op["operationId"] == "getOrderStatus"
    assert spec["components"]["securitySchemes"]["functionKey"]["name"] == "x-functions-key"


@pytest.mark.parametrize("use_connection", [True, False])
def test_prepare_spec_sets_server_and_security(use_connection):
    spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    prepared = prepare_openapi_spec(spec, "https://my-func.azurewebsites.net/api/", use_connection)
    validate(prepared)
    assert prepared["servers"][0]["url"] == "https://my-func.azurewebsites.net/api"
    assert ("security" in prepared) is use_connection
    assert ("securitySchemes" in prepared["components"]) is use_connection
    assert "security" in spec  # original untouched


def test_prepare_spec_rejects_http():
    with pytest.raises(ValueError):
        prepare_openapi_spec({}, "http://insecure.example.com/api", False)
    with pytest.raises(ValueError):
        prepare_openapi_spec({}, "http://localhost:7071/api", False)


def test_agent_definition_serialises():
    spec = prepare_openapi_spec(
        json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8")),
        "https://f.azurewebsites.net/api",
        True,
    )
    definition = build_definition(
        model="gpt-4.1-mini",
        instructions=SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        temperature=0.2,
        search_connection_id="conn-search",
        index_name="contoso-policy-index",
        top_k=5,
        openapi_spec=spec,
        order_api_connection_id="conn-key",
    )
    data = definition.as_dict()
    assert data["kind"] == "prompt"
    types = [t["type"] for t in data["tools"]]
    assert types == ["azure_ai_search", "openapi"]
    index = data["tools"][0]["azure_ai_search"]["indexes"][0]
    assert index["query_type"] == "vector_semantic_hybrid"
    assert index["top_k"] == 5
    assert data["tools"][1]["openapi"]["auth"]["type"] == "project_connection"


@pytest.mark.parametrize("raw,expected", [(None, None), ("none", None), ("0.2", 0.2), ("0", 0.0)])
def test_temperature_parsing(raw, expected):
    from orchestrator.config import _parse_temperature

    assert _parse_temperature(raw) == expected


def test_temperature_omitted_from_definition_when_unset():
    spec = prepare_openapi_spec(
        json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8")),
        "https://f.azurewebsites.net/api",
        False,
    )
    definition = build_definition(
        model="gpt-5-mini",
        instructions="x",
        temperature=None,
        search_connection_id="c",
        index_name="i",
        top_k=5,
        openapi_spec=spec,
        order_api_connection_id=None,
    )
    assert "temperature" not in definition.as_dict()
