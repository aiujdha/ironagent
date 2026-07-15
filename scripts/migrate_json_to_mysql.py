from __future__ import annotations

from dataclasses import dataclass

from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.shared.repositories import (
    DatabaseApprovalRepository,
    DatabaseAssistantRepository,
    DatabaseAuditRepository,
    JsonApprovalRepository,
    JsonAssistantRepository,
    JsonAuditRepository,
)


@dataclass(slots=True)
class MigrationSummary:
    name: str
    total: int


def main() -> None:
    settings = get_settings()
    logger = get_logger("scripts.migrate_json_to_mysql")

    assistant_source = JsonAssistantRepository(settings.assistants_store_path)
    approval_source = JsonApprovalRepository(settings.approval_store_path)
    audit_source = JsonAuditRepository(settings.audit_log_path)

    assistant_target = DatabaseAssistantRepository()
    approval_target = DatabaseApprovalRepository()
    audit_target = DatabaseAuditRepository()

    assistant_records = assistant_source.list_assistants()
    approval_records = approval_source.list_requests()
    audit_rows = audit_source.list_recent(limit=max(audit_source.count_events(), 1_000_000))

    for record in assistant_records:
        assistant_target.upsert_assistant(record)
    for record in approval_records:
        if approval_target.get_request(record.id) is None:
            approval_target.create_request(record)
        else:
            approval_target.save_request(record)
    for row in reversed(audit_rows):
        audit_target.append_event(row)

    summaries = [
        MigrationSummary(name="assistants", total=len(assistant_records)),
        MigrationSummary(name="approvals", total=len(approval_records)),
        MigrationSummary(name="audit_events", total=len(audit_rows)),
    ]
    for summary in summaries:
        logger.info("migration_completed", table=summary.name, total=summary.total)


if __name__ == "__main__":
    main()
