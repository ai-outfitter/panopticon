"""Optional remote execution through the published Outfitter CLI. No inference calls."""

from __future__ import annotations

import fcntl
import json
import os
import select
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from panopticon.client import JsonObj, TaskServiceClient
from panopticon.sessionservice.auth_readiness import configured_environment
from panopticon.sessionservice.git_credentials import repo_git_transport
from panopticon.sessionservice.local_runner import session_name
from panopticon.sessionservice.tmux_defaults import new_session_argv
from panopticon.taskservice.auth import snapshot_task_capability


class OutfitterRunner:
    def __init__(
        self,
        client: TaskServiceClient,
        *,
        runner_id: str,
        service_url: str,
        state_root: Path,
        binary: str = "outfitter",
    ) -> None:
        self.client, self.runner_id, self.service_url = client, runner_id, service_url
        self.root, self.binary = state_root, binary
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        identity = self.root / "installation"
        with (self.root / "installation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not identity.exists():
                identity.write_text(str(uuid.uuid4()))
            identity.chmod(0o600)
        self.installation = str(uuid.UUID(identity.read_text()))
        self.bridges: dict[str, subprocess.Popen[str]] = {}
        self.capabilities: dict[str, Path] = {}
        self.application_homes: dict[str, str] = {}

    def command(
        self, *args: str, payload: JsonObj | None = None, application_home: str | None = None
    ) -> JsonObj:
        workspace = args[1] if len(args) > 1 else ""
        app_home = application_home or self.application_homes.get(workspace)
        env = {**os.environ, **({"OUTFITTER_USER_HOME": app_home} if app_home else {})}
        result = subprocess.run(
            [self.binary, "workspaces", *args],
            input=json.dumps(payload) if payload is not None else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=40,
            env=env,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("Invalid Outfitter response")
        return value

    def workspace(self, task_id: str) -> str:
        return str(uuid.uuid5(uuid.UUID(self.installation), task_id))

    def validate_configuration(self) -> None:
        if self.command("capabilities").get("panopticonSessions") != 1:
            raise ValueError("Outfitter gateway does not support Panopticon sessions")

    def validate_task(self, task: JsonObj, repo: JsonObj, layer: str = "") -> None:
        if task.get("harness") != "outfitter":
            raise ValueError("Only Outfitter tasks support remote execution")
        transport = repo_git_transport(repo)
        if not transport.operation_url.startswith("https://github.com/"):
            raise ValueError("Remote execution requires a GitHub repository")
        if (
            repo.get("image_layer_file")
            or layer.strip()
            or repo.get("hook_file")
            or (repo.get("capabilities") or {}).get("docker_in_docker")
        ):
            raise ValueError(
                "Remote execution does not support custom image layers, host hooks, or privileged Docker"
            )
        if task.get("starting_model") not in (None, "", "panopticon"):
            raise ValueError(
                "Remote v1 uses the panopticon agent and your configured Outfitter inference"
            )
        if not os.environ.get("PANOPTICON_SERVICE_AUTH_FILE"):
            raise ValueError("Remote execution requires enforced task-service authentication")
        from panopticon.core.dirs import secrets_file_path

        if reference := repo.get("credential_dir"):
            app_home = str(Path(str(secrets_file_path(reference))) / "outfitter")
            self.application_homes[self.workspace(task["id"])] = app_home
        if (
            self.command(
                "capabilities",
                application_home=self.application_homes.get(self.workspace(task["id"])),
            ).get("panopticonSessions")
            != 1
        ):
            raise ValueError("Incompatible Outfitter gateway")
        # Probe the selected runtime before acquiring the task claim. Creation is idempotent.
        import time

        workspace = self.workspace(task["id"])
        self.command("create", workspace)
        deadline = time.monotonic() + 120
        while self.command("inspect", workspace).get("state") not in {"Running", "Sleeping"}:
            if time.monotonic() >= deadline:
                raise ValueError("Outfitter runtime did not become ready")
            time.sleep(1)
        if self.command("inspect", workspace).get("state") == "Sleeping":
            self.command("wake", workspace)
            while self.command("inspect", workspace).get("state") != "Running":
                if time.monotonic() >= deadline:
                    raise ValueError("Outfitter runtime did not wake")
                time.sleep(1)
        capabilities = self.command("session", workspace, payload={"operation": "capabilities"})
        if capabilities.get("panopticonSessions") != 1:
            self.command("expire", workspace)
            raise ValueError("Incompatible Outfitter runtime")

    def ensure_bridge(self, task_id: str) -> None:
        from panopticon.core.dirs import secrets_file_path

        task = self.client.get_task(task_id)
        repo = self.client.get_repo(task["repo_id"])
        if reference := repo.get("credential_dir"):
            self.application_homes[self.workspace(task_id)] = str(
                Path(str(secrets_file_path(reference))) / "outfitter"
            )
        previous = self.bridges.get(task_id)
        if previous is not None and previous.poll() is None:
            return
        for snapshot in self.root.glob(f"{task_id}.capability-*.json"):
            snapshot.unlink(missing_ok=True)
        capability = snapshot_task_capability(
            os.environ["PANOPTICON_SERVICE_AUTH_FILE"],
            task_id,
            directory=self.root,
            prefix=f"{task_id}.capability-",
        )
        self.capabilities[task_id] = capability
        log = (self.root / f"{task_id}.bridge.log").open("a")
        process = subprocess.Popen(
            [
                self.binary,
                "workspaces",
                "connect",
                self.workspace(task_id),
                "--installation",
                self.installation,
                "--service-url",
                self.service_url,
                "--task-token-file",
                str(capability),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            text=True,
            env={
                **os.environ,
                **(
                    {"OUTFITTER_USER_HOME": self.application_homes[self.workspace(task_id)]}
                    if self.workspace(task_id) in self.application_homes
                    else {}
                ),
            },
        )
        log.close()
        self.bridges[task_id] = process
        if process.stdout is None or not select.select([process.stdout], [], [], 30)[0]:
            process.terminate()
            raise RuntimeError("Outfitter bridge did not connect")
        connected = process.stdout.readline()
        if not connected or not json.loads(connected).get("connected"):
            raise RuntimeError("Outfitter bridge connection failed")

    def spawn_task(self, task: JsonObj, repo: JsonObj) -> str:
        task_id = str(task["id"])
        workspace = self.workspace(task_id)
        self.command("create", workspace, "--title", str(task.get("memo") or "Panopticon")[:60])
        self.command(
            "session",
            workspace,
            payload={
                "operation": "bind",
                "installation": self.installation,
                "task": task_id,
                "id": str(uuid.uuid5(uuid.UUID(workspace), "session")),
            },
        )
        view = self.command("inspect", workspace)
        if view.get("state") == "Interrupted":
            raise RuntimeError("Remote compute was interrupted; recover explicitly before retrying")
        if view.get("state") == "Sleeping":
            self.command("wake", workspace)
        self.ensure_bridge(task_id)
        transport = repo_git_transport(repo)
        config: JsonObj = {
            "taskId": task_id,
            "runnerId": self.runner_id,
            "repoUrl": transport.operation_url,
            "taskCapability": json.loads(self.capabilities[task_id].read_text())["task"],
            "initialPrompt": task.get("initial_prompt"),
            "turn": task.get("turn") or "agent",
        }
        configured = configured_environment(repo.get("env_file"))
        if token := configured.get("GH_TOKEN"):
            config["ghToken"] = token.strip().strip("\"'")
        import time

        deadline = time.monotonic() + 120
        while self.command("inspect", workspace).get("state") != "Running":
            if time.monotonic() >= deadline:
                raise RuntimeError("Outfitter workspace did not become ready")
            time.sleep(1)
        self.command(
            "session",
            workspace,
            payload={"operation": "start", "runnerId": self.runner_id, "config": config},
        )
        self.attach_session(task_id)
        return workspace

    def attach_session(self, task_id: str) -> None:
        session = session_name(task_id)
        exists = (
            subprocess.run(
                ["tmux", "-L", "panopticon", "has-session", "-t", session], capture_output=True
            ).returncode
            == 0
        )
        if not exists:
            command = shlex.join(
                [
                    self.binary,
                    "workspaces",
                    "attach",
                    self.workspace(task_id),
                    "--installation",
                    self.installation,
                ]
            )
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    "panopticon",
                    *new_session_argv("panopticon", "-d", "-s", session, command),
                ],
                check=True,
            )

    def status(self, task_id: str) -> JsonObj:
        view = self.command("inspect", self.workspace(task_id))
        if view.get("state") in {"Sleeping", "Starting", "Provisioning"}:
            return {"exists": True, "running": False, "sleeping": True}
        if view.get("state") == "Interrupted":
            return {"exists": True, "running": False, "interrupted": True}
        self.ensure_bridge(task_id)
        return self.command_session_status(task_id)

    def command_session_status(self, task_id: str) -> JsonObj:
        return self.command("session-status", self.workspace(task_id))

    def has_session(self, task_id: str) -> bool:
        status = self.status(task_id)
        # No automatic replay on lost compute; explicit R is the recovery surface.
        return bool(status.get("exists"))

    def is_running(self, task_id: str) -> bool:
        return bool(self.status(task_id).get("running"))

    def provision(self, task: JsonObj) -> str | None:
        if not task.get("slug") or task.get("provisioned"):
            return None
        result = self.command(
            "session",
            self.workspace(task["id"]),
            payload={"operation": "provision", "slug": task["slug"]},
        )
        self.client.record_provisioning(
            task["id"], result["branch"], result["clone"], self.runner_id, True
        )
        return str(result["branch"])

    def stop(self, workspace: str) -> None:
        self.command("session", workspace, payload={"operation": "interrupt"})

    def close_task(self, task_id: str) -> None:
        self.command("close", self.workspace(task_id))
        process = self.bridges.pop(task_id, None)
        if process is not None and process.poll() is None:
            process.terminate()
        capability = self.capabilities.pop(task_id, None)
        if capability:
            capability.unlink(missing_ok=True)
        subprocess.run(
            ["tmux", "-L", "panopticon", "kill-session", "-t", session_name(task_id)],
            capture_output=True,
        )

    def deliver_session_input(
        self, task_id: str, delivery_id: str, text: str, *, submit: bool
    ) -> tuple[bool, str | None]:
        task = self.client.get_task(task_id)
        self.spawn_task(task, self.client.get_repo(task["repo_id"]))
        result = self.command(
            "session",
            self.workspace(task_id),
            payload={
                "operation": "input",
                "deliveryId": delivery_id,
                "text": text,
                "submit": submit,
            },
        )
        return bool(result.get("delivered")), None if result.get(
            "delivered"
        ) else "remote-input-uncertain"

    def submit_prompt(self, task_id: str, prompt: str) -> bool:
        history = self.client.get_task(task_id).get("history", [])
        key = f"entry-{len(history)}"
        return self.deliver_session_input(task_id, key, prompt, submit=True)[0]

    def capture_session_transcript(self, task_id: str) -> dict[str, Any] | None:
        if not self.is_running(task_id):
            return None
        return self.command("session-transcript", self.workspace(task_id))
