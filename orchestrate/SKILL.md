---
name: orchestrate
description: Coordinate a user-approved complex lane map without changing its scope or authority.
---

# Orchestrate

Role: optional dependency coordinator for complex work only.

Input: a user-approved lane map, dependencies, non-overlapping ownership domains, and authority limits.

Allowed: classify proposed work with `voice_state.py classify`; for an explicitly approved complex map, invoke `voice_state.py orchestrate` to normalize ownership, validate dependency references and cycles, reject conflicts, and report blocked lanes.

Forbidden: add lanes, broaden scope, create issues, select delivery, restart agents, or grant authority.

Output: concise lane receipt with ready, blocked, and integration actions for the coordinator/user.
