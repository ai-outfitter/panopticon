# Outfitter remote execution

## Overview

Local Panopticon remains the task authority. Managed Outfitter workspaces optionally
host Outfitter agents; Claude and Codex continue to run locally.

## Requirements

### 1: Harness compatibility

1. An Outfitter task MUST launch using the verified published CLI release.
2. A resumed Outfitter task MUST retain its conversation.
3. An Outfitter task MUST retain its workflow operations.
4. A first Outfitter launch MUST receive its initial prompt.
5. An Outfitter task MUST report agent and user turns to the local task service.

### 2: Remote admission

1. With remote execution disabled, tasks MUST retain their existing local execution path.
2. With remote execution enabled, Claude and Codex tasks MUST retain their local execution path.
3. A remote task using a local-only repository, privileged Docker, custom image layers, or host hooks MUST be rejected before its claim is acquired.
4. Repeated startup of a task within one runner installation MUST reuse the same workspace across retries, concurrent startup, and runner restarts.
5. An incompatible gateway or runtime MUST be rejected before a task claim is acquired.

### 3: Local authority

1. A remote agent MUST access the local task service using its task-scoped capability.
2. A remote agent MUST NOT receive a fleet credential.
3. A remote agent MUST NOT receive the user's Outfitter refresh token.
4. A remote agent MUST NOT receive an upstream inference provider key.
5. Reconnection MUST NOT replay a task-service mutation whose outcome is unknown.
6. Callback forwarding MUST be restricted to the configured loopback task service.
7. Remote repository provisioning MUST be recorded through the local task authority.
8. A remote agent MUST be refused access to unrelated tasks.
9. An orchestrator MUST retain access to its governed descendants.

### 4: Sessions

1. Detaching a terminal MUST leave the remote agent session running.
2. Sleeping and waking a healthy workspace MUST preserve its repository.
3. Recorded completion or drop MUST release the workspace storage hold.
4. Released workspace storage MUST be retained for 24 hours after recorded completion or drop.
5. Host disconnection MUST NOT imply task completion.
6. Unexpected compute loss MUST require operator recovery before agent execution resumes.
7. Active execution MUST prevent idle sleep.
8. Sleeping and waking a healthy workspace MUST preserve its conversation.

### 5: Machine authorization

1. An inference-only device session MUST NOT manage workspaces.
2. Workspace permission MUST require explicit device approval.
3. A bridge ticket MUST be refused after its expiry.
4. A bridge ticket MUST be usable only once.
5. A user MUST NOT obtain a bridge ticket for another user's workspace.

### 6: Inference and retention

1. Remote inference MUST use the workspace owner's configured provider chain through the workspace relay.
2. Released storage MUST become eligible for deletion after its 24-hour retention window.
3. A bridge connection MUST be refused when its workspace or installation differs from the ticket binding.

## Implementation notes

The initial verified package is @ai-outfitter/outfitter 1.18.0. Launch uses
`outfitter run panopticon --harness pi -- ...` and a durable isolated
`OUTFITTER_USER_HOME`. Workspace access and bridge commands require the matching
updated host CLI build. The remote runtime needs only the published run command.

