"""Guarded, additive deployment. By default this script is read-only."""

import argparse
import base64
import ipaddress
import json
import re
import secrets
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "assistant-demo"
OWNER = "local-pdf-assistant"
LABEL = "app.kubernetes.io/part-of"
MIB = 1024 ** 2


def quantity(value: str) -> Decimal:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([numkKMGTPE]i?)?", str(value))
    if not match:
        raise ValueError(f"Unsupported Kubernetes resource quantity: {value}")
    number, suffix = match.groups()
    factors = {
        None: Decimal(1), "n": Decimal("0.000000001"),
        "u": Decimal("0.000001"), "m": Decimal("0.001"),
        **{suffix: Decimal(1000) ** power for power, suffix in enumerate("kMGTPE", 1)},
        "K": Decimal(1000),
        **{suffix + "i": Decimal(1024) ** power for power, suffix in enumerate("KMGTPE", 1)},
    }
    return Decimal(number) * factors[suffix]


def pod_request(pod: dict, resource: str) -> Decimal:
    spec = pod["spec"]
    regular = sum(
        (quantity(container.get("resources", {}).get("requests", {}).get(resource, "0"))
         for container in spec.get("containers", [])), Decimal(0),
    )
    sidecars = Decimal(0)
    peak = Decimal(0)
    for container in spec.get("initContainers", []):
        request = quantity(container.get("resources", {}).get("requests", {}).get(resource, "0"))
        if container.get("restartPolicy") == "Always":
            sidecars += request
            peak = max(peak, sidecars)
        else:
            peak = max(peak, sidecars + request)
    overhead = quantity(spec.get("overhead", {}).get(resource, "0"))
    return max(regular + sidecars, peak) + overhead


class Kubectl:
    def __init__(self, context: str):
        self.context = context

    def run(self, *args: str, payload: dict | None = None) -> str:
        result = subprocess.run(
            ["kubectl", "--context", self.context, *args],
            input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, timeout=90, check=False,
        )
        if result.returncode:
            # Never include stdin or raw API diagnostics: they can contain Secret values.
            raise RuntimeError(f"kubectl {args[0]} failed (exit {result.returncode}). Check context, RBAC and API availability.")
        return result.stdout.strip()

    def get(self, kind: str, name: str | None = None, namespace: str | None = None):
        args = ["get", kind]
        if name:
            args.extend([name, "--ignore-not-found"])
        if namespace:
            args.extend(["-n", namespace])
        elif kind == "pods" and not name:
            args.append("--all-namespaces")
        args.extend(["-o", "json"])
        output = self.run(*args)
        return json.loads(output) if output else None


def choose_node(nodes: list[dict], pods: list[dict]) -> tuple[str, str]:
    # Reserve every demo workload, including one concurrent indexer, plus 1 GiB headroom.
    required_memory = 2496 * MIB
    required_cpu = Decimal("0.710")
    reports = []
    for node in nodes:
        name = node["metadata"]["name"]
        conditions = {condition["type"]: condition["status"] for condition in node["status"]["conditions"]}
        if node["spec"].get("unschedulable") or any(
            taint["effect"] in {"NoSchedule", "NoExecute"} for taint in node["spec"].get("taints", [])
        ) or conditions.get("Ready") != "True" or conditions.get("MemoryPressure") == "True":
            continue
        existing = [
            pod for pod in pods
            if pod["spec"].get("nodeName") == name
            and pod["status"].get("phase") not in {"Succeeded", "Failed"}
            and not (
                pod["metadata"].get("namespace") == NAMESPACE
                and pod["metadata"].get("labels", {}).get(LABEL) == OWNER
            )
        ]
        used_memory = sum((pod_request(pod, "memory") for pod in existing), Decimal(0))
        used_cpu = sum((pod_request(pod, "cpu") for pod in existing), Decimal(0))
        free_memory = quantity(node["status"]["allocatable"]["memory"]) - used_memory
        free_cpu = quantity(node["status"]["allocatable"]["cpu"]) - used_cpu
        reports.append(f"{name}: {free_memory / MIB:.0f} MiB and {free_cpu:.3f} CPU unreserved")
        if free_memory >= required_memory + 1024 * MIB and free_cpu >= required_cpu + Decimal("0.250"):
            return name, "\n".join(reports)
    raise RuntimeError(
        "Insufficient safe worker capacity. Need 2496 MiB + 1024 MiB headroom and "
        "0.710 CPU + 0.250 CPU headroom on one Ready, untainted worker. "
        "Do not change existing workloads or use the control plane.\n" + "\n".join(reports)
    )


def metadata(name: str) -> dict:
    return {"name": name, "namespace": NAMESPACE, "labels": {LABEL: OWNER}}


