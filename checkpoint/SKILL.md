---
name: checkpoint
description: Transfer one durable task safely to a confirmed successor.
---

# Checkpoint

Role: atomic handoff.

Input: current task record and revision, claimed writer, registered Painter successor, exact head, proof, authority limits, and next action.

Allowed: atomically freeze the outgoing writer and transfer the single writer slot to one credentialed successor through the locked state kernel.

Forbidden: auto-restart, alter scope or authority, bypass revision checks, create a second writer, or hide a failed handoff.

Output: concise receipt with successor, preserved ownership, current proof, and exact next action.
