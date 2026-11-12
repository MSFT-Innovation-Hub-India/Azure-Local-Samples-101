"""Development validation only: local UI/retrieval, existing remote Qwen model."""

import argparse
import base64
import json
import os
import ssl
import subprocess
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from assistant.app import create_app
from assistant.clients import ModelClient, validate_vectors
from assistant.config import Settings
from assistant.embeddings import load_model
from assistant.indexer import index_folder
from assistant.service import AssistantService


ROOT = Path(__file__).resolve().parents[1]


def get_resource(kind: str, name: str) -> dict:
    result = subprocess.run(
        [
            "kubectl", "--context", "zines-22325-aks", "get", kind, name,
            "-n", "foundry-local-operator", "-o", "json",
        ],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(result.stdout)


class LocalEmbedder:
    def __init__(self):
        os.environ["MODEL_CACHE"] = str(ROOT / ".local" / "models")
        os.environ["HF_HUB_OFFLINE"] = "1"
        self.model = load_model(offline=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return validate_vectors([vector.tolist() for vector in self.model.embed(texts)], len(texts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    print("DEVELOPMENT ONLY: UI, PDF indexing and vector search run on this computer.")
    print("Only Qwen inference runs on Azure Local through the existing port-forward.")
    ca = get_resource("configmap", "foundry-local-operator-ca-bundle")["data"]["ca.crt"]
    secret = get_resource("secret", "qwen2-5-0-5b-cpu-api-keys")["data"]["primary-key"]
    model_id = get_resource("modeldeployment", "qwen2-5-0-5b-cpu")["status"]["resolvedModel"]["name"]
    context = ssl.create_default_context(cadata=ca)

    def model_hostname(request: httpx.Request) -> None:
        # Connect to localhost while validating the real service certificate's DNS identity.
        request.extensions["sni_hostname"] = (
            "qwen2-5-0-5b-cpu.foundry-local-operator.svc.cluster.local"
        )

    settings = Settings(
        model_id=model_id, model_url="https://localhost:5000/v1/chat/completions",
        static_dir=str(ROOT / "frontend" / "dist"),
    )
    embedder = LocalEmbedder()
    with closing(QdrantClient(path=str(ROOT / ".local" / "preview-vectors"))) as database, httpx.Client(
        verify=context, timeout=180, trust_env=False,
        event_hooks={"request": [model_hostname]},
    ) as model_http:
        result = index_folder(ROOT / ".local" / "documents", database, embedder)
        print(json.dumps(result))
        service = AssistantService(
            database, embedder,
            ModelClient(model_http, settings, base64.b64decode(secret, validate=True).decode()),
        )
        app = create_app(settings, service)
        if args.check_only:
            with TestClient(app) as browser:
                token = browser.get("/api/status").json()["csrf_token"]
                for question in (
                    "What is the service recovery code?",
                    "What is that recovery code again?",
                ):
                    response = browser.post(
                        "/api/chat", json={"question": question},
                        headers={"X-CSRF-Token": token},
                    )
                    response.raise_for_status()
                    answer = response.json()
                    print(json.dumps({"question": question, **answer}))
                    if "mango" not in answer["answer"].lower():
                        raise RuntimeError("The live small model did not answer the synthetic recovery-code test correctly.")
                    if not any(
                        source["document"] == "demo-guide.pdf" and source["page"] == 1
                        for source in answer["sources"]
                    ):
                        raise RuntimeError("The response did not include the expected PDF/page reference.")
        else:
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
