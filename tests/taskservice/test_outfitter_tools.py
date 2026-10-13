"""Outfitter REST tools against Panopticon's authenticated authority."""

from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_credential_scoping_spec import WRITE_TOKEN, _bearer, _client, _create_task, _task_token


# 2119: outfitter-remote.1.3
# 2119: outfitter-remote.3.8
# 2119: outfitter-remote.3.9
def test_outfitter_rest_tools_preserve_operations_and_orchestrator_boundaries(
    tmp_path, monkeypatch
):
    from panopticon.container import task_api
    from panopticon.sessionservice.outfitter_runner import OutfitterRunner

    with _client(tmp_path) as fleet:
        governor = _create_task(fleet, workflow="orchestrator")
        unrelated = _create_task(fleet)
        token = _task_token(governor["id"])
        monkeypatch.setenv("PANOPTICON_TASK_ID", str(governor["id"]))
        monkeypatch.setenv("PANOPTICON_SERVICE_URL", "http://testserver")
        monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_TOKEN", token)

        def local_client(**kwargs):
            from contextlib import nullcontext

            return nullcontext(TestClient(fleet.app, headers=kwargs["headers"]))

        monkeypatch.setattr(task_api.httpx, "Client", local_client)
        children = [
            task_api.invoke("create_task", {"workflow": "scoped", "harness": "outfitter"})
            for _ in range(2)
        ]
        assert children[0]["id"] != children[1]["id"]
        runner = OutfitterRunner(
            MagicMock(),
            runner_id="host",
            service_url="http://testserver",
            state_root=tmp_path / "runner",
        )
        assert runner.workspace(children[0]["id"]) != runner.workspace(children[1]["id"])
        for child in children:
            monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_TOKEN", token)
            assert child["governor_task_id"] == governor["id"]
            assert task_api.invoke("get_task", {"task_id": child["id"]})["id"] == child["id"]
            task_api.invoke(
                "put_artifact",
                {"task_id": child["id"], "name": "plan.md", "content": "governed plan"},
            )
            assert (
                task_api.invoke("get_artifact", {"task_id": child["id"], "name": "plan.md"})
                == "governed plan"
            )
            monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_TOKEN", _task_token(child["id"]))
            task_api.invoke(
                "resolve_responsibility", {"task_id": child["id"], "key": "ready", "status": "met"}
            )
            assert (
                task_api.invoke(
                    "apply_operation", {"task_id": child["id"], "operation": "advance"}
                )["state"]
                == "REVIEW"
            )
        monkeypatch.setenv("PANOPTICON_SERVICE_AUTH_TOKEN", token)
        with pytest.raises(httpx.HTTPStatusError):
            task_api.invoke("set_slug", {"task_id": unrelated["id"], "slug": "forbidden"})
        assert (
            fleet.get(f"/tasks/{unrelated['id']}", headers=_bearer(WRITE_TOKEN)).json()["slug"]
            is None
        )
