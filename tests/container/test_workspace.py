"""Runtime boundaries, persistence and idempotent input without real agents."""

# 2119-spec: outfitter-remote
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from panopticon.container.workspace import SessionConfig, WorkspaceRuntime, create_apps


def config() -> SessionConfig:
    return SessionConfig(
        taskId="11111111111141118111111111111111",
        runnerId="runner",
        repoUrl="https://github.com/ai-outfitter/panopticon.git",
        taskCapability="ptc1.task.self.mac",
    )


# 2119: 4.6
@pytest.mark.parametrize("generation", ["1", "2"])
async def test_unexpected_compute_generation_never_relaunches(tmp_path, monkeypatch, generation):
    runtime = WorkspaceRuntime(tmp_path)
    runtime.state = {"config": config().model_dump(), "state": "running", "generation": "1"}
    runtime.save()
    monkeypatch.setenv("WORKSPACE_COMPUTE_GENERATION", generation)
    monkeypatch.setattr(runtime, "status", lambda: {"running": False})
    with pytest.raises(Exception, match="409"):
        await runtime.start(config())
    assert json.loads(runtime.ledger.read_text())["generation"] == "1"


# 2119: 4.2
# 2119: 4.8
async def test_healthy_sleep_preserves_repository_and_conversation(tmp_path, monkeypatch):
    runtime = WorkspaceRuntime(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    work = tmp_path / "repo" / "unfinished.txt"
    work.write_text("work remains")
    session = tmp_path / "home" / ".outfitter" / ".pi" / "agent" / "sessions" / "history.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("conversation remains")
    runtime.state = {"config": config().model_dump(), "state": "sleeping", "generation": "1"}
    monkeypatch.setenv("WORKSPACE_COMPUTE_GENERATION", "2")
    monkeypatch.setattr(runtime, "status", lambda: {"running": False, "active": False})
    monkeypatch.setattr(runtime, "tmux", lambda *args, **kwargs: "")
    from panopticon.container import workspace

    monkeypatch.setattr(
        workspace.subprocess, "run", lambda *args, **kwargs: MagicMock(stdout="%1", returncode=0)
    )
    monkeypatch.setattr(workspace.subprocess, "Popen", lambda *args, **kwargs: MagicMock())
    await runtime.start(config())
    assert work.read_text() == "work remains"
    assert session.read_text() == "conversation remains"
    assert runtime.state["generation"] == "2"


# 2119: 3.5
def test_uncertain_input_is_not_replayed(tmp_path, monkeypatch):
    runtime = WorkspaceRuntime(tmp_path)
    runtime.state = {"session": "task", "state": "sleeping"}
    monkeypatch.setenv("WORKSPACE_TOKEN", "runtime-token")
    monkeypatch.setattr(runtime, "status", lambda: {"running": False})
    app, _ = create_apps(runtime)
    with TestClient(app) as client:
        request = {"deliveryId": "delivery-1", "text": "apply changes", "submit": True}
        headers = {"Authorization": "Bearer runtime-token"}
        assert client.post("/sessions/input", json=request, headers=headers).status_code == 409
        assert client.post("/sessions/input", json=request, headers=headers).json() == {
            "delivered": False
        }
    assert json.loads(runtime.ledger.read_text())["deliveries"]["delivery-1"] == "attempted"


# 2119: 4.7
def test_active_agent_cannot_be_suspended(tmp_path, monkeypatch):
    runtime = WorkspaceRuntime(tmp_path)
    monkeypatch.setenv("WORKSPACE_TOKEN", "runtime-token")
    monkeypatch.setattr(runtime, "status", lambda: {"active": True})
    app, _ = create_apps(runtime)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        assert (
            client.post(
                "/sessions/suspend", headers={"Authorization": "Bearer runtime-token"}
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "https://github.com.evil/repo",
        "https://user:secret@github.com/org/repo",
        "https://github.com/org/../repo",
    ],
)
async def test_repository_input_rejects_non_github_sources(tmp_path: Path, url: str):
    runtime = WorkspaceRuntime(tmp_path)
    with pytest.raises(Exception, match="400"):
        await runtime.start(config().model_copy(update={"repoUrl": url}))


async def test_close_cannot_be_overwritten_by_start_during_clone(tmp_path, monkeypatch):
    import asyncio

    from panopticon.container import workspace

    entered, release = asyncio.Event(), asyncio.Event()

    async def clone(*args, **kwargs):
        entered.set()
        await release.wait()
        return MagicMock(wait=wait)

    async def wait():
        return 0

    runtime = WorkspaceRuntime(tmp_path)
    monkeypatch.setattr(workspace.asyncio, "create_subprocess_exec", clone)
    monkeypatch.setattr(
        workspace.subprocess, "run", lambda *args, **kwargs: MagicMock(returncode=0)
    )
    monkeypatch.setattr(runtime, "tmux", MagicMock())
    starting = asyncio.create_task(runtime.start(config()))
    await entered.wait()
    closing = asyncio.create_task(runtime.stop("closed"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(Exception, match="409"):
        await starting
    await closing
    assert runtime.state["state"] == "closed"
    runtime.tmux.assert_not_called()


def test_idle_activity_persists_user_turn_for_healthy_resume(tmp_path, monkeypatch):
    runtime = WorkspaceRuntime(tmp_path)
    runtime.state = {"config": config().model_dump(), "active": True, "state": "running"}
    _, proxy = create_apps(runtime)
    with TestClient(proxy) as client:
        assert client.post("/activity", json={"active": False}).status_code == 200
    restored = WorkspaceRuntime(tmp_path)
    assert restored.state["config"]["turn"] == "user"
    assert restored.state["active"] is False


# 2119: 4.1
# 2119: 4.2
# 2119: 4.8
async def test_real_tmux_fake_agent_survives_detach_and_healthy_sleep(tmp_path, monkeypatch):
    import asyncio
    import shlex
    import shutil
    import subprocess
    import sys
    import uuid

    from panopticon.container import workspace

    if not shutil.which("tmux"):
        pytest.skip("requires tmux; no real agent or provider is used")
    socket = f"panopticon-test-{uuid.uuid4().hex}"
    (tmp_path / "repo").mkdir()
    subprocess.run(["git", "init", str(tmp_path / "repo")], check=True, capture_output=True)
    history = tmp_path / "home" / ".outfitter" / ".pi" / "agent" / "sessions" / "fake.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text('{"message":"accepted plan"}\n')
    work = tmp_path / "repo" / "unfinished.txt"
    work.write_text("preserved work")
    observed = tmp_path / "observed.json"
    fake = tmp_path / "fake.py"
    fake.write_text(f"""import pathlib,time,signal
pathlib.Path({str(observed)!r}).write_text(pathlib.Path({str(history)!r}).read_text())
print("fake agent ready",flush=True)
while True: time.sleep(0.1)
""")
    runtime = WorkspaceRuntime(
        tmp_path, socket=socket, agent_command=shlex.join([sys.executable, str(fake)])
    )
    original_popen = subprocess.Popen

    def popen(args, **kwargs):
        if args == ["python", "-m", "panopticon.container"]:
            return MagicMock(poll=lambda: 0)
        return original_popen(args, **kwargs)

    monkeypatch.setattr(workspace.subprocess, "Popen", popen)
    monkeypatch.setenv("WORKSPACE_TOKEN", "test-runtime-token")
    try:
        await runtime.start(config())
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.02)
        assert runtime.status()["running"]
        assert observed.read_text() == history.read_text()
        app, _ = create_apps(runtime)
        with TestClient(app) as client:
            with client.websocket_connect(
                "/terminal", headers={"Authorization": "Bearer test-runtime-token"}
            ) as ws:
                ws.send_json({"type": "resize", "columns": 100, "rows": 30})
                assert ws.receive_json()["type"] == "output"
            assert runtime.status()["running"]
        await runtime.stop("sleeping")
        observed.unlink()
        monkeypatch.setenv("WORKSPACE_COMPUTE_GENERATION", "2")
        await runtime.start(config())
        for _ in range(100):
            if observed.exists():
                break
            await asyncio.sleep(0.02)
        assert observed.read_text() == '{"message":"accepted plan"}\n'
        assert work.read_text() == "preserved work"
        assert runtime.status()["running"]
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], check=False, capture_output=True)
