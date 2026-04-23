from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "reviewer-loop" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_loop_lib import LoopError
from run_review_loop import apply_fixer_edits, ensure_fixer_succeeded, parse_args, positive_int


class RunReviewLoopTests(unittest.TestCase):
    def test_positive_int_rejects_non_positive_values(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            positive_int("0")

    def test_parse_args_defaults_max_review_rounds_to_five(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["run_review_loop.py", "--repo", ".", "--test-command", "python3 -m pytest -q"],
        ):
            args = parse_args()

        self.assertEqual(args.max_review_rounds, 5)

    def test_ensure_fixer_succeeded_accepts_fixed_status(self) -> None:
        ensure_fixer_succeeded({"status": "fixed", "summary": "applied"})

    def test_ensure_fixer_succeeded_raises_blocking_reason(self) -> None:
        with self.assertRaises(LoopError) as error:
            ensure_fixer_succeeded(
                {
                    "status": "blocked",
                    "summary": "blocked",
                    "blocking_reason": "workspace access unavailable",
                }
            )

        self.assertEqual(str(error.exception), "workspace access unavailable")

    def test_ensure_fixer_succeeded_raises_failed_status(self) -> None:
        with self.assertRaises(LoopError) as error:
            ensure_fixer_succeeded(
                {
                    "status": "failed",
                    "summary": "syntax error while preparing edits",
                    "blocking_reason": None,
                }
            )

        self.assertIn("Fixer failed: syntax error while preparing edits", str(error.exception))

    def test_apply_fixer_edits_rewrites_matching_text_once(self) -> None:
        repo = Path(tempfile.mkdtemp())
        target = repo / "app.txt"
        target.write_text("before\\n")

        apply_fixer_edits(
            repo,
            {
                "status": "fixed",
                "summary": "updated file",
                "notes": "applied replacement",
                "blocking_reason": None,
                "edits": [
                    {
                        "path": "app.txt",
                        "action": "replace",
                        "expected_old_text": "before\\n",
                        "new_text": "after\\n",
                    }
                ],
            },
        )

        self.assertEqual(target.read_text(), "after\\n")

    def test_apply_fixer_edits_raises_when_expected_text_is_missing(self) -> None:
        repo = Path(tempfile.mkdtemp())
        target = repo / "app.txt"
        target.write_text("before\\n")

        with self.assertRaises(LoopError) as error:
            apply_fixer_edits(
                repo,
                {
                    "status": "fixed",
                    "summary": "updated file",
                    "notes": "applied replacement",
                    "blocking_reason": None,
                    "edits": [
                        {
                            "path": "app.txt",
                            "action": "replace",
                            "expected_old_text": "missing\\n",
                            "new_text": "after\\n",
                        }
                    ],
                },
            )

        self.assertIn("did not contain the expected text", str(error.exception))


if __name__ == "__main__":
    unittest.main()
