# 004 - Security Boundaries

## Security Philosophy

This project prioritizes:

* infrastructure safety
* operational predictability
* human approval
* least privilege

The AI agent is NOT trusted with unrestricted infrastructure control.

---

# Forbidden Operations

The MCP MUST NEVER expose unrestricted access to:

* rm -rf
* docker rm
* docker kill
* docker stop mass operations
* killall
* shutdown
* reboot
* firewall disabling
* SSH reconfiguration
* arbitrary shell execution

---

# Arbitrary Command Execution

The following pattern is strictly forbidden:

```python
run(user_input)
```

The MCP must never directly execute raw model-generated shell commands.

---

# Allowed Command Strategy

Only predefined and validated operations are allowed.

Example:

```python
ALLOWED_COMMANDS = {
    "docker_ps": "docker ps",
    "docker_stats": "docker stats --no-stream",
}
```

---

# Infrastructure Isolation

The MCP must never:

* interfere with unrelated projects
* assume ownership of the host
* modify unknown containers
* reclaim ports destructively

---

# Human Approval Policy

Mutative operations require explicit human approval.

Examples:

* restarting services
* deployments
* deleting resources
* editing files
* changing configurations

---

# Audit-Only Default

Default MCP behavior must always be:

* inspect
* analyze
* suggest
* report

Never:

* destroy
* force
* override

---

# Port Management Policy

If a requested port is occupied:

* identify ownership
* report current usage
* suggest alternatives
* request approval

Never:

* terminate existing services automatically

---

# Logging Requirements

All mutative operations must:

* be logged
* be traceable
* include timestamps
* include operator intent

---

# Trust Model

The human operator is the final authority.

The AI system acts only as:

* auditor
* advisor
* assistant

Never as autonomous infrastructure owner.
