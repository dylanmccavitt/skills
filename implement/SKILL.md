---
name: implement
description: Implement one approved durable task as its sole writer and return exact-head proof.
---

# Implement

Role: sole writer for one approved task.

Input: approved task contract, writer claim, branch/worktree, and bounded implementation authority.

Allowed: edit only contract scope, test, commit, push, and open one PR when recorded authority permits.

Forbidden: change scope, self-review, merge, close issues, deploy, or accept delivery authority.

Output: concise receipt with current head, changed files, checks, and the next action: independent review.
