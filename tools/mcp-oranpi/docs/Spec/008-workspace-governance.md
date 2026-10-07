# 008 - Workspace Governance

## Overview

The target OrangePi host is a multi-project development and infrastructure environment.

The host contains:

* multiple independent repositories
* multiple Docker Compose projects
* shared infrastructure services
* multiple databases
* multiple development environments

The MCP system must operate with strong workspace isolation and strict project boundaries.

The MCP must NEVER assume ownership of the entire host.

---

# Governance Philosophy

The system follows a:

* workspace-scoped
* least-privilege
* non-destructive
* explicitly-registered

governance model.

The agent may operate only inside approved workspaces.

The agent must never traverse arbitrary filesystem paths.

---

# Workspace Definition

A workspace is a registered project directory located under a controlled root directory.

Example:

```text id="y8jlwm"
/home/gonzalo/Gonzalo_codigo/guardian
```

Each workspace represents:

* one repository
* one project
* one infrastructure context

---

# Root Workspace Directory

All workspaces must exist under a single controlled root directory.

Example:

```text id="rwjlwm"
/home/gonzalo/Gonzalo_codigo
```

The MCP must never access directories outside the configured workspace root.

---

# Registered Workspaces Only

The MCP may only interact with explicitly registered workspaces.

Example configuration:

```yaml id="z0jlwm"
allowed_workspaces:
  guardian:
    path: /home/gonzalo/Gonzalo_codigo/guardian

  crm:
    path: /home/gonzalo/Gonzalo_codigo/crm

  lab:
    path: /home/gonzalo/Gonzalo_codigo/lab
```

Unregistered paths are forbidden.

---

# Forbidden Behavior

The MCP MUST NEVER:

* accept arbitrary filesystem paths
* execute arbitrary cd commands
* traverse parent directories
* access /etc, /root, /var outside allowlists
* operate on unknown repositories
* escape the workspace root

The following patterns are forbidden:

```python id="4vjlwm"
run(f"cd {user_input}")
```

```python id="3wjlwm"
Path(user_input)
```

without validation against the workspace registry.

---

# Workspace Resolution

The agent must reference workspaces by logical identifier only.

Example:

```json id="1xjlwm"
{
  "workspace": "guardian"
}
```

The MCP resolves the real path internally.

The agent must never manipulate raw filesystem paths directly.

---

# Workspace Isolation

All project operations must be scoped to a single workspace.

Examples:

* Docker inspection
* Compose inspection
* Git inspection
* log inspection
* deployment analysis

must execute only inside the selected workspace context.

---

# Docker Compose Awareness

The MCP should understand workspace-local Docker Compose environments.

Example operations:

* compose ps
* compose config
* compose logs
* compose services

must execute from the workspace directory.

The MCP must avoid global Docker assumptions whenever possible.

---

# Shared Host Awareness

The OrangePi host may contain:

* multiple PostgreSQL containers
* multiple Redis containers
* shared reverse proxies
* unrelated Compose stacks

The MCP must preserve isolation between projects.

A problem detected in one workspace must never trigger modifications in another workspace.

---

# Workspace Metadata

The MCP may expose readonly workspace metadata.

Example:

```json id="jlwmx8"
{
  "workspace": "guardian",
  "path": "/home/gonzalo/Gonzalo_codigo/guardian",
  "git_repo": true,
  "docker_compose": true,
  "python_project": true
}
```

Metadata exposure must remain readonly.

---

# Recommended Workspace Tools

Recommended readonly MCP tools:

## list_workspaces

Returns:

* registered workspace identifiers
* optional metadata

---

## workspace_inspect

Returns:

* project type
* compose presence
* git presence
* detected services

---

## workspace_docker_ps

Workspace-scoped container inspection.

Must avoid unrelated containers when possible.

---

## workspace_logs

Workspace-scoped logs only.

---

## workspace_ports

Workspace-scoped port visibility.

---

# Path Validation Rules

All workspace paths must:

* be normalized
* be absolute
* remain inside ROOT_WORKSPACE_DIR
* reject symlinks escaping the root
* reject traversal attempts

Forbidden examples:

```text id="jlwmw1"
../../
```

```text id="jlwmw2"
/etc/passwd
```

```text id="jlwmw3"
/home/gonzalo/other_dir
```

---

# Symlink Policy

Symlinks must be validated carefully.

The MCP must reject symlinks resolving outside the workspace root.

Example forbidden case:

```text id="jlwmw4"
/home/gonzalo/Gonzalo_codigo/project/logs -> /etc
```

---

# Workspace Selection Model

The agent should:

1. list available workspaces
2. inspect workspace metadata
3. select one workspace
4. operate only within that workspace

The MCP should encourage deterministic workspace selection.

---

# Human Approval Requirements

Future mutative operations must require:

* explicit workspace selection
* explicit human approval
* explicit operation scope

Example:

```json id="jlwmw5"
{
  "workspace": "guardian",
  "action": "restart_api"
}
```

The MCP must never perform host-wide mutative actions.

---

# OpenCode Workflow Expectations

Typical workflow:

1. Agent lists workspaces
2. Human selects workspace
3. Agent performs readonly inspection
4. Agent proposes actions
5. Human approves explicitly

The workflow is intentionally:

* constrained
* observable
* deterministic
* human-governed

---

# Future Considerations

Potential future extensions:

* workspace RBAC
* workspace labels
* environment classification
* project ownership metadata
* approval policies per workspace

These are OUT OF SCOPE for v1.

---

# Security Philosophy

Workspace governance exists to prevent:

* cross-project interference
* accidental destructive actions
* infrastructure confusion
* unrestricted filesystem access
* unsafe host-wide assumptions

The MCP must behave as:

* a constrained infrastructure assistant

Never as:

* an unrestricted host controller.
