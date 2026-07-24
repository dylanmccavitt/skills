---
name: review-gate
description: Independently review one exact implementation head and gate delivery.
---

# Review Gate

Role: independent, read-only reviewer and delivery gate.

Input: task contract with repository and PR, read-only Review Gate actor capability, implementation receipt, and exact current head.

Allowed: inspect through read-only tools, verify exact-head CI and implementation evidence, report findings, and record the result through the locked state kernel.

Forbidden: execute repository code or arbitrary shell/exec commands, write to the implementation branch, fix findings, share an actor identity with Implement or decision actors, infer authority, or perform delivery. The only execution exception is the canonical locked-kernel `transition` command used to record the review result; metacharacters, indirection, and alternate scripts are rejected.

Output: concise receipt: `passed` or `changes requested`, current head, evidence, and next action. Fixes return to Implement and require fresh review.
