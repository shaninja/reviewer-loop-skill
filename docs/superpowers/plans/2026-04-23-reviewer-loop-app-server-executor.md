# Reviewer Loop App-Server Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace nested `codex exec` reviewer/fixer runs with an app-server JSON-RPC executor, keep the existing CLI and artifact layout, move fixer-side file application into the outer Python controller, and change the default review/fix cap to `5`.

**Architecture:** The controller will keep scope resolution, artifact persistence, and test gating. Inner reviewer and fixer reasoning will run through `codex app-server --listen stdio://` using JSON-RPC `thread/start` and `turn/start`. Reviewers stay read-only. The fixer also stays read-only and returns structured edit instructions; the Python controller applies those edits locally, then reruns required tests before another review round.

**Tech Stack:** Python 3, `subprocess`, `json`, `threading`, `unittest`

---

### Task 1: Add Plan-Covering Failing Tests

**Files:**
- Modify: `tests/test_run_review_loop.py`
- Modify: `tests/test_reviewer_loop_lib.py`

- [ ] **Step 1: Write the failing test for the new default review cap**

```python
def test_parse_args_defaults_max_review_rounds_to_five(self) -> None:
    args = parse_args(["--repo", ".", "--test-command", "python3 -m pytest -q"])
    self.assertEqual(args.max_review_rounds, 5)
```

- [ ] **Step 2: Write the failing test for executor-driven reviewer fan-out**

```python
def test_run_parallel_reviewers_uses_executor_and_writes_artifacts(self) -> None:
    executor = FakeExecutor(
        reviewer_payloads={
            "correctness": {"summary": "ok", "blocked_reason": None, "findings": []},
        }
    )
    payloads = run_parallel_reviewers(..., executor=executor, ...)
    self.assertEqual(payloads[0]["reviewer"], "correctness")
    self.assertTrue((review_round_dir / "correctness" / "prompt.txt").exists())
    self.assertTrue((review_round_dir / "correctness" / "result.json").exists())
```

- [ ] **Step 3: Write the failing test for controller-applied fixer edits**

```python
def test_apply_fixer_edits_rewrites_target_file(self) -> None:
    payload = {
        "status": "fixed",
        "summary": "updated file",
        "notes": "applied replacement",
        "blocking_reason": None,
        "edits": [
            {
                "path": "app.txt",
                "action": "replace",
                "expected_old_text": "one\\n",
                "new_text": "two\\n",
            }
        ],
    }
    apply_fixer_edits(repo, payload)
    self.assertEqual((repo / "app.txt").read_text(), "two\\n")
```

- [ ] **Step 4: Write the failing test for app-server JSON-RPC payload assembly**

```python
def test_build_turn_start_request_includes_output_schema(self) -> None:
    request = build_turn_start_request(
        thread_id="thread-1",
        prompt="hello",
        output_schema={"type": "object"},
        model="gpt-5.4",
    )
    self.assertEqual(request["method"], "turn/start")
    self.assertEqual(request["params"]["threadId"], "thread-1")
    self.assertEqual(request["params"]["outputSchema"], {"type": "object"})
```

- [ ] **Step 5: Run the focused tests to verify RED**

Run: `python3 -m pytest -q tests/test_run_review_loop.py tests/test_reviewer_loop_lib.py`

Expected: FAIL because the helper functions, executor abstraction, and new fixer edit contract do not exist yet.

### Task 2: Introduce App-Server Executor Primitives

**Files:**
- Modify: `reviewer-loop/scripts/reviewer_loop_lib.py`
- Test: `tests/test_reviewer_loop_lib.py`

- [ ] **Step 1: Add the execution/result dataclasses and request builders**

```python
@dataclass
class ExecutionArtifacts:
    payload: dict[str, Any]
    stdout_log: str
    stderr_log: str


def build_thread_start_request(*, cwd: Path, sandbox: str, model: str | None) -> dict[str, Any]:
    return {
        "method": "thread/start",
        "params": {
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "model": model,
            "personality": "pragmatic",
        },
    }
```

- [ ] **Step 2: Add a small stdio JSON-RPC client for `codex app-server`**

```python
class CodexAppServerClient:
    def __init__(self, repo: Path) -> None:
        self._process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
```

- [ ] **Step 3: Add a helper that runs one read-only turn and captures final answer text**

```python
def run_app_server_turn(..., output_schema: dict[str, Any]) -> ExecutionArtifacts:
    client = CodexAppServerClient(repo)
    thread_id = client.start_thread(...)
    payload, stdout_log = client.run_turn(...)
    return ExecutionArtifacts(payload=payload, stdout_log=stdout_log, stderr_log=client.stderr_text())
```

- [ ] **Step 4: Add the failing-then-passing tests for request builders and response collection**

Run: `python3 -m pytest -q tests/test_reviewer_loop_lib.py::ReviewerLoopLibTests::test_build_turn_start_request_includes_output_schema`

Expected: PASS after the new helpers are added.

### Task 3: Move Reviewer and Fixer Execution Behind the Executor Interface

