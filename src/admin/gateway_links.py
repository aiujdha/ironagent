from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def _append_chat_path(base_url: str) -> str:
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/")
    if path.endswith("/chat") or path == "/chat":
        return urlunsplit(parts)
    next_path = f"{path}/chat" if path else "/chat"
    return urlunsplit(parts._replace(path=next_path))


def normalize_control_ui_gateway_url(gateway_url: str | None) -> str | None:
    if not gateway_url:
        return gateway_url
    parts = urlsplit(gateway_url)
    if parts.scheme == "http":
        return urlunsplit(parts._replace(scheme="ws"))
    if parts.scheme == "https":
        return urlunsplit(parts._replace(scheme="wss"))
    return gateway_url


def _is_control_ui_bootstrap_param(key: str) -> bool:
    normalized = key.strip()
    return normalized in {
        "gatewayUrl",
        "session",
        "ironAgentApiUrl",
        "launchTicket",
        "token",
    } or normalized.lower().endswith("token")


def build_gateway_launch_url(
    gateway_url: str | None,
    gateway_token: str | None,
) -> str | None:
    """Build a dashboard URL that bootstraps auth via fragment instead of query params."""
    if not gateway_url:
        return None
    if not gateway_token:
        return gateway_url

    parts = urlsplit(gateway_url)
    fragment_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.fragment, keep_blank_values=True)
        if key != "token"
    ]
    fragment_pairs.append(("token", gateway_token))
    fragment = urlencode(fragment_pairs, doseq=True, quote_via=quote)
    return urlunsplit(parts._replace(fragment=fragment))


def build_go_gateway_assistant_ui_url(
    *,
    go_gateway_public_url: str | None,
    assistant_id: str | None,
    launch_ticket: str | None,
) -> str | None:
    """Build the single-port Go Gateway UI proxy URL for one assistant."""
    base_url = (go_gateway_public_url or "").strip().rstrip("/")
    normalized_assistant_id = (assistant_id or "").strip()
    normalized_ticket = (launch_ticket or "").strip()
    if not base_url or not normalized_assistant_id or not normalized_ticket:
        return None

    parts = urlsplit(base_url)
    base_path = parts.path.rstrip("/")
    proxy_path = f"/assistants/{quote(normalized_assistant_id, safe='')}/ui/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"launchTicket", "ticket"}
    ]
    query_pairs.append(("launchTicket", normalized_ticket))
    return urlunsplit(
        parts._replace(
            path=f"{base_path}{proxy_path}",
            query=urlencode(query_pairs, doseq=True, quote_via=quote),
            fragment="",
    )
        )


def build_spdbpanda_chat_launch_url(
    *,
    control_ui_url: str | None,
    gateway_url: str | None,
    gateway_token: str | None,
    session_key: str = "main",
    security_service_url: str | None = None,
    launch_ticket: str | None = None,
) -> str | None:
    """Build the branded chat URL without placing secrets in query params."""
    if not control_ui_url:
        return build_gateway_launch_url(gateway_url, gateway_token)

    normalized_gateway_url = normalize_control_ui_gateway_url(gateway_url)
    chat_url = _append_chat_path(control_ui_url)
    parts = urlsplit(chat_url)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_control_ui_bootstrap_param(key)
    ]
    if session_key.strip():
        query_pairs.append(("session", session_key.strip()))

    fragment_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.fragment, keep_blank_values=True)
        if not _is_control_ui_bootstrap_param(key)
    ]
    if security_service_url:
        fragment_pairs.append(("ironAgentApiUrl", security_service_url.rstrip("/")))
    if launch_ticket:
        fragment_pairs.append(("launchTicket", launch_ticket))
    else:
        if normalized_gateway_url:
            fragment_pairs.append(("gatewayUrl", normalized_gateway_url))
        if gateway_token:
            fragment_pairs.append(("token", gateway_token))

    return urlunsplit(
        parts._replace(
            query=urlencode(query_pairs, doseq=True, quote_via=quote),
            fragment=urlencode(fragment_pairs, doseq=True, quote_via=quote),
    )
        )
