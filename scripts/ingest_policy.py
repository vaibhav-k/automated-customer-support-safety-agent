"""Chunk, embed, and index ``data/contoso_policy.md`` into Azure AI Search.

Creates (or updates) a vector + semantic index whose vector field is bound to an
**Azure OpenAI vectorizer** (``text-embedding-3-small`` at 1536 dims by default, or
``text-embedding-3-large`` at up to 3072 dims). The
integrated vectorizer is what lets the Foundry agent's Azure AI Search tool run
``vector_semantic_hybrid`` queries without the agent embedding anything itself.

Usage (from the repo root)::

    python -m scripts.ingest_policy --dry-run      # show chunks, no Azure calls
    python -m scripts.ingest_policy                # create/update index + upload
    python -m scripts.ingest_policy --recreate     # drop and rebuild the index

Chunk IDs are derived from the heading path, so after renaming or removing headings in
the policy run with ``--recreate`` to avoid stale chunks remaining in the index.

Lab fallback when you cannot get roles assigned (NOT for production):
* ``AZURE_SEARCH_API_KEY``            — admin key (Search > Settings > Keys). Used instead of Entra ID
                                        for index management and uploads. Requires API access control
                                        set to "API keys" or "Both".
* ``AZURE_OPENAI_VECTORIZER_API_KEY`` — Foundry/Azure OpenAI key (Foundry resource > Keys and Endpoint)
                                        stored in the index vectorizer, so query-time embedding works
                                        without granting the search service's managed identity a role.
Embeddings for ingestion still use your Entra ID identity (Cognitive Services OpenAI User).

Required roles for the identity running this script (keyless):
* **Search Service Contributor**   — create/update the index definition
* **Search Index Data Contributor** — upload documents
* **Cognitive Services OpenAI User** on the Azure OpenAI / Foundry resource — embeddings

The *search service's* system-assigned managed identity also needs
**Cognitive Services OpenAI User** so the vectorizer can embed queries at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient

from orchestrator.auth import make_credential
from orchestrator.config import POLICY_PATH, ConfigError, Settings

logger = logging.getLogger("contoso.ingest")

MAX_CHUNK_CHARS = 1800
OVERLAP_CHARS = 200
EMBED_BATCH_SIZE = 16
VECTORIZER_NAME = "aoai-embedding"  # model-agnostic: the model is set by EMBEDDING_MODEL_NAME
VECTOR_PROFILE_NAME = "hnsw-aoai-profile"
HNSW_ALGORITHM_NAME = "hnsw-cosine"
SEMANTIC_CONFIG_NAME = "contoso-semantic-config"


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    section: str
    content: str
    source: str
    url: str
    chunk_index: int


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _hard_split(para: str, max_chars: int, overlap: int) -> tuple[list[str], str]:
    """Cut a paragraph longer than ``max_chars`` into fixed-size heads (with overlap) and a remainder."""
    heads: list[str] = []
    while len(para) > max_chars:
        heads.append(para[:max_chars])
        para = para[max_chars - overlap :]
    return heads, para


def _append_paragraph(pieces: list[str], current: str, para: str, max_chars: int, overlap: int) -> str:
    """Add ``para`` to the chunk being built, starting a new (overlapping) chunk when it would overflow."""
    candidate = f"{current}\n\n{para}" if current else para
    if len(candidate) <= max_chars:
        return candidate
    pieces.append(current)
    tail = current[-overlap:] if overlap else ""
    with_tail = f"{tail}\n\n{para}" if tail else para
    return with_tail if len(with_tail) <= max_chars else para


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split on paragraph boundaries; hard-split any paragraph that is still too long."""
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap must be >= 0 and smaller than max_chars")
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        heads, para = _hard_split(para, max_chars, overlap)
        if heads:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(heads)
        current = _append_paragraph(pieces, current, para, max_chars, overlap)
    if current:
        pieces.append(current)
    return pieces


