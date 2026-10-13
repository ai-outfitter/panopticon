"""Cross-repository contract tests, run by the private gateway's integration CI.

The public fork never embeds private gateway source or credentials. Set both paths
explicitly to run its platform contract suites against matching source checkouts.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def companion(name: str) -> Path:
    configured = os.environ.get(f"PANOPTICON_CONTRACT_{name}")
    if not configured:
        pytest.skip(f"set PANOPTICON_CONTRACT_{name} to the matching companion checkout")
    root = Path(configured)
    if not (root / "node_modules").is_dir():
        pytest.fail(f"Install the matching {name} checkout's test dependencies first")
    return root


# 2119: outfitter-remote.3.1
# 2119: outfitter-remote.3.5
# 2119: outfitter-remote.3.6
def test_cli_scoped_callbacks_reconnect_without_replaying_uncertain_mutations():
    subprocess.run(
        [
            "npm",
            "exec",
            "--workspace",
            "@ai-outfitter/outfitter",
            "--",
            "vitest",
            "run",
            "tests/unit/workspace-client.test.ts",
        ],
        cwd=companion("CLI"),
        check=True,
        timeout=60,
    )


# 2119: outfitter-remote.4.3
# 2119: outfitter-remote.4.4
# 2119: outfitter-remote.4.5
# 2119: outfitter-remote.4.7
# 2119: outfitter-remote.5.1
# 2119: outfitter-remote.5.2
# 2119: outfitter-remote.5.3
# 2119: outfitter-remote.5.4
# 2119: outfitter-remote.5.5
# 2119: outfitter-remote.6.1
# 2119: outfitter-remote.6.2
# 2119: outfitter-remote.6.3
def test_gateway_scoped_authorization_owner_inference_and_session_retention():
    subprocess.run(
        [
            "node",
            "--import",
            "tsx",
            "--test",
            "tests/cli-device-authorization.test.ts",
            "tests/panopticon-bridge.test.ts",
            "tests/panopticon-gateway.test.ts",
            "tests/inference-resolve.test.ts",
            "tests/workspace-pods.test.ts",
        ],
        cwd=companion("WEBAPP"),
        check=True,
        timeout=120,
    )
