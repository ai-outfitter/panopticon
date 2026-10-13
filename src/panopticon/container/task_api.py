"""REST equivalents of Panopticon tools for harnesses without an MCP client."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import quote

import httpx


def invoke(name: str, arguments: dict[str, Any]) -> Any:
    own = os.environ["PANOPTICON_TASK_ID"]
    target = quote(str(arguments.get("task_id", own)), safe="")
    token = os.environ.get("PANOPTICON_SERVICE_AUTH_TOKEN", "")
    with httpx.Client(
        base_url=os.environ["PANOPTICON_SERVICE_URL"],
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=30,
    ) as client:
        fields = {
            "set_slug": "slug",
            "set_url": "url",
            "set_turn": "turn",
            "set_blocked": "blocked",
            "set_state": "state",
            "set_attention": "attention",
            "set_token_estimate": "token_estimate",
        }
        if name == "get_task":
            response = client.get(f"/tasks/{target}")
        elif name == "list_workflows":
            response = client.get("/workflows")
        elif name == "create_task":
            acting = client.get(f"/tasks/{quote(own, safe='')}")
            acting.raise_for_status()
            payload = {
                key: value
                for key, value in arguments.items()
                if key
                in {
                    "workflow",
                    "memo",
                    "initial_prompt",
                    "harness",
                    "starting_model",
                    "artifacts",
                    "depends_on_task_ids",
                }
            }
            payload.update(repo_id=acting.json()["repo_id"], governor_task_id=own)
            response = client.post("/tasks", json=payload)
        elif name in fields:
            field = fields[name]
            path = field.replace("_", "-")
            response = client.put(f"/tasks/{target}/{path}", json={field: arguments[field]})
        elif name == "resolve_responsibility":
            response = client.post(
                f"/tasks/{target}/responsibilities",
                json={
                    key: arguments[key] for key in ("key", "status", "comment") if key in arguments
                },
            )
        elif name == "apply_operation":
            response = client.post(
                f"/tasks/{target}/operations/{quote(arguments['operation'], safe='')}"
            )
        elif name == "put_artifact":
            response = client.put(
                f"/tasks/{target}/artifacts/{quote(arguments['name'], safe='')}",
                content=arguments["content"].encode(),
            )
        elif name == "list_artifacts":
            response = client.get(f"/tasks/{target}/artifacts")
        elif name == "get_artifact":
            response = client.get(f"/tasks/{target}/artifacts/{quote(arguments['name'], safe='')}")
        else:
            raise ValueError(f"Unsupported REST tool: {name}")
        response.raise_for_status()
        if response.status_code == 204:
            return {"ok": True}
        try:
            return response.json()
        except ValueError:
            return response.text


GUIDE = """
This harness uses REST instead of MCP. For every Panopticon tool named in the
workflow, invoke `python -m panopticon.container.task_api TOOL '{JSON arguments}'`.
The command reads credentials from the environment; never print or pass credentials.
Supported tools: get_task, list_workflows, create_task, set_slug, set_url, set_turn,
set_blocked, set_state, set_attention, set_token_estimate, resolve_responsibility,
apply_operation, put_artifact, list_artifacts, get_artifact. Arguments retain the
tool names and fields. create_task automatically uses your own repository and sets
you as the governor. Children inherit the repository's configured harness unless
you explicitly pass harness=outfitter. Your capability cannot access unrelated tasks.
Read artifact content using get_artifact with task_id and name. Publish plans with
put_artifact and a content string. Resolve gates before invoking apply_operation.
"""

if __name__ == "__main__":
    print(json.dumps(invoke(sys.argv[1], json.loads(sys.argv[2]))))