def chunk_markdown(
    markdown: str,
    source: str,
    base_url: str,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[Chunk]:
    """Heading-aware chunking: one chunk per ``##``/``###`` section (split further if long).

    Each chunk is prefixed with its heading path so it is self-describing for both
    BM25/semantic ranking and embeddings.
    """
    doc_title = "Contoso Policy"
    h2 = ""
    h3 = ""
    buffer: list[str] = []
    sections: list[tuple[str, str]] = []  # (heading path, body)

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = " > ".join(p for p in (h2, h3) if p) or doc_title
            sections.append((path, body))
        buffer.clear()

    for line in markdown.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            flush()
            doc_title = line[2:].strip()
            h2 = h3 = ""
        elif line.startswith("## "):
            flush()
            h2, h3 = line[3:].strip(), ""
        elif line.startswith("### "):
            flush()
            h3 = line[4:].strip()
        else:
            buffer.append(line)
    flush()

    chunks: list[Chunk] = []
    for path, body in sections:
        anchor = _slugify(path.split(" > ")[-1])
        for index, piece in enumerate(_split_long(body, max_chars, overlap)):
            content = f"{doc_title} — {path}\n\n{piece}".strip()
            digest = hashlib.sha1(f"{source}|{path}|{index}".encode()).hexdigest()[:16]
            chunks.append(
                Chunk(
                    id=f"{_slugify(source)}-{digest}",
                    title=path,
                    section=path.split(" > ")[0],
                    content=content,
                    source=source,
                    url=f"{base_url}#{anchor}",
                    chunk_index=index,
                )
            )
    return chunks


def _batched(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def build_index(
    index_name: str,
    aoai_endpoint: str,
    embedding_deployment: str,
    embedding_model: str,
    dimensions: int,
    vectorizer_api_key: str | None = None,
):
    from azure.search.documents.indexes.models import (
        AzureOpenAIVectorizer,
        AzureOpenAIVectorizerParameters,
        HnswAlgorithmConfiguration,
        SearchField,
        SearchIndex,
        SemanticConfiguration,
        SemanticField,
        SemanticPrioritizedFields,
        SemanticSearch,
        VectorSearch,
        VectorSearchProfile,
    )

    string, vector = "Edm.String", "Collection(Edm.Single)"
    fields = [
        SearchField(name="id", type=string, key=True, filterable=True, retrievable=True),
        SearchField(name="title", type=string, searchable=True, retrievable=True),
        SearchField(
            name="section",
            type=string,
            searchable=True,
            filterable=True,
            facetable=True,
            retrievable=True,
        ),
        SearchField(
            name="content",
            type=string,
            searchable=True,
            retrievable=True,
            analyzer_name="en.microsoft",
        ),
        SearchField(name="source", type=string, filterable=True, retrievable=True),
        SearchField(name="url", type=string, retrievable=True),
        SearchField(
            name="chunk_index",
            type="Edm.Int32",
            filterable=True,
            sortable=True,
            retrievable=True,
        ),
        SearchField(
            name="content_vector",
            type=vector,
            searchable=True,
            retrievable=False,
            stored=False,
            vector_search_dimensions=dimensions,
            vector_search_profile_name=VECTOR_PROFILE_NAME,
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=HNSW_ALGORITHM_NAME)],
        profiles=[
            VectorSearchProfile(
                name=VECTOR_PROFILE_NAME,
                algorithm_configuration_name=HNSW_ALGORITHM_NAME,
                vectorizer_name=VECTORIZER_NAME,
            )
        ],
        vectorizers=[
            AzureOpenAIVectorizer(
                vectorizer_name=VECTORIZER_NAME,
                # No api_key => the search service uses its system-assigned managed identity (keyless).
                # With api_key => lab fallback; the key is stored (encrypted) in the index definition.
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=aoai_endpoint.rstrip("/"),
                    deployment_name=embedding_deployment,
                    model_name=embedding_model,
                    api_key=vectorizer_api_key or None,
                ),
            )
        ],
    )
    semantic_search = SemanticSearch(
        default_configuration_name=SEMANTIC_CONFIG_NAME,
        configurations=[
            SemanticConfiguration(
                name=SEMANTIC_CONFIG_NAME,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                    keywords_fields=[SemanticField(field_name="section")],
                ),
            )
        ],
    )
    return SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
        description="Contoso customer-support policy knowledge base (RAG).",
    )


