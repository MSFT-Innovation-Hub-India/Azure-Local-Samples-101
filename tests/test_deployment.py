import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.deploy_assistant import Kubectl, MIB, choose_node, ensure_owned, manifest_resources, pod_request, prepare, quantity, registry_config


def node(memory="16242728Ki", taints=None):
    return {
        "metadata": {"name": "worker"},
        "spec": {"taints": taints or []},
        "status": {
            "allocatable": {"memory": memory, "cpu": "8"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def pod(memory, namespace="foundry-local-operator", labels=None):
    return {
        "metadata": {"namespace": namespace, "labels": labels or {}},
        "spec": {
            "nodeName": "worker",
            "containers": [{"resources": {"requests": {"memory": memory, "cpu": "1"}}}],
        },
        "status": {"phase": "Running"},
    }


class DeploymentTests(unittest.TestCase):
    def test_every_workload_uses_selected_coredns_service(self):
        image = "demo.azurecr.io/app@sha256:" + "1" * 64
        args = SimpleNamespace(
            storage_class="default", dns_service="kube-dns-upstream",
            app_image=image, embeddings_image=image, qdrant_image=image,
            registry_config=None, registry_config_stdin=False,
        )
        resources = [
            {"kind": kind, "metadata": {"name": kind.lower()}, "spec": {"template": {"spec": {"containers": [{"image": "APP_IMAGE"}]}}}}
            for kind in ("Deployment", "StatefulSet")
        ]
        kubectl = Mock()
        kubectl.get.side_effect = [
            None, {"items": []}, {"items": []},
            {"provisioner": "azlocal.csi.azure.com"},
            {"spec": {"clusterIP": "10.96.214.2"}},
        ]
        kubectl.run.return_value = "\n".join(map(json.dumps, resources))
        with patch("tools.deploy_assistant.choose_node", return_value=("worker", "capacity passed")):
            prepared, _ = prepare(args, kubectl)
        for resource in prepared[:2]:
            spec = resource["spec"]["template"]["spec"]
            self.assertEqual(spec["dnsPolicy"], "None")
            self.assertEqual(spec["dnsConfig"]["nameservers"], ["10.96.214.2"])
            self.assertEqual(spec["dnsConfig"]["searches"][0], "assistant-demo.svc.cluster.local")
        kubectl.get.assert_any_call("service", "kube-dns-upstream", "kube-system")

    def test_manifest_output_accepts_kubectl_json_stream_and_list(self):
        resources = [{"kind": "Namespace"}, {"kind": "Deployment"}]
        self.assertEqual(manifest_resources("\n".join(map(json.dumps, resources))), resources)
        self.assertEqual(manifest_resources(json.dumps({"kind": "List", "items": resources})), resources)
        self.assertEqual(manifest_resources(json.dumps(resources[0])), resources[:1])
        for invalid in ("", "[]", '{"kind":"Namespace"} broken'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                manifest_resources(invalid)

    def test_registry_credentials_are_validated_without_exposing_values(self):
        auth = base64.b64encode(b"user:secret").decode()
        raw = json.dumps({"auths": {"demo.azurecr.io": {"auth": auth}}}).encode()
        self.assertEqual(base64.b64decode(registry_config(raw, "demo.azurecr.io")), raw)
        with self.assertRaisesRegex(ValueError, "selected ACR"):
            registry_config(raw, "other.azurecr.io")
        for invalid in (b"[]", b"{}", b'{"auths":null}', b'{"auths":"invalid"}', b"x" * 65537, b'{"auths":{"demo.azurecr.io":{}}}'):
            with self.subTest(invalid=invalid[:40]), self.assertRaises(ValueError):
                registry_config(invalid, "demo.azurecr.io")

    def test_registry_auth_rejects_empty_username_or_password(self):
        for value in (b":secret", b"user:", b"user", b"user:secret"):
            raw = json.dumps({"auths": {"demo.azurecr.io": {"auth": base64.b64encode(value).decode()}}}).encode()
            if value == b"user:secret":
                registry_config(raw, "demo.azurecr.io")
            else:
                with self.assertRaisesRegex(ValueError, "nonempty username and password"):
                    registry_config(raw, "demo.azurecr.io")

    def test_quantity_units(self):
        self.assertEqual(quantity("1Gi"), 1024 * MIB)
        self.assertEqual(quantity("512Mi"), 512 * MIB)
        self.assertEqual(quantity("250m"), quantity("0.25"))

    def test_current_lab_capacity_is_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "Insufficient safe worker capacity"):
            choose_node([node()], [pod("15250Mi")])

    def test_exact_capacity_threshold_and_headroom(self):
        self.assertEqual(choose_node([node("3520Mi")], [])[0], "worker")
        with self.assertRaises(RuntimeError):
            choose_node([node("3519Mi")], [])

    def test_control_plane_is_never_used(self):
        with self.assertRaises(RuntimeError):
            choose_node([node("64Gi", [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}])], [])

    def test_native_sidecars_are_counted(self):
        resource = pod("100Mi")
        resource["spec"]["initContainers"] = [
            {"restartPolicy": "Always", "resources": {"requests": {"memory": "50Mi"}}},
            {"resources": {"requests": {"memory": "300Mi"}}},
            {"restartPolicy": "Always", "resources": {"requests": {"memory": "20Mi"}}},
        ]
        self.assertEqual(pod_request(resource, "memory"), 350 * MIB)

    def test_unrelated_namespace_labels_do_not_bypass_capacity(self):
        resource = pod("15250Mi", "other", {"app.kubernetes.io/part-of": "local-pdf-assistant"})
        with self.assertRaises(RuntimeError):
            choose_node([node()], [resource])

    def test_unowned_resources_are_not_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "unowned"):
            ensure_owned({"metadata": {}}, "Namespace", "assistant-demo")

    def test_pod_inventory_is_cluster_wide(self):
        with patch("tools.deploy_assistant.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"items":[]}'
            Kubectl("test").get("pods")
            self.assertIn("--all-namespaces", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
