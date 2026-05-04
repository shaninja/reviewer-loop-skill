---
name: reviewer-loop
description: Use when a Git repo change needs repeated multi-agent code review and fix rounds in Codex, especially across uncommitted changes or recent commits where one review pass is not enough and required tests must gate every fix round.
---

# Reviewer Loop

## Overview

Use the bundled controller to run a multi-reviewer loop against a target Git repo. The controller fans out specialist reviewers, aggregates their findings, sends one remediation payload to a fixer, reruns the required tests, and only returns to review after tests pass.

## When To Use It

- The user wants a review loop, not a one-shot review
- The same change needs several reviewer personas with different focus areas
- The work should stay in the current working tree
- Required tests must run after every fix round
- The repo may have project-specific review rules in `AGENTS.md`

## Scope Model

Supported scopes:

- `auto`
- `uncommitted`
- `last-commit`
- `last-n-commits`
- `base-diff`

`auto` resolves like this:

- dirty repo -> current working tree vs `HEAD`
- clean repo -> last commit

Committed scopes (`last-commit`, `last-n-commits`, `base-diff`) require a clean repo at the start of the run. Use `auto` or `uncommitted` when the tree is already dirty.

## Required Inputs

- A target repo path, usually `.`
- At least one `--test-command`
- An explicit scope when `auto` is not the right choice

Fail closed if the user does not provide test commands. The controller is designed to keep fixes from drifting into broken states.

## Default Review Personas

The bundled reviewer set is:

- correctness
- maintainability
- scope-and-regression
- edge-cases

Repo-specific review constraints come from the target repo at runtime. If the repo has an `AGENTS.md` file with a `## Code Review Workflow` section, the controller passes that guidance to all reviewer agents.

## Commands

Run from the target repo unless the user wants a different `--repo` path:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/reviewer-loop/scripts/run_review_loop.py" \
  --repo . \
  --scope auto \
  --test-command "python3 -m pytest -q"
```

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/reviewer-loop/scripts/run_review_loop.py" \
  --repo . \
  --scope last-n-commits \
  --commit-count 2 \
  --test-command "python3 -m pytest -q test/foo.py"
```

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/reviewer-loop/scripts/run_review_loop.py" \
  --repo . \
  --scope base-diff \
  --base-ref origin/main \
  --test-command "npm test -- --runInBand"
```

If nested `codex exec` runs are blocked by the local environment's sandboxing, add:

```bash
  --dangerously-bypass-codex-sandbox
```

Use that flag only when you trust the target repo and understand the local execution risk.

## Outputs

Artifacts are written inside the target repo under `.codex/reviewer-loop-runs/<timestamp>/`.

At the end of a completed or escalated run, the controller writes
`manager-closeout.md` and includes its path in the final JSON payload as
`manager_closeout`. The manager must read this file before replying to the
user and explain:

- each issue the loop found;
- why it was an issue;
- how the fix addressed it;
- the test evidence recorded after the fix.

If the final verdict is `approved_with_notes`, the manager must add TODO
comments in the target repo code for any unresolved code-specific notes before
replying to the user. Place each TODO near the affected code and mention the
TODO path in the final response.

Inspect that directory when:

- a run escalates
- you want the merged findings payload
- you want the per-reviewer outputs
- you want the test logs for a failed fix round
- you need the manager closeout explanation for the final response
