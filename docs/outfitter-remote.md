# Outfitter managed execution

This fork keeps tasks, orchestration, approvals and artifacts in the local Panopticon
service. Each Outfitter task receives its own managed workspace. Claude, Codex and
shell workflows keep their existing local execution paths. Remote execution is opt-in.

## Prerequisites

Use the matching Outfitter host CLI build with `outfitter workspaces` support. The
agent runtime pins the published `@ai-outfitter/outfitter@1.18.0`; that published
version alone does not include these new host commands. The gateway must advertise
`panopticonSessions: 1`, and the selected runtime must pass its capability probe
before a task can be claimed. The initial toolchain supports GitHub repositories
without custom image layers, host hooks or privileged Docker.

Enable task-service authentication using the existing `PANOPTICON_SERVICE_AUTH_FILE`
configuration. Start the service and runner with `PANOPTICON_OUTFITTER_REMOTE=1`.
Choose Outfitter during connection setup: login opens Outfitter's device authorization
and explicitly requests workspace access. Alternatively, run `outfitter login
--workspaces` with the same `OUTFITTER_USER_HOME` as the connection. Existing
inference-only logins are never silently upgraded.

The runner's `PANOPTICON_SERVICE_URL` must be a loopback HTTP origin (default
`http://127.0.0.1:8000`). No inbound tunnel or public task-service endpoint is needed.
The CLI holds OAuth credentials locally and reconnects an outbound ticket-authenticated
WebSocket. Task containers receive only their task capability, the repository's
configured GitHub credential when needed, and workspace-scoped relay identity.

## Lifecycle

A durable installation ID determines each task's workspace ID. Keep the runner state
directory under the task directory when restarting the runner. A local tmux attachment
connects the existing dashboard to the runtime's tmux session. Detaching or losing the
host connection does not complete work or remove files. Transport reconnects never
replay uncertain callback mutations or terminal input.

The gateway keeps active agents awake and suspends idle sessions before sleeping their
compute. Repository files and Pi conversation state remain on the persistent volume.
Healthy wakes continue the saved conversation; an approval wait remains a user turn.
Completion or drop closes the session and releases storage after a fixed 24-hour
retention window. Unexpected compute/pane loss is interrupted work, never automatically
relaunched. Preserve and inspect the retained workspace before manually recovering
work; this initial rollout does not automatically recover interrupted compute.

## Deployment and acceptance

The companion webapp changes document gateway configuration in
`docs/panopticon-workspaces.md`. Build and publish the runtime through the Workspace
runtime GitHub workflow; copy its immutable `ghcr.io/...@sha256:...` reference into
trusted gateway configuration. A mutable tag is refused. Leave the gateway feature
flag disabled until the matching CLI, database migration, ingress, gateway and runtime
are deployed together.

Before enabling production, create an Outfitter task locally, attach, exercise an
approval, produce a GitHub PR and complete the task. Verify the 24-hour retention
boundary and then create two orchestrated children with distinct workspace IDs.
Also exercise disconnection during a mutation, sleep/wake at an approval boundary,
quota exhaustion and unexpected compute loss. Automated tests use fake agents and
providers; they do not substitute for this live acceptance.
