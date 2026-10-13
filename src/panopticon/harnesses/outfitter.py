"""Outfitter 1.18 harness using dotagents and an isolated durable application home."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from panopticon.core.models import Skill
from panopticon.harnesses.base import INTERRUPT_PROMPT, BootstrapContext, Harness, LaunchContext
from panopticon.harnesses.codex import write_skills
from panopticon.harnesses.pi import (
    API_KEY_ENV_VARS,
    AUTH_FILE,
    NODE_VERSION,
    TURN_EXTENSION,
    operation_instructions,
)

OUTFITTER_VERSION = "1.18.0"
WORKFLOW_OVERVIEW_FILE = "workflow-overview.md"
EXTENSION_FILE = "turn.ts"


class OutfitterHarness(Harness):
    """Outfitter's profile manager/launcher, fixed to its primary pi adapter."""

    name: ClassVar[str] = "outfitter"
    config_dirname: ClassVar[str] = ".outfitter"
    host_binary: ClassVar[str] = "outfitter"
    install_hint: ClassVar[str] = (
        "Install Outfitter (`npm install --global @ai-outfitter/outfitter`)."
    )
    field_label: ClassVar[str] = "agent"

    def __init__(self, profile_sources_root: Path | None = None) -> None:
        self.profile_sources_root = profile_sources_root

    def suggested_models(self) -> tuple[tuple[str, str], ...]:
        """Suggest agent names from the current dotagents application home."""
        root = self.profile_sources_root or self.config_dir(Path.home()) / ".agents" / "agents"
        try:
            return tuple(
                (path.parent.name, path.parent.name) for path in sorted(root.glob("*/agent.md"))
            )
        except OSError:
            return ()

    def image_layer(self) -> str:
        """Install pinned Node and the verified published Outfitter release."""
        return (
            "RUN set -eux; \\\n"
            '    arch="$(uname -m)"; \\\n'
            '    case "$arch" in \\\n'
            '      x86_64) node_arch="x64" ;; \\\n'
            '      aarch64) node_arch="arm64" ;; \\\n'
            '      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\\n'
            "    esac; \\\n"
            "    curl --fail --silent --show-error --location \\\n"
            f'      "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.gz" \\\n'
            "      | tar --extract --gzip --directory /usr/local --strip-components=1; \\\n"
            "    npm install --global --ignore-scripts "
            f"@ai-outfitter/outfitter@{OUTFITTER_VERSION}"
        )

    def missing_auth(self, environ: Mapping[str, str], *, home: Path) -> str | None:
        if any(environ.get(var) for var in API_KEY_ENV_VARS):
            return None
        state = self.config_dir(home)
        if (state / ".pi" / "agent" / AUTH_FILE).is_file():
            return None
        if environ.get("INFERENCE_BASE_URL") and environ.get("AI_MODEL"):
            return None
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        if (
            credentials
            and (Path(credentials) / "outfitter" / ".pi" / "agent" / AUTH_FILE).is_file()
        ):
            return None
        if credentials and (Path(credentials) / AUTH_FILE).is_file():
            return None
        return "Sign in with outfitter login using this task's OUTFITTER_USER_HOME, or configure provider credentials."

    def bootstrap(self, ctx: BootstrapContext) -> None:
        config_dir = self.config_dir(ctx.home)
        config_dir.mkdir(parents=True, exist_ok=True)
        application_home = self._application_home(ctx.home, ctx.environ)
        application_home.mkdir(parents=True, exist_ok=True)
        agent_dir = application_home / ".agents" / "agents" / "panopticon"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent = agent_dir / "agent.md"
        if not agent.exists():
            agent.write_text(
                "---\nname: panopticon\n---\nFollow the supplied Panopticon workflow.\n"
            )
        settings = application_home / ".agents" / "settings.yml"
        if not settings.exists():
            settings.write_text("experimental:\n  outfitter_provider: true\n")
        from panopticon.container.task_api import GUIDE

        (config_dir / WORKFLOW_OVERVIEW_FILE).write_text(ctx.overview + "\n" + GUIDE)
        # The published CLI bundles Pi 0.80.3, whose completion event is agent_end.
        extension = TURN_EXTENSION.replace("agent_settled", "agent_end")
        extension = extension.replace(
            "    try {\n      await fetch(url, {",
            '    try {\n      if (process.env.PANOPTICON_ACTIVITY_URL) await fetch(process.env.PANOPTICON_ACTIVITY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: turn === "agent" }), signal: controller.signal });\n      await fetch(url, {',
        )
        (config_dir / EXTENSION_FILE).write_text(extension)
        entries = list(ctx.skills) + [
            Skill(
                name=name,
                description=f"Apply {name}.",
                instructions=operation_instructions(
                    name,
                    target,
                    ctx.task_id,
                    ctx.service_url,
                    authenticated=bool(ctx.environ.get("PANOPTICON_SERVICE_AUTH_TOKEN")),
                ),
            )
            for name, target in ctx.operations.items()
        ]
        write_skills(entries, ctx.home, ctx.task_id)
        pi = config_dir / ".pi" / "agent"
        pi.mkdir(parents=True, exist_ok=True)
        credentials = ctx.environ.get("PANOPTICON_CREDENTIALS")
        if credentials and not (pi / AUTH_FILE).exists():
            source = Path(credentials) / AUTH_FILE
            if source.is_file():
                (pi / AUTH_FILE).symlink_to(source)
        # A managed runtime uses pod identity at the loopback relay, never user OAuth.
        if endpoint := ctx.environ.get("INFERENCE_BASE_URL"):
            import json

            model = ctx.environ["AI_MODEL"]
            (pi / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "workspace": {
                                "baseUrl": endpoint,
                                "api": "openai-completions",
                                "apiKey": "workspace-relay",
                                "models": [
                                    {
                                        "id": model,
                                        "name": model,
                                        "reasoning": False,
                                        "input": ["text"],
                                        "contextWindow": 128000,
                                        "maxTokens": 32768,
                                        "cost": {
                                            "input": 0,
                                            "output": 0,
                                            "cacheRead": 0,
                                            "cacheWrite": 0,
                                        },
                                    }
                                ],
                            }
                        }
                    }
                )
            )
            agent.write_text(
                f"---\nname: panopticon\nmodel: workspace/{model}\n---\nFollow the supplied Panopticon workflow.\n"
            )

    def argv(self, ctx: LaunchContext) -> list[str]:
        config_dir = self.config_dir(ctx.home)
        argv = [
            "outfitter",
            "run",
            ctx.starting_model or "panopticon",
            "--harness",
            "pi",
            "--",
            "--session-dir",
            str(config_dir / ".pi" / "agent" / "sessions"),
        ]
        extension = config_dir / EXTENSION_FILE
        if extension.exists():
            argv += ["--extension", str(extension)]
        overview = config_dir / WORKFLOW_OVERVIEW_FILE
        if overview.exists() and (content := overview.read_text()).strip():
            argv += ["--append-system-prompt", content]
        skills = ctx.home / ".agents" / "skills"
        if skills.exists():
            for skill in sorted(path for path in skills.iterdir() if path.is_dir()):
                argv += ["--skill", str(skill)]
        sessions = config_dir / ".pi" / "agent" / "sessions"
        if sessions.exists() and any(sessions.rglob("*.jsonl")):
            argv.append("--continue")
            if ctx.turn == "agent":
                argv.append(INTERRUPT_PROMPT)
        elif ctx.initial_prompt:
            argv.append(ctx.initial_prompt)
        return argv

    def _application_home(self, home: Path, environ: Mapping[str, str]) -> Path:
        credentials = environ.get("PANOPTICON_CREDENTIALS")
        shared = Path(credentials) / "outfitter" if credentials else None
        if (
            not environ.get("INFERENCE_BASE_URL")
            and shared is not None
            and (shared / ".agents").is_dir()
        ):
            return shared
        return self.config_dir(home)

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        return {"OUTFITTER_USER_HOME": str(self._application_home(ctx.home, os.environ))}
