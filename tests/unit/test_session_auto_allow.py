from src.security_service.session_auto_allow import (
    clear_all,
    clear_session,
    handle_allow_always,
    is_auto_allowed,
    mark_auto_allow,
    register_approval_session,
)


def setup_function() -> None:
    clear_all()


def test_mark_auto_allow_enables_auto_allowed() -> None:
    assert not is_auto_allowed("assistant-1", "session-1")

    mark_auto_allow("assistant-1", "session-1")

    assert is_auto_allowed("assistant-1", "session-1")


def test_auto_allow_is_scoped_to_assistant_and_session() -> None:
    mark_auto_allow("assistant-1", "session-1")

    assert is_auto_allowed("assistant-1", "session-1")
    assert not is_auto_allowed("assistant-1", "session-2")
    assert not is_auto_allowed("assistant-2", "session-1")


def test_auto_allow_ignores_missing_assistant_id() -> None:
    mark_auto_allow(None, "session-1")
    mark_auto_allow("", "session-1")

    assert not is_auto_allowed(None, "session-1")
    assert not is_auto_allowed("", "session-1")


def test_auto_allow_ignores_missing_session_id() -> None:
    mark_auto_allow("assistant-1", None)
    mark_auto_allow("assistant-1", "")

    assert not is_auto_allowed("assistant-1", None)
    assert not is_auto_allowed("assistant-1", "")


def test_register_and_handle_allow_always() -> None:
    register_approval_session("approval-123", "assistant-1", "session-1")

    # Before handle_allow_always, session is not auto-allowed
    assert not is_auto_allowed("assistant-1", "session-1")

    # After handle_allow_always, session is auto-allowed
    handle_allow_always("approval-123")

    assert is_auto_allowed("assistant-1", "session-1")


def test_handle_allow_always_with_unknown_approval_id() -> None:
    # Should not raise, just silently skip
    handle_allow_always("nonexistent-approval")
    assert not is_auto_allowed("assistant-1", "session-1")


def test_handle_allow_always_consumes_mapping() -> None:
    register_approval_session("approval-456", "assistant-1", "session-1")

    handle_allow_always("approval-456")
    assert is_auto_allowed("assistant-1", "session-1")

    # Calling again should not crash (mapping already consumed)
    handle_allow_always("approval-456")


def test_clear_session_removes_auto_allow() -> None:
    mark_auto_allow("assistant-1", "session-1")
    assert is_auto_allowed("assistant-1", "session-1")

    clear_session("assistant-1", "session-1")
    assert not is_auto_allowed("assistant-1", "session-1")


def test_clear_all_removes_everything() -> None:
    mark_auto_allow("assistant-1", "session-1")
    mark_auto_allow("assistant-2", "session-2")
    register_approval_session("approval-789", "assistant-3", "session-3")

    clear_all()

    assert not is_auto_allowed("assistant-1", "session-1")
    assert not is_auto_allowed("assistant-2", "session-2")
    # approval-session mapping also cleared
    handle_allow_always("approval-789")
    assert not is_auto_allowed("assistant-3", "session-3")


def test_register_approval_session_ignores_missing_keys() -> None:
    # Should not raise
    register_approval_session("approval-1", None, "session-1")
    register_approval_session("approval-2", "assistant-1", None)
    register_approval_session("approval-3", None, None)


def test_multiple_approvals_for_same_session() -> None:
    """Multiple approvals in the same session should all map correctly."""
    register_approval_session("approval-A", "assistant-1", "session-1")
    register_approval_session("approval-B", "assistant-1", "session-1")

    # Either approval's allow-always should mark the session
    handle_allow_always("approval-A")
    assert is_auto_allowed("assistant-1", "session-1")

    # The other approval's mapping is still there (not consumed yet)
    clear_all()
    register_approval_session("approval-C", "assistant-1", "session-1")
    register_approval_session("approval-D", "assistant-1", "session-1")

    handle_allow_always("approval-D")
    assert is_auto_allowed("assistant-1", "session-1")
