from src.admin.gateway_links import (
    build_gateway_launch_url,
    build_go_gateway_assistant_ui_url,
    build_spdbpanda_chat_launch_url,
    normalize_control_ui_gateway_url,
)


def test_build_gateway_launch_url_uses_fragment_token() -> None:
    result = build_gateway_launch_url("http://127.0.0.1:6681", "ironagent-local-demo-main")

    assert result == "http://127.0.0.1:6681#token=ironagent-local-demo-main"


def test_build_gateway_launch_url_replaces_existing_fragment_token() -> None:
    result = build_gateway_launch_url(
        "http://127.0.0.1:6681#view=chat&token=stale-token",
        "fresh token",
    )

    assert result == "http://127.0.0.1:6681#view=chat&token=fresh%20token"


def test_build_gateway_launch_url_returns_plain_url_without_token() -> None:
    result = build_gateway_launch_url("http://127.0.0.1:6681", None)

    assert result == "http://127.0.0.1:6681"


def test_build_go_gateway_assistant_ui_url_uses_single_port_proxy() -> None:
    result = build_go_gateway_assistant_ui_url(
        go_gateway_public_url="https://gateway.example.com/base",
        assistant_id="demo-main",
        launch_ticket="ticket-123",
    )

    assert result == "https://gateway.example.com/base/assistants/demo-main/ui/?launchTicket=ticket-123"


def test_build_go_gateway_assistant_ui_url_requires_ticket() -> None:
    result = build_go_gateway_assistant_ui_url(
        go_gateway_public_url="https://gateway.example.com",
        assistant_id="demo-main",
        launch_ticket="",
    )

    assert result is None


def test_build_spdbpanda_chat_launch_url_uses_branded_entrypoint() -> None:
    result = build_spdbpanda_chat_launch_url(
        control_ui_url="http://127.0.0.1:64685",
        gateway_url="ws://127.0.0.1:60599",
        gateway_token="gateway-token",
        session_key="main",
        security_service_url="http://127.0.0.1:19000/",
        launch_ticket="launch-ticket-123",
    )

    assert result == (
        "http://127.0.0.1:64685/chat?session=main#"
        "ironAgentApiUrl=http%3A%2F%2F127.0.0.1%3A19000&"
        "launchTicket=launch-ticket-123"
    )


def test_build_spdbpanda_chat_launch_url_preserves_existing_base_path() -> None:
    result = build_spdbpanda_chat_launch_url(
        control_ui_url="https://spdbpanda.example/internal/ui?theme=blue",
        gateway_url="wss://gateway.example/assistant-a",
        gateway_token="fresh token",
        session_key="agent:main:main",
    )

    assert result == (
        "https://spdbpanda.example/internal/ui/chat?theme=blue&"
        "session=agent%3Amain%3Amain#gatewayUrl="
        "wss%3A%2F%2Fgateway.example%2Fassistant-a&token=fresh%20token"
    )


def test_build_spdbpanda_chat_launch_url_normalizes_http_gateway_url_to_ws() -> None:
    result = build_spdbpanda_chat_launch_url(
        control_ui_url="http://127.0.0.1:64685",
        gateway_url="http://172.28.135.254:2049",
        gateway_token="gateway-token",
        session_key="main",
        security_service_url="http://127.0.0.1:19000/",
    )

    assert result == (
        "http://127.0.0.1:64685/chat?session=main#"
        "ironAgentApiUrl=http%3A%2F%2F127.0.0.1%3A19000&"
        "gatewayUrl=ws%3A%2F%2F172.28.135.254%3A2049&"
        "token=gateway-token"
    )


def test_normalize_control_ui_gateway_url_upgrades_http_schemes() -> None:
    assert normalize_control_ui_gateway_url("http://127.0.0.1:2049") == "ws://127.0.0.1:2049"
    assert normalize_control_ui_gateway_url("https://gateway.example/ironagent") == (
        "wss://gateway.example/ironagent"
    )
    assert normalize_control_ui_gateway_url("ws://127.0.0.1:2049") == "ws://127.0.0.1:2049"


def test_build_spdbpanda_chat_launch_url_falls_back_to_direct_fragment_bootstrap() -> None:
    result = build_spdbpanda_chat_launch_url(
        control_ui_url="http://127.0.0.1:64685",
        gateway_url="ws://127.0.0.1:60599",
        gateway_token="gateway-token",
        session_key="main",
        security_service_url="http://127.0.0.1:19000/",
    )

    assert result == (
        "http://127.0.0.1:64685/chat?session=main#"
        "ironAgentApiUrl=http%3A%2F%2F127.0.0.1%3A19000&"
        "gatewayUrl=ws%3A%2F%2F127.0.0.1%3A60599&"
        "token=gateway-token"
    )
