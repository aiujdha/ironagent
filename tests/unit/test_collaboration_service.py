from datetime import timedelta
from pathlib import Path

import pytest

from src.approval.service import ApprovalService
from src.collaboration import repository as repository_module
from src.collaboration.service import (
    CollaborationObjectStorageDefaults,
    CollaborationService,
)
from src.shared.config import get_settings


@pytest.fixture(autouse=True)
def approval_mode_enabled(monkeypatch):
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "enabled")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_service(tmp_path: Path) -> CollaborationService:
    return CollaborationService(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'collaboration.db'}",
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        storage_defaults=CollaborationObjectStorageDefaults(
            backend="cos",
            bucket="demo-bucket",
            region="ap-shanghai",
            endpoint="https://cos.example.com",
            prefix="heinu/",
        ),
        claim_lease_seconds=60,
    )


def test_create_shared_object_assigns_storage_defaults_and_source_permission(tmp_path) -> None:
    service = _build_service(tmp_path)

    obj = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="报价 汇总.pdf",
        created_by="user-1",
        retention_policy="task_bound",
    )

    assert obj.storage_backend == "cos"
    assert obj.storage_bucket == "demo-bucket"
    assert obj.storage_region == "ap-shanghai"
    assert obj.storage_endpoint == "https://cos.example.com"
    assert obj.storage_key.startswith("heinu/")
    fetched = service.get_shared_object(obj.id, assistant_id="assistant-a")
    assert fetched.id == obj.id


def test_create_shared_object_is_idempotent_by_key(tmp_path) -> None:
    service = _build_service(tmp_path)

    first = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="first.txt",
        created_by="gateway",
        idempotency_key="task-123-message",
        meta_json={"request_id": "req-123"},
    )
    second = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="second.txt",
        created_by="gateway",
        idempotency_key="task-123-message",
        meta_json={"request_id": "req-123"},
    )

    objects = service.list_shared_objects()
    assert second.id == first.id
    assert len(objects) == 1
    assert objects[0].filename == "first.txt"
    assert objects[0].idempotency_key == "task-123-message"
    assert objects[0].meta_json["idempotency_key"] == "task-123-message"


def test_create_shared_object_rejects_values_beyond_schema_lengths(tmp_path) -> None:
    service = _build_service(tmp_path)

    try:
        service.create_shared_object(
            source_assistant_id="assistant-" + ("x" * 80),
            filename="report.txt",
        )
    except ValueError as exc:
        assert "source_assistant_id must be at most 64 characters" in str(exc)
    else:
        raise AssertionError("long source_assistant_id should be rejected")


