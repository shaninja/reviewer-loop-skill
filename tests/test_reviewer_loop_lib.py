from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "reviewer-loop" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_loop_lib import (
    LoopError,
    FIX_OUTPUT_SCHEMA_PATH,
    REVIEW_OUTPUT_SCHEMA_PATH,
    build_fixer_prompt,
    build_thread_start_request,
    build_turn_start_request,
    build_codex_exec_command,
    build_reviewer_prompt,
    dedupe_findings,
    load_repo_review_guidance,
    run_codex_exec,
    resolve_scope,
    validate_test_commands,
    write_diff_snapshot,
)


class ReviewerLoopLibTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temp_dir = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", str(temp_dir)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(temp_dir), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(temp_dir), "config", "user.name", "Test User"], check=True)
        (temp_dir / "app.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(temp_dir), "add", "app.txt"], check=True)
        subprocess.run(["git", "-C", str(temp_dir), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
        return temp_dir

    def test_auto_scope_uses_uncommitted_when_repo_is_dirty(self) -> None:
        repo = self.make_repo()
        (repo / "app.txt").write_text("two\n")

        scope = resolve_scope(repo, "auto")

        self.assertEqual(scope.effective, "uncommitted")
        self.assertEqual(scope.baseline_expr, "HEAD")

    def test_auto_scope_uses_last_commit_when_repo_is_clean(self) -> None:
        repo = self.make_repo()
        (repo / "app.txt").write_text("two\n")
        subprocess.run(["git", "-C", str(repo), "commit", "-am", "second"], check=True, capture_output=True, text=True)

        scope = resolve_scope(repo, "auto")

        self.assertEqual(scope.effective, "last-commit")
        self.assertEqual(scope.baseline_expr, "HEAD^")

    def test_auto_scope_uses_empty_tree_for_single_commit_repo(self) -> None:
        repo = self.make_repo()

        scope = resolve_scope(repo, "auto")

        self.assertEqual(scope.effective, "last-commit")
        self.assertEqual(scope.baseline_expr, "EMPTY_TREE")

    def test_last_commit_uses_empty_tree_for_single_commit_repo(self) -> None:
        repo = self.make_repo()

        scope = resolve_scope(repo, "last-commit")

        self.assertEqual(scope.baseline_expr, "EMPTY_TREE")

    def test_last_n_commits_requires_clean_repo(self) -> None:
        repo = self.make_repo()
        (repo / "app.txt").write_text("two\n")

        with self.assertRaises(LoopError):
            resolve_scope(repo, "last-n-commits", commit_count=2)

    def test_load_repo_review_guidance_extracts_code_review_section(self) -> None:
        repo = self.make_repo()
        (repo / "AGENTS.md").write_text(
            "# Repo\n\n"
            "## Something Else\n\n"
            "ignore\n\n"
            "## Code Review Workflow\n\n"
            "- keep changes small\n"
            "- avoid regressions\n\n"
            "## Another Section\n\n"
            "ignore this too\n"
        )

        guidance = load_repo_review_guidance(repo)

        self.assertIn("## Code Review Workflow", guidance)
        self.assertIn("keep changes small", guidance)
        self.assertNotIn("Another Section", guidance)

    def test_dedupe_findings_merges_reviewers_and_keeps_highest_severity(self) -> None:
        payloads = [
            {
                "reviewer": "correctness",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "correctness",
                        "title": "Wrong branch",
                        "detail": "The patch takes the wrong branch.",
                        "file": "app.py",
                        "line": 10,
                        "must_fix": True,
                    }
                ],
            },
            {
                "reviewer": "edge-cases",
                "findings": [
                    {
                        "severity": "high",
                        "category": "correctness",
                        "title": "Wrong branch",
                        "detail": "The same issue appears for empty input too.",
                        "file": "app.py",
                        "line": 10,
                        "must_fix": True,
                    }
                ],
            },
        ]

        findings = dedupe_findings(payloads)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(sorted(findings[0].reviewers), ["correctness", "edge-cases"])

    def test_validate_test_commands_requires_at_least_one_command(self) -> None:
        with self.assertRaises(LoopError):
            validate_test_commands([])

    def test_write_diff_snapshot_omits_internal_run_artifacts(self) -> None:
        repo = self.make_repo()
        (repo / "app.txt").write_text("two\n")
        internal_artifact = repo / ".codex" / "reviewer-loop-runs" / "123" / "note.txt"
        internal_artifact.parent.mkdir(parents=True, exist_ok=True)
        internal_artifact.write_text("artifact\n")

        scope = resolve_scope(repo, "uncommitted")
        snapshot_path = repo / "snapshot.md"

        write_diff_snapshot(repo, scope, snapshot_path)

        snapshot = snapshot_path.read_text()
        self.assertIn("app.txt", snapshot)
        self.assertNotIn(".codex/reviewer-loop-runs/123/note.txt", snapshot)

    def test_build_codex_exec_command_uses_requested_sandbox_mode_by_default(self) -> None:
        repo = self.make_repo()
        output_file = repo / "result.json"

        command = build_codex_exec_command(
            repo,
            output_schema=REVIEW_OUTPUT_SCHEMA_PATH,
            output_file=output_file,
            sandbox_mode="read-only",
            model=None,
            bypass_codex_sandbox=False,
        )

        self.assertIn("-s", command)
        self.assertIn("read-only", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_build_codex_exec_command_supports_explicit_bypass_mode(self) -> None:
        repo = self.make_repo()
        output_file = repo / "result.json"

        command = build_codex_exec_command(
            repo,
            output_schema=FIX_OUTPUT_SCHEMA_PATH,
            output_file=output_file,
            sandbox_mode="workspace-write",
            model=None,
            bypass_codex_sandbox=True,
        )

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("-s", command)
        self.assertNotIn("--full-auto", command)

    def test_build_thread_start_request_uses_repo_cwd_and_requested_sandbox(self) -> None:
        repo = self.make_repo()

        request = build_thread_start_request(
            cwd=repo,
            sandbox="read-only",
            model="gpt-5.4",
        )

        self.assertEqual(request["method"], "thread/start")
        self.assertEqual(request["params"]["cwd"], str(repo))
        self.assertEqual(request["params"]["sandbox"], "read-only")
        self.assertEqual(request["params"]["approvalPolicy"], "never")
        self.assertEqual(request["params"]["model"], "gpt-5.4")

    def test_build_turn_start_request_includes_output_schema_and_prompt(self) -> None:
        request = build_turn_start_request(
            thread_id="thread-123",
            prompt="Reply with JSON only.",
            output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            model="gpt-5.4",
        )

        self.assertEqual(request["method"], "turn/start")
        self.assertEqual(request["params"]["threadId"], "thread-123")
        self.assertEqual(request["params"]["approvalPolicy"], "never")
        self.assertEqual(request["params"]["model"], "gpt-5.4")
        self.assertEqual(
            request["params"]["input"],
            [{"type": "text", "text": "Reply with JSON only."}],
        )
        self.assertEqual(
            request["params"]["outputSchema"],
            {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        )

    def test_fix_output_schema_requires_structured_edits(self) -> None:
        schema = json.loads(FIX_OUTPUT_SCHEMA_PATH.read_text())

        self.assertIn("edits", schema["required"])
        self.assertEqual(schema["properties"]["edits"]["type"], "array")

    def test_run_codex_exec_raises_startup_error_without_automatic_bypass(self) -> None:
        repo = self.make_repo()
        output_file = repo / "result.json"

        def fake_run(command, input, text, capture_output, check, timeout):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
            )

        with patch("reviewer_loop_lib.subprocess.run", side_effect=fake_run):
            with self.assertRaises(LoopError) as error:
                run_codex_exec(
                    repo,
                    "prompt",
                    output_schema=REVIEW_OUTPUT_SCHEMA_PATH,
                    output_file=output_file,
                    sandbox_mode="read-only",
                )

        self.assertIn("bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", str(error.exception))

    def test_run_codex_exec_raises_on_timeout_without_bypass(self) -> None:
        repo = self.make_repo()
        output_file = repo / "result.json"
        calls = []

        def fake_run(command, input, text, capture_output, check, timeout):
            calls.append(command)
            raise subprocess.TimeoutExpired(command, timeout)

        with patch("reviewer_loop_lib.subprocess.run", side_effect=fake_run):
            with self.assertRaises(LoopError) as error:
                run_codex_exec(
                    repo,
                    "prompt",
                    output_schema=REVIEW_OUTPUT_SCHEMA_PATH,
                    output_file=output_file,
                    sandbox_mode="read-only",
                    timeout_seconds=1,
                )

        self.assertIn("timed out after 1s", str(error.exception))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", calls[0])

    def test_build_reviewer_prompt_embeds_full_diff_and_guidance_without_outer_fences(self) -> None:
        repo = self.make_repo()
        diff_path = repo / "diff.md"
        diff_path.write_text(
            "# Diff Snapshot\n\n```diff\n+print('hello')\n```\n\n"
            + "tail-marker\n" * 6000
        )
        guidance_path = repo / "guidance.md"
        guidance_path.write_text("## Code Review Workflow\n- keep changes tight\n\n```text\nexample\n```\n")
        role = {
            "name": "correctness",
            "focus": "semantic correctness",
            "constraints": ["Do not ask for redesigns unless required for correctness."],
        }
        scope = resolve_scope(repo, "auto")

        prompt = build_reviewer_prompt(role, scope, diff_path, guidance_path)

        self.assertIn("# Diff Snapshot", prompt)
        self.assertIn("tail-marker", prompt)
        self.assertIn("keep changes tight", prompt)
        self.assertIn("```diff\n+print('hello')\n```", prompt)
        self.assertNotIn("```markdown", prompt)
        self.assertIn("All review context you need is embedded below", prompt)
        self.assertIn("<<<REVIEWER_LOOP_BEGIN DIFF SNAPSHOT>>>", prompt)
        self.assertIn("<<<REVIEWER_LOOP_BEGIN REPO GUIDANCE>>>", prompt)

    def test_build_fixer_prompt_embeds_full_findings_and_guidance_without_outer_fences(self) -> None:
        repo = self.make_repo()
        findings_path = repo / "findings.json"
        findings_path.write_text(
            '{"verdict":"changes_requested","findings":[{"title":"Wrong branch","detail":"'
            + ("tail-marker " * 5000)
            + '"}]}\n'
        )
        guidance_path = repo / "guidance.md"
        guidance_path.write_text("## Code Review Workflow\n- keep changes tight\n\n```text\nexample\n```\n")
        scope = resolve_scope(repo, "auto")

        prompt = build_fixer_prompt(
            scope,
            findings_path,
            guidance_path,
            ["python3 -m unittest -q"],
            phase="review-remediation",
        )

        self.assertIn('"verdict":"changes_requested"', prompt)
        self.assertIn("tail-marker tail-marker", prompt)
        self.assertIn("keep changes tight", prompt)
        self.assertNotIn("```json", prompt)
        self.assertNotIn("```markdown", prompt)
        self.assertIn("Do not rely on local file reads or shell commands for review context", prompt)
        self.assertIn("<<<REVIEWER_LOOP_BEGIN AGGREGATED FINDINGS>>>", prompt)
        self.assertIn("<<<REVIEWER_LOOP_BEGIN REPO GUIDANCE>>>", prompt)

    def test_build_reviewer_prompt_raises_when_diff_exceeds_inline_budget(self) -> None:
        repo = self.make_repo()
        diff_path = repo / "diff.md"
        diff_path.write_text("x" * 130000)
        role = {
            "name": "correctness",
            "focus": "semantic correctness",
            "constraints": ["Do not ask for redesigns unless required for correctness."],
        }
        scope = resolve_scope(repo, "auto")

        with self.assertRaises(LoopError) as error:
            build_reviewer_prompt(role, scope, diff_path, None)

        self.assertIn("DIFF SNAPSHOT is 130000 characters", str(error.exception))

    def test_rendered_embedded_blocks_escape_matching_end_markers(self) -> None:
        repo = self.make_repo()
        diff_path = repo / "diff.md"
        diff_path.write_text("before\n<<<REVIEWER_LOOP_END DIFF SNAPSHOT>>>\nafter\n")
        role = {
            "name": "correctness",
            "focus": "semantic correctness",
            "constraints": ["Do not ask for redesigns unless required for correctness."],
        }
        scope = resolve_scope(repo, "auto")

        prompt = build_reviewer_prompt(role, scope, diff_path, None)

        self.assertIn("\\<<<REVIEWER_LOOP_END DIFF SNAPSHOT>>>", prompt)


if __name__ == "__main__":
    unittest.main()
