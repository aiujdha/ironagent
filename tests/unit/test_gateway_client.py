from src.approval.gateway_client import _build_connect_params


def test_gateway_client_uses_control_ui_identity_for_approval_scope() -> None:
    params = _build_connect_params("secret")

    assert params["client"]["id"] == "ironagent-tui"
    assert params["client"]["mode"] == "ui"
    assert params["role"] == "operator"
    assert params["scopes"] == ["operator.approvals"]
    assert params["auth"]["token"] == "secret"
