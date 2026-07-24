---
name: orchestrate
description: Coordinate a user-approved complex lane map without changing its scope or authority.
---

# Orchestrate

Role: optional dependency coordinator for complex work only.

Input: a lane map previously approved through a credentialed coordinator/user `approve-lanes` transition, its dependencies, non-overlapping ownership domains, and authority limits.

Allowed: classify proposed work with `voice_state.py classify`; after the decision actor records the immutable map digest, invoke credentialed `voice_state.py orchestrate` to normalize ownership, validate dependency references and cycles, reject conflicts, and report blocked lanes.

Forbidden: add lanes, broaden scope, create issues, select delivery, restart agents, or grant authority.

Output: concise lane receipt with ready, blocked, and integration actions for the coordinator/user.