def test_sql_repository_uses_connection_pool_liveness_options(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    fake_engine = object()

    class FakeSettings:
        db_pool_size = 7
        db_max_overflow = 3
        db_pool_timeout_seconds = 11
        db_pool_recycle_seconds = 120
        db_connect_timeout_seconds = 5

    def fake_create_engine(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return fake_engine

    monkeypatch.setattr(repository_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(repository_module, "create_engine", fake_create_engine)

    engine = repository_module.SqlAlchemyCollaborationRepository(
        "mysql+pymysql://ironagent:ironagent@mysql:3306/ironagent"
    )._create_engine()

    assert engine is fake_engine
    assert calls[0][1]["pool_pre_ping"] is True
    assert calls[0][1]["pool_size"] == 7
    assert calls[0][1]["max_overflow"] == 3
    assert calls[0][1]["pool_timeout"] == 11
    assert calls[0][1]["pool_recycle"] == 120
    assert calls[0][1]["connect_args"] == {"connect_timeout": 5}


def test_sql_repository_rejects_auto_create_in_production(monkeypatch, tmp_path) -> None:
    class FakeSettings:
        env = "production"
        collaboration_auto_create_tables = True
        db_pool_timeout_seconds = 11
        db_pool_size = 7
        db_max_overflow = 3
        db_pool_recycle_seconds = 120
        db_connect_timeout_seconds = 5

    monkeypatch.setattr(repository_module, "get_settings", lambda: FakeSettings())
    repository = repository_module.SqlAlchemyCollaborationRepository(
        f"sqlite+pysqlite:///{tmp_path / 'collaboration.db'}"
    )

    with pytest.raises(RuntimeError, match="must be disabled in production"):
        repository._ensure_ready()


def test_create_task_requires_approval_and_syncs_after_resolution(
    tmp_path,
) -> None:
    service = _build_service(tmp_path)
    obj = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="contract.pdf",
        created_by="assistant-a",
    )

    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=obj.id,
        instruction="Summarize this contract.",
        approval_mode="user_required",
        created_by="assistant-a",
    )

    assert task.status == "pending_approval"
    assert task.approval_id is not None

    approval_service = service.approval_service
    approval_service.resolve_request(task.approval_id or "", "allow")

    reloaded = service.get_task(task.id)
    assert reloaded.status == "pending_claim"
    fetched = service.get_shared_object(obj.id, assistant_id="assistant-b")
    assert fetched.id == obj.id


def test_create_task_bypasses_approval_when_approval_mode_disabled(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("IRON_AGENT_APPROVAL_MODE", "disabled")
    get_settings.cache_clear()
    try:
        service = _build_service(tmp_path)
        obj = service.create_shared_object(
            source_assistant_id="assistant-a",
            filename="contract.pdf",
            created_by="assistant-a",
        )

        task = service.create_task(
            source_assistant_id="assistant-a",
            target_assistant_id="assistant-b",
            input_object_id=obj.id,
            instruction="Summarize this contract.",
            approval_mode="user_required",
            created_by="assistant-a",
        )

        assert task.status == "pending_claim"
        assert task.approval_id is None
        assert service.approval_service.list_requests() == []
        fetched = service.get_shared_object(obj.id, assistant_id="assistant-b")
        assert fetched.id == obj.id
    finally:
        get_settings.cache_clear()


def test_phase_one_rejects_nested_collaboration_tasks(tmp_path) -> None:
    service = _build_service(tmp_path)
    obj = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="notes.txt",
    )

    try:
        service.create_task(
            source_assistant_id="assistant-a",
            target_assistant_id="assistant-b",
            input_object_id=obj.id,
            instruction="Continue processing.",
            parent_task_id="parent-task-1",
        )
    except ValueError as exc:
        assert "nested collaboration tasks are disabled" in str(exc)
    else:
        raise AssertionError("nested collaboration task should have been rejected")


@pytest.mark.parametrize(
    "failed_reason_code",
    [
        "runtime_result_object_failed",
        "gateway_task_stale_progress",
    ],
)
def test_create_task_reopens_retryable_failed_idempotent_task(
    tmp_path,
    failed_reason_code: str,
) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
        idempotency_key="gateway-task-1",
    )
    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")
    service.start_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )
    failed = service.fail_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
        failed_reason_code=failed_reason_code,
        failed_reason_text=f"{failed_reason_code} should be retried",
    )
    assert failed.status == "failed"

    reopened = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
        idempotency_key="gateway-task-1",
    )

    assert reopened.id == task.id
    assert reopened.status == "pending_claim"
    assert reopened.failed_reason_code is None
    assert reopened.failed_reason_text is None


def test_create_task_reopens_idempotent_task_after_claim_expiry(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
        idempotency_key="gateway-task-expired-claim",
    )
    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")
    service.start_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )
    state = service._load_state(sync_approvals=False)
    stored_claim = next(item for item in state.claims if item.id == claim.id)
    stored_claim.lease_expires_at = stored_claim.claimed_at - timedelta(seconds=1)
    service._write_state(state)

    reopened = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
        idempotency_key="gateway-task-expired-claim",
    )

    assert reopened.id == task.id
    assert reopened.status == "pending_claim"


