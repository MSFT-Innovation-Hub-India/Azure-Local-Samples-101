"""Author, package and publish additive PDF scenarios without rebuilding the app."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from assistant.scenarios import extract_package, package_scenario, read_source, validate_definition
from tools.deploy_assistant import (
    Kubectl, LABEL, NAMESPACE, OWNER, ROOT, choose_node, ensure_owned, manifest_resources, metadata,
)


def new_scenario(scenario_id: str, name: str, destination: Path) -> Path:
    definition = validate_definition({
        "schema_version": 1, "id": scenario_id, "name": name,
        "description": f"Answers grounded in the {name} scenario documents.",
        "example_questions": ["What are the key requirements?"],
    })
    folder = destination / scenario_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "documents").mkdir()
    (folder / "documents" / ".gitkeep").touch()
    (folder / "scenario.json").write_text(json.dumps(definition.model_dump(), indent=2) + "\n", encoding="utf-8")
    (folder / "system-prompt.md").write_text(
        f"You assist users with {name}. Explain domain terms clearly. "
        "Use only the supplied sources and acknowledge missing information.\n", encoding="utf-8",
    )
    return folder


def index_job(kubectl: Kubectl, app: dict, remote_package: str) -> dict:
    job = manifest_resources(kubectl.run(
        "create", "--dry-run=client", "--validate=false",
        "-f", str(ROOT / "deploy" / "index-job.yaml"), "-o", "json",
    ))[0]
    source = app["spec"]["template"]["spec"]
    spec = job["spec"]["template"]["spec"]
    for key in ("dnsPolicy", "dnsConfig"):
        if not source.get(key):
            raise RuntimeError(f"Deployed app is missing {key}; run the guarded core deployment first.")
        spec[key] = source[key]
    if source.get("imagePullSecrets"):
        spec["imagePullSecrets"] = source["imagePullSecrets"]
    container = spec["containers"][0]
    container["image"] = source["containers"][0]["image"]
    if ".azurecr.io/" not in container["image"] or "@sha256:" not in container["image"]:
        raise RuntimeError("The indexer must reuse the approved immutable ACR app image.")
    container["command"] = ["python", "-m", "assistant.indexer", "--scenario-package", remote_package]
    return job


def deploy_package(package: Path, context: str, timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="scenario-check-") as temporary:
        folder = Path(temporary)
        extract_package(package, folder)
        definition, _, _ = read_source(folder)
    kubectl = Kubectl(context)
    namespace = kubectl.get("namespace", NAMESPACE)
    if not namespace:
        raise RuntimeError("Deploy the shared assistant core before publishing a scenario.")
    ensure_owned(namespace, "Namespace", NAMESPACE)
    app = kubectl.get("deployment", "assistant-web", NAMESPACE)
    helper = kubectl.get("deployment", "documents", NAMESPACE)
    pvc = kubectl.get("pvc", "pdf-documents", NAMESPACE)
    for resource, kind, name in (
        (app, "Deployment", "assistant-web"), (helper, "Deployment", "documents"),
        (pvc, "PersistentVolumeClaim", "pdf-documents"),
    ):
        if not resource:
            raise RuntimeError(f"Shared assistant resource is missing: {kind}/{name}")
        ensure_owned(resource, kind, name)
    if pvc["status"].get("phase") != "Bound":
        raise RuntimeError("Document storage is not Bound.")
    _, report = choose_node(kubectl.get("nodes")["items"], kubectl.get("pods")["items"])
    print(report, file=sys.stderr)
    active = kubectl.get("jobs", namespace=NAMESPACE)["items"]
    if any(job.get("status", {}).get("active", 0) for job in active):
        raise RuntimeError("An indexing Job is active. Wait for it before publishing another scenario.")
    # An atomic namespaced lock prevents two publishers passing the active-Job check together.
    lock_name = "scenario-publication-lock"
    if kubectl.get("configmap", lock_name, NAMESPACE):
        raise RuntimeError("Another scenario publisher holds the publication lock; inspect it before retrying.")
    lock = {
        "apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(lock_name),
        "data": {"scenario": definition.id, "publisher": uuid.uuid4().hex},
    }
    kubectl.run("create", "-f", "-", payload=lock)
    job_name = None
    try:
        pods = kubectl.get("pods", namespace=NAMESPACE)["items"]
        pod = next((
            pod for pod in pods
            if pod["metadata"].get("labels", {}).get("app") == "documents"
            and pod["metadata"].get("labels", {}).get(LABEL) == OWNER
            and not pod["metadata"].get("deletionTimestamp")
            and any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
        ), None)
        if not pod:
            raise RuntimeError("No Ready owned document-transfer pod is available.")
        remote_name = f"{definition.id}-{uuid.uuid4().hex}.zip"
        remote_package = f"/documents/.scenario-packages/{remote_name}"
        kubectl.run(
            "exec", pod["metadata"]["name"], "-n", NAMESPACE, "--", "python", "-c",
            "from pathlib import Path; p=Path('/documents/.scenario-packages'); "
            "p.mkdir(exist_ok=True); assert not p.is_symlink() and p.resolve().is_relative_to(Path('/documents').resolve())",
        )
        # kubectl interprets a drive-letter colon as a remote path: use a relative Windows path.
        relative = os.path.relpath(package.resolve(), Path.cwd())
        kubectl.run("cp", relative, f"{NAMESPACE}/{pod['metadata']['name']}:{remote_package}", "-c", "documents")
        rendered = index_job(kubectl, app, remote_package)
        created = json.loads(kubectl.run("create", "-f", "-", "-o", "json", payload=rendered))
        job_name = created["metadata"]["name"]
        print(f"Indexing {definition.id} in {NAMESPACE}/job/{job_name}", file=sys.stderr)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = kubectl.get("job", job_name, NAMESPACE)
            if not job:
                raise RuntimeError("The indexing Job disappeared before publication could be verified.")
            conditions = {condition["type"]: condition["status"] for condition in job.get("status", {}).get("conditions", [])}
            if conditions.get("Failed") == "True":
                raise RuntimeError(f"Indexing failed; inspect kubectl logs -n {NAMESPACE} job/{job_name}. Previous scenario remains published.")
            if conditions.get("Complete") == "True":
                lines = kubectl.run("logs", f"job/{job_name}", "-n", NAMESPACE).splitlines()
                result = json.loads(next(line for line in reversed(lines) if line.startswith("{")))
                if result.get("scenario") != definition.id:
                    raise RuntimeError("Indexing result scenario does not match the uploaded package.")
                return {**result, "job": job_name, "stored_package": remote_package}
            time.sleep(2)
        raise RuntimeError(f"Publication wait timed out; inspect job/{job_name}. It may still complete.")
    finally:
        # Keep the lock if a timed-out Job is still running; never permit overlapping publications.
        job = kubectl.get("job", job_name, NAMESPACE) if job_name else None
        terminal = not job or any(
            condition["type"] in {"Complete", "Failed"} and condition["status"] == "True"
            for condition in job.get("status", {}).get("conditions", [])
        )
        if terminal:
            current = kubectl.get("configmap", lock_name, NAMESPACE)
            if current and current.get("data") == lock["data"]:
                ensure_owned(current, "ConfigMap", lock_name)
                kubectl.run("delete", "configmap", lock_name, "-n", NAMESPACE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Scaffold a scenario definition, prompt and documents folder.")
    new.add_argument("id")
    new.add_argument("--name", required=True)
    new.add_argument("--destination", type=Path, default=ROOT / "scenarios")
    package = commands.add_parser("package", help="Build a bounded ZIP containing only scenario inputs.")
    package.add_argument("folder", type=Path)
    package.add_argument("--output", type=Path, default=ROOT / ".local" / "scenario-packages")
    deploy = commands.add_parser("deploy", help="Upload a package and publish it through an in-cluster Job.")
    deploy.add_argument("package", type=Path)
    deploy.add_argument("--context", default="zines-22325-aks")
    deploy.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    try:
        if args.command == "new":
            result = {"folder": str(new_scenario(args.id, args.name, args.destination))}
        elif args.command == "package":
            result = {"package": str(package_scenario(args.folder, args.output))}
        else:
            if not 1 <= args.timeout <= 1800:
                raise ValueError("Publication timeout must be between 1 and 1800 seconds.")
            result = deploy_package(args.package, args.context, args.timeout)
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        print(f"Scenario operation stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
