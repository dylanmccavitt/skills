---
name: implement
description: Implement one approved durable task as its sole writer and return exact-head proof.
---

# Implement

Role: sole writer for one approved task.

Input: approved task contract with repository and PR, registered Implement actor capability, writer claim, and exact branch/worktree context.

Allowed: claim the single writer slot through the locked state kernel; edit only contract scope; test, commit, push, and open one PR when the contract permits.

Forbidden: change scope, use another actor's capability, bypass revision checks, self-review, merge, close issues, deploy, or grant/accept delivery authority.

Output: concise receipt with current head, changed files, checks, and the next action: independent review.
