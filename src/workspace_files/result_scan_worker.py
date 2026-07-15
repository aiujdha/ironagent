"""Background polling worker for assistant workspace result files."""

from __future__ import annotations

from dataclasses import dataclass

from src.assistants.service import AssistantService
from src.shared.logging import get_logger
from src.workspace_files.service import WorkspaceFileService

log = get_logger(__name__)


@dataclass(slots=True)
class ScanCycleResult:
    assistants_scanned: int
    files_ingested: int


class WorkspaceResultScanWorker:
    def __init__(
        self,
        *,
        assistant_service: AssistantService | None = None,
        file_service: WorkspaceFileService | None = None,
    ) -> None:
        self.assistant_service = assistant_service or AssistantService()
        self.file_service = file_service or WorkspaceFileService()

    def scan_once(self) -> ScanCycleResult:
        assistants = self.assistant_service.list_assistants()
        total_ingested = 0
        scanned_assistants = 0
        for assistant in assistants:
            if assistant.deleted:
                continue
            workspace_root = self.file_service._workspace_dir(assistant.assistant_id)
            if not workspace_root.exists():
                continue
            scanned_assistants += 1
            ingested = self.file_service.scan_workspace_results(assistant.assistant_id)
            if ingested:
                log.info(
                    "workspace_result_scan_ingested",
                    assistant_id=assistant.assistant_id,
                    file_count=len(ingested),
                )
            total_ingested += len(ingested)
        result = ScanCycleResult(
            assistants_scanned=scanned_assistants,
            files_ingested=total_ingested,
        )
        log.info(
            "workspace_result_scan_completed",
            assistants_scanned=result.assistants_scanned,
            files_ingested=result.files_ingested,
        )
        return result
