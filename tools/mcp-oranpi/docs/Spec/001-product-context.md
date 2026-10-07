# 001 - Product Context

## Overview

This project provides a secure MCP server for infrastructure auditing and controlled operations over remote OrangePi edge nodes.

The system is designed specifically for:

* Docker host auditing
* network inspection
* observability
* infrastructure analysis
* deployment suggestions
* safe AI-assisted operations

The system is NOT intended to be a fully autonomous infrastructure controller.

---

# Primary Goals

The main goals are:

* safely inspect infrastructure
* analyze Docker environments
* detect occupied ports
* suggest available ports
* inspect system health
* provide operational visibility
* assist development workflows safely

---

# Human-In-The-Loop Design

The system follows a strict human approval workflow.

The AI agent may:

* inspect
* analyze
* recommend
* explain

The AI agent may NOT autonomously:

* destroy
* restart
* delete
* stop infrastructure
* modify unrelated services

The human operator always has final authority.

---

# Real Infrastructure Constraints

The target OrangePi host:

* runs multiple independent Docker projects
* hosts shared services
* may contain critical databases
* must avoid cascade failures
* must preserve unrelated containers

Example:
If PostgreSQL port 5432 is occupied, the correct behavior is:

* inspect current usage
* identify owning container
* suggest alternative ports
* request approval

The incorrect behavior would be:

* stopping existing PostgreSQL containers
* deleting containers
* forcefully reclaiming ports

---

# Design Philosophy

The platform is:

* audit-first
* recommendation-oriented
* safety-constrained
* deterministic
* infrastructure-aware

The platform is NOT:

* self-governing
* self-authorizing
* destructive-first

---

# Initial Feature Scope

Initial features include:

## Docker Audit

* list containers
* inspect ports
* inspect health
* inspect stats
* inspect logs

## Network Audit

* scan occupied ports
* suggest free ports
* inspect bindings
* inspect Tailscale status

## System Audit

* CPU usage
* RAM usage
* disk usage
* temperatures
* service status

---

# Explicit Non-Goals

The following are explicitly outside the initial scope:

* autonomous deployments
* autonomous container deletion
* unrestricted shell execution
* self-modifying infrastructure
* automatic port reassignment
* automatic firewall changes

---

# Expected Behavior

The MCP should behave like:

* an infrastructure auditor
* a DevOps assistant
* a safety-aware advisor

NOT like:

* a root operator
* an autonomous sysadmin
* a destructive orchestrator