def ensure_owned(resource: dict | None, kind: str, name: str) -> None:
    if resource and resource["metadata"].get("labels", {}).get(LABEL) != OWNER:
        raise RuntimeError(f"Refusing to overwrite an existing, unowned {kind}/{name}.")


def registry_config(raw: bytes, registry: str) -> str:
    if len(raw) > 65536:
        raise ValueError("Registry credential JSON exceeds 64 KiB.")
    config = json.loads(raw)
    if not isinstance(config, dict) or not isinstance(config.get("auths"), dict) or set(config["auths"]) != {registry}:
        raise ValueError("Registry credentials must contain only the selected ACR login server.")
    entry = config["auths"][registry]
    if not isinstance(entry, dict) or not isinstance(entry.get("auth"), str) or not entry["auth"]:
        raise ValueError("Registry credentials require a nonempty auth entry.")
    decoded = base64.b64decode(entry["auth"], validate=True).decode("utf-8")
    username, separator, password = decoded.partition(":")
    if not separator or not username or not password:
        raise ValueError("Registry auth must encode a nonempty username and password.")
    return base64.b64encode(raw).decode("ascii")


def manifest_resources(output: str) -> list[dict]:
    resources = []
    decoder = json.JSONDecoder()
    remaining = output.lstrip()
    while remaining:
        resource, end = decoder.raw_decode(remaining)
        if not isinstance(resource, dict):
            raise ValueError("kubectl returned an invalid manifest object.")
        resources.extend(resource["items"] if resource.get("kind") == "List" else [resource])
        remaining = remaining[end:].lstrip()
    if not resources:
        raise ValueError("kubectl returned no manifest resources.")
    return resources


