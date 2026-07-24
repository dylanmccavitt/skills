---
name: checkpoint
description: Transfer one durable task safely to a confirmed successor.
---

# Checkpoint

Role: atomic handoff.

Input: current task record, claimed writer, exact head, proof, authority limits, and next action.

Allowed: create or attach one confirmed successor and atomically record the handoff.

Forbidden: auto-restart, alter scope or authority, create a second writer, or hide a failed handoff.

Output: concise receipt with successor, preserved ownership, current proof, and exact next action.