# Maximum output dimensions per embedding model (text-embedding-3 models can be shortened; ada-002 cannot).
EMBEDDING_MAX_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
_FIXED_DIMENSION_MODELS = {"text-embedding-ada-002"}


def validate_embedding_settings(deployment: str, model: str, dimensions: int) -> None:
    """Catch the classic mismatches before anything is created or embedded. Raises ConfigError."""
    max_dims = EMBEDDING_MAX_DIMENSIONS.get(model)
    if max_dims is None:
        logger.warning("Unknown EMBEDDING_MODEL_NAME %r; skipping dimension checks.", model)
        return
    if dimensions > max_dims or (model in _FIXED_DIMENSION_MODELS and dimensions != max_dims):
        raise ConfigError(
            f"EMBEDDING_DIMENSIONS={dimensions} is not valid for {model} (max {max_dims}). "
            f"Set EMBEDDING_DIMENSIONS={max_dims} and re-run with --recreate."
        )
    for size in ("small", "large"):
        other = "large" if size == "small" else "small"
        if f"3-{size}" in deployment and f"3-{other}" in model:
            raise ConfigError(
                f"EMBEDDING_DEPLOYMENT_NAME={deployment!r} looks like text-embedding-3-{size} but "
                f"EMBEDDING_MODEL_NAME={model!r}. Set both (and EMBEDDING_DIMENSIONS) for the same model."
            )


