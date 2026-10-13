"""Remote admission and task identity without a cloud account or agent."""

# 2119-spec: outfitter-remote
from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from panopticon.sessionservice.outfitter_runner import OutfitterRunner
from panopticon.sessionservice.spawner import Spawner
from panopticon.taskservice.auth import snapshot_task_capability


class Remote(OutfitterRunner):
    def __init__(self, root: Path, client: MagicMock) -> None:
        super().__init__(
            client, runner_id="runner", service_url="http://127.0.0.1:8000", state_root=root
        )
        self.calls: list[tuple[tuple[str, ...], dict | None]] = []
        self.supported = True

    def command(self, *args, payload=None, application_home=None):
        self.calls.append((args, payload))
        if args[0] == "capabilities":
            return {"panopticonSessions": 1 if self.supported else 0}
        if payload and payload.get("operation") == "capabilities":
            return {"panopticonSessions": 1}
        if args[0] == "inspect":
            return {"state": "Running"}
        return {"running": True}

    def ensure_bridge(self, task_id: str) -> None:
        import os

        self.capabilities[task_id] = snapshot_task_capability(
            os.environ["PANOPTICON_SERVICE_AUTH_FILE"], task_id, directory=self.root
        )

    def attach_session(self, task_id: str) -> None:
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    config = tmp_path / "config"
    secrets = config / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    auth = secrets / "auth.json"
    auth.write_text(json.dumps({"write": ["fleet-token-that-must-never-leave-host"], "read": []}))
    auth.chmod(0o600)
    monkeypatch.setenv("PANOPTICON_CONFIG", str(config))
    monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_FILE", "auth.json")
    client = MagicMock()
    task = {
        "id": str(uuid.uuid4()),
        "harness": "outfitter",
        "repo_id": "repo",
        "workflow": "spike",
        "state": "WORKING",
        "claimed_by": None,
        "turn": "agent",
    }
    repo = {
        "id": "repo",
        "name": "project",
        "git_url": "https://github.com/ai-outfitter/panopticon.git",
    }
    client.get_repo.return_value = repo
    client.get_task.return_value = task
    client.workflow_image_layer.return_value = ""
    client.workflow_execution.return_value = {"runner_type": "container"}
    return Remote(tmp_path / "remote", client), client, task, repo


# 2119: 2.4
def test_identity_survives_repeated_start_and_runner_restart(context):
    remote, client, task, repo = context
    first = remote.spawn_task(task, repo)
    second = remote.spawn_task(task, repo)
    restarted = Remote(remote.root, client)
    assert first == second == restarted.workspace(task["id"])
    other = {**task, "id": str(uuid.uuid4())}
    assert remote.workspace(other["id"]) != first
    assert remote.installation == restarted.installation


# 2119: 3.2
# 2119: 3.3
# 2119: 3.4
def test_only_the_task_capability_is_transmitted(context, monkeypatch):
    remote, _, task, repo = context
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("OUTFITTER_REFRESH_TOKEN", "refresh-secret")
    remote.spawn_task(task, repo)
    starts = [
        payload for _, payload in remote.calls if payload and payload.get("operation") == "start"
    ]
    config = starts[0]["config"]
    assert config["taskCapability"].startswith("ptc1.")
    body = json.dumps(starts)
    assert "fleet-token-that-must-never-leave-host" not in body
    assert "provider-secret" not in body and "refresh-secret" not in body
    assert config["taskId"] == task["id"]


# 2119: 2.3
@pytest.mark.parametrize(
    "changes",
    [
        {"git_url": "/local/repo"},
        {"capabilities": {"docker_in_docker": True}},
        {"image_layer_file": "custom"},
        {"hook_file": "host.sh"},
    ],
)
def test_unsupported_remote_tasks_are_rejected_before_claim(context, changes):
    remote, client, task, repo = context
    client.get_repo.return_value = {**repo, **changes}
    spawner = Spawner(
        client,
        MagicMock(),
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    with pytest.raises(ValueError):
        spawner.spawn_one(task)
    client.claim.assert_not_called()


# 2119: 2.5
def test_incompatible_gateway_is_rejected_before_claim(context):
    remote, client, task, _ = context
    remote.supported = False
    spawner = Spawner(
        client,
        MagicMock(),
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    with pytest.raises(ValueError, match="Incompatible"):
        spawner.spawn_one(task)
    client.claim.assert_not_called()


# 2119: 2.1
# 2119: 2.2
def test_execution_location_does_not_change_local_harnesses(context):
    remote, client, task, _ = context
    local = MagicMock()
    disabled = Spawner(client, local, runner_id="runner", cache=MagicMock(), tasks_root="/unused")
    enabled = Spawner(
        client,
        local,
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    assert disabled._runner_for(task) is local
    assert enabled._runner_for(task) is remote
    for name in ("claude", "codex", "pi"):
        assert enabled._runner_for({**task, "harness": name}) is local


# 2119: 4.6
def test_remote_agent_work_is_not_automatically_healed(context):
    remote, client, task, _ = context
    spawner = Spawner(
        client,
        MagicMock(),
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    assert spawner.heal({**task, "claimed_by": "runner", "container_status": "down"}) is None
    assert remote.calls == []


# 2119: 2.4
def test_concurrent_installation_startup_keeps_one_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    task = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(
            pool.map(lambda _: Remote(tmp_path, MagicMock()).workspace(task), range(16))
        )
    assert len(set(identities)) == 1


# 2119: 2.5
def test_incompatible_runtime_is_rejected_before_claim(context):
    remote, client, task, _repo = context
    original = remote.command

    def command(*args, payload=None, application_home=None):
        if payload and payload.get("operation") == "capabilities":
            return {"panopticonSessions": 0}
        return original(*args, payload=payload, application_home=application_home)

    remote.command = command
    spawner = Spawner(
        client,
        MagicMock(),
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    with pytest.raises(ValueError, match="runtime"):
        spawner.spawn_one(task)
    assert ("expire", remote.workspace(task["id"])) == remote.calls[-1][0]
    client.claim.assert_not_called()


# 2119: 3.7
def test_remote_branch_provisioning_is_recorded_through_local_service(context):
    remote, client, task, _repo = context
    task.update(slug="remote-plan", provisioned=False)
    original = remote.command

    def command(*args, payload=None, application_home=None):
        if payload and payload.get("operation") == "provision":
            return {"branch": "panopticon/remote-plan", "clone": "/workspace/repo"}
        return original(*args, payload=payload, application_home=application_home)

    remote.command = command
    assert remote.provision(task) == "panopticon/remote-plan"
    client.record_provisioning.assert_called_once_with(
        task["id"], "panopticon/remote-plan", "/workspace/repo", "runner", True
    )


# 2119: 4.3
@pytest.mark.parametrize("state", ["COMPLETE", "DROPPED"])
def test_terminal_tasks_close_the_managed_session(context, monkeypatch, state):
    from panopticon.sessionservice import outfitter_runner

    remote, client, task, _repo = context
    monkeypatch.setattr(outfitter_runner.subprocess, "run", lambda *args, **kwargs: MagicMock())
    spawner = Spawner(
        client,
        MagicMock(),
        remote_runner=remote,
        runner_id="runner",
        cache=MagicMock(),
        tasks_root="/unused",
    )
    spawner.cleanup({**task, "state": state, "claimed_by": "runner"})
    assert remote.calls == [(("close", remote.workspace(task["id"])), None)]
    client.release.assert_called_once_with(task["id"])
