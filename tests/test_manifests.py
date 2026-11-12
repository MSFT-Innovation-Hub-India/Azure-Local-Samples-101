import unittest
from pathlib import Path

import yaml

from tools.deploy_assistant import MIB, NAMESPACE, OWNER, LABEL, pod_request


ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.stack = list(yaml.safe_load_all((ROOT / "deploy" / "stack.yaml").read_text()))
        self.job = yaml.safe_load((ROOT / "deploy" / "index-job.yaml").read_text())

    def test_capacity_guard_matches_actual_workload_requests(self):
        templates = [
            resource["spec"]["template"] for resource in self.stack
            if resource["kind"] in {"Deployment", "StatefulSet"}
        ] + [self.job["spec"]["template"]]
        self.assertEqual(sum(pod_request(pod, "memory") for pod in templates), 2496 * MIB)
        self.assertEqual(str(sum(pod_request(pod, "cpu") for pod in templates)), "0.710")

    def test_all_writes_are_owned_and_namespaced(self):
        for resource in self.stack + [self.job]:
            self.assertEqual(resource["metadata"]["labels"][LABEL], OWNER)
            if resource["kind"] != "Namespace":
                self.assertEqual(resource["metadata"]["namespace"], NAMESPACE)
        self.assertFalse(any(resource["kind"] in {"ClusterRole", "ClusterRoleBinding"} for resource in self.stack))

    def test_no_privileged_pods_or_service_account_tokens(self):
        for resource in self.stack + [self.job]:
            if resource["kind"] not in {"Deployment", "StatefulSet", "Job"}:
                continue
            spec = resource["spec"]["template"]["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertNotIn("tolerations", spec)
            for container in spec["containers"]:
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])

    def test_indexer_mounts_documents_read_only_and_only_database_key(self):
        spec = self.job["spec"]["template"]["spec"]
        mounts = spec["containers"][0]["volumeMounts"]
        self.assertTrue(next(mount for mount in mounts if mount["name"] == "documents")["readOnly"])
        credentials = next(volume for volume in spec["volumes"] if volume["name"] == "credentials")
        self.assertEqual(credentials["secret"]["items"], [{"key": "qdrant-key", "path": "qdrant-key"}])

    def test_dns_egress_is_limited_to_coredns_namespace_and_ports(self):
        policy = next(resource for resource in self.stack if resource["kind"] == "NetworkPolicy")
        dns = policy["spec"]["egress"][0]
        self.assertEqual(dns["to"], [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}])
        self.assertEqual(dns["ports"], [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}])
        self.assertNotIn({"ipBlock": {"cidr": "0.0.0.0/0"}}, dns["to"])


if __name__ == "__main__":
    unittest.main()
