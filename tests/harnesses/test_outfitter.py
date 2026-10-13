"""Published Outfitter command contract and durable application state."""

# 2119-spec: outfitter-remote
from pathlib import Path

import pytest

from panopticon.core.models import Skill
from panopticon.harnesses import BootstrapContext, LaunchContext
from panopticon.harnesses.outfitter import OutfitterHarness


def bootstrap(home: Path) -> OutfitterHarness:
    harness = OutfitterHarness()
    harness.bootstrap(
        BootstrapContext(
            home=home,
            cwd=home / "repo",
            service_url="http://127.0.0.1:8000",
            task_id="task-1",
            skills=[Skill(name="plan", description="Plan", instructions="Write a plan")],
            operations={"advance": "COMPLETE"},
            overview="Workflow instructions",
            environ={"PANOPTICON_SERVICE_AUTH_TOKEN": "scoped"},
        )
    )
    return harness


# 2119: 1.1
# 2119: 1.4
def test_published_agent_command_receives_initial_prompt(tmp_path: Path) -> None:
    harness = bootstrap(tmp_path)
    argv = harness.argv(LaunchContext(home=tmp_path, cwd=tmp_path, initial_prompt="first prompt"))
    assert argv[:6] == ["outfitter", "run", "panopticon", "--harness", "pi", "--"]
    assert argv[-1] == "first prompt"
    assert "--profile" not in argv and "--agent" not in argv
    assert "@ai-outfitter/outfitter@1.18.0" in harness.image_layer()


# 2119: 1.2
def test_resume_uses_the_persisted_task_session_directory(tmp_path: Path) -> None:
    harness = bootstrap(tmp_path)
    sessions = tmp_path / ".outfitter" / ".pi" / "agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text("preserved")
    argv = harness.argv(
        LaunchContext(home=tmp_path, cwd=tmp_path, initial_prompt="do not repeat", turn="agent")
    )
    assert "--continue" in argv
    assert argv[argv.index("--session-dir") + 1] == str(sessions)
    assert "do not repeat" not in argv
    assert (sessions / "session.jsonl").read_text() == "preserved"
    assert harness.env(LaunchContext(home=tmp_path, cwd=tmp_path))["OUTFITTER_USER_HOME"] == str(
        tmp_path / ".outfitter"
    )


# 2119: 1.3
def test_workflow_operations_and_rest_tool_guide_survive_bootstrap(tmp_path: Path) -> None:
    harness = bootstrap(tmp_path)
    operation = (tmp_path / ".agents" / "skills" / "advance" / "SKILL.md").read_text()
    assert "/tasks/task-1/operations/advance" in operation
    assert "PANOPTICON_SERVICE_AUTH_TOKEN" in operation
    overview = (tmp_path / ".outfitter" / "workflow-overview.md").read_text()
    assert "Workflow instructions" in overview
    assert "panopticon.container.task_api" in overview
    assert "create_task" in overview
    assert "--skill" in harness.argv(LaunchContext(home=tmp_path, cwd=tmp_path))


# 2119: 1.5
def test_published_pi_hooks_report_turns_and_runtime_activity(tmp_path: Path) -> None:
    bootstrap(tmp_path)
    extension = (tmp_path / ".outfitter" / "turn.ts").read_text()
    assert 'pi.on("agent_start"' in extension and 'pi.on("agent_end"' in extension
    assert "PANOPTICON_ACTIVITY_URL" in extension
    assert "PANOPTICON_SERVICE_AUTH_TOKEN" in extension
    assert "agent_settled" not in extension


def test_custom_agent_selection_is_preserved(tmp_path: Path) -> None:
    harness = bootstrap(tmp_path)
    argv = harness.argv(LaunchContext(home=tmp_path, cwd=tmp_path, starting_model="engineering"))
    assert argv[2] == "engineering"


def test_bootstrap_keeps_existing_user_settings_and_agents(tmp_path: Path) -> None:
    harness = bootstrap(tmp_path)
    settings = tmp_path / ".outfitter" / ".agents" / "settings.yml"
    settings.write_text("telemetry: false\n")
    agent = settings.parent / "agents" / "panopticon" / "agent.md"
    agent.write_text("user edited agent")
    bootstrap(tmp_path)
    assert settings.read_text() == "telemetry: false\n"
    assert agent.read_text() == "user edited agent"
    assert harness.suggested_models() == ()


# 2119: 3.3
# 2119: 3.4
def test_runtime_uses_relay_without_user_or_provider_credentials(tmp_path: Path) -> None:
    import json

    harness = OutfitterHarness()
    env = {"INFERENCE_BASE_URL": "http://127.0.0.1:4141/v1", "AI_MODEL": "flash"}
    assert harness.missing_auth(env, home=tmp_path) is None
    harness.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=tmp_path,
            service_url="http://127.0.0.1:8081",
            task_id="task",
            environ=env,
        )
    )
    state = tmp_path / ".outfitter"
    config = json.loads((state / ".pi" / "agent" / "models.json").read_text())
    assert config["providers"]["workspace"]["baseUrl"] == env["INFERENCE_BASE_URL"]
    assert config["providers"]["workspace"]["apiKey"] == "workspace-relay"
    assert not (state / ".pi" / "agent" / "auth.json").exists()


