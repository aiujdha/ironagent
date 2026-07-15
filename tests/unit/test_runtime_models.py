from src.runtime.models import AssistantRuntimeRecord


def test_assistant_runtime_record_preserves_paused_and_stopped_status() -> None:
    paused = AssistantRuntimeRecord.model_validate({"status": "paused"})
    stopped = AssistantRuntimeRecord.model_validate({"status": "stopped"})

    assert paused.status == "paused"
    assert paused.desired_state == "paused"
    assert paused.observed_state == "paused"
    assert paused.sync_state == "current"
    assert paused.last_error == ""
    assert stopped.status == "stopped"


def test_assistant_runtime_record_derives_state_fields_for_legacy_status() -> None:
    runtime = AssistantRuntimeRecord.model_validate({"status": "running"})

    assert runtime.status == "running"
    assert runtime.desired_state == "running"
    assert runtime.observed_state == "running"
    assert runtime.sync_state == "current"
    assert runtime.last_runtime_error == ""


def test_assistant_runtime_record_keeps_restarting_compatibility() -> None:
    runtime = AssistantRuntimeRecord.model_validate({"status": "restarting"})

    assert runtime.status == "restarting"
    assert runtime.desired_state == "running"
    assert runtime.observed_state == "restarting"
    assert runtime.sync_state == "pending"