def test_claim_start_and_complete_task_require_active_claim_token(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    result_object = service.create_shared_object(
        source_assistant_id="assistant-b",
        filename="result.xlsx",
        parent_object_id=input_object.id,
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Convert this into a spreadsheet.",
    )

    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")
    started = service.start_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )
    completed = service.complete_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
        result_object_id=result_object.id,
    )

    assert started.status == "running"
    assert completed.status == "completed"
    assert completed.result_object_id == result_object.id

    try:
        service.complete_task(
            task_id=task.id,
            assistant_id="assistant-b",
            claim_token=claim.claim_token,
            result_object_id=result_object.id,
        )
    except ValueError as exc:
        assert "can no longer accept execution updates" in str(exc) or "cannot be completed" in str(
            exc
        )
    else:
        raise AssertionError("completed task should not accept duplicate completion")


def test_claim_task_is_idempotent_for_same_active_assistant_claim(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
    )

    first = service.claim_task(task_id=task.id, assistant_id="assistant-b")
    second = service.claim_task(task_id=task.id, assistant_id="assistant-b")

    assert second.id == first.id
    assert second.claim_token == first.claim_token
    assert second.assistant_id == "assistant-b"

    state = service._load_state(sync_approvals=False)
    task_claims = [claim for claim in state.claims if claim.task_id == task.id]
    assert len(task_claims) == 1
    assert task_claims[0].status == "active"


def test_claim_task_reclaims_stale_claimed_task_before_start(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
    )
    first = service.claim_task(task_id=task.id, assistant_id="assistant-b")

    state = service._load_state(sync_approvals=False)
    stored_claim = next(claim for claim in state.claims if claim.id == first.id)
    stored_claim.lease_expires_at = stored_claim.claimed_at - timedelta(seconds=1)
    service._write_state(state)

    second = service.claim_task(task_id=task.id, assistant_id="assistant-b")

    assert second.id != first.id
    assert second.claim_token != first.claim_token
    reloaded = service.get_task(task.id)
    assert reloaded.status == "claimed"
    assert reloaded.started_at is None

    state = service._load_state(sync_approvals=False)
    task_claims = [claim for claim in state.claims if claim.task_id == task.id]
    assert sorted(claim.status for claim in task_claims) == ["active", "expired"]


def test_high_frequency_task_updates_use_direct_repository_paths(tmp_path, monkeypatch) -> None:
    service = _build_service(tmp_path)

    def fail_full_state(*args, **kwargs):
        raise AssertionError("gateway hot-path state updates must not load/save full state")

    monkeypatch.setattr(service.repository, "load_state", fail_full_state)
    monkeypatch.setattr(service.repository, "save_state", fail_full_state)

    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    result_object = service.create_shared_object(
        source_assistant_id="assistant-b",
        filename="result.txt",
        parent_object_id=input_object.id,
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
    )

    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")
    started = service.start_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )
    completed = service.complete_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
        result_object_id=result_object.id,
    )

    assert started.status == "running"
    assert completed.status == "completed"
    assert completed.result_object_id == result_object.id


def test_complete_task_persists_multiple_result_workspace_file_ids(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Create multiple files.",
    )
    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")

    completed = service.complete_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
        result_workspace_file_ids=["file-2", "file-1", "file-2"],
    )

    assert completed.status == "completed"
    assert completed.result_workspace_file_id == "file-2"
    assert completed.meta_json["runtime_result_workspace_file_ids"] == ["file-2", "file-1"]
    assert completed.meta_json["runtime_result_file_count"] == 2
    reloaded = service.get_task(task.id)
    assert reloaded is not None
    assert reloaded.result_workspace_file_id == "file-2"
    assert reloaded.meta_json["runtime_result_workspace_file_ids"] == ["file-2", "file-1"]


