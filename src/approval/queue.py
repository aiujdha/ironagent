from collections import deque

from src.approval.models import ApprovalRequest
from src.approval.timeout import expire_if_timed_out


class InMemoryApprovalQueue:
    def __init__(self) -> None:
        self._items: deque[ApprovalRequest] = deque()

    def push(self, item: ApprovalRequest) -> None:
        self._items.append(item)

    def list_pending(self) -> list[ApprovalRequest]:
        for item in self._items:
            expire_if_timed_out(item)
        return [item for item in self._items if item.status == "pending"]