def prepare(args, kubectl: Kubectl) -> tuple[list[dict], str]:
    namespace = kubectl.get("namespace", NAMESPACE)
    ensure_owned(namespace, "namespace", NAMESPACE)
    node, report = choose_node(kubectl.get("nodes")["items"], kubectl.get("pods", namespace=None)["items"])
    print(report)
    storage = kubectl.get("storageclass", args.storage_class)
    if storage is None:
        raise RuntimeError("The requested StorageClass does not exist.")
    if storage.get("provisioner") != "azlocal.csi.azure.com":
        raise RuntimeError("Verify block-backed Azure Local storage before using another provisioner.")
    dns_service = kubectl.get("service", args.dns_service, "kube-system")
    if not dns_service:
        raise RuntimeError("The CoreDNS Service is missing. Set --dns-service to its name in kube-system.")
    dns_ip = str(ipaddress.ip_address(dns_service["spec"]["clusterIP"]))
    if not args.app_image or not args.embeddings_image or not args.qdrant_image:
        raise RuntimeError("Supply the three approved ACR image references to prepare a deployment.")
    registries = set()
    for image in (args.app_image, args.embeddings_image, args.qdrant_image):
        if "@sha256:" not in image:
            raise RuntimeError("Use immutable image digest references, not mutable tags.")
        registry = image.split("/")[0]
        if not registry.endswith(".azurecr.io"):
            raise RuntimeError("All runtime images must come from the approved ACR.")
        registries.add(registry)
    if len(registries) != 1:
        raise RuntimeError("Use a single approved ACR for this demo.")
    credentials = None
    if args.registry_config or args.registry_config_stdin:
        raw = (
            sys.stdin.buffer.read(65537) if args.registry_config_stdin
            else Path(args.registry_config).read_bytes()
        )
        credentials = registry_config(raw, next(iter(registries)))
    stack = manifest_resources(kubectl.run(
        "create", "--dry-run=client", "--validate=false",
        "-f", str(ROOT / "deploy" / "stack.yaml"), "-o", "json",
    ))
    for resource in stack:
        if resource["kind"] == "PersistentVolumeClaim":
            resource["spec"]["storageClassName"] = args.storage_class
        if resource["kind"] in {"Deployment", "StatefulSet"}:
            spec = resource["spec"]["template"]["spec"]
            spec["nodeSelector"] = {"kubernetes.io/hostname": node}
            spec["dnsPolicy"] = "None"
            spec["dnsConfig"] = {
                "nameservers": [dns_ip],
                "searches": [f"{NAMESPACE}.svc.cluster.local", "svc.cluster.local", "cluster.local"],
                "options": [{"name": "ndots", "value": "5"}],
            }
            if credentials:
                spec["imagePullSecrets"] = [{"name": "assistant-registry"}]
            for container in spec["containers"]:
                container["image"] = {
                    "APP_IMAGE": args.app_image, "EMBEDDINGS_IMAGE": args.embeddings_image,
                    "QDRANT_IMAGE": args.qdrant_image,
                }.get(container["image"], container["image"])
        if resource["kind"] == "NetworkPolicy":
            destination = resource["spec"]["egress"][-1]["to"][0]
            destination["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] = args.model_namespace
            destination["podSelector"]["matchLabels"]["app.kubernetes.io/name"] = args.model_deployment
    extras = [
        {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": metadata("assistant-settings"),
            "data": {"model-id": ""},
        },
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata("model-trust"), "data": {}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": metadata("assistant-credentials"), "data": {}},
    ]
    if credentials:
        extras.append({
            "apiVersion": "v1", "kind": "Secret", "metadata": metadata("assistant-registry"),
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": credentials},
        })
    for resource in stack + extras:
        if resource["kind"] == "Namespace":
            continue
        existing = kubectl.get(resource["kind"], resource["metadata"]["name"], NAMESPACE) if namespace else None
        ensure_owned(existing, resource["kind"], resource["metadata"]["name"])
    return stack + extras, node


def apply(args, kubectl: Kubectl, resources: list[dict]) -> None:
    model = kubectl.get("modeldeployment", args.model_deployment, args.model_namespace)
    model_id = model["status"]["resolvedModel"]["name"]
    source_key = kubectl.get("secret", f"{args.model_deployment}-api-keys", args.model_namespace)
    encoded_key = source_key["data"]["primary-key"]
    if not base64.b64decode(encoded_key, validate=True).strip():
        raise RuntimeError("The existing model API key is empty.")
    ca = kubectl.get("configmap", args.model_ca_configmap, args.model_namespace)["data"]["ca.crt"]
    if "-----BEGIN CERTIFICATE-----" not in ca:
        raise RuntimeError("The model CA bundle is invalid.")
    existing = kubectl.get("secret", "assistant-credentials", NAMESPACE)
    database_key = existing["data"]["qdrant-key"] if existing else base64.b64encode(secrets.token_bytes(32).hex().encode()).decode()
    for resource in resources:
        name = resource["metadata"]["name"]
        if name == "assistant-settings":
            resource["data"] = {
                "model-id": model_id,
                "model-url": (
                    f"https://{args.model_deployment}.{args.model_namespace}."
                    "svc.cluster.local:5000/v1/chat/completions"
                ),
            }
        elif name == "model-trust":
            resource["data"] = {"ca.crt": ca}
        elif name == "assistant-credentials":
            resource["data"] = {"model-key": encoded_key, "qdrant-key": database_key}
    # No resource-group, infrastructure, extension, or existing model writes occur here.
    for resource in resources:
        if resource["kind"] == "Namespace":
            print(kubectl.run("apply", "-f", "-", payload=resource))
    # Dependencies first: deployments may otherwise start before their configuration is present.
    ordered = sorted(
        (resource for resource in resources if resource["kind"] != "Namespace"),
        key=lambda resource: resource["kind"] not in {"Secret", "ConfigMap", "PersistentVolumeClaim", "NetworkPolicy", "Service"},
    )
    for resource in ordered:
        print(kubectl.run(
            "apply", "--server-side", "--field-manager=local-pdf-assistant",
            "-f", "-", payload=resource,
        ))
    print("Created/updated only assistant-demo resources. Check rollout and PVC binding using the runbook.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="zines-22325-aks")
    parser.add_argument("--storage-class", default="default")
    parser.add_argument("--dns-service", default="kube-dns-upstream", help="CoreDNS Service in kube-system, bypassing host-network NodeLocal DNS.")
    parser.add_argument("--model-namespace", default="foundry-local-operator")
    parser.add_argument("--model-deployment", default="qwen2-5-0-5b-cpu")
    parser.add_argument("--model-ca-configmap", default="foundry-local-operator-ca-bundle")
    parser.add_argument("--app-image")
    parser.add_argument("--embeddings-image")
    parser.add_argument("--qdrant-image")
    pull = parser.add_mutually_exclusive_group()
    pull.add_argument("--registry-config", help="Path to approved registry dockerconfigjson; never committed.")
    pull.add_argument("--registry-config-stdin", action="store_true", help="Read registry dockerconfigjson from stdin without a temporary credential file.")
    parser.add_argument("--apply", action="store_true", help="Permit writes only after read-only preflight passes.")
    args = parser.parse_args()
    kubectl = Kubectl(args.context)
    try:
        resources, node = prepare(args, kubectl)
        print(f"Preflight passed for worker {node}. Review lab-owner capacity approval before --apply.")
        if args.apply:
            apply(args, kubectl, resources)
        else:
            print("Read-only mode: no cluster resources were created or changed.")
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Deployment stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
