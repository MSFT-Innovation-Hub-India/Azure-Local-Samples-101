import argparse
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from contextlib import closing
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from qdrant_client import QdrantClient, models

from assistant.clients import Embedder, EmbeddingClient, qdrant_client, validate_vectors
from assistant.config import (
    CHUNK_OVERLAP, CHUNK_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EMBEDDING_REVISION,
    INDEX_ALIAS, Settings,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    document: str
    page: int
    number: int
    text: str
    content_hash: str


def extract_chunks(content: bytes, relative: str, content_hash: str) -> list[Chunk]:
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError(f"Encrypted PDF is not supported: {relative}")
        if len(reader.pages) > 200:
            raise ValueError(f"PDF exceeds 200 pages: {relative}")
        chunks = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP):
                part = text[start:start + CHUNK_SIZE]
                if part.strip():
                    chunks.append(Chunk(
                        relative, page_number, len(chunks), part, content_hash,
                    ))
                if start + CHUNK_SIZE >= len(text):
                    break
    except (PdfReadError, OSError) as exc:
        raise ValueError(f"Could not read PDF: {relative}") from exc
    if not chunks:
        raise ValueError(f"No usable text in PDF; scanned documents need OCR: {relative}")
    return chunks


def read_corpus(folder: Path, allow_empty: bool) -> tuple[str, list[Chunk]]:
    if not folder.is_dir():
        raise ValueError(f"PDF folder does not exist: {folder}")
    paths = sorted(
        (path for path in folder.rglob("*") if path.suffix.lower() == ".pdf"),
        key=lambda path: path.relative_to(folder).as_posix(),
    )
    if not paths and not allow_empty:
        raise ValueError("No PDFs found; use --allow-empty only to intentionally clear the index.")
    if len(paths) > 200:
        raise ValueError("Demo corpus exceeds 200 PDFs.")
    manifest = {
        "model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION, "dimensions": EMBEDDING_DIMENSIONS,
        "chunk_size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP,
        "extractor": f"pypdf-{version('pypdf')}-v1", "documents": [],
    }
    chunks = []
    root = folder.resolve()
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("PDF symlinks or paths outside the source folder are not allowed.")
        relative = path.relative_to(folder).as_posix()
        with path.open("rb") as source:
            content = source.read(25 * 1024 * 1024 + 1)
        if len(content) > 25 * 1024 * 1024:
            raise ValueError(f"PDF exceeds 25 MiB: {path.name}")
        digest = hashlib.sha256(content).hexdigest()
        manifest["documents"].append([relative, digest])
        chunks.extend(extract_chunks(content, relative, digest))
        if len(chunks) > 5000:
            raise ValueError("Demo corpus exceeds 5000 chunks.")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return digest, chunks


def active_collection(client: QdrantClient, alias_name: str = INDEX_ALIAS) -> str | None:
    return next((
        alias.collection_name for alias in client.get_aliases().aliases
        if alias.alias_name == alias_name
    ), None)


def index_folder(
    folder: Path, client: QdrantClient, embedder: Embedder, *, allow_empty: bool = False,
    alias: str = INDEX_ALIAS, prefix: str = "assistant-pdf",
) -> dict:
    fingerprint, chunks = read_corpus(folder, allow_empty)
    current = active_collection(client, alias)
    if current and current.startswith(f"{prefix}-{fingerprint[:24]}-"):
        return {"status": "unchanged", "chunks": len(chunks), "collection": current}
    # A unique candidate prevents failed/concurrent builds from corrupting an active snapshot.
    candidate = f"{prefix}-{fingerprint[:24]}-{uuid.uuid4().hex[:8]}"
    client.create_collection(
        candidate,
        vectors_config=models.VectorParams(
            size=EMBEDDING_DIMENSIONS, distance=models.Distance.COSINE,
        ),
    )
    publishing = False
    try:
        for start in range(0, len(chunks), 16):
            batch = chunks[start:start + 16]
            vectors = validate_vectors(embedder.embed([chunk.text for chunk in batch]), len(batch))
            points = [
                models.PointStruct(
                    id=str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{chunk.document}:{chunk.content_hash}:{chunk.page}:{chunk.number}",
                    )),
                    vector=vector,
                    payload={
                        "document": chunk.document, "page": chunk.page,
                        "chunk": chunk.number, "text": chunk.text,
                        "content_hash": chunk.content_hash,
                        "embedding_model": EMBEDDING_MODEL,
                        "embedding_revision": EMBEDDING_REVISION,
                    },
                )
                for chunk, vector in zip(batch, vectors)
            ]
            client.upsert(candidate, points=points, wait=True)
        operations = []
        if current:
            operations.append(models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=alias),
            ))
        operations.append(models.CreateAliasOperation(
            create_alias=models.CreateAlias(
                collection_name=candidate, alias_name=alias,
            ),
        ))
        publishing = True
        client.update_collection_aliases(change_aliases_operations=operations)
    except Exception:
        if publishing:
            # A timeout can follow a committed alias update; deleting here could destroy the active index.
            logger.exception("Index publication outcome is uncertain; retain candidate and inspect the active alias")
        else:
            logger.exception("Indexing failed; previous active snapshot is preserved")
            client.delete_collection(candidate)
        raise
    return {"status": "indexed", "chunks": len(chunks), "collection": candidate}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a local PDF index snapshot.")
    parser.add_argument("--folder", type=Path, default=Path("/documents"))
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--scenario-package", type=Path, help="Validate, index and publish a complete scenario ZIP.")
    args = parser.parse_args()
    if args.scenario_package and (args.allow_empty or args.folder != Path("/documents")):
        parser.error("--scenario-package cannot be combined with --folder or --allow-empty.")
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    with closing(qdrant_client(settings)) as database, httpx.Client(
        base_url=settings.embedding_url, timeout=120, trust_env=False,
    ) as http:
        if args.scenario_package:
            from assistant.scenarios import publish_scenario
            result = publish_scenario(args.scenario_package, database, EmbeddingClient(http))
        else:
            result = index_folder(
                args.folder, database, EmbeddingClient(http), allow_empty=args.allow_empty,
            )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
