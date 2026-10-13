"""Route existing workflow wake and transcript operations to the selected backend."""

from typing import Any

from panopticon.client import TaskServiceClient
from panopticon.sessionservice.local_runner import LocalRunner
from panopticon.sessionservice.outfitter_runner import OutfitterRunner


class RemoteSessionIO:
    def __init__(
        self, client: TaskServiceClient, local: LocalRunner, remote: OutfitterRunner | None
    ) -> None:
        self.client, self.local, self.remote = client, local, remote

    def backend(self, task_id: str) -> LocalRunner | OutfitterRunner:
        if self.remote is not None and self.client.get_task(task_id).get("harness") == "outfitter":
            return self.remote
        return self.local

    def submit_prompt(self, task_id: str, prompt: str) -> bool:
        return self.backend(task_id).submit_prompt(task_id, prompt)

    def deliver_session_input(
        self, task_id: str, delivery_id: str, text: str, *, submit: bool
    ) -> tuple[bool, str | None]:
        return self.backend(task_id).deliver_session_input(
            task_id, delivery_id, text, submit=submit
        )

    def capture_session_transcript(self, task_id: str) -> dict[str, Any] | None:
        return self.backend(task_id).capture_session_transcript(task_id)

    def can_wake(self, task_id: str) -> bool:
        backend = self.backend(task_id)
        if isinstance(backend, OutfitterRunner):
            status = backend.status(task_id)
            return bool(status.get("sleeping") and not status.get("interrupted"))
        return False