# 2119: 1.5
def test_generated_turn_handlers_execute_against_local_http_service(tmp_path):
    import json
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            requests.append(
                (
                    self.path,
                    self.headers.get("authorization"),
                    json.loads(self.rfile.read(int(self.headers["content-length"]))),
                )
            )
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            self.do_PUT()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bootstrap(tmp_path)
    source = (
        (tmp_path / ".outfitter" / "turn.ts")
        .read_text()
        .replace("export default function", "const extension = function")
    )
    source += """
const handlers = {};
extension({ on: (event, handler) => { handlers[event] = handler; } });
await handlers.agent_start();
await handlers.agent_end();
await handlers.input({}, { isIdle: () => true });
"""
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        subprocess.run(
            ["node", "--input-type=module", "--eval", source],
            check=True,
            timeout=8,
            env={
                **os.environ,
                "PANOPTICON_SERVICE_URL": base,
                "PANOPTICON_TASK_ID": "task-1",
                "PANOPTICON_SERVICE_AUTH_TOKEN": "ptc1.own.self.mac",
                "PANOPTICON_ACTIVITY_URL": base + "/activity",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert [body["turn"] for path, _, body in requests if path.endswith("/turn")] == [
        "agent",
        "user",
        "user",
    ]
    assert [body["active"] for path, _, body in requests if path == "/activity"] == [
        True,
        False,
        False,
    ]
    assert all(
        auth == "Bearer ptc1.own.self.mac" for path, auth, _ in requests if path.endswith("/turn")
    )


# 2119: 1.2
def test_fake_agent_reads_conversation_and_does_not_repeat_initial_prompt(tmp_path):
    import json
    import subprocess
    import sys

    harness = bootstrap(tmp_path)
    sessions = tmp_path / ".outfitter" / ".pi" / "agent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text('{"message":"accepted plan"}')
    fake = tmp_path / "fake_agent.py"
    fake.write_text("""import sys,json
from pathlib import Path
args=sys.argv[1:]
sessions=Path(args[args.index("--session-dir")+1])
assert "--continue" in args
assert "initial task" not in args
assert "interrupted" not in " ".join(args)
print(json.dumps([p.read_text() for p in sessions.glob("*.jsonl")]))
""")
    launch = LaunchContext(home=tmp_path, cwd=tmp_path, initial_prompt="initial task", turn="user")
    result = subprocess.run(
        [sys.executable, str(fake), *harness.argv(launch)[1:]],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == ['{"message":"accepted plan"}']


def test_shared_login_home_receives_the_actual_agent_definition(tmp_path, monkeypatch):
    shared = tmp_path / "credentials" / "outfitter"
    (shared / ".agents").mkdir(parents=True)
    monkeypatch.setenv("PANOPTICON_CREDENTIALS", str(shared.parent))
    harness = OutfitterHarness()
    harness.bootstrap(
        BootstrapContext(
            home=tmp_path,
            cwd=tmp_path,
            service_url="http://local",
            task_id="task",
            environ={"PANOPTICON_CREDENTIALS": str(shared.parent)},
        )
    )
    actual = Path(harness.env(LaunchContext(home=tmp_path, cwd=tmp_path))["OUTFITTER_USER_HOME"])
    assert (actual / ".agents" / "agents" / "panopticon" / "agent.md").is_file()
    assert actual == shared
    assert str(tmp_path / ".outfitter" / ".pi" / "agent" / "sessions") in harness.argv(
        LaunchContext(home=tmp_path, cwd=tmp_path)
    )


# 2119: REQ-016.1.1
@pytest.mark.parametrize("stalled", ["activity", "turn"])
def test_modified_outfitter_hooks_bound_stalled_activity_and_turn_writes(tmp_path, stalled):
    import subprocess

    bootstrap(tmp_path)
    source = (
        (tmp_path / ".outfitter" / "turn.ts")
        .read_text()
        .replace("export default function", "const extension = function")
    )
    source += f"\nconst stalled = {stalled!r};\n"
    source += """
const calls = [];
let aborted = 0;
const handlers = {};
extension({on: (event, handler) => {handlers[event] = handler;}});
process.env.PANOPTICON_ACTIVITY_URL = "http://stalled/activity";
globalThis.fetch = (url, options) => {
  calls.push(url);
  if (stalled === "turn" && url.endsWith("/activity")) return Promise.resolve({ok:true});
  return new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => { aborted += 1; reject(new Error("timeout")); }, {once:true});
  });
};
const started = Date.now();
await Promise.all([handlers.agent_start(), handlers.agent_end(), handlers.input({}, {isIdle: () => false})]);
if (Date.now() - started >= 2800 || Date.now() - started < 1500) throw new Error("turn handlers did not exercise bounded timeout");
if (aborted !== 3) throw new Error("not every hook aborted its stalled request");
if (calls.filter(url => url.endsWith("/turn")).length !== (stalled === "turn" ? 3 : 0)) throw new Error("wrong stalled request exercised");
"""
    subprocess.run(["node", "--input-type=module", "--eval", source], check=True, timeout=4)