**Files:**
- Modify: `reviewer-loop/scripts/run_review_loop.py`
- Modify: `reviewer-loop/scripts/reviewer_loop_lib.py`
- Test: `tests/test_run_review_loop.py`

- [ ] **Step 1: Add a controller-facing executor abstraction**

```python
class LoopExecutor(Protocol):
    def run_reviewer(... ) -> ExecutionArtifacts: ...
    def run_fixer(... ) -> ExecutionArtifacts: ...
```

- [ ] **Step 2: Implement `AppServerLoopExecutor` using read-only inner turns**

```python
class AppServerLoopExecutor:
    def run_reviewer(...):
        return run_app_server_turn(..., sandbox="read-only", output_schema=review_schema)

    def run_fixer(...):
        return run_app_server_turn(..., sandbox="read-only", output_schema=fix_schema)
```

- [ ] **Step 3: Change `run_parallel_reviewers` and `run_fixer` to use the executor**

```python
artifacts = executor.run_reviewer(...)
write_text(stdout_path, artifacts.stdout_log)
write_text(stderr_path, artifacts.stderr_log)
write_json(output_path, artifacts.payload)
```

- [ ] **Step 4: Change `parse_args` to default `--max-review-rounds` to `5`**

```python
parser.add_argument("--max-review-rounds", type=int, default=5)
```

- [ ] **Step 5: Run the controller tests to verify GREEN**

Run: `python3 -m pytest -q tests/test_run_review_loop.py`

Expected: PASS, including the default-cap test and executor usage tests.

### Task 4: Change the Fixer Contract So the Outer Controller Applies Edits

**Files:**
- Modify: `reviewer-loop/references/fix_output_schema.json`
- Modify: `reviewer-loop/scripts/reviewer_loop_lib.py`
- Modify: `reviewer-loop/scripts/run_review_loop.py`
- Test: `tests/test_reviewer_loop_lib.py`
- Test: `tests/test_run_review_loop.py`

- [ ] **Step 1: Extend the fixer schema with structured edits**

```json
{
  "required": ["status", "summary", "notes", "blocking_reason", "edits"],
  "properties": {
    "edits": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["path", "action", "expected_old_text", "new_text"],
        "properties": {
          "path": { "type": "string" },
          "action": { "type": "string", "enum": ["replace"] },
          "expected_old_text": { "type": "string" },
          "new_text": { "type": "string" }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Update the fixer prompt to forbid inner file edits and require edit instructions only**

```python
Do not modify files directly in this turn.
Return concrete edit instructions in the schema's `edits` array.
Each edit must target an existing file and replace exact current text with new text.
```

- [ ] **Step 3: Add an outer-controller edit applier**

```python
def apply_fixer_edits(repo: Path, payload: dict[str, Any]) -> None:
    for edit in payload["edits"]:
        path = repo / edit["path"]
        current = path.read_text()
        if edit["expected_old_text"] not in current:
            raise LoopError(...)
        path.write_text(current.replace(edit["expected_old_text"], edit["new_text"], 1))
```

- [ ] **Step 4: Call `apply_fixer_edits()` immediately after a successful fixer turn and before rerunning tests**

```python
fix_payload = run_fixer(...)
ensure_fixer_succeeded(fix_payload)
apply_fixer_edits(repo, fix_payload)
test_results = run_required_tests(...)
```

- [ ] **Step 5: Run the focused tests to verify the outer edit path**

Run: `python3 -m pytest -q tests/test_run_review_loop.py tests/test_reviewer_loop_lib.py`

Expected: PASS, including the controller-applied edit behavior.

### Task 5: Preserve Artifact Logging and Verify the End-to-End Contract

**Files:**
- Modify: `reviewer-loop/scripts/reviewer_loop_lib.py`
- Modify: `reviewer-loop/scripts/run_review_loop.py`
- Test: `tests/test_run_review_loop.py`

- [ ] **Step 1: Make app-server execution emit stable synthetic logs**

```python
stdout_lines = [
    f"transport: app-server-stdio",
    f"thread_id: {thread_id}",
    f"turn_id: {turn_id}",
    final_text,
]
```

- [ ] **Step 2: Preserve existing artifact filenames**

```python
write_text(prompt_path, prompt)
write_json(output_path, artifacts.payload)
write_text(stdout_path, artifacts.stdout_log)
write_text(stderr_path, artifacts.stderr_log)
```

- [ ] **Step 3: Add an integration-style controller test that exercises one review round with a fake executor and confirms artifact paths**

```python
def test_controller_writes_review_and_fix_artifacts(self) -> None:
    ...
    self.assertTrue((run_dir / "artifacts" / "reviews" / "round-1" / "correctness" / "stdout.log").exists())
    self.assertTrue((run_dir / "artifacts" / "fixes" / "round-1" / "attempt-1" / "result.json").exists())
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer-loop/references/fix_output_schema.json reviewer-loop/scripts/reviewer_loop_lib.py reviewer-loop/scripts/run_review_loop.py tests/test_reviewer_loop_lib.py tests/test_run_review_loop.py docs/superpowers/plans/2026-04-23-reviewer-loop-app-server-executor.md
git commit -m "Refactor reviewer-loop to use app-server executor"
```
