from pathlib import Path

import yaml


def test_runtime_provisioner_role_can_manage_runtime_services() -> None:
    manifest_path = Path(
        "deployment/kustomize/ironagent/overlays/runtime-k8s-pvc/runtime-rbac.yaml"
    )
    docs = list(yaml.safe_load_all(manifest_path.read_text(encoding="utf-8")))
    role = next(doc for doc in docs if doc.get("kind") == "Role")

    service_rule = next(
        rule
        for rule in role["rules"]
        if "" in rule["apiGroups"] and "services" in rule["resources"]
    )

    assert {"get", "list", "watch", "create", "update", "patch", "delete"} <= set(
        service_rule["verbs"]
    )
