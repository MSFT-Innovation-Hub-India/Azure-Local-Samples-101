import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from reportlab.pdfgen import canvas

from assistant.app import create_app
from assistant.clients import (
    CompletionResult, DependencyError, EmbeddingClient, ModelClient, ModelRouter, TokenUsage, validate_vectors,
)
from assistant.config import (
    EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EMBEDDING_REVISION, ModelOption, Settings, load_models,
)
from assistant.indexer import active_collection, index_folder, read_corpus
from assistant.service import AssistantService, NO_EVIDENCE


def write_pdf(path: Path, pages: list[str]) -> None:
    output = canvas.Canvas(str(path))
    for text in pages:
        output.drawString(50, 750, text)
        output.showPage()
    output.save()


class FakeEmbedder:
    def embed(self, texts):
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.client = QdrantClient(":memory:")
        self.addCleanup(self.client.close)
        self.embedder = FakeEmbedder()

    def test_pdf_pages_and_idempotent_index(self):
        write_pdf(self.folder / "guide.pdf", ["The access code is mango.", "The backup code is plum."])
        result = index_folder(self.folder, self.client, self.embedder)
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(index_folder(self.folder, self.client, self.embedder)["status"], "unchanged")
        points, _ = self.client.scroll(active_collection(self.client), limit=10)
        self.assertEqual({point.payload["page"] for point in points}, {1, 2})
        self.assertTrue(all(point.payload["document"] == "guide.pdf" for point in points))

    def test_changed_and_deleted_documents_replace_active_snapshot(self):
        write_pdf(self.folder / "old.pdf", ["Old policy."])
        write_pdf(self.folder / "keep.pdf", ["Original policy."])
        first = index_folder(self.folder, self.client, self.embedder)
        (self.folder / "old.pdf").unlink()
        write_pdf(self.folder / "keep.pdf", ["Updated policy."])
        second = index_folder(self.folder, self.client, self.embedder)
        self.assertNotEqual(first["collection"], second["collection"])
        points, _ = self.client.scroll(active_collection(self.client))
        self.assertEqual(len(points), 1)
        self.assertIn("Updated", points[0].payload["text"])
        self.assertTrue(self.client.collection_exists(first["collection"]))

    def test_failed_embedding_preserves_active_index_and_cleans_candidate(self):
        write_pdf(self.folder / "guide.pdf", ["Original policy."])
        first = index_folder(self.folder, self.client, self.embedder)
        write_pdf(self.folder / "guide.pdf", ["Changed policy."])
        failing = Mock()
        failing.embed.side_effect = DependencyError("Embedding unavailable")
        with self.assertLogs("assistant.indexer", level="ERROR"):
            with self.assertRaises(DependencyError):
                index_folder(self.folder, self.client, failing)
        self.assertEqual(active_collection(self.client), first["collection"])
        self.assertEqual(len(self.client.get_collections().collections), 1)

    def test_unusable_pdf_does_not_replace_active_index(self):
        write_pdf(self.folder / "good.pdf", ["A valid document."])
        first = index_folder(self.folder, self.client, self.embedder)
        write_pdf(self.folder / "scan.pdf", [""])
        with self.assertRaisesRegex(ValueError, "OCR"):
            index_folder(self.folder, self.client, self.embedder)
        self.assertEqual(active_collection(self.client), first["collection"])

    def test_publication_timeout_does_not_delete_a_committed_index(self):
        write_pdf(self.folder / "guide.pdf", ["Original policy."])
        first = index_folder(self.folder, self.client, self.embedder)
        write_pdf(self.folder / "guide.pdf", ["New policy."])
        publish = self.client.update_collection_aliases

        def uncertain_publish(*args, **kwargs):
            publish(*args, **kwargs)
            raise TimeoutError("Response lost after commit")

        with patch.object(self.client, "update_collection_aliases", side_effect=uncertain_publish):
            with self.assertLogs("assistant.indexer", level="ERROR"):
                with self.assertRaises(TimeoutError):
                    index_folder(self.folder, self.client, self.embedder)
        current = active_collection(self.client)
        self.assertNotEqual(current, first["collection"])
        self.assertTrue(self.client.collection_exists(current))
        self.assertEqual(self.client.count(current).count, 1)

    def test_empty_corpus_requires_explicit_flag(self):
        with self.assertRaisesRegex(ValueError, "No PDFs"):
            index_folder(self.folder, self.client, self.embedder)
        result = index_folder(self.folder, self.client, self.embedder, allow_empty=True)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(self.client.count(active_collection(self.client)).count, 0)

    def test_corrupt_pdf_is_reported(self):
        (self.folder / "bad.pdf").write_bytes(b"not a PDF")
        with self.assertRaisesRegex(ValueError, "Could not read"):
            read_corpus(self.folder, False)

    def test_uppercase_extension_and_nested_filename(self):
        (self.folder / "policies").mkdir()
        write_pdf(self.folder / "policies" / "GUIDE.PDF", ["A useful policy."])
        _, chunks = read_corpus(self.folder, False)
        self.assertEqual(chunks[0].document, "policies/GUIDE.PDF")

    def test_qdrant_persistence_after_reopen(self):
        path = self.folder / "database"
        documents = self.folder / "documents"
        documents.mkdir()
        write_pdf(documents / "guide.pdf", ["Persistent policy."])
        client = QdrantClient(path=str(path))
        result = index_folder(documents, client, self.embedder)
        client.close()
        reopened = QdrantClient(path=str(path))
        try:
            self.assertEqual(active_collection(reopened), result["collection"])
            self.assertEqual(reopened.count(result["collection"]).count, 1)
        finally:
            reopened.close()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        write_pdf(folder / "operations.pdf", ["The service recovery code is mango."])
        self.database = QdrantClient(":memory:")
        self.addCleanup(self.database.close)
        self.embedder = Mock(wraps=FakeEmbedder())
        index_folder(folder, self.database, self.embedder)
        self.model = Mock()
        self.model.complete.return_value = CompletionResult(
            "The recovery code is mango. [1]", TokenUsage(120, 10, 130),
        )
        self.service = AssistantService(self.database, self.embedder, self.model)
        app = create_app(Settings(static_dir=str(folder / "absent")), self.service)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.token = self.client.get("/api/status").json()["csrf_token"]

    def post(self, path, body=None):
        return self.client.post(path, json=body, headers={"X-CSRF-Token": self.token})

    def test_chat_returns_sources_and_exact_prompt_context(self):
        result = self.post("/api/chat", {"question": "What is the recovery code?"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["sources"][0]["document"], "operations.pdf")
        self.assertEqual(result.json()["sources"][0]["page"], 1)
        self.assertTrue(result.json()["model_called"])
        self.assertEqual(result.json()["usage"], {
            "prompt_tokens": 120, "completion_tokens": 10, "total_tokens": 130,
        })
        prompt = self.model.complete.call_args.args[0]
        self.assertIn("mango", prompt[-1]["content"])
        self.assertIn("untrusted", prompt[0]["content"])

    def test_followup_retrieval_and_generation_include_previous_turns(self):
        self.post("/api/chat", {"question": "What is the recovery code?"})
        self.post("/api/chat", {"question": "Repeat that code."})
        query = self.embedder.embed.call_args.args[0][0]
        self.assertIn("What is the recovery code?", query)
        self.assertIn("Repeat that code.", query)
        prompt = self.model.complete.call_args.args[0]
        self.assertEqual(prompt[1]["content"], "What is the recovery code?")
        self.assertEqual(prompt[2]["role"], "assistant")

    def test_failed_turn_not_saved(self):
        self.model.complete.side_effect = DependencyError("Model unavailable")
        self.assertEqual(self.post("/api/chat", {"question": "Failed question"}).status_code, 503)
        self.model.complete.side_effect = None
        self.post("/api/chat", {"question": "Successful question"})
        self.assertNotIn("Failed question", str(self.model.complete.call_args))

    def test_new_topic_retrieval_does_not_reuse_old_questions(self):
        self.post("/api/chat", {"question": "What is the recovery code?"})
        self.post("/api/chat", {"question": "Who won the cricket world cup?"})
        self.assertEqual(self.embedder.embed.call_args.args[0], ["Who won the cricket world cup?"])

    def test_reset_clears_history(self):
        self.post("/api/chat", {"question": "First question"})
        self.assertEqual(self.post("/api/reset").status_code, 200)
        self.post("/api/chat", {"question": "Second question"})
        self.assertNotIn("First question", str(self.model.complete.call_args))

    def test_sessions_are_isolated(self):
        self.post("/api/chat", {"question": "Private first question"})
        self.client.cookies.clear()
        self.post("/api/chat", {"question": "Another browser"})
        self.assertNotIn("Private first question", str(self.model.complete.call_args))

    def test_no_evidence_does_not_call_model(self):
        self.embedder.embed.return_value = [[0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)]
        result = self.post("/api/chat", {"question": "Unrelated question"})
        self.assertEqual(result.json()["answer"], NO_EVIDENCE)
        self.assertEqual(result.json()["sources"], [])
        self.assertFalse(result.json()["model_called"])
        self.assertIsNone(result.json()["usage"])
        self.model.complete.assert_not_called()

    def test_missing_model_usage_is_not_reported_as_zero(self):
        self.model.complete.return_value = CompletionResult("A grounded answer without usage.")
        result = self.post("/api/chat", {"question": "What is the recovery code?"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["model_called"])
        self.assertIsNone(result.json()["usage"])

    def test_blank_question_rejected(self):
        self.assertEqual(self.post("/api/chat", {"question": "   "}).status_code, 422)

    def test_long_question_rejected(self):
        self.assertEqual(self.post("/api/chat", {"question": "x" * 1001}).status_code, 422)

    def test_missing_csrf_token_rejected(self):
        result = self.client.post("/api/chat", json={"question": "Hello"})
        self.assertEqual(result.status_code, 403)

    def test_cross_origin_rejected(self):
        result = self.client.post(
            "/api/chat", json={"question": "Hello"},
            headers={"X-CSRF-Token": self.token, "Origin": "https://other.invalid"},
        )
        self.assertEqual(result.status_code, 403)

    def test_no_index_reported(self):
        for alias in self.database.get_aliases().aliases:
            from qdrant_client import models
            self.database.update_collection_aliases([
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias.alias_name)),
            ])
        self.assertFalse(self.client.get("/api/status").json()["indexed"])
        self.assertEqual(self.post("/api/chat", {"question": "Hello"}).status_code, 503)

    def test_busy_request_is_explicit(self):
        self.service.lock.acquire()
        try:
            self.assertEqual(self.post("/api/chat", {"question": "Hello"}).status_code, 503)
        finally:
            self.service.lock.release()


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        write_pdf(folder / "operations.pdf", ["The service recovery code is mango."])
        self.database = QdrantClient(":memory:")
        self.addCleanup(self.database.close)
        index_folder(folder, self.database, FakeEmbedder())
        self.model = Mock()
        self.model.complete.return_value = CompletionResult("The code is mango. [1]")
        options = [
            ModelOption("small", "Small", "Fast.", "https://secret-url", "s", "/secret/key"),
            ModelOption("large", "Large", "Better."),
        ]
        service = AssistantService(self.database, FakeEmbedder(), self.model, options)
        self.client = TestClient(create_app(Settings(static_dir=str(folder / "absent")), service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.token = self.client.get("/api/status").json()["csrf_token"]

    def chat(self, **body):
        return self.client.post(
            "/api/chat", json={"question": "What is the recovery code?", **body},
            headers={"X-CSRF-Token": self.token},
        )

    def test_models_endpoint_hides_backend_details(self):
        payload = self.client.get("/api/models").json()
        self.assertEqual(payload["default_model"], "small")
        self.assertEqual(payload["models"][1], {"id": "large", "label": "Large", "description": "Better."})
        self.assertNotIn("secret", str(payload))

    def test_chat_uses_default_then_selected_model(self):
        first = self.chat()
        self.assertEqual(self.model.complete.call_args.args[1], "small")
        self.assertEqual(first.json()["model"]["label"], "Small")
        second = self.chat(model="large")
        self.assertEqual(self.model.complete.call_args.args[1], "large")
        self.assertEqual(second.json()["model"]["id"], "large")

    def test_history_is_not_shared_between_models(self):
        self.chat(model="small")
        self.chat(model="large")
        self.assertEqual(len(self.model.complete.call_args.args[0]), 2)

    def test_unknown_or_malformed_model_is_rejected(self):
        self.assertEqual(self.chat(model="missing").status_code, 404)
        self.assertEqual(self.chat(model="../etc").status_code, 422)
        self.model.complete.assert_not_called()


class ClientTests(unittest.TestCase):
    def test_router_dispatches_to_the_selected_model_and_rejects_unknown(self):
        small, large = Mock(), Mock()
        small.complete.return_value = CompletionResult("small")
        large.complete.return_value = CompletionResult("large")
        router = ModelRouter({"small": small, "large": large})
        self.assertEqual(router.complete([]).content, "small")
        self.assertEqual(router.complete([], "large").content, "large")
        with self.assertRaises(DependencyError):
            router.complete([], "missing")

    def test_model_catalog_loading_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "models.json"
            self.assertEqual(load_models(Settings(models_file=str(path)))[0].id, "default")
            entry = {"id": "a", "label": "A", "url": "https://a", "model_id": "m", "key_file": "/k"}
            path.write_text(json.dumps({"models": [entry, {**entry, "id": "b"}]}))
            self.assertEqual([m.id for m in load_models(Settings(models_file=str(path)))], ["a", "b"])
            for invalid in ({"models": []}, {"models": [entry, entry]}, {"models": [{**entry, "id": "Bad Id"}]}, {"x": 1}):
                path.write_text(json.dumps(invalid))
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    load_models(Settings(models_file=str(path)))
    def test_embedding_payload_and_revision_are_validated(self):
        response = {
            "model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION,
            "vectors": FakeEmbedder().embed(["a"]),
        }
        with httpx.Client(
            base_url="http://local",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
        ) as client:
            self.assertEqual(len(EmbeddingClient(client).embed(["a"])[0]), 384)
            response["revision"] = "different-revision"
            with self.assertRaisesRegex(DependencyError, "model"):
                EmbeddingClient(client).embed(["a"])

    def test_production_lifespan_closes_qdrant_without_context_protocol(self):
        database = QdrantClient(":memory:")
        with patch.object(database, "close", wraps=database.close) as close, patch(
            "assistant.app.qdrant_client", return_value=database,
        ), patch(
            "assistant.app.model_http_client", return_value=httpx.Client(),
        ), patch("assistant.clients.read_secret", return_value="test-key"):
            with TestClient(create_app(Settings(static_dir="absent"))) as browser:
                self.assertEqual(browser.get("/healthz").status_code, 200)
                self.assertFalse(browser.get("/api/status").json()["indexed"])
            close.assert_called_once()

    def test_embedding_model_mismatch_rejected(self):
        client = httpx.Client(
            base_url="http://local",
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"model": "wrong", "vectors": FakeEmbedder().embed(["a"])},
            )),
        )
        self.addCleanup(client.close)
        with self.assertRaisesRegex(DependencyError, "model"):
            EmbeddingClient(client).embed(["a"])

    def test_invalid_embedding_values_rejected(self):
        for values in (
            [[1.0]], [[0.0] * EMBEDDING_DIMENSIONS],
            [[float("nan")] * EMBEDDING_DIMENSIONS], [[True] * EMBEDDING_DIMENSIONS],
        ):
            with self.subTest(values=str(values)[:20]):
                with self.assertRaises(DependencyError):
                    validate_vectors(values, 1)

    def test_model_payload_and_errors(self):
        captured = []

        def handler(request):
            import json
            captured.append(json.loads(request.content))
            self.assertEqual(request.headers["api-key"], "test-key")
            return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            model = ModelClient(client, Settings(), "test-key")
            result = model.complete([{"role": "user", "content": "Hello"}])
            self.assertEqual(result.content, "Answer")
            self.assertIsNone(result.usage)
        self.assertEqual(captured[0]["max_tokens"], 256)
        self.assertEqual(captured[0]["messages"][0]["content"], "Hello")

    def test_model_usage_preserves_reported_counts_including_zero(self):
        for usage in (
            {"prompt_tokens": 1262, "completion_tokens": 2, "total_tokens": 1264},
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        ):
            with self.subTest(usage=usage), httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={
                    "choices": [{"message": {"content": "Answer"}}], "usage": usage,
                }),
            )) as client:
                result = ModelClient(client, Settings(), "test-key").complete([])
                self.assertEqual(result.usage, TokenUsage(**usage))

    def test_invalid_or_inconsistent_usage_is_explicit(self):
        valid = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        for usage in (
            {}, [], "12", {**valid, "prompt_tokens": True},
            {**valid, "completion_tokens": -1}, {**valid, "total_tokens": "12"},
            {**valid, "total_tokens": 12.0}, {**valid, "total_tokens": 99},
        ):
            with self.subTest(usage=usage), httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={
                    "choices": [{"message": {"content": "Answer"}}], "usage": usage,
                }),
            )) as client:
                with self.assertRaisesRegex(DependencyError, "token usage"):
                    ModelClient(client, Settings(), "test-key").complete([])

    def test_model_http_failure_is_explicit(self):
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(401),
        )) as client:
            with self.assertRaisesRegex(DependencyError, "Local model request failed"):
                ModelClient(client, Settings(), "test-key").complete([])


if __name__ == "__main__":
    unittest.main()
