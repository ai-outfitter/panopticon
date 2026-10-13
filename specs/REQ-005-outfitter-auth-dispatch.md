# REQ-005: Outfitter setup-repo authentication

## Overview

Repository setup recognizes Outfitter as a supported harness and uses the current
CLI's native device login in the repository's private application home.

## Requirements

### REQ-005.1: Supported harness

1. Repository setup MUST accept `outfitter` as a supported harness rather than report it as unsupported.

### REQ-005.2: Authentication behavior

1. Outfitter repository setup MUST use the native Outfitter login for authentication.

### REQ-005.3: Workspace permission

1. Outfitter repository setup MUST request workspace permission only when remote execution is explicitly enabled.

### REQ-005.4: Application home

1. Successful Outfitter repository setup MUST configure an isolated application home containing the Panopticon agent definition.
