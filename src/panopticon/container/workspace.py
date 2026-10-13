"""Managed workspace runtime. Panopticon's local service remains the sole task authority."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from panopticon.sessionservice.prefill import readiness_log, watch_pane
from panopticon.sessionservice.session_io import capture_pane_snapshot, deliver_pane_input
from panopticon.sessionservice.tmux_defaults import new_session_argv

ROOT = Path(os.environ.get("PANOPTICON_WORKSPACE_ROOT", "/workspace"))
REPOSITORY = re.compile(r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}(?:\.git)?$")
TASK_ID = r"^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$"


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    taskId: str = Field(pattern=TASK_ID)
    runnerId: str = Field(min_length=1, max_length=200)
    repoUrl: str = Field(max_length=200)
    taskCapability: str = Field(pattern=r"^ptc1\.", max_length=1000)
    initialPrompt: str | None = Field(default=None, max_length=16000)
    agent: str = Field(default="panopticon", pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")
    turn: str = Field(default="agent", pattern=r"^(agent|user)$")
    ghToken: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=200)


class WorkspaceRuntime:
    def __init__(
        self,
        root: Path = ROOT,
        *,
        socket: str = "panopticon",
        agent_command: str = "python -m panopticon.container.workspace_agent",
    ) -> None:
        self.socket = socket
        self.agent_command = agent_command
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = root / "panopticon-session.json"
        self.state: dict[str, Any] = (
            json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        )
        self.lock = asyncio.Lock()
        self.draining = False
        self.closing = False
        self.registration: subprocess.Popen[bytes] | None = None

    def save(self) -> None:
        temporary = self.ledger.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state))
        temporary.chmod(0o600)
        temporary.replace(self.ledger)

    def tmux(self, *args: str, check: bool = True) -> str:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], check=check, capture_output=True, text=True
        ).stdout

    def status(self) -> dict[str, Any]:
        session = self.state.get("session")
        running = bool(
            session
            and subprocess.run(
                ["tmux", "-L", self.socket, "has-session", "-t", session], capture_output=True
            ).returncode
            == 0
        )
        return {
            "exists": bool(self.state),
            "running": running,
            "active": bool(running and self.state.get("active")),
            "state": self.state.get("state", "empty"),
            "taskId": self.state.get("config", {}).get("taskId"),
        }

    async def start(self, config: SessionConfig) -> dict[str, Any]:
        if self.draining or self.closing:
            raise HTTPException(503, "Runtime is draining")
        if not REPOSITORY.fullmatch(config.repoUrl) or config.repoUrl.split("/")[-1] in {
            ".",
            "..",
            ".git",
        }:
            raise HTTPException(400, "Only GitHub HTTPS repositories are supported")
        async with self.lock:
            prior = self.state.get("config", {})
            if prior and any(
                prior.get(key) != getattr(config, key) for key in ("taskId", "runnerId", "repoUrl")
            ):
                raise HTTPException(409, "Workspace already belongs to another task")
            if self.status()["running"]:
                return self.status()
            if self.state.get("state") == "closed":
                raise HTTPException(409, "Session is closed")
            generation = os.environ.get("WORKSPACE_COMPUTE_GENERATION", "1")
            if self.state.get("state") in {"running", "starting", "interrupted"}:
                raise HTTPException(409, "Compute was lost; automatic replay is forbidden")
            home = self.root / "home"
            home.mkdir(exist_ok=True)
            auth = home / "task-auth.json"
            auth.write_text(json.dumps({"task": config.taskCapability}))
            auth.chmod(0o600)
            environment = {
                **os.environ,
                "HOME": str(home),
                "PANOPTICON_SERVICE_URL": "http://127.0.0.1:8081",
                "PANOPTICON_SERVICE_AUTH_FILE": str(auth),
                "PANOPTICON_TASK_ID": config.taskId,
                "PANOPTICON_RUNNER_ID": config.runnerId,
                "PANOPTICON_HARNESS": "outfitter",
                "PANOPTICON_STARTING_MODEL": config.agent,
                "PANOPTICON_TASK_TURN": config.turn,
                "PANOPTICON_INITIAL_PROMPT": config.initialPrompt or "",
                "PANOPTICON_ACTIVITY_URL": "http://127.0.0.1:8081/activity",
                "AI_MODEL": config.model or os.environ.get("AI_MODEL", "flash"),
            }
            if config.ghToken:
                environment["GH_TOKEN"] = config.ghToken
            repo = self.root / "repo"
            if not (repo / ".git").exists():
                process = await asyncio.create_subprocess_exec(
                    "git",
                    "-c",
                    "credential.https://github.com.helper=!gh auth git-credential",
                    "clone",
                    "--",
                    config.repoUrl,
                    str(repo),
                    env=environment,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await process.wait():
                    raise HTTPException(
                        422, "Repository clone failed; check repository credentials"
                    )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "credential.https://github.com.helper",
                    "!gh auth git-credential",
                ],
                check=True,
                env=environment,
            )
            if self.closing:
                raise HTTPException(409, "Session is closing")
            self.state = {
                "config": config.model_dump(),
                "session": f"panopticon-{config.taskId}",
                "state": "starting",
                "generation": generation,
                "active": False,
                "deliveries": self.state.get("deliveries", {}),
            }
            self.save()
            # tmux inherits this server's environment only when explicitly updated.
            self.tmux(*new_session_argv(self.socket, "-d", "-s", "runtime"), check=False)
            for key, value in environment.items():
                if key.startswith(("PANOPTICON_", "INFERENCE_")) or key in {
                    "HOME",
                    "AI_MODEL",
                    "GH_TOKEN",
                }:
                    self.tmux("set-environment", key, value)
            command = self.agent_command
            self.tmux(
                *new_session_argv(
                    self.socket, "-d", "-s", self.state["session"], "-c", str(repo), "sleep 86400"
                )
            )

            def run(args: Any, *, check: bool = True) -> str:
                return subprocess.run(args, check=check, capture_output=True, text=True).stdout

            pane = watch_pane(self.state["session"], run=run, prefix=("tmux", "-L", self.socket))
            self.tmux("respawn-pane", "-k", "-t", pane or self.state["session"], command)
            self.registration = subprocess.Popen(
                ["python", "-m", "panopticon.container"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.state["state"] = "running"
            self.save()
            return self.status()

    async def stop(self, state: str) -> dict[str, Any]:
        if state == "closed":
            self.closing = True
        async with self.lock:
            if session := self.state.get("session"):
                self.tmux("kill-session", "-t", session, check=False)
            if self.registration and self.registration.poll() is None:
                self.registration.terminate()
                await asyncio.to_thread(self.registration.wait, 5)
            self.state.update(state=state, active=False)
            self.save()
            return self.status()


def create_apps(runtime: WorkspaceRuntime) -> tuple[FastAPI, FastAPI]:
    app, proxy = FastAPI(), FastAPI()

    @app.middleware("http")
    async def authorized(request: Request, call_next: Any) -> Any:
        if (
            request.headers.get("authorization")
            != f"Bearer {os.environ.get('WORKSPACE_TOKEN', '')}"
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "panopticonSessions": 1, "draining": runtime.draining}

    @app.get("/sessions")
    async def status() -> dict[str, Any]:
        return runtime.status()

    @app.post("/sessions/start")
    async def start(config: SessionConfig) -> dict[str, Any]:
        return await runtime.start(config)

    @app.post("/sessions/resume")
    async def resume() -> dict[str, Any]:
        if not runtime.state.get("config"):
            raise HTTPException(409, "No saved session")
        if runtime.status()["running"]:
            return runtime.status()
        if runtime.state.get("state") != "sleeping":
            raise HTTPException(409, "Session stopped unexpectedly; use explicit recovery")
        return await runtime.start(SessionConfig.model_validate(runtime.state["config"]))

    @app.post("/sessions/provision")
    async def provision(request: Request) -> dict[str, str]:
        data = await request.json()
        slug = data.get("slug", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
            raise HTTPException(400, "Invalid task slug")
        branch = f"panopticon/{slug}"
        repo = str(runtime.root / "repo")
        current = subprocess.run(
            ["git", "-C", repo, "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if current != branch:
            subprocess.run(
                ["git", "-C", repo, "checkout", "-b", branch], check=True, capture_output=True
            )
        return {"branch": branch, "clone": repo}

    @app.post("/sessions/input")
    async def input_(request: Request) -> dict[str, bool]:
        data = await request.json()
        delivery = data.get("deliveryId")
        text = data.get("text")
        if (
            not isinstance(delivery, str)
            or len(delivery) > 200
            or not isinstance(text, str)
            or len(text) > 16000
        ):
            raise HTTPException(400, "Invalid input")
        receipts = runtime.state.setdefault("deliveries", {})
        if delivery in receipts:
            return {"delivered": receipts[delivery] == "delivered"}
        # An uncertain paste is never replayed, even after a process restart.
        receipts[delivery] = "attempted"
        runtime.save()
        session = str(runtime.state.get("session") or "")
        if not runtime.status()["running"]:
            raise HTTPException(409, "Session is not running")

        def run(args: Any, *, check: bool = True) -> str:
            return subprocess.run(args, check=check, capture_output=True, text=True).stdout

        delivered, reason = await asyncio.to_thread(
            deliver_pane_input,
            session,
            text,
            submit=bool(data.get("submit", True)),
            run=run,
            raw_log=readiness_log(session),
            sleep=time.sleep,
            prefix=("tmux", "-L", runtime.socket),
        )
        if not delivered:
            raise HTTPException(409, reason or "Input outcome unknown; not replayed")
        receipts[delivery] = "delivered"
        runtime.save()
        return {"delivered": True}

    @app.post("/sessions/suspend")
    async def suspend() -> dict[str, Any]:
        if runtime.status()["active"]:
            raise HTTPException(409, "Agent is active")
        return await runtime.stop("sleeping")

    @app.post("/sessions/close")
    async def close() -> dict[str, Any]:
        return await runtime.stop("closed")

    @app.post("/sessions/interrupt")
    async def interrupt() -> dict[str, Any]:
        if runtime.status()["running"]:
            runtime.tmux("send-keys", "-t", runtime.state["session"], "C-c")
        runtime.state["active"] = False
        runtime.save()
        return runtime.status()

    @app.get("/sessions/transcript")
    async def transcript() -> dict[str, Any]:
        def run(args: Any, *, check: bool = True) -> str:
            return subprocess.run(args, check=check, capture_output=True, text=True).stdout

        snapshot = capture_pane_snapshot(
            runtime.state["session"], run=run, prefix=("tmux", "-L", runtime.socket)
        )
        if snapshot is None:
            raise HTTPException(503, "Transcript unavailable")
        return snapshot

    @proxy.post("/activity")
    async def activity(request: Request) -> dict[str, bool]:
        runtime.state["active"] = bool((await request.json()).get("active"))
        if runtime.state.get("config"):
            runtime.state["config"]["turn"] = "agent" if runtime.state["active"] else "user"
        runtime.save()
        return {"ok": True}

    @proxy.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    async def callback(path: str, request: Request) -> Any:
        if re.search(r"\\|%2e|%5c|(?:^|/)\.\.(?:/|$)", path, re.I):
            raise HTTPException(400, "Invalid callback path")
        body = await request.body()
        if len(body) > 8 * 1024 * 1024:
            raise HTTPException(413, "Callback too large")
        base = os.environ["WORKSPACE_GATEWAY_URL"].rstrip("/")
        workspace = os.environ["WORKSPACE_ID"]
        url = f"{base}/panopticon/{workspace}/callback/{path}"
        if request.url.query:
            url += "?" + request.url.query
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=10))
        upstream = await client.send(
            client.build_request(
                request.method,
                url,
                content=body,
                headers={
                    "Authorization": f"Bearer {os.environ['WORKSPACE_TOKEN']}",
                    "Content-Type": request.headers.get("content-type", "application/json"),
                },
            ),
            stream=True,
        )

        async def cleanup() -> None:
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(cleanup),
        )

    @app.websocket("/terminal")
    async def terminal(ws: WebSocket) -> None:
        if (
            ws.headers.get("authorization") != f"Bearer {os.environ.get('WORKSPACE_TOKEN', '')}"
            or not runtime.status()["running"]
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        master, slave = pty.openpty()
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "-L",
            runtime.socket,
            "attach-session",
            "-t",
            runtime.state["session"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
            start_new_session=True,
        )
        os.close(slave)

        async def output() -> None:
            while data := await asyncio.to_thread(os.read, master, 65536):
                await ws.send_json({"type": "output", "data": base64.b64encode(data).decode()})

        output_task = asyncio.create_task(output())
        try:
            while True:
                frame = await ws.receive_json()
                if frame.get("type") == "input":
                    data = base64.b64decode(frame.get("data", ""), validate=True)
                    if len(data) > 65536:
                        break
                    os.write(master, data)
                elif frame.get("type") == "resize":
                    columns, rows = int(frame["columns"]), int(frame["rows"])
                    if not (1 <= columns <= 1000 and 1 <= rows <= 1000):
                        break
                    fcntl.ioctl(
                        master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
                    )
        finally:
            output_task.cancel()
            os.close(master)
            if process.returncode is None:
                process.terminate()
            await process.wait()
            with contextlib.suppress(Exception):
                await output_task

    return app, proxy


async def serve() -> None:
    from collections.abc import Iterator

    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    runtime = WorkspaceRuntime()
    app, proxy = create_apps(runtime)
    servers = [
        Server(uvicorn.Config(app, host="0.0.0.0", port=8080, access_log=False)),
        Server(uvicorn.Config(proxy, host="127.0.0.1", port=8081, access_log=False)),
    ]

    async def shutdown() -> None:
        runtime.draining = True
        deadline = time.monotonic() + 30
        while runtime.status()["active"] and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        await runtime.stop("interrupted" if runtime.status()["active"] else "sleeping")
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(serve())
