from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.assistants.models import AssistantRecord
from src.assistants.service import AssistantService
from src.shared.config import get_settings
from src.shared.logging import get_logger
from src.workspace_files.service import WorkspaceFileService

log = get_logger(__name__)


@dataclass(slots=True)
class WorkspaceScanSummary:
    scanned_assistants: int = 0
    synced_files: int = 0


class WorkspaceResultScanner:
    def __init__(
        self,
        *,
        assistant_service: AssistantService | None = None,
        file_service: WorkspaceFileService | None = None,
        scan_interval_ms: int | None = None,
    ) -> None:
        settings = get_settings()
        self.assistant_svc = assistant_service or AssistantService()
        self.file_svc = file_service or WorkspaceFileService()
        self.scan_interval_ms = scan_interval_ms or settings.workspace_scan_interval_ms

    @staticmethod
    def _running_assistants(items: Iterable[AssistantRecord]) -> list[AssistantRecord]:
        running: list[AssistantRecord] = []
        seen: set[str] = set()
        for item in items:
            if item.deleted:
                continue
            if item.runtime.status != "running" or not item.runtime.workspace_root:
                continue
            if item.assistant_id in seen:
                continue
            seen.add(item.assistant_id)
            running.append(item)
        return running

    def _workspace_root_matches_file_service(self, item: AssistantRecord) -> bool:
        """Skip recovered stale runtimes that point outside this file service root."""
        try:
            runtime_root = Path(item.runtime.workspace_root).expanduser().resolve()
            service_root = (
                Path(self.file_svc._workspace_dir(item.assistant_id))
                .expanduser()
                .resolve()
            )
        except Exception:
            return True
        return runtime_root == service_root

    async def scan_once(self) -> WorkspaceScanSummary:
        assistants = await self.assistant_svc.alist_assistants()
        running = [
            item
            for item in self._running_assistants(assistants)
            if self._workspace_root_matches_file_service(item)
        ]
        summary = WorkspaceScanSummary(scanned_assistants=len(running))

        for assistant in running:
            records = await asyncio.to_thread(
                self.file_svc.scan_workspace_results,
                assistant.assistant_id,
            )
            summary.synced_files += len(records)

        if summary.scanned_assistants or summary.synced_files:
            log.info(
                "workspace_scan_completed",
                scanned_assistants=summary.scanned_assistants,
                synced_files=summary.synced_files,
            )
        return summary

    async def run(self, stop_event: asyncio.Event) -> None:
        interval_seconds = max(self.scan_interval_ms, 1000) / 1000
        log.info("workspace_scanner_started", interval_seconds=interval_seconds)
        try:
            while not stop_event.is_set():
                try:
                    await self.scan_once()
                except Exception as exc:  # pragma: no cover - defensive runtime guard
                    log.exception("workspace_scan_failed", error=str(exc))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue
        finally:
            log.info("workspace_scanner_stopped")
