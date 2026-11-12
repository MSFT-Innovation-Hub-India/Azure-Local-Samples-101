import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from assistant.app import create_app
from assistant.clients import CompletionResult, DependencyError
from assistant.config import Settings
from assistant.indexer import active_collection, index_folder
from assistant.scenarios import (
    CATALOG, MAX_PACKAGE_BYTES, ScenarioError, UnknownScenario, extract_package, get_scenario,
    list_scenarios, package_scenario, parse_published, publish_scenario, read_source, validate_definition,
)
from assistant.service import AssistantService
from tests.test_assistant import FakeEmbedder, write_pdf
from tools.deploy_assistant import ROOT
from tools.scenario import index_job, new_scenario


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = QdrantClient(":memory:")
        self.addCleanup(self.db.close)
        self.embedder = FakeEmbedder()
        self.model = Mock()
        self.model.complete.return_value = CompletionResult("A grounded answer.")

    def source(self, name="retail", text="The approval code is amber.", prompt="You are a retail policy assistant."):
        folder = new_scenario(name, name.title(), self.root)
        (folder / "system-prompt.md").write_text(prompt, encoding="utf-8")
        write_pdf(folder / "documents" / f"{name}.pdf", [text])
        return folder

    def package(self, folder):
        return package_scenario(folder, self.root / "packages")

    def publish(self, folder):
        return publish_scenario(self.package(folder), self.db, self.embedder)

    def test_scaffold_and_package_round_trip_excludes_unrelated_files(self):
        folder = self.source()
        (folder / ".env").write_text("not-part-of-the-package", encoding="utf-8")
        archive = self.package(folder)
        with zipfile.ZipFile(archive) as source:
            self.assertEqual(set(source.namelist()), {"scenario.json", "system-prompt.md", "documents/retail.pdf"})
        output = self.root / "expanded"
        output.mkdir()
        extract_package(archive, output)
        definition, prompt, files = read_source(output)
        self.assertEqual(definition.id, "retail")
        self.assertIn("retail policy", prompt)
        self.assertEqual(len(files), 3)

    def test_invalid_slug_schema_and_empty_questions_are_rejected(self):
        valid = {"schema_version": 1, "id": "retail", "name": "Retail", "description": "Returns"}
        for update in (
            {"id": "../escape"}, {"id": "Retail"}, {"schema_version": 2},
            {"schema_version": True}, {"name": " "}, {"example_questions": [""]},
            {"example_questions": ["x" * 201]}, {"unknown_option": True},
        ):
            with self.subTest(update=update), self.assertRaises(ScenarioError):
                validate_definition({**valid, **update})

    def test_missing_documents_unsupported_formats_and_prompt_limits(self):
        folder = new_scenario("retail", "Retail", self.root)
        with self.assertRaisesRegex(ScenarioError, "1-200"):
            self.package(folder)
        (folder / "documents" / "manual.docx").write_bytes(b"unsupported")
        with self.assertRaisesRegex(ScenarioError, "Only PDF"):
            self.package(folder)
        (folder / "documents" / "manual.docx").unlink()
        write_pdf(folder / "documents" / "retail.pdf", ["Valid PDF"])
        (folder / "system-prompt.md").write_text("x" * 2001)
        with self.assertRaisesRegex(ScenarioError, "1-2000"):
            self.package(folder)

    def test_unsafe_duplicate_noncanonical_and_linked_zip_entries_are_rejected(self):
        for name in ("../escape.pdf", "/absolute.pdf", "documents/../escape.pdf",
                     "documents\\escape.pdf", "documents/./retail.pdf", "documents//retail.pdf", ".env"):
            archive = self.root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("scenario.json", "{}")
                output.writestr("system-prompt.md", "Prompt")
                entry = zipfile.ZipInfo("entry.pdf")
                entry.filename = name
                output.writestr(entry, "Not a PDF")
            with self.subTest(name=name), self.assertRaises(ScenarioError):
                extract_package(archive, self.root / "unsafe-output")
        archive = self.root / "link.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("scenario.json", "{}")
            output.writestr("system-prompt.md", "Prompt")
            link = zipfile.ZipInfo("documents/link.pdf")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(link, "/private")
        with self.assertRaises(ScenarioError):
            extract_package(archive, self.root / "linked")
        archive = self.root / "duplicate.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("scenario.json", "{}")
            output.writestr("system-prompt.md", "Prompt")
            output.writestr("documents/retail.pdf", "one")
            output.writestr("documents/RETAIL.pdf", "two")
        with self.assertRaises(ScenarioError):
            extract_package(archive, self.root / "duplicates")

    def test_package_size_limit_is_enforced_before_upload(self):
        folder = self.source()
        with patch("assistant.scenarios.MAX_PACKAGE_BYTES", 10):
            with self.assertRaisesRegex(ScenarioError, "48 MiB"):
                self.package(folder)
        self.assertEqual(MAX_PACKAGE_BYTES, 48 * 1024 * 1024)

    def test_existing_operations_remains_available_when_adding_scenarios(self):
        original = self.root / "legacy"
        original.mkdir()
        write_pdf(original / "operations.pdf", ["The service recovery code is mango."])
        indexed = index_folder(original, self.db, self.embedder)
        self.publish(self.source())
        self.assertEqual({scenario.id for scenario in list_scenarios(self.db)}, {"operations", "retail"})
        self.assertEqual(get_scenario(self.db, "operations").collection, indexed["collection"])
        self.assertEqual(active_collection(self.db), indexed["collection"])

    def test_scenario_metadata_cannot_reference_another_domains_snapshot(self):
        self.publish(self.source())
        payload = get_scenario(self.db, "retail").model_dump()
        payload["collection"] = "assistant-manufacturing-pdf-" + "a" * 24 + "-" + "b" * 8
        with self.assertRaisesRegex(ScenarioError, "invalid index snapshot"):
            parse_published(payload)

    def test_scenario_indexes_prompts_and_histories_are_isolated(self):
        retail = self.source()
        manufacturing = self.source("manufacturing", "The approval code is cobalt.", "You are a maintenance planning assistant.")
        self.publish(retail)
        self.publish(manufacturing)
        service = AssistantService(self.db, self.embedder, self.model)
        first = service.answer("same-browser", "Retail question", "retail")
        second = service.answer("same-browser", "What is the approval code?", "manufacturing")
        self.assertEqual({source["document"] for source in first["sources"]}, {"retail.pdf"})
        self.assertEqual({source["document"] for source in second["sources"]}, {"manufacturing.pdf"})
        prompt = self.model.complete.call_args.args[0]
        self.assertIn("maintenance planning", prompt[0]["content"])
        self.assertIn("untrusted", prompt[0]["content"])
        self.assertNotIn("Retail question", str(prompt))
        self.assertNotIn("amber", str(prompt))
        service.reset("same-browser", "manufacturing")
        self.assertEqual({key[1] for key in service.sessions}, {"retail"})

    def test_prompt_only_update_reuses_vectors_and_starts_new_history(self):
        folder = self.source()
        first = self.publish(folder)
        service = AssistantService(self.db, self.embedder, self.model)
        service.answer("browser", "Old conversation", "retail")
        (folder / "system-prompt.md").write_text("Use the revised retail support style.")
        second = self.publish(folder)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["collection"], second["collection"])
        self.assertNotEqual(first["revision"], second["revision"])
        service.answer("browser", "New conversation", "retail")
        prompt = self.model.complete.call_args.args[0]
        self.assertIn("revised retail", prompt[0]["content"])
        self.assertNotIn("Old conversation", str(prompt))

    def test_unchanged_package_is_idempotent_and_deleted_documents_leave_active_index(self):
        folder = self.source()
        write_pdf(folder / "documents" / "removed.pdf", ["A temporary retail rule."])
        first = self.publish(folder)
        repeat = self.publish(folder)
        self.assertEqual(first["revision"], repeat["revision"])
        self.assertEqual(repeat["status"], "unchanged")
        (folder / "documents" / "removed.pdf").unlink()
        updated = self.publish(folder)
        self.assertEqual(updated["chunks"], 1)
        points, _ = self.db.scroll(get_scenario(self.db, "retail").collection)
        self.assertEqual({point.payload["document"] for point in points}, {"retail.pdf"})
        self.assertTrue(self.db.collection_exists(first["collection"]))

    def test_failed_index_does_not_replace_published_prompt_or_snapshot(self):
        folder = self.source()
        self.publish(folder)
        old = get_scenario(self.db, "retail")
        write_pdf(folder / "documents" / "retail.pdf", ["A changed policy."])
        (folder / "system-prompt.md").write_text("New domain prompt.")
        broken = Mock()
        broken.embed.side_effect = DependencyError("Embedding failed")
        with self.assertLogs("assistant.indexer", level="ERROR"), self.assertRaises(DependencyError):
            publish_scenario(self.package(folder), self.db, broken)
        self.assertEqual(get_scenario(self.db, "retail"), old)

    def test_catalog_failure_and_uncertain_commit_preserve_coherent_records(self):
        folder = self.source()
        self.publish(folder)
        old = get_scenario(self.db, "retail")
        (folder / "system-prompt.md").write_text("Revised prompt for the same PDFs.")
        with patch.object(self.db, "upsert", side_effect=TimeoutError("Catalog unavailable")):
            with self.assertRaises(TimeoutError):
                self.publish(folder)
        self.assertEqual(get_scenario(self.db, "retail"), old)
        original = self.db.upsert

        def committed_then_timeout(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError("Response lost")

        with patch.object(self.db, "upsert", side_effect=committed_then_timeout):
            with self.assertRaises(TimeoutError):
                self.publish(folder)
        current = get_scenario(self.db, "retail")
        self.assertEqual(current.system_prompt, "Revised prompt for the same PDFs.")
        self.assertTrue(self.db.collection_exists(current.collection))

    def test_operations_package_migration_pins_legacy_before_failure(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        write_pdf(legacy / "operations.pdf", ["Original operations policy."])
        first = index_folder(legacy, self.db, self.embedder)
        operations = self.source("operations", "Replacement operations policy.", "New operations role.")
        broken = Mock()
        broken.embed.side_effect = DependencyError("Embedding failed")
        with self.assertLogs("assistant.indexer", level="ERROR"), self.assertRaises(DependencyError):
            publish_scenario(self.package(operations), self.db, broken)
        self.assertEqual(get_scenario(self.db, "operations").collection, first["collection"])
        self.assertEqual(get_scenario(self.db, "operations").system_prompt, "")

    def test_api_scenario_selection_and_unknown_ids_never_fall_back(self):
        self.publish(self.source())
        service = AssistantService(self.db, self.embedder, self.model)
        with TestClient(create_app(Settings(static_dir="absent"), service)) as client:
            catalog = client.get("/api/scenarios").json()
            self.assertEqual({scenario["id"] for scenario in catalog["scenarios"]}, {"operations", "retail"})
            self.assertNotIn("system_prompt", str(catalog))
            status = client.get("/api/status?scenario=retail").json()
            self.assertEqual(status["chunks"], 1)
            token = status["csrf_token"]
            answer = client.post("/api/chat", json={"scenario": "retail", "question": "Approval code?"},
                                 headers={"X-CSRF-Token": token})
            self.assertEqual(answer.status_code, 200)
            self.assertEqual(answer.json()["scenario"], "retail")
            self.assertEqual(client.get("/api/status?scenario=unknown").status_code, 404)
            missing = client.post("/api/chat", json={"scenario": "unknown", "question": "Code?"},
                                  headers={"X-CSRF-Token": token})
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(client.get("/api/status?scenario=../retail").status_code, 422)

    def test_index_job_inherits_dns_credentials_image_and_read_only_documents(self):
        import yaml
        job = yaml.safe_load((ROOT / "deploy" / "index-job.yaml").read_text())
        kubectl = Mock()
        kubectl.run.return_value = json.dumps(job)
        image = "demo.azurecr.io/app@sha256:" + "1" * 64
        app = {"spec": {"template": {"spec": {
            "containers": [{"image": image}], "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["10.96.214.2"]},
            "imagePullSecrets": [{"name": "assistant-registry"}],
        }}}}
        rendered = index_job(kubectl, app, "/documents/.scenario-packages/retail.zip")
        spec = rendered["spec"]["template"]["spec"]
        self.assertEqual(spec["dnsConfig"], app["spec"]["template"]["spec"]["dnsConfig"])
        self.assertEqual(spec["containers"][0]["image"], image)
        self.assertEqual(spec["containers"][0]["command"][-2:], ["--scenario-package", "/documents/.scenario-packages/retail.zip"])
        mount = next(m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "documents")
        self.assertTrue(mount["readOnly"])


if __name__ == "__main__":
    unittest.main()
