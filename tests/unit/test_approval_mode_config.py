from pathlib import Path

from src.shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_CONTROL_PLANE_ENVS = (
    "deployment/kustomize/ironagent/overlays/prod-ingress/config/control-plane-shared.env",
    "deployment/kustomize/ironagent/overlays/prod-k8s-single-entry-nodeport/config/control-plane-shared.env",
    "deployment/kustomize/ironagent/overlays/prod-nodeport/config/control-plane-shared.env",
    "deployment/kustomize/ironagent/overlays/prod-single-entry-nodeport/config/control-plane-shared.env",
)


def test_approval_mode_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("IRON_AGENT_APPROVAL_MODE", raising=False)
    monkeypatch.delenv("IRONAGENT_APPROVAL_MODE", raising=False)

    assert Settings().approval_mode == "disabled"


def test_compose_defaults_approval_mode_to_disabled() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count('IRON_AGENT_APPROVAL_MODE: "${IRON_AGENT_APPROVAL_MODE:-disabled}"') == 2
    assert 'IRON_AGENT_APPROVAL_MODE: "${IRON_AGENT_APPROVAL_MODE:-enabled}"' not in compose


def test_prod_overlays_disable_approval_mode() -> None:
    for relative_path in PROD_CONTROL_PLANE_ENVS:
        content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

        assert "IRON_AGENT_APPROVAL_MODE=disabled" in content
        assert "IRON_AGENT_APPROVAL_MODE=enabled" not in content
