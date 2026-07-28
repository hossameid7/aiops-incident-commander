"""
app/rag.py - ChromaDB vector store initialisation and retrieval layer.

Responsibilities:
  - Load plain-text runbook files from data/runbooks/ at startup.
  - Embed them using a local sentence-transformers model (no external API calls).
  - Persist the collection to disk via ChromaDB's PersistentClient.
  - Provide a synchronous `query_runbooks` function for use by LangGraph agents.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
from chromadb import PersistentClient, Collection

from config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons (initialised once at startup)
# ---------------------------------------------------------------------------
_chroma_client: Optional[PersistentClient] = None
_collection: Optional[Collection] = None

RUNBOOKS_DIR = Path(__file__).parent.parent / "data" / "runbooks"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # Lightweight, CPU-friendly model


def _build_embedding_function() -> embedding_functions.SentenceTransformerEmbeddingFunction:
    """Return a ChromaDB-compatible sentence-transformers embedding function."""
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )


def _load_runbook_documents() -> tuple[list[str], list[str], list[dict]]:
    """
    Walk the runbooks directory and return three parallel lists:
      - ids       : unique string IDs per document chunk
      - documents : raw text content
      - metadatas : dict with 'source' filename
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    if not RUNBOOKS_DIR.exists():
        logger.warning("Runbooks directory not found at %s", RUNBOOKS_DIR)
        return ids, documents, metadatas

    for idx, filepath in enumerate(sorted(RUNBOOKS_DIR.glob("*.txt"))):
        try:
            content = filepath.read_text(encoding="utf-8").strip()
            if not content:
                logger.warning("Runbook file is empty, skipping: %s", filepath.name)
                continue

            # Split long runbooks into overlapping 500-char chunks for better retrieval
            chunks = _chunk_text(content, chunk_size=500, overlap=50)
            for chunk_idx, chunk in enumerate(chunks):
                doc_id = f"{filepath.stem}__chunk{chunk_idx}"
                ids.append(doc_id)
                documents.append(chunk)
                metadatas.append({"source": filepath.name, "chunk": chunk_idx})

            logger.info("Loaded runbook: %s (%d chunks)", filepath.name, len(chunks))
        except OSError as exc:
            logger.error("Failed to read runbook %s: %s", filepath, exc)

    return ids, documents, metadatas


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping character-level chunks."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def initialise_rag() -> None:
    """
    Initialise the ChromaDB persistent client and populate the runbooks collection.
    This function is idempotent: calling it multiple times is safe.
    Should be called once at application startup.
    """
    global _chroma_client, _collection

    settings = get_settings()
    persist_dir = os.path.abspath(settings.chroma_persist_dir)
    os.makedirs(persist_dir, exist_ok=True)

    logger.info("Initialising ChromaDB at %s", persist_dir)
    _chroma_client = chromadb.PersistentClient(path=persist_dir)

    embed_fn = _build_embedding_function()

    # Get or create collection
    _collection = _chroma_client.get_or_create_collection(
        name=settings.chroma_collection_name,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )

    # Only ingest documents if the collection is empty (avoid duplicates on restart)
    existing_count = _collection.count()
    if existing_count == 0:
        ids, documents, metadatas = _load_runbook_documents()
        if documents:
            _collection.add(ids=ids, documents=documents, metadatas=metadatas)
            logger.info(
                "Ingested %d document chunks into ChromaDB collection '%s'.",
                len(documents),
                settings.chroma_collection_name,
            )
        else:
            logger.warning("No runbook documents found to ingest.")
    else:
        logger.info(
            "ChromaDB collection '%s' already contains %d chunks — skipping ingestion.",
            settings.chroma_collection_name,
            existing_count,
        )


def query_runbooks(query: str, n_results: int = 3) -> str:
    """
    Retrieve the most semantically relevant runbook passages for a given query.

    Args:
        query: Natural-language query string describing the incident.
        n_results: Number of top chunks to retrieve.

    Returns:
        A single concatenated string of the most relevant runbook passages,
        or an informational message if no collection is available.
    """
    if _collection is None:
        logger.warning("RAG collection not initialised. Returning empty context.")
        return "No runbook knowledge base available."

    try:
        results = _collection.query(
            query_texts=[query],
            n_results=min(n_results, _collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        passages: list[str] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, distances):
            similarity = 1.0 - dist  # cosine distance -> similarity
            source = meta.get("source", "unknown")
            passages.append(
                f"[Source: {source} | Relevance: {similarity:.2f}]\n{doc}"
            )

        if not passages:
            return "No relevant runbook passages found."

        return "\n\n---\n\n".join(passages)

    except Exception as exc:
        logger.error("ChromaDB query failed: %s", exc, exc_info=True)
        return f"Runbook retrieval error: {exc}"


def get_collection_stats() -> dict:
    """Return basic statistics about the ChromaDB collection."""
    if _collection is None:
        return {"status": "not_initialised"}
    return {
        "collection": _collection.name,
        "document_count": _collection.count(),
        "status": "ready",
    }
