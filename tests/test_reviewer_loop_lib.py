from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "reviewer-loop" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_loop_lib import (
    LoopError,
    FIX_OUTPUT_SCHEMA_PATH,
    REVIEW_OUTPUT_SCHEMA_PATH,
    build_codex_exec_command,
    dedupe_findings,
    load_repo_review_guidance,
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


if __name__ == "__main__":
    unittest.main()