def embed_chunks(
    chunks: list[Chunk],
    aoai_endpoint: str,
    deployment: str,
    dimensions: int,
    credential,
) -> list[list[float]]:
    from azure.identity import get_bearer_token_provider
    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=aoai_endpoint,
        azure_ad_token_provider=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
        api_version="2024-10-21",
        max_retries=5,
    )
    vectors: list[list[float]] = []
    try:
        for batch in _batched(chunks, EMBED_BATCH_SIZE):
            result = client.embeddings.create(
                model=deployment,
                input=[c.content for c in batch],
                dimensions=dimensions,
            )
            ordered = sorted(result.data, key=lambda d: d.index)
            vectors.extend(d.embedding for d in ordered)
            logger.info("Embedded %d/%d chunks", len(vectors), len(chunks))
    finally:
        client.close()
    if len(vectors) != len(chunks):
        raise RuntimeError(f"Expected {len(chunks)} embeddings, got {len(vectors)}")
    return vectors


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index Contoso policies into Azure AI Search.")
    parser.add_argument("--file", type=Path, default=None, help="Markdown file to ingest.")
    parser.add_argument("--dry-run", action="store_true", help="Print chunks; make no Azure calls.")
    parser.add_argument("--recreate", action="store_true", help="Delete the index before rebuilding.")
    return parser.parse_args(argv)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Keep our INFO messages but silence per-request HTTP/credential logging from the Azure SDKs.
    for noisy in ("azure", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _search_credential(settings: Settings, credential: TokenCredential) -> AzureKeyCredential | TokenCredential:
    """Entra ID by default; the admin key only when the lab fallback is configured."""
    if settings.vectorizer_api_key:
        logger.warning(
            "Storing AZURE_OPENAI_VECTORIZER_API_KEY in the index vectorizer (lab fallback). "
            "Prefer the search service's managed identity + Cognitive Services OpenAI User."
        )
    if not settings.search_api_key:
        return credential
    logger.warning(
        "Using AZURE_SEARCH_API_KEY for Azure AI Search (lab fallback). "
        "Remove it and use RBAC (Search Index Data Contributor) when possible."
    )
    return AzureKeyCredential(settings.search_api_key)


def _to_documents(chunks: list[Chunk], vectors: list[list[float]]) -> list[dict]:
    return [
        {
            "id": c.id,
            "title": c.title,
            "section": c.section,
            "content": c.content,
            "source": c.source,
            "url": c.url,
            "chunk_index": c.chunk_index,
            "content_vector": v,
        }
        for c, v in zip(chunks, vectors, strict=True)
    ]


def _upload(search_client: SearchClient, documents: list[dict]) -> list[tuple[str, str]]:
    """Upload in batches; return (key, error) for every document that failed."""
    failed: list[tuple[str, str]] = []
    for batch in _batched(documents, 100):
        for result in search_client.merge_or_upload_documents(documents=batch):
            if not result.succeeded:
                failed.append((result.key, result.error_message or "unknown error"))
    return failed


def _log_search_error(exc: HttpResponseError, using_api_key: bool) -> None:
    logger.error("Azure AI Search error: %s", exc.message)
    if exc.status_code != 403:
        return
    if using_api_key:
        logger.error(
            "403 Forbidden with an API key: set Settings > Keys > API access control to "
            "'API keys' or 'Both', and use an ADMIN key (query keys cannot write)."
        )
    else:
        logger.error(
            "403 Forbidden: check that the search service has RBAC enabled "
            "('Role-based access control' or 'Both' under Settings > Keys) and that "
            "you hold Search Service Contributor + Search Index Data Contributor. "
            "If you cannot get roles assigned, set AZURE_SEARCH_API_KEY (lab fallback)."
        )


def _ensure_index(index_client: SearchIndexClient, settings: Settings, recreate: bool) -> None:
    if recreate:
        try:
            index_client.delete_index(settings.search_index_name)
            logger.info("Deleted index %s", settings.search_index_name)
        except ResourceNotFoundError:
            pass
    index = build_index(
        settings.search_index_name,
        settings.required_str("azure_openai_endpoint"),
        settings.embedding_deployment_name,
        settings.embedding_model_name,
        settings.embedding_dimensions,
        settings.vectorizer_api_key,
    )
    index_client.create_or_update_index(index)
    logger.info("Index %s is ready", settings.search_index_name)


def _ingest(settings: Settings, chunks: list[Chunk], recreate: bool) -> int:
    """Create/update the index, embed the chunks, and upload them. Returns a process exit code."""
    search_endpoint = settings.required_str("search_endpoint")
    credential = make_credential()
    search_credential = _search_credential(settings, credential)
    index_client = SearchIndexClient(search_endpoint, search_credential)
    try:
        _ensure_index(index_client, settings, recreate)
        vectors = embed_chunks(
            chunks,
            settings.required_str("azure_openai_endpoint"),
            settings.embedding_deployment_name,
            settings.embedding_dimensions,
            credential,
        )
        documents = _to_documents(chunks, vectors)
        with SearchClient(search_endpoint, settings.search_index_name, search_credential) as search_client:
            failed = _upload(search_client, documents)
        for key, message in failed:
            logger.error("Upload failed for %s: %s", key, message)
        if failed:
            return 1
        logger.info("Uploaded %d documents to %s", len(documents), settings.search_index_name)
        return 0
    except HttpResponseError as exc:
        _log_search_error(exc, using_api_key=bool(settings.search_api_key))
        return 1
    finally:
        index_client.close()
        credential.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    path: Path = args.file or POLICY_PATH
    if not path.is_file():
        print(f"Policy file not found: {path}", file=sys.stderr)
        return 2
    chunks = chunk_markdown(path.read_text(encoding="utf-8"), path.name, settings.policy_base_url)
    logger.info("Prepared %d chunks from %s", len(chunks), path.name)

    if args.dry_run:
        for c in chunks:
            print(f"[{c.id}] {c.title} ({len(c.content)} chars) -> {c.url}")
        return 0

    try:
        settings.require(
            "search_endpoint",
            "search_index_name",
            "azure_openai_endpoint",
            "embedding_deployment_name",
        )
        validate_embedding_settings(
            settings.embedding_deployment_name,
            settings.embedding_model_name,
            settings.embedding_dimensions,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return _ingest(settings, chunks, args.recreate)


if __name__ == "__main__":
    sys.exit(main())
