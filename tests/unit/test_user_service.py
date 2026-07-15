"""Unit tests for user service and user-assistant bindings."""

from src.users.service import UserService


def test_create_and_list_users(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    user = svc.create_user("emp001", display_name="Alice", employee_id="E001", department="Eng")
    assert user.user_id == "emp001"
    assert user.display_name == "Alice"
    users = svc.list_users()
    assert len(users) == 1
    assert users[0].user_id == "emp001"


def test_get_user(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001", display_name="Alice")
    assert svc.get_user("emp001") is not None
    assert svc.get_user("nonexist") is None


def test_duplicate_user_raises(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    try:
        svc.create_user("emp001")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_update_user(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001", display_name="Alice")
    updated = svc.update_user("emp001", display_name="Bob")
    assert updated is not None
    assert updated.display_name == "Bob"
    assert svc.update_user("nonexist", display_name="X") is None


def test_delete_user_removes_bindings(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    svc.bind_assistant("emp001", "assistant-1")
    assert svc.delete_user("emp001")
    assert svc.get_user("emp001") is None
    assert svc.list_bindings_for_user("emp001") == []


def test_delete_user_removes_bindings_before_user_record() -> None:
    events: list[str] = []

    class UserRepo:
        async def aget_user(self, user_id: str):
            return object()

        async def adelete_user(self, user_id: str) -> bool:
            assert events == ["delete_bindings"]
            events.append("delete_user")
            return True

    class BindingRepo:
        async def adelete_bindings_for_user(self, user_id: str) -> int:
            events.append("delete_bindings")
            return 1

    svc = UserService(repository=UserRepo(), binding_repository=BindingRepo())

    assert svc.delete_user("emp001")
    assert events == ["delete_bindings", "delete_user"]


def test_bind_and_unbind_assistant(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    binding = svc.bind_assistant("emp001", "assistant-1", alias="My Bot", source_system="OA")
    assert binding.user_id == "emp001"
    assert binding.assistant_id == "assistant-1"
    assert binding.alias == "My Bot"

    bindings = svc.list_bindings_for_user("emp001")
    assert len(bindings) == 1

    assert svc.unbind_assistant("emp001", "assistant-1")
    assert svc.list_bindings_for_user("emp001") == []


def test_rebind_updates_alias(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    svc.bind_assistant("emp001", "assistant-1", alias="Bot A")
    svc.bind_assistant("emp001", "assistant-1", alias="Bot B")
    bindings = svc.list_bindings_for_user("emp001")
    assert len(bindings) == 1
    assert bindings[0].alias == "Bot B"


def test_multiple_assistants_per_user(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    svc.bind_assistant("emp001", "assistant-1")
    svc.bind_assistant("emp001", "assistant-2")
    svc.bind_assistant("emp001", "assistant-3")
    assert len(svc.list_bindings_for_user("emp001")) == 3
    assert set(svc.resolve_assistant_ids("emp001")) == {
        "assistant-1",
        "assistant-2",
        "assistant-3",
    }


def test_list_bindings_grouped_by_user(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    svc.create_user("emp002")
    svc.bind_assistant("emp001", "assistant-1")
    svc.bind_assistant("emp001", "assistant-2")
    svc.bind_assistant("emp002", "assistant-3")

    grouped = svc.list_bindings_grouped_by_user()

    assert [item.assistant_id for item in grouped["emp001"]] == [
        "assistant-1",
        "assistant-2",
    ]
    assert [item.assistant_id for item in grouped["emp002"]] == ["assistant-3"]


def test_resolve_primary_assistant(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    assert svc.resolve_primary_assistant("emp001") is None
    svc.bind_assistant("emp001", "assistant-1")
    svc.bind_assistant("emp001", "assistant-2")
    primary = svc.resolve_primary_assistant("emp001")
    assert primary == "assistant-1"


def test_list_bindings_for_assistant(tmp_path) -> None:
    svc = UserService(
        store_path=tmp_path / "users.json",
        bindings_path=tmp_path / "bindings.json",
    )
    svc.create_user("emp001")
    svc.create_user("emp002")
    svc.bind_assistant("emp001", "shared-bot")
    svc.bind_assistant("emp002", "shared-bot")
    bindings = svc.list_bindings_for_assistant("shared-bot")
    assert len(bindings) == 2
    assert {b.user_id for b in bindings} == {"emp001", "emp002"}