def test_renew_claim_extends_active_claim_lease(tmp_path) -> None:
    service = _build_service(tmp_path)
    input_object = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="source.pdf",
    )
    task = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=input_object.id,
        instruction="Process this file.",
    )
    claim = service.claim_task(task_id=task.id, assistant_id="assistant-b")

    state = service._load_state()
    stored_claim = next(item for item in state.claims if item.id == claim.id)
    stored_claim.lease_expires_at = stored_claim.claimed_at + timedelta(seconds=1)
    old_expiry = stored_claim.lease_expires_at
    service._write_state(state)

    renewed = service.renew_claim(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )

    assert renewed.lease_expires_at > old_expiry
    started = service.start_task(
        task_id=task.id,
        assistant_id="assistant-b",
        claim_token=claim.claim_token,
    )
    assert started.status == "running"


def test_cleanup_expires_pending_task_but_keeps_running_input_object(tmp_path) -> None:
    service = _build_service(tmp_path)
    expiring = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="soon-expire.txt",
        retention_policy="temp_shared",
    )
    stable = service.create_shared_object(
        source_assistant_id="assistant-a",
        filename="stable.txt",
        retention_policy="persistent_shared",
    )
    expiring.expires_at = expiring.created_at
    stable.expires_at = stable.created_at

    task_pending = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-b",
        input_object_id=expiring.id,
        instruction="Read this temporary file.",
    )
    task_running = service.create_task(
        source_assistant_id="assistant-a",
        target_assistant_id="assistant-c",
        input_object_id=stable.id,
        instruction="Process this long-running file.",
    )
    claim = service.claim_task(task_id=task_running.id, assistant_id="assistant-c")
    service.start_task(
        task_id=task_running.id,
        assistant_id="assistant-c",
        claim_token=claim.claim_token,
    )

    state = service._load_state(sync_approvals=False)
    for obj in state.objects:
        if obj.id == expiring.id:
            obj.expires_at = obj.created_at
        if obj.id == stable.id:
            obj.expires_at = obj.created_at
    service._write_state(state)

    result = service.cleanup_expired_objects()
    reloaded_pending = service.get_task(task_pending.id)
    reloaded_running = service.get_task(task_running.id)
    expired_object = service.list_shared_objects(status="expired")

    assert result.expired_objects >= 1
    assert reloaded_pending.status == "expired"
    assert reloaded_running.status == "running"
    assert any(item.id == expiring.id for item in expired_object)
    assert all(item.id != stable.id for item in expired_object)


def test_sql_repository_persists_state_across_service_instances(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'collaboration.db'}"
    first = CollaborationService(
        database_url=database_url,
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        storage_defaults=CollaborationObjectStorageDefaults(
            backend="cos",
            bucket="demo-bucket",
            region="ap-shanghai",
            endpoint="https://cos.example.com",
            prefix="heinu/",
        ),
        claim_lease_seconds=60,
    )
    created = first.create_shared_object(
        source_assistant_id="assistant-a",
        filename="persisted.pdf",
    )

    second = CollaborationService(
        database_url=database_url,
        approval_service=ApprovalService(store_path=tmp_path / "approvals.json"),
        storage_defaults=CollaborationObjectStorageDefaults(
            backend="cos",
            bucket="demo-bucket",
            region="ap-shanghai",
            endpoint="https://cos.example.com",
            prefix="heinu/",
        ),
        claim_lease_seconds=60,
    )

    fetched = second.get_shared_object(created.id, assistant_id="assistant-a")
    assert fetched.id == created.id


def test_sql_repository_readyz_checks_required_indexes(tmp_path) -> None:
    service = _build_service(tmp_path)

    ready = service.check_ready()

    assert ready["status"] == "ok"
    assert ready["indexes_checked"] == 3


def test_collaboration_readyz_accepts_later_alembic_head() -> None:
    assert repository_module._alembic_revision_satisfies_required(
        ["025_workspace_files_active_path_key"]
    )


def test_collaboration_readyz_rejects_revision_before_required_schema() -> None:
    assert not repository_module._alembic_revision_satisfies_required(
        ["021_repair_legacy_assistant_ironagent_config"]
    )
