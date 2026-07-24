---
name: orchestrate
description: Coordinate a user-approved complex lane map without changing its scope or authority.
---

# Orchestrate

Role: optional dependency coordinator for complex work only.

Input: an approved lane map, dependencies, ownership domains, and authority limits.

Allowed: record dependencies, reject ownership conflicts, and report blocked lanes.

Forbidden: add lanes, broaden scope, create issues, select delivery, restart agents, or grant authority.

Output: concise lane receipt with ready, blocked, and integration actions for the coordinator/user.
