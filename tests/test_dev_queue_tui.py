"""Hermetic Zero-PII unit tests for `dev_queue_tui.py`, live HITL board sync, and `agy-queue` CLI."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
TUI_SCRIPT = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts" / "dev_queue_tui.py"

if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

import dev_common  # noqa: E402


def _load_tui_module():
    spec = importlib.util.spec_from_file_location("dev_queue_tui_under_test", str(TUI_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tui = _load_tui_module()


def _make_args(state_dir: Path, **overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "watch": None,
        "all": False,
        "repo": "",
        "status": "",
        "sync": False,
        "write_board": "",
        "open_hitl": "",
        "state_dir": str(state_dir),
        "repos_dir": "",
        "plain_urls": False,
        "json": False,
        "no_color": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _write_state(
    state_dir: Path,
    *,
    items: list[dict[str, object]],
    locks: dict[str, dict[str, object]] | None = None,
    orchestrator_id: str = "11111111-2222-4333-8444-555555555555",
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dev_queue.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-11-20T19:00:00Z",
                "orchestrator_conversation_id": orchestrator_id,
                "items": items,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if locks is not None:
        (state_dir / "actions_state.json").write_text(
            json.dumps({"actions_log_audit_state": {"dev_pr_locks": locks}}, indent=2),
            encoding="utf-8",
        )


def _git(repo_dir: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Bot",
        "GIT_AUTHOR_EMAIL": "bot@acme.example",
        "GIT_COMMITTER_NAME": "Test Bot",
        "GIT_COMMITTER_EMAIL": "bot@acme.example",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return proc.stdout.strip()


class TestDevQueueTui(unittest.TestCase):
    """Hermetic unit tests using synthetic `acme/repo-a` and `acme/repo-b` fixtures."""

    def test_multi_repo_pr_ready_bars_never_average_with_unstarted_downstream_repos(self) -> None:
        """PR 1/2 renders full bright-yellow bar (`PR 1/2 ready`), PR 2/2 and 1/1 render bright-green, never 45% cyan."""
        item_part = {
            "conversation_id": "aaaa1111-2222-4333-8444-555555555555",
            "priority": 10,
            "status": "pr_open",
            "repo_slugs": ["acme/repo-a", "acme/repo-b"],
            "merged_repos": [],
            "title": "Cross-repo rollout",
        }
        lock_ready = {
            "active": True,
            "status": "PENDING_HUMAN_PR_VALIDATION",
            "pr_gate_ready": True,
            "conversation_id": item_part["conversation_id"],
            "branch_name": "fix/20261120-demo-aaaa1111",
        }
        locks_by_cid = {item_part["conversation_id"]: [("acme/repo-a", lock_ready)]}
        prog_part = tui._compute_item_progress(item_part, locks_by_cid, {}, is_human_pr=True)
        self.assertIsNotNone(prog_part)
        assert prog_part is not None
        self.assertEqual(prog_part["mode"], "pr_part")
        self.assertEqual(prog_part["pct"], 100)
        self.assertEqual(prog_part["tag"], "PR 1/2")

        rendered_part = tui._render_inline_progress(prog_part, 20, color_on=True)
        self.assertIn("PR 1/2 ready", rendered_part)
        self.assertIn("\033[1;33m", rendered_part)
        self.assertNotIn("45%", rendered_part)
        self.assertNotIn("\033[1;36m", rendered_part)

        # Final repo of multi-repo item (repo-a already merged, repo-b PR ready) -> bright green
        item_final = {
            **item_part,
            "merged_repos": ["acme/repo-a"],
        }
        locks_final = {item_final["conversation_id"]: [("acme/repo-b", lock_ready)]}
        prog_final = tui._compute_item_progress(item_final, locks_final, {}, is_human_pr=True)
        self.assertIsNotNone(prog_final)
        assert prog_final is not None
        self.assertEqual(prog_final["mode"], "pr_final")
        rendered_final = tui._render_inline_progress(prog_final, 20, color_on=True)
        self.assertIn("PR 2/2 ready", rendered_final)
        self.assertIn("\033[1;32m", rendered_final)

        # Single-repo PR ready -> bright green "PR ready"
        item_single = {**item_part, "repo_slugs": ["acme/repo-a"]}
        prog_single = tui._compute_item_progress(item_single, locks_by_cid, {}, is_human_pr=True)
        self.assertIsNotNone(prog_single)
        assert prog_single is not None
        self.assertEqual(prog_single["mode"], "pr_final")
        rendered_single = tui._render_inline_progress(prog_single, 20, color_on=True)
        self.assertIn("PR ready", rendered_single)
        self.assertIn("\033[1;32m", rendered_single)

        # Active IN_PROGRESS development keeps numeric percentage + live inactivity semaphore
        item_dev = {
            "conversation_id": "bbbb1111-2222-4333-8444-555555555555",
            "priority": 20,
            "status": "in_progress",
            "repo_slugs": ["acme/repo-a"],
            "merged_repos": [],
            "title": "Active coding task",
        }
        now_ts_dev = tui._flow_opt.parse_iso_timestamp("2026-11-20T19:00:45Z")
        lock_dev = {
            "active": True,
            "status": "DEVELOPING",
            "pr_gate_ready": False,
            "progress_pct": 35,
            "last_activity_at": "2026-11-20T19:00:00Z",
            "conversation_id": item_dev["conversation_id"],
        }
        prog_dev = tui._compute_item_progress(
            item_dev,
            {item_dev["conversation_id"]: [("acme/repo-a", lock_dev)]},
            {},
            is_human_pr=False,
            now_ts=now_ts_dev,
        )
        self.assertIsNotNone(prog_dev)
        assert prog_dev is not None
        self.assertEqual(prog_dev["mode"], "dev")
        rendered_dev = tui._render_inline_progress(prog_dev, 20, color_on=True)
        self.assertIn("35%", rendered_dev)
        self.assertIn("ok·45s", rendered_dev)
        self.assertNotIn("PR ready", rendered_dev)

    def test_pr_repo_star_attribution_truncation_and_unlock_footer(self) -> None:
        """PR-ready repo gets `★`, stays first on truncation (`+N`), and footer prints unlock sequencing."""
        label_map = tui._build_repo_labels({"acme/repo-alpha", "acme/repo-beta", "acme/repo-gamma"})
        wide = tui._compact_repos(
            ["acme/repo-alpha", "acme/repo-beta"],
            label_map,
            set(),
            max_w=30,
            pr_repo_slug="acme/repo-alpha",
        )
        self.assertEqual(wide, "repo-alpha★, repo-beta")

        # When truncated, bring the PR-ready second repo first with `★` and `+2`
        narrow = tui._compact_repos(
            ["acme/repo-alpha", "acme/repo-beta", "acme/repo-gamma"],
            label_map,
            {"acme/repo-alpha"},
            max_w=14,
            pr_repo_slug="acme/repo-beta",
        )
        self.assertTrue(narrow.startswith("repo-beta★"), msg=f"Unexpected narrow repo string: {narrow}")
        self.assertTrue(narrow.endswith("+2"))

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid = "cccc1111-2222-4333-8444-555555555555"
            rich_url = (
                "https://github.com/acme/repo-a/compare/main...fix/20261120-multi-cccc1111"
                "?expand=1&title=feat%28sync%29%3A+part+1&body=Verdict%3A+PASS+%2898%2F100%29"
            )
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                        "merged_repos": [],
                        "title": "Two-repo feature",
                        "pr_urls": [rich_url],
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": cid,
                        "branch_name": "fix/20261120-multi-cccc1111",
                        "manual_pr_url": rich_url,
                    }
                },
            )
            out, row_links, _, first_pr, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIn("repo-a★, repo-b", out)
            self.assertIn("PR 1/2 ready", out)
            self.assertIn("[1] repo-a (cccc1111 · part 1/2 · unlocks repo-b) [+verified]:", out)
            self.assertEqual(row_links[1][1], rich_url)
            self.assertIsNotNone(first_pr)
            assert first_pr is not None
            self.assertEqual(first_pr[1], rich_url)

    def test_richest_pr_url_ranking_beats_trailing_bare_expand_url(self) -> None:
        """Created `/pull/` and verified `?title=&body=` URLs always beat trailing bare `?expand=1` URLs."""
        bare_url = "https://github.com/acme/repo-a/compare/main...fix/20261120-demo-dddd1111?expand=1"
        pending_url = f"{bare_url}&title=fix%3A+demo&body=Pending+independent+evaluation"
        verified_url = f"{bare_url}&title=fix%3A+demo&body=Automated+Verification+PASS+97%2F100"
        created_pr_url = "https://github.com/acme/repo-a/pull/42"
        gitlab_new_mr = "https://gitlab.example/acme/repo-a/-/merge_requests/new?merge_request[source_branch]=fix/demo"
        gitlab_created_mr = "https://gitlab.example/acme/repo-a/-/merge_requests/19"

        self.assertFalse(tui._is_created_pr_url(gitlab_new_mr))
        self.assertTrue(tui._is_created_pr_url(gitlab_created_mr))
        self.assertGreater(
            tui._score_pr_url_richness(verified_url, order_idx=0),
            tui._score_pr_url_richness(bare_url, order_idx=9),
        )
        self.assertGreater(
            tui._score_pr_url_richness(verified_url, order_idx=0),
            tui._score_pr_url_richness(pending_url, order_idx=5),
        )
        self.assertGreater(
            tui._score_pr_url_richness(created_pr_url, order_idx=0),
            tui._score_pr_url_richness(verified_url, order_idx=5),
        )

        # Lock has trailing bare URL while queue item pr_urls has verified full URL -> verified wins
        lk = {
            "active": True,
            "status": "PENDING_HUMAN_PR_VALIDATION",
            "pr_gate_ready": True,
            "branch_name": "fix/20261120-demo-dddd1111",
            "manual_pr_url": bare_url,
        }
        kind, best_u, is_pr, _ = tui._resolve_lock_human_pr_link(
            "acme/repo-a",
            lk,
            {"acme/repo-a": ("github", "https://github.com/acme/repo-a")},
            {},
            extra_urls=[verified_url, bare_url],
        )
        self.assertEqual(kind, "PR")
        self.assertTrue(is_pr)
        self.assertEqual(best_u, verified_url)

        # Queue item without active lock: verified URL at index 0 beats trailing bare URL at index 1
        item = {
            "conversation_id": "dddd1111-2222-4333-8444-555555555555",
            "status": "pr_open",
            "repo_slugs": ["acme/repo-a"],
            "merged_repos": [],
            "pr_urls": [verified_url, bare_url],
        }
        _, resolved_u, resolved_is_pr, _, slug = tui._resolve_item_human_pr_link(
            item,
            {},
            {"acme/repo-a": ("github", "https://github.com/acme/repo-a")},
            {},
        )
        self.assertTrue(resolved_is_pr)
        self.assertEqual(slug, "acme/repo-a")
        self.assertEqual(resolved_u, verified_url)

        # Created /pull/42 beats compare URLs even when placed first
        item_with_created = {**item, "pr_urls": [created_pr_url, verified_url, bare_url]}
        _, created_u, _, _, _ = tui._resolve_item_human_pr_link(
            item_with_created,
            {},
            {"acme/repo-a": ("github", "https://github.com/acme/repo-a")},
            {},
        )
        self.assertEqual(created_u, created_pr_url)

    def test_full_url_preservation_in_osc8_hotkeys_and_board_markdown(self) -> None:
        """Full `?title=&body=` is preserved in OSC-8 targets, hotkeys, and escaped Markdown board links."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            board_path = root / "artifacts" / "dev_queue_board.md"
            db_path = root / "conversation_summaries.db"
            brain_dir = root / "brain"

            pr_cid = "eeee1111-2222-4333-8444-555555555555"
            hitl_cid = "ffff1111-2222-4333-8444-555555555555"
            full_url = (
                "https://github.com/acme/repo-a/compare/main...fix/20261120-tui-eeee1111"
                "?expand=1&title=feat(tui):+board&body=Automated+Verification+(PASS+98/100)"
            )
            short_display = "https://github.com/acme/repo-a/compare/main...fix/20261120-tui-eeee1111?expand=1"

            # Populate synthetic conversation_summaries.db and proposal artifact
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?)",
                (hitl_cid, "Approve Zero-Hardcode TUI Design Contract"),
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?)",
                (pr_cid, "Implement Dev Queue TUI"),
            )
            conn.commit()
            conn.close()

            hitl_brain = brain_dir / hitl_cid
            hitl_brain.mkdir(parents=True, exist_ok=True)
            (hitl_brain / "design_proposal.md").write_text("# Design\n", encoding="utf-8")

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": pr_cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Build TUI",
                        "pr_urls": [full_url],
                    },
                    {
                        "conversation_id": hitl_cid,
                        "priority": 30,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "Fallback HITL Title",
                        "notes": "Confirm rollout options",
                    },
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": pr_cid,
                        "branch_name": "fix/20261120-tui-eeee1111",
                        "manual_pr_url": full_url,
                    }
                },
            )

            out, row_links, hitl_convs, first_pr, _, _ = tui.render(
                _make_args(state_dir, write_board=str(board_path), no_color=False, plain_urls=False),
                conv_db_path=db_path,
                brain_dir=brain_dir,
                is_tty_override=True,
                term_width_override=124,
            )

            # OSC-8 hyperlink preserves FULL URL while showing compact non-wrapping label
            self.assertIn(f"\033]8;;{full_url}\007", out)
            self.assertIn("↗ Open PR · repo-a (main...fix/20261120-tui-eeee1111)", out)
            # Plain-URL fallback emits raw clean_u
            out_plain, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                conv_db_path=db_path,
                brain_dir=brain_dir,
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIn(short_display, out_plain)
            # Hotkey targets keep FULL URL intact
            self.assertEqual(row_links[1][1], full_url)
            self.assertIsNotNone(first_pr)
            assert first_pr is not None
            self.assertEqual(first_pr[1], full_url)

            # HITL conversation resolved real sidebar title from SQLite DB
            self.assertIn(1, hitl_convs)
            self.assertEqual(hitl_convs[1][1], hitl_cid)
            self.assertEqual(hitl_convs[1][2], "Approve Zero-Hardcode TUI Design Contract")
            self.assertIn("[h1] Agy", out)
            self.assertIn("Approve Zero-Hardcode TUI Design Contract", out)

            # Live Markdown board artifact preserves escaped full PR URL and native conversation:// links
            self.assertTrue(board_path.is_file())
            board_md = board_path.read_text(encoding="utf-8")
            escaped_expected = (
                "https://github.com/acme/repo-a/compare/main...fix/20261120-tui-eeee1111"
                "?expand=1&title=feat%28tui%29:+board&body=Automated+Verification+%28PASS+98/100%29"
            )
            self.assertIn(f"[Open PR]({escaped_expected})", board_md)
            self.assertIn(f"[Approve Zero-Hardcode TUI Design Contract](conversation://{hitl_cid})", board_md)
            self.assertIn("[design_proposal.md](file://", board_md)

    def test_strict_pr_gate_ready_blocks_unreviewed_or_iterating_locks(self) -> None:
        """Locks with `pr_gate_ready=False` or unready statuses NEVER surface PR links (even via `item.pr_urls`)."""
        self.assertTrue(
            tui.is_lock_human_ready({"status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True})
        )
        self.assertTrue(
            tui.is_lock_human_ready({"status": "PENDING_HUMAN_PR_VALIDATION"})  # legacy lock without key
        )
        self.assertFalse(
            tui.is_lock_human_ready({"status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": False})
        )
        for blocked_status in (
            "AWAITING_AGENT_REVIEW",
            "NEEDS_REVISION_ITERATING",
            "ESCALATED_MAX_REVIEW_ITERATIONS",
            "DEVELOPING",
        ):
            self.assertFalse(
                tui.is_lock_human_ready({"status": blocked_status, "pr_gate_ready": True}),
                msg=f"Status {blocked_status} must never pass human PR gate",
            )

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid = "12121212-2222-4333-8444-555555555555"
            leaked_url = "https://github.com/acme/repo-a/compare/main...fix/20261120-unready-12121212?expand=1"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Still under agent review",
                        "pr_urls": [leaked_url],
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "AWAITING_AGENT_REVIEW",
                        "pr_gate_ready": False,
                        "conversation_id": cid,
                        "branch_name": "fix/20261120-unready-12121212",
                        "manual_pr_url": leaked_url,
                    }
                },
            )
            out, row_links, _, first_pr, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIsNone(first_pr)
            self.assertEqual(row_links, {})
            self.assertNotIn("[1] PR", out)
            self.assertNotIn("PR ready", out)
            self.assertNotIn(leaked_url, out)
            self.assertIn("(rev)", out)

    def test_local_git_ref_validation_distinguishes_open_merged_and_missing(self) -> None:
        """Real local git refs cleanly classify open, merged (`Sync`), and deleted (`Stale`) branches offline."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare_origin = root / "origin.git"
            bare_origin.mkdir()
            _git(bare_origin, "init", "--bare", "-b", "main")

            repo = root / "repo-a"
            repo.mkdir()
            _git(repo, "init", "-b", "main")
            _git(repo, "remote", "add", "origin", str(bare_origin))
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "chore: initial commit")
            _git(repo, "push", "-u", "origin", "main")

            # Branch 1: open unmerged remote branch
            _git(repo, "checkout", "-b", "fix/20261120-open-aaaabbbb")
            (repo / "open.txt").write_text("open\n", encoding="utf-8")
            _git(repo, "add", "open.txt")
            _git(repo, "commit", "-m", "feat: open change")
            _git(repo, "push", "-u", "origin", "fix/20261120-open-aaaabbbb")

            # Branch 2: merged into main (remote ref still present, ancestor of origin/main)
            _git(repo, "checkout", "main")
            _git(repo, "checkout", "-b", "fix/20261120-merged-ccccdddd")
            (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
            _git(repo, "add", "merged.txt")
            _git(repo, "commit", "-m", "feat: merged change")
            _git(repo, "push", "-u", "origin", "fix/20261120-merged-ccccdddd")
            _git(repo, "checkout", "main")
            _git(repo, "merge", "--no-ff", "fix/20261120-merged-ccccdddd", "-m", "merge branch")
            _git(repo, "push", "origin", "main")

            repo_dirs = {"acme/repo-a": repo}
            self.assertEqual(
                tui._check_local_remote_branch_state("acme/repo-a", "fix/20261120-open-aaaabbbb", repo_dirs),
                "open",
            )
            self.assertEqual(
                tui._check_local_remote_branch_state("acme/repo-a", "fix/20261120-merged-ccccdddd", repo_dirs),
                "merged",
            )
            self.assertEqual(
                tui._check_local_remote_branch_state("acme/repo-a", "fix/20261120-gone-eeeeffff", repo_dirs),
                "missing",
            )
            self.assertEqual(
                tui._find_live_unmerged_remote_branch_for_cid("acme/repo-a", "aaaabbbb", repo_dirs),
                "fix/20261120-open-aaaabbbb",
            )
            self.assertEqual(
                tui._find_live_unmerged_remote_branch_for_cid("acme/repo-a", "ccccdddd", repo_dirs),
                "",
            )

            # Verify lock link resolution returns PR / Sync / Stale accurately
            web_bases = {"acme/repo-a": ("github", "https://github.com/acme/repo-a")}
            base_lk = {"active": True, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True}
            kind_open, _, is_pr_open, st_open = tui._resolve_lock_human_pr_link(
                "acme/repo-a", {**base_lk, "branch_name": "fix/20261120-open-aaaabbbb"}, web_bases, repo_dirs
            )
            self.assertEqual((kind_open, is_pr_open, st_open), ("PR", True, "open"))

            kind_merged, _, is_pr_merged, st_merged = tui._resolve_lock_human_pr_link(
                "acme/repo-a", {**base_lk, "branch_name": "fix/20261120-merged-ccccdddd"}, web_bases, repo_dirs
            )
            self.assertEqual((kind_merged, is_pr_merged, st_merged), ("Sync", False, "merged"))

            kind_stale, _, is_pr_stale, st_stale = tui._resolve_lock_human_pr_link(
                "acme/repo-a", {**base_lk, "branch_name": "fix/20261120-gone-eeeeffff"}, web_bases, repo_dirs
            )
            self.assertEqual((kind_stale, is_pr_stale, st_stale), ("Stale", False, "stale"))

            # Verify offline sync never invokes git fetch
            state_dir = root / "state"
            _write_state(state_dir, items=[])
            with mock.patch.object(tui.subprocess, "run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                msg = tui._run_live_sync(state_dir, repo_dirs, offline=True)
                self.assertIn("Offline sync OK (0 repos fetched", msg)
                for call in mock_run.call_args_list:
                    self.assertNotIn("fetch", call.args[0])

    def test_watch_ansi_frames_clamping_and_cursor_restoration(self) -> None:
        """Watch frames use synchronized ANSI updates, wrap-aware viewport clamping, and restore cursor/alt screen."""
        body = [f"line-{i:02d} " + ("x" * 20) for i in range(20)]
        clamped = tui._clamp_lines_to_viewport(body, term_w=40, term_h=8, footer="[q] quit")
        self.assertEqual(clamped[-1], "[q] quit")
        self.assertLessEqual(len(clamped), 8)
        self.assertIn("more lines", clamped[-2])

        frame = tui._build_watch_frame(["header", "row 1"], term_w=80, term_h=12, footer="[q] quit")
        self.assertTrue(frame.startswith("\033[?2026h\033[H"))
        self.assertIn("header\033[K\nrow 1\033[K\n[q] quit\033[K", frame)
        self.assertTrue(frame.endswith("\033[J\033[?2026l"))

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_state(state_dir, items=[])
            buf = io.StringIO()
            rc = tui.run_watch_loop(
                _make_args(state_dir, watch=0.5, no_color=True),
                stream=buf,
                use_tty_override=True,
                open_tty=False,
                max_iterations=1,
                term_size_override=(100, 20),
            )
            self.assertEqual(rc, 0)
            out_stream = buf.getvalue()
            self.assertTrue(out_stream.startswith(tui.ALT_SCREEN_ENTER))
            self.assertIn("\033[?2026h\033[H", out_stream)
            self.assertTrue(out_stream.endswith(tui.ALT_SCREEN_EXIT))

            # Verify finally restores alt-screen & cursor even when render raises
            buf_err = io.StringIO()
            with mock.patch.object(tui, "render", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    tui.run_watch_loop(
                        _make_args(state_dir, watch=0.5),
                        stream=buf_err,
                        use_tty_override=True,
                        open_tty=False,
                        max_iterations=1,
                    )
            self.assertTrue(buf_err.getvalue().endswith(tui.ALT_SCREEN_EXIT))

    def test_open_hitl_copies_sidebar_title_and_focuses_gui_without_cli_conversation_flag(self) -> None:
        """`--open-hitl h1` copies exact sidebar title and focuses Antigravity App (never spawns `agy --conversation`)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            db_path = root / "conversation_summaries.db"
            hitl_cid = "99991111-2222-4333-8444-555555555555"

            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", (hitl_cid, "Exact Sidebar Title"))
            conn.commit()
            conn.close()

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": hitl_cid,
                        "priority": 15,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Short queue title",
                    }
                ],
            )
            with (
                mock.patch.dict(os.environ, {"AGY_CONVERSATION_SUMMARIES_DB": str(db_path)}),
                mock.patch.object(tui, "_copy_to_clipboard", return_value=True) as mock_copy,
                mock.patch.object(tui, "_focus_antigravity_app", return_value=True) as mock_focus,
                mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out,
            ):
                rc = tui.main(["--state-dir", str(state_dir), "--open-hitl", "h1"])
            self.assertEqual(rc, 0)
            mock_copy.assert_called_once_with("Exact Sidebar Title")
            mock_focus.assert_called_once_with()
            self.assertIn("Exact Sidebar Title", mock_out.getvalue())

    def test_json_output_collision_safe_ids_and_dev_common_entrypoint(self) -> None:
        """`--json`, collision-safe IDs, and `dev_common.run_dev_queue_tui_cli` work hermetically."""
        cids = [
            {"conversation_id": "abcd1234-queue-alpha"},
            {"conversation_id": "abcd1234-queue-beta"},
            {"conversation_id": "ef567890-single"},
        ]
        disp = tui._build_display_ids(cids)
        self.assertEqual(len(set(disp.values())), 3)
        self.assertEqual(disp["ef567890-single"], "ef567890")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "abcd1234-queue-alpha",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Pending item",
                    }
                ],
            )
            with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                rc = dev_common.run_dev_queue_tui_cli(["--state-dir", str(state_dir), "--json"])
            self.assertEqual(rc, 0)
            payload = json.loads(mock_out.getvalue())
            self.assertEqual(payload["managed_repos"], ["acme/repo-a"])
            self.assertEqual(len(payload["items"]), 1)

    def test_dynamic_repo_discovery_multi_vcs_board_pointer_and_filters(self) -> None:
        """Dynamic `--repos-dir` discovers GitHub/GitLab/Bitbucket repos; `dev_queue_board_path.txt` auto-syncs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos_root = root / "repos"
            state_dir = root / "state"
            board_file = root / "live_board.md"

            for dirname, remote_url in (
                ("repo-a", "git@github.com:acme/repo-a.git"),
                ("repo-b", "https://gitlab.example/acme/repo-b.git"),
                ("repo-c", "git@bitbucket.org:acme/repo-c.git"),
            ):
                rdir = repos_root / dirname
                rdir.mkdir(parents=True)
                _git(rdir, "init", "-b", "main")
                _git(rdir, "remote", "add", "origin", remote_url)

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "1111aaaa-2222-4333-8444-555555555555",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Active task A",
                    },
                    {
                        "conversation_id": "2222bbbb-2222-4333-8444-555555555555",
                        "priority": 20,
                        "status": "merged",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": ["acme/repo-b"],
                        "title": "Merged task B",
                    },
                ],
            )
            (state_dir / "dev_queue_board_path.txt").write_text(str(board_file), encoding="utf-8")

            out_default, _, _, _, _, local_dirs = tui.render(
                _make_args(state_dir, repos_dir=str(repos_root), no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIn("acme/repo-a", local_dirs)
            self.assertIn("acme/repo-b", local_dirs)
            self.assertIn("acme/repo-c", local_dirs)
            self.assertIn("Active task A", out_default)
            self.assertNotIn("Merged task B", out_default)
            self.assertTrue(board_file.is_file())
            board_text = board_file.read_text(encoding="utf-8")
            self.assertIn("https://gitlab.example/acme/repo-b/-/merge_requests", board_text)
            self.assertIn("https://bitbucket.org/acme/repo-c/pull-requests", board_text)

            out_all, _, _, _, _, _ = tui.render(
                _make_args(state_dir, repos_dir=str(repos_root), all=True, repo="repo-b", no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIn("Merged task B", out_all)
            self.assertNotIn("Active task A", out_all)

    def test_git_client_deploy_repo_to_home_includes_executable_shell_wrapper(self) -> None:
        """`git_client.deploy_repo_to_home()` deploys tracked `shell/*.sh` scripts with `0755`."""
        import git_client

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_repo = root / "src_plugin"
            fake_home = root / "home"
            src_repo.mkdir()
            fake_home.mkdir()
            _git(src_repo, "init", "-b", "main")
            (src_repo / "plugin.json").write_text(
                json.dumps({"name": "acme-dev-plugin", "version": "0000000-20261120"}) + "\n",
                encoding="utf-8",
            )
            (src_repo / "shell").mkdir()
            (src_repo / "shell" / "agy_dev_queue.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
            _git(src_repo, "add", "plugin.json", "shell/agy_dev_queue.sh")
            _git(src_repo, "commit", "-m", "chore: seed plugin")

            vcs = git_client.GitVcsIntegrationClient()
            res = vcs.deploy_repo_to_home(repo_dir=src_repo, home_dir=fake_home)
            self.assertEqual(res["status"], "ok")
            deployed_sh = fake_home / ".gemini" / "config" / "plugins" / "acme-dev-plugin" / "shell" / "agy_dev_queue.sh"
            self.assertTrue(deployed_sh.is_file())
            self.assertEqual(deployed_sh.stat().st_mode & 0o777, 0o755)
            self.assertTrue((fake_home / ".local" / "bin" / "agy-queue").is_symlink())
            self.assertTrue((fake_home / ".local" / "bin" / "agy-doctor").is_symlink())

    def test_live_sync_delegates_to_queue_sync_and_never_prematurely_merges_partial_multi_repo_item(self) -> None:
        """`_run_live_sync()` delegates to `pr_remediator.py queue-sync` and never falsely merges an unstarted 2nd repo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"

            # Repo A: real bare origin + merged feature branch
            origin_a = root / "origin-a.git"
            origin_a.mkdir()
            _git(origin_a, "init", "--bare", "-b", "main")
            repo_a = root / "repo-a"
            repo_a.mkdir()
            _git(repo_a, "init", "-b", "main")
            _git(repo_a, "remote", "add", "origin", str(origin_a))
            (repo_a / "base.txt").write_text("base\n", encoding="utf-8")
            _git(repo_a, "add", "base.txt")
            _git(repo_a, "commit", "-m", "chore: init repo-a")
            _git(repo_a, "push", "-u", "origin", "main")

            br_a = "fix/20261120-part1-77778888"
            _git(repo_a, "checkout", "-b", br_a)
            (repo_a / "part1.txt").write_text("part1\n", encoding="utf-8")
            _git(repo_a, "add", "part1.txt")
            _git(repo_a, "commit", "-m", "feat: part 1")
            _git(repo_a, "push", "-u", "origin", br_a)
            _git(repo_a, "checkout", "main")
            _git(repo_a, "merge", "--no-ff", br_a, "-m", f"Merge {br_a}")
            _git(repo_a, "push", "origin", "main")

            # Repo B: fresh unstarted repo (no feature branch exists yet!)
            origin_b = root / "origin-b.git"
            origin_b.mkdir()
            _git(origin_b, "init", "--bare", "-b", "main")
            repo_b = root / "repo-b"
            repo_b.mkdir()
            _git(repo_b, "init", "-b", "main")
            _git(repo_b, "remote", "add", "origin", str(origin_b))
            (repo_b / "base.txt").write_text("base\n", encoding="utf-8")
            _git(repo_b, "add", "base.txt")
            _git(repo_b, "commit", "-m", "chore: init repo-b")
            _git(repo_b, "push", "-u", "origin", "main")

            cid = "77778888-2222-4333-8444-555555555555"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                        "merged_repos": [],
                        "branch_names": [br_a],
                        "pr_urls": [f"https://github.com/acme/repo-a/compare/main...{br_a}?expand=1"],
                        "title": "Two-repo progressive rollout",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": False,
                        "repo_slug": "acme/repo-a",
                        "target_repo_dir": str(repo_a),
                    },
                    "acme/repo-b": {
                        "active": False,
                        "repo_slug": "acme/repo-b",
                        "target_repo_dir": str(repo_b),
                    },
                },
            )

            msg = tui._run_live_sync(
                state_dir,
                {"acme/repo-a": repo_a, "acme/repo-b": repo_b},
                offline=True,
            )
            self.assertIn("Offline sync OK (0 repos fetched, 1 PRs reconciled)", msg)

            q_after = json.loads((state_dir / "dev_queue.json").read_text(encoding="utf-8"))
            updated_item = q_after["items"][0]
            self.assertIn("acme/repo-a", updated_item["merged_repos"])
            self.assertNotIn("acme/repo-b", updated_item["merged_repos"])
            self.assertNotEqual(updated_item["status"], "merged")
            self.assertEqual(updated_item["status"], "pending")

            # Verify in-process queue_store reconciliation passes fetch=True ONLY when offline=False (zero pr_remediator subprocesses)
            qs_mod = tui._get_queue_store()
            self.assertIsNotNone(qs_mod)
            with (
                mock.patch.object(tui.subprocess, "run") as mock_run,
                mock.patch.object(
                    qs_mod,
                    "reconcile_dev_queue_with_git",
                    return_value={"status": "ok", "reconciled_count": 2, "reconciled_items": [{}, {}]},
                ) as mock_recon,
            ):
                mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                msg_online = tui._run_live_sync(state_dir, {"acme/repo-a": repo_a}, offline=False)
                self.assertIn("Sync OK (1 repos fetched, 2 PRs reconciled)", msg_online)
                self.assertEqual(mock_recon.call_count, 1)
                self.assertTrue(mock_recon.call_args.kwargs.get("fetch"))
                self.assertFalse(any("pr_remediator" in " ".join(map(str, c.args[0])) for c in mock_run.call_args_list))

                mock_run.reset_mock()
                mock_recon.reset_mock()
                msg_off = tui._run_live_sync(state_dir, {"acme/repo-a": repo_a}, offline=True)
                self.assertIn("Offline sync OK (0 repos fetched, 2 PRs reconciled)", msg_off)
                self.assertEqual(mock_recon.call_count, 1)
                self.assertFalse(mock_recon.call_args.kwargs.get("fetch"))
                self.assertEqual(mock_run.call_count, 0)

    def test_json_mode_applies_status_repo_all_filters_sorting_and_sidebar_title_enrichment(self) -> None:
        """`--json` honors `--status`, `--repo`, `--all`, priority sorting, `counts`, `locks`, and `sidebar_title`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            db_path = root / "conversation_summaries.db"

            cid_hitl_low = "40404040-2222-4333-8444-555555555555"
            cid_hitl_high = "10101010-2222-4333-8444-555555555555"
            cid_pending = "20202020-2222-4333-8444-555555555555"
            cid_merged = "30303030-2222-4333-8444-555555555555"

            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", (cid_hitl_low, "Later HITL Decision"))
            conn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", (cid_hitl_high, "Urgent HITL Decision"))
            conn.commit()
            conn.close()

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_hitl_low,
                        "priority": 40,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "Low prio HITL",
                    },
                    {
                        "conversation_id": cid_pending,
                        "priority": 15,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Pending repo-a",
                    },
                    {
                        "conversation_id": cid_hitl_high,
                        "priority": 10,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "High prio HITL",
                    },
                    {
                        "conversation_id": cid_merged,
                        "priority": 5,
                        "status": "merged",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": ["acme/repo-b"],
                        "title": "Merged repo-b",
                    },
                ],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": cid_hitl_low,
                    }
                },
            )

            # `--status hitl_blocked --json`: filtered, sorted by priority asc, enriched with sidebar_title
            raw_hitl, _, _, _, _, _ = tui.render(
                _make_args(state_dir, status="hitl_blocked", json=True),
                conv_db_path=db_path,
            )
            data_hitl = json.loads(raw_hitl)
            self.assertEqual([it["conversation_id"] for it in data_hitl["items"]], [cid_hitl_high, cid_hitl_low])
            self.assertEqual(data_hitl["items"][0]["sidebar_title"], "Urgent HITL Decision")
            self.assertEqual(data_hitl["items"][1]["sidebar_title"], "Later HITL Decision")
            self.assertEqual(data_hitl["counts"]["hitl_blocked"], 2)
            self.assertEqual(data_hitl["counts"]["merged"], 1)
            self.assertIn("acme/repo-b", data_hitl["locks"])

            # `--repo acme/repo-b --json` without `--all`: excludes merged item, filters repos/locks
            raw_repo, _, _, _, _, _ = tui.render(
                _make_args(state_dir, repo="acme/repo-b", json=True),
                conv_db_path=db_path,
            )
            data_repo = json.loads(raw_repo)
            self.assertEqual([it["conversation_id"] for it in data_repo["items"]], [cid_hitl_low])
            self.assertEqual(data_repo["managed_repos"], ["acme/repo-b"])
            self.assertEqual(list(data_repo["locks"].keys()), ["acme/repo-b"])

            # `--repo acme/repo-b --all --json`: includes merged item
            raw_all, _, _, _, _, _ = tui.render(
                _make_args(state_dir, repo="acme/repo-b", all=True, json=True),
                conv_db_path=db_path,
            )
            data_all = json.loads(raw_all)
            self.assertEqual({it["conversation_id"] for it in data_all["items"]}, {cid_hitl_low, cid_merged})

    def test_lock_parity_inactive_override_and_sibling_conversation_pr_gate(self) -> None:
        """`_load_locks()` pops inactive overrides and `sibling_conversation_ids` enforces the unready PR gate."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "dev_pr_lock.json").write_text(
                json.dumps(
                    {
                        "dev_pr_locks": {
                            "acme/repo-a": {"active": True, "status": "PENDING_HUMAN_PR_VALIDATION"},
                            "acme/repo-b": {"active": True, "status": "DEVELOPING"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_locks": {
                                "acme/repo-a": {"active": False, "status": "RELEASED"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            active_locks, all_locks = tui._load_locks(state_dir)
            self.assertNotIn("acme/repo-a", active_locks)
            self.assertIn("acme/repo-b", active_locks)
            self.assertIn("acme/repo-a", all_locks)

            # Active unready lock linked ONLY via sibling_conversation_ids must still block stale item pr_urls
            state_dir_sib = Path(tmp) / "state_sib"
            child_cid = "88881111-2222-4333-8444-555555555555"
            parent_cid = "99990000-2222-4333-8444-555555555555"
            stale_url = "https://github.com/acme/repo-a/compare/main...fix/20261120-sib-88881111?expand=1"
            _write_state(
                state_dir_sib,
                items=[
                    {
                        "conversation_id": child_cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "pr_urls": [stale_url],
                        "title": "Sibling-locked task",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "AWAITING_AGENT_REVIEW",
                        "pr_gate_ready": False,
                        "conversation_id": parent_cid,
                        "sibling_conversation_ids": [child_cid],
                        "branch_name": "fix/20261120-sib-88881111",
                        "manual_pr_url": stale_url,
                    }
                },
            )
            out, row_links, _, first_pr, _, _ = tui.render(
                _make_args(state_dir_sib, no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=124,
            )
            self.assertIsNone(first_pr)
            self.assertEqual(row_links, {})
            self.assertNotIn("[1] PR", out)
            self.assertNotIn(stale_url, out)

    def test_markdown_board_escaping_and_watch_sigterm_footer_board_hotkey_hardening(self) -> None:
        """Pipes/newlines never break `dev_queue_board.md`, wrapped footer stays in budget, SIGTERM restores, `[b]` is honest."""
        import math
        import signal

        url_with_pipe = (
            "https://github.com/acme/repo-a/compare/main...fix/20261120-esc-55556666"
            "?expand=1&title=feat(a|b):+ok&body=PASS+|+98/100"
        )
        escaped = tui._escape_md_url(url_with_pipe)
        self.assertNotIn("|", escaped)
        self.assertIn("%7C", escaped)
        self.assertIn("%28", escaped)
        self.assertIn("%29", escaped)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            board_path = root / "board.md"
            pr_cid = "55556666-2222-4333-8444-555555555555"
            hitl_cid = "66667777-2222-4333-8444-555555555555"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": pr_cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "pr_urls": [url_with_pipe],
                        "title": "Line one\nLine two | pipe",
                    },
                    {
                        "conversation_id": hitl_cid,
                        "priority": 20,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "HITL\r\nTitle | Pipe",
                        "notes": "Option A | Option B\nSecond line detail",
                    },
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": pr_cid,
                        "branch_name": "fix/20261120-esc-55556666",
                        "manual_pr_url": url_with_pipe,
                    }
                },
            )
            tui.render(
                _make_args(state_dir, write_board=str(board_path), no_color=True),
                is_tty_override=False,
                term_width_override=124,
            )
            board_lines = board_path.read_text(encoding="utf-8").splitlines()
            table_rows = [ln for ln in board_lines if ln.startswith("|")]
            self.assertGreaterEqual(len(table_rows), 5)
            # Every Markdown table row must have balanced unescaped column separators and no raw newlines
            self.assertTrue(any("Line one Line two / pipe" in ln for ln in table_rows))
            self.assertTrue(any("Option A / Option B Second line detail" in ln for ln in table_rows))
            self.assertTrue(any(f"[Open PR]({escaped})" in ln for ln in table_rows))

            # Wrapped footer budget inside _clamp_lines_to_viewport
            long_footer = "[o] PR · [1-9] open · [h+#] Agy · [b] board · [s] sync · [a] merged · [q] quit (3s) EXTRA TEXT"
            body = [f"row-{i:02d}" for i in range(15)]
            clamped = tui._clamp_lines_to_viewport(body, term_w=40, term_h=8, footer=long_footer)
            total_wrapped_rows = sum(max(1, math.ceil(tui._visible_len(ln) / 40)) for ln in clamped)
            self.assertLessEqual(total_wrapped_rows, 8)
            self.assertEqual(clamped[-1], long_footer)

            # SIGTERM handler saved & restored cleanly in run_watch_loop
            custom_handler = lambda _sig, _frame: None  # noqa: E731
            orig_sigterm = signal.getsignal(signal.SIGTERM)
            try:
                signal.signal(signal.SIGTERM, custom_handler)
                buf = io.StringIO()
                tui.run_watch_loop(
                    _make_args(state_dir, watch=0.5, no_color=True),
                    stream=buf,
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=1,
                    term_size_override=(90, 20),
                )
                self.assertIs(signal.getsignal(signal.SIGTERM), custom_handler)
            finally:
                signal.signal(signal.SIGTERM, orig_sigterm)

            # Hotkey [b] reports honestly when unconfigured and syncs + focuses when configured
            msg_unconfigured = tui._handle_board_hotkey(_make_args(state_dir), state_dir)
            self.assertIn("No board path configured", msg_unconfigured)

            board_path_2 = root / "hotkey_board.md"
            with mock.patch.object(tui, "_focus_antigravity_app", return_value=True) as mock_focus:
                msg_configured = tui._handle_board_hotkey(
                    _make_args(state_dir, write_board=str(board_path_2)),
                    state_dir,
                )
            self.assertIn("hotkey_board.md", msg_configured)
            self.assertTrue(board_path_2.is_file())
            mock_focus.assert_called_once_with()

    def test_stalled_worker_progress_bar_alert_and_compact_opt_footer_hint(self) -> None:
        """Stalled DEVELOPING/NEEDS_REVISION_ITERATING locks render STALL alert (never 35% ~6m) and compact OPT: footer."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            stale_wt = root / "wt-stale"
            stale_wt.mkdir(parents=True)
            _git(stale_wt, "init", "-b", "main")
            (stale_wt / "README.md").write_text("# Acme\n", encoding="utf-8")
            _git(stale_wt, "add", "README.md")
            _git(stale_wt, "commit", "-m", "init")
            # Create a dirty uncommitted edit backdated by 1h42m (6120s)
            (stale_wt / "dirty.py").write_text("PENDING = True\n", encoding="utf-8")

            now_iso = "2027-01-15T10:00:00Z"
            stale_iso = "2027-01-15T08:18:00Z"
            now_ts = tui._flow_opt.parse_iso_timestamp(now_iso)
            stale_ts = now_ts - 6120.0
            for fn in ("README.md", "dirty.py"):
                os.utime(stale_wt / fn, (stale_ts, stale_ts))

            cid_stale = "abab1111-2222-4333-8444-555555555555"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_stale,
                        "priority": 10,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Zombie worker task",
                        "target_files": ["dirty.py"],
                    },
                    {
                        "conversation_id": "cdcd1111-2222-4333-8444-555555555555",
                        "priority": 30,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "UI badge polish 1",
                        "target_files": ["a.css"],
                    },
                    {
                        "conversation_id": "cdcd2222-2222-4333-8444-555555555555",
                        "priority": 31,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "UI badge polish 2",
                        "target_files": ["b.css"],
                    },
                    {
                        "conversation_id": "cdcd3333-2222-4333-8444-555555555555",
                        "priority": 32,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "title": "UI badge polish 3",
                        "target_files": ["c.css"],
                    },
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": cid_stale,
                        "branch_name": "fix/20270115-081800-zombie-abab1111",
                        "worktree_path": str(stale_wt),
                        "acquired_at": stale_iso,
                        "updated_at": stale_iso,
                        "last_activity_at": stale_iso,
                    }
                },
            )

            # 1. Stalled DEVELOPING lock MUST NOT display misleading 35% ~6m
            lp_stale = tui._compute_lock_progress(
                {
                    "active": True,
                    "status": "DEVELOPING",
                    "pr_gate_ready": False,
                    "worktree_path": str(stale_wt),
                    "updated_at": stale_iso,
                },
                now_ts=now_ts,
                brain_dir="",
            )
            self.assertTrue(lp_stale["is_stalled"])
            self.assertEqual(lp_stale["tag"], "STALL 1h42m")

            # 2. Inline progress bar uses red/magenta alert ANSI styling (1;31 + 1;35)
            prog_stalled = {"pct": 0, "tag": "STALL 1h42m", "mode": "stalled", "is_stalled": True}
            cell_color = tui._render_inline_progress(prog_stalled, 20, color_on=True)
            self.assertIn("\033[1;31m", cell_color)
            self.assertIn("\033[1;35m", cell_color)
            self.assertIn("STALL 1h42m", cell_color)
            self.assertNotIn("35%", cell_color)
            self.assertNotIn("~6m", cell_color)

            # 3. Full render surfaces STALL progress cell, STALLED WORKER warning, and compact OPT: footer
            out_txt, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                brain_dir="",
                is_tty_override=False,
                term_width_override=124,
                now_ts_override=now_ts,
            )
            self.assertIn("STALL 1h42m", out_txt)
            self.assertIn("STALLED WORKER:", out_txt)
            self.assertIn("OPT:", out_txt)
            self.assertNotIn("35% ~6m", out_txt)

            # 4. `--no-opt` hides OPT: footer hint while preserving STALLED WORKER alert
            out_no_opt, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True, no_opt=True),
                brain_dir="",
                is_tty_override=False,
                term_width_override=124,
                now_ts_override=now_ts,
            )
            self.assertIn("STALLED WORKER:", out_no_opt)
            self.assertNotIn("OPT:", out_no_opt)

            # 5. Fresh dirty DEVELOPING lock reports 35% ok·2m (not stalled, zero fake ~6m ETA)
            fresh_iso = "2027-01-15T09:58:00Z"
            lp_fresh = tui._compute_lock_progress(
                {
                    "active": True,
                    "status": "DEVELOPING",
                    "pr_gate_ready": False,
                    "worktree_path": str(stale_wt),
                    "last_activity_at": fresh_iso,
                },
                now_ts=now_ts,
                brain_dir="",
            )
            self.assertFalse(lp_fresh["is_stalled"])
            self.assertEqual(lp_fresh["pct"], 35)
            self.assertEqual(lp_fresh["tag"], "ok·2m")

    def test_untracked_ad_hoc_locks_synthesized_and_phantom_pr_open_never_hallucinates_pulls(self) -> None:
        """Untracked active ad-hoc locks surface in table/counters/PR footer; phantom `pr_open` renders Stale (never fake `/pulls` PR)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            board_file = root / "board.md"

            adhoc_pr_cid = "2770c9ef-1111-4222-8333-444444444444"
            adhoc_dev_cid = "0305246e-1111-4222-8333-555555555555"
            phantom_cid = "2170eff2-1111-4222-8333-666666666666"
            real_pr_url = (
                "https://github.com/acme/repo-ui/compare/main...fix/20261121-092514-adhoc-2770c9ef"
                "?expand=1&title=fix%28ui%29%3A%20real%20ad-hoc%20PR&body=PASS%2099%2F100"
            )
            now_iso = "2027-01-15T10:00:00Z"
            now_ts = tui._flow_opt.parse_iso_timestamp(now_iso)

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": phantom_cid,
                        "priority": 18,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-api", "acme/repo-ui"],
                        "merged_repos": ["acme/repo-api"],
                        "pr_urls": [],
                        "title": "Phantom partial multi-repo PR",
                    }
                ],
                locks={
                    "acme/repo-ui": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": adhoc_pr_cid,
                        "proposal_title": "fix(ui): real ad-hoc PR",
                        "branch_name": "fix/20261121-092514-adhoc-2770c9ef",
                        "manual_pr_url": real_pr_url,
                        "pr_url": real_pr_url,
                        "acquired_at": now_iso,
                        "updated_at": now_iso,
                        "last_activity_at": now_iso,
                    },
                    "acme/repo-api": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": adhoc_dev_cid,
                        "action_id": "adhoc-api-worker",
                        "branch_name": "fix/20261121-093401-adhoc-0305246e",
                        "acquired_at": now_iso,
                        "updated_at": now_iso,
                        "last_activity_at": now_iso,
                    },
                },
            )

            out_txt, row_links, _, first_pr, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True, write_board=str(board_file)),
                brain_dir="",
                is_tty_override=False,
                term_width_override=124,
                now_ts_override=now_ts,
            )
            # Ad-hoc locks MUST be visible in counters (`run:1`) and table
            self.assertIn("run:1", out_txt)
            self.assertIn("[ad-hoc] fix(ui): real ad-hoc PR", out_txt)
            self.assertIn("[ad-hoc] adhoc-api-worker", out_txt)
            # First PR hotkey & footer MUST point to the real ad-hoc PR (never phantom `/pulls`!)
            self.assertIsNotNone(first_pr)
            assert first_pr is not None
            self.assertEqual(first_pr[1], real_pr_url)
            self.assertEqual(row_links[1][1], real_pr_url)
            self.assertIn("[+verified]", out_txt)
            # Phantom pr_open row MUST render `[↻ Stale]` and NEVER `PR 2/2 ready` or `[commit fallback]`
            self.assertIn("[↻ Stale]", out_txt)
            self.assertNotIn("PR 2/2 ready", out_txt)
            self.assertNotIn("[commit fallback]", out_txt)
            # Board artifact includes the real ad-hoc PR link
            board_md = board_file.read_text(encoding="utf-8")
            self.assertIn("fix/20261121-092514-adhoc-2770c9ef", board_md)


    def test_auto_reload_fingerprints_syntax_guard_execv_banner_and_manual_R_hotkey(self) -> None:
        """Auto-reload validates Python syntax with `ast.parse`, tears down alt-screen before `os.execv`, and supports `[R]`."""
        watched = tui._collect_watched_script_paths()
        self.assertGreaterEqual(len(watched), 1)
        self.assertEqual(watched[0], Path(tui.__file__).resolve())
        watched_names = {p.name for p in watched}
        for mod_name in (
            "tui_models.py",
            "tui_state.py",
            "tui_progress.py",
            "tui_render.py",
            "tui_board.py",
            "tui_watch.py",
        ):
            self.assertIn(mod_name, watched_names)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            _write_state(state_dir, items=[])
            fake_script = root / "dev_queue_tui_copy.py"
            fake_script.write_text("VALUE = 1\n", encoding="utf-8")

            argv_built = tui._build_reload_exec_argv(fake_script, ["-w", "2", "--no-auto-sync"])
            self.assertEqual(
                argv_built,
                [sys.executable, str(fake_script.resolve()), "-w", "2", "--no-auto-sync"],
            )

            # 1. Broken syntax on disk MUST NOT crash or call exec_fn; shows warning in watch footer
            exec_calls: list[tuple[str, list[str]]] = []

            def _fake_exec(exe: str, argv: list[str]) -> None:
                exec_calls.append((exe, list(argv)))

            def _corrupt_later() -> None:
                time.sleep(0.04)
                fake_script.write_text("def broken(:\n", encoding="utf-8")

            corrupt_t = threading.Thread(target=_corrupt_later, daemon=True)
            corrupt_t.start()
            buf_bad = io.StringIO()
            rc_bad = tui.run_watch_loop(
                _make_args(state_dir, watch=0.4, no_color=True, no_auto_sync=True),
                stream=buf_bad,
                use_tty_override=True,
                open_tty=False,
                max_iterations=2,
                term_size_override=(120, 22),
                exec_fn=_fake_exec,
                watched_paths_override=[fake_script],
                reload_debounce_seconds=0.0,
            )
            corrupt_t.join(timeout=1.0)
            self.assertEqual(rc_bad, 0)
            self.assertEqual(exec_calls, [])
            self.assertIn("Reload skipped (dev_queue_tui_copy.py: SyntaxError)", buf_bad.getvalue())

            # 2. Valid script update triggers clean alt-screen exit, sets _AGY_QUEUE_RELOADED=1, and calls exec_fn
            fake_script.write_text("VALUE = 2\n", encoding="utf-8")

            def _update_valid_later() -> None:
                time.sleep(0.04)
                fake_script.write_text("VALUE = 300\n", encoding="utf-8")

            valid_t = threading.Thread(target=_update_valid_later, daemon=True)
            valid_t.start()
            buf_ok = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("_AGY_QUEUE_RELOADED", None)
                rc_ok = tui.run_watch_loop(
                    _make_args(state_dir, watch=0.5, no_color=True, no_auto_sync=True),
                    stream=buf_ok,
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=5,
                    term_size_override=(120, 22),
                    exec_fn=_fake_exec,
                    watched_paths_override=[fake_script],
                    reload_debounce_seconds=0.0,
                    raw_argv=["-w", "--no-auto-sync"],
                )
                valid_t.join(timeout=1.0)
                self.assertEqual(rc_ok, 0)
                self.assertEqual(len(exec_calls), 1)
                self.assertEqual(exec_calls[0][0], sys.executable)
                self.assertEqual(
                    exec_calls[0][1],
                    [sys.executable, str(fake_script.resolve()), "-w", "--no-auto-sync"],
                )
                self.assertEqual(os.environ.get("_AGY_QUEUE_RELOADED"), "1")
                self.assertTrue(buf_ok.getvalue().endswith(tui.ALT_SCREEN_EXIT))

                # 3. Startup after reload consumes _AGY_QUEUE_RELOADED=1 and displays reload banner
                buf_reloaded = io.StringIO()
                tui.run_watch_loop(
                    _make_args(state_dir, watch=0.2, no_color=True, no_auto_sync=True),
                    stream=buf_reloaded,
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=1,
                    term_size_override=(120, 22),
                )
                self.assertNotIn("_AGY_QUEUE_RELOADED", os.environ)
                self.assertIn("Auto-reloaded after plugin update", buf_reloaded.getvalue())

            # 4. Lowercase [r] repaints without exec_fn; uppercase [R] triggers safe exec_fn reload
            exec_calls.clear()
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, b"rR")
                os.close(w_fd)
                w_fd = -1
                buf_keys = io.StringIO()
                with mock.patch.dict(os.environ, {}, clear=False):
                    rc_keys = tui.run_watch_loop(
                        _make_args(state_dir, watch=0.5, no_color=True, no_auto_sync=True),
                        stream=buf_keys,
                        use_tty_override=True,
                        open_tty=False,
                        tty_fd_override=r_fd,
                        max_iterations=5,
                        term_size_override=(120, 22),
                        exec_fn=_fake_exec,
                        watched_paths_override=[fake_script],
                        raw_argv=["-w"],
                    )
                self.assertEqual(rc_keys, 0)
                self.assertIn("Refreshed", buf_keys.getvalue())
                self.assertEqual(len(exec_calls), 1)
            finally:
                os.close(r_fd)
                if w_fd >= 0:
                    os.close(w_fd)

    def test_priority_viewport_keeps_pr_hitl_opt_footers_visible_and_scroll_compact_open_all_hotkeys(self) -> None:
        """22 pending rows on a 26-row terminal never clip PR/HITL/STALLED/OPT footers; `[j]/[k]/↑/↓`, `[p]`, and `[O]` work."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            pr1_cid = "1111aaaa-2222-4333-8444-555555555555"
            pr2_cid = "2222bbbb-2222-4333-8444-555555555555"
            hitl_cid = "3333cccc-2222-4333-8444-555555555555"
            url_1 = "https://github.com/acme/repo-a/pull/101"
            url_2 = "https://github.com/acme/repo-b/pull/202"

            pending_items = [
                {
                    "conversation_id": f"9999{i:04d}-2222-4333-8444-555555555555",
                    "priority": 20 + i,
                    "status": "pending",
                    "repo_slugs": ["acme/repo-a"],
                    "merged_repos": [],
                    "title": f"Queued task item {i:02d}",
                }
                for i in range(1, 23)
            ]
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": pr1_cid,
                        "priority": 5,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "pr_urls": [url_1],
                        "title": "Ready PR One",
                    },
                    {
                        "conversation_id": pr2_cid,
                        "priority": 6,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "pr_urls": [url_2],
                        "title": "Ready PR Two",
                    },
                    {
                        "conversation_id": hitl_cid,
                        "priority": 12,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Approve Architecture Contract",
                    },
                    *pending_items,
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": pr1_cid,
                        "branch_name": "fix/20261121-pr1-1111aaaa",
                        "pr_url": url_1,
                    },
                    "acme/repo-b": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": pr2_cid,
                        "branch_name": "fix/20261121-pr2-2222bbbb",
                        "pr_url": url_2,
                    },
                },
            )

            layout: dict[str, object] = {}
            tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                brain_dir="",
                is_tty_override=True,
                term_width_override=124,
                layout_out=layout,
            )
            self.assertEqual(len(layout["all_pr_links"]), 2)  # type: ignore[arg-type]
            self.assertTrue(layout["has_active_work"])

            vp_top = tui._compose_priority_viewport(
                layout["header_lines"],  # type: ignore[arg-type]
                layout["active_row_lines"],  # type: ignore[arg-type]
                layout["middle_row_lines"],  # type: ignore[arg-type]
                layout["sticky_footer_lines"],  # type: ignore[arg-type]
                term_w=124,
                term_h=26,
                footer="[q] quit",
                scroll_offset=0,
                compact_pending=False,
            )
            self.assertLessEqual(len(vp_top), 26)
            joined_top = "\n".join(vp_top)
            self.assertIn("PRs READY FOR MERGE", joined_top)
            self.assertIn(url_1, joined_top)
            self.assertIn(url_2, joined_top)
            self.assertIn("HITL — ANTIGRAVITY APP VALIDATION", joined_top)
            self.assertIn("Queued task item 01", joined_top)
            self.assertIn("[j/k] scroll", joined_top)

            # Scrolling down reveals deeper pending items while keeping sticky PR/HITL footers intact
            vp_scrolled = tui._compose_priority_viewport(
                layout["header_lines"],  # type: ignore[arg-type]
                layout["active_row_lines"],  # type: ignore[arg-type]
                layout["middle_row_lines"],  # type: ignore[arg-type]
                layout["sticky_footer_lines"],  # type: ignore[arg-type]
                term_w=124,
                term_h=26,
                footer="[q] quit",
                scroll_offset=18,
                compact_pending=False,
            )
            joined_scrolled = "\n".join(vp_scrolled)
            self.assertIn("PRs READY FOR MERGE", joined_scrolled)
            self.assertIn("HITL — ANTIGRAVITY APP VALIDATION", joined_scrolled)
            self.assertIn("Queued task item 22", joined_scrolled)

            # Compact pending mode keeps at most 3 PENDING rows + all HITL_BLOCK rows via QueueRowView.status (zero ANSI scraping)
            vp_compact = tui._compose_priority_viewport(
                layout["header_lines"],  # type: ignore[arg-type]
                layout["active_row_lines"],  # type: ignore[arg-type]
                layout["middle_row_lines"],  # type: ignore[arg-type]
                layout["sticky_footer_lines"],  # type: ignore[arg-type]
                term_w=124,
                term_h=26,
                footer="[q] quit",
                scroll_offset=0,
                compact_pending=True,
                middle_rows=layout["middle_rows"],  # type: ignore[arg-type]
            )
            joined_compact = "\n".join(vp_compact)
            self.assertIn("Approve Architecture Contract", joined_compact)
            self.assertIn("Queued task item 03", joined_compact)
            self.assertNotIn("Queued task item 04", joined_compact)
            self.assertIn("[p] full", joined_compact)

            # Verify _filter_compact_middle_rows filters directly on QueueRowView.status even without status text in raw_line
            masked_rows = [
                tui.QueueRowView(
                    idx=i,
                    priority=10 + i,
                    status="hitl_blocked" if i == 5 else "pending",
                    raw_status="hitl_blocked" if i == 5 else "pending",
                    band="middle",
                    display_id=f"row{i}",
                    conversation_id=f"cid-{i}",
                    sidebar_title="",
                    title=f"Masked {i}",
                    repos_str="repo",
                    repos_md="repo",
                    raw_line=f"\033[33mMASKED_ROW_{i}\033[0m",
                )
                for i in range(1, 7)
            ]
            compact_masked = tui._filter_compact_middle_rows(masked_rows, max_pending=3)
            self.assertEqual(len(compact_masked), 4)
            self.assertIn("MASKED_ROW_5", compact_masked[-1])

            # Interactive watch hotkeys: [j], Down (\x1b[B), [k], Up (\x1b[A), [p], [o] (1 PR), [O] (all 2 PRs), [q]
            r_fd, w_fd = os.pipe()
            opened_urls: list[str] = []
            try:
                os.write(w_fd, b"j\x1b[Bk\x1b[ApoOq")
                os.close(w_fd)
                w_fd = -1
                buf_watch = io.StringIO()
                with mock.patch.object(
                    tui,
                    "_open_url_in_browser",
                    side_effect=lambda u: opened_urls.append(u) or True,
                ):
                    rc = tui.run_watch_loop(
                        _make_args(state_dir, watch=0.4, no_color=True, plain_urls=True, no_auto_sync=True),
                        stream=buf_watch,
                        use_tty_override=True,
                        open_tty=False,
                        tty_fd_override=r_fd,
                        max_iterations=12,
                        term_size_override=(124, 26),
                    )
                self.assertEqual(rc, 0)
                watch_txt = buf_watch.getvalue()
                self.assertIn("Pending view: COMPACT", watch_txt)
                self.assertIn("Opening 2 ready PR(s)", watch_txt)
                # [o] opened url_1 (1), then [O] opened url_1 + url_2 (2) -> total 3 browser calls
                self.assertEqual(opened_urls, [url_1, url_1, url_2])
            finally:
                os.close(r_fd)
                if w_fd >= 0:
                    os.close(w_fd)

    def test_async_background_auto_sync_gating_single_flight_async_s_and_deduplicated_fetch(self) -> None:
        """`_run_live_sync` avoids triple-fetching; background auto-sync respects single-flight, locks/PRs, `--no-auto-sync`, and `AGY_OFFLINE_ONLY`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()

            # 1. Active lock + zero unlocked PRs -> ONLY `lock-status --all --sync-git` runs (no duplicate queue-sync or git fetch!)
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "aaaa1111-2222-4333-8444-555555555555",
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "pr_urls": ["https://github.com/acme/repo-a/pull/1"],
                        "title": "Locked repo PR",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": "aaaa1111-2222-4333-8444-555555555555",
                    }
                },
            )
            ls_mod = tui._get_lock_store()
            qs_mod = tui._get_queue_store()
            self.assertIsNotNone(ls_mod)
            self.assertIsNotNone(qs_mod)
            with (
                mock.patch.object(tui.subprocess, "run") as mock_run,
                mock.patch.object(
                    ls_mod,
                    "sync_dev_pr_lock_with_git",
                    return_value={"status": "ok", "queue_reconciliation": {"reconciled_count": 1}},
                ) as mock_lock_sync,
                mock.patch.object(
                    qs_mod,
                    "reconcile_dev_queue_with_git",
                    return_value={"status": "ok", "reconciled_count": 0},
                ) as mock_q_recon,
            ):
                msg_dedup = tui._run_live_sync(
                    state_dir,
                    {"acme/repo-a": repo_a, "acme/repo-b": repo_b},
                    offline=False,
                )
                self.assertIn("Sync OK (1 repos fetched, 1 PRs reconciled)", msg_dedup)
                self.assertEqual(mock_lock_sync.call_count, 1)
                self.assertEqual(mock_q_recon.call_count, 0)
                self.assertEqual(mock_run.call_count, 0)

            # 2. Active lock on repo-a + unlocked pr_open on repo-b -> lock_store sync + scoped reconcile_dev_queue_with_git(repo_slug="acme/repo-b", fetch=True)
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "bbbb2222-2222-4333-8444-555555555555",
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "pr_urls": ["https://github.com/acme/repo-b/pull/2"],
                        "title": "Unlocked repo PR",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": "aaaa1111-2222-4333-8444-555555555555",
                    }
                },
            )
            with (
                mock.patch.object(tui.subprocess, "run") as mock_run,
                mock.patch.object(
                    ls_mod,
                    "sync_dev_pr_lock_with_git",
                    return_value={"status": "ok", "queue_reconciliation": {"reconciled_count": 0}},
                ) as mock_lock_sync,
                mock.patch.object(
                    qs_mod,
                    "reconcile_dev_queue_with_git",
                    return_value={"status": "ok", "reconciled_count": 0},
                ) as mock_q_recon,
            ):
                msg_split = tui._run_live_sync(
                    state_dir,
                    {"acme/repo-a": repo_a, "acme/repo-b": repo_b},
                    offline=False,
                )
                self.assertIn("Sync OK (2 repos fetched", msg_split)
                self.assertEqual(mock_lock_sync.call_count, 1)
                self.assertEqual(mock_q_recon.call_count, 1)
                self.assertEqual(mock_q_recon.call_args.kwargs.get("repo_slug"), "acme/repo-b")
                self.assertTrue(mock_q_recon.call_args.kwargs.get("fetch"))
                self.assertEqual(mock_run.call_count, 0)

            # 3. Single-flight controller never spawns overlapping threads and formats live sync badge
            gate = threading.Event()
            started = threading.Event()
            sync_invocations = 0

            def _slow_sync(_sd: Path, _dirs: dict[str, Path]) -> str:
                nonlocal sync_invocations
                sync_invocations += 1
                started.set()
                gate.wait(timeout=1.5)
                return "↻ Sync OK (1 repos fetched, 1 PRs reconciled)"

            ctrl = tui._AsyncSyncController(sync_fn=_slow_sync)
            self.assertEqual(ctrl.format_badge(), "")
            self.assertTrue(ctrl.trigger(state_dir, {}))
            self.assertTrue(started.wait(timeout=1.0))
            self.assertEqual(ctrl.format_badge(), "sync:syncing…")
            self.assertFalse(ctrl.trigger(state_dir, {}))
            gate.set()
            deadline = time.monotonic() + 1.0
            while ctrl.is_in_flight() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(sync_invocations, 1)
            self.assertIn("Sync OK", ctrl.consume_result() or "")
            self.assertTrue(ctrl.format_badge().startswith("sync:"))

            # 4. Watch auto-sync gating: fires when active work exists; skipped when idle, --no-auto-sync, or AGY_OFFLINE_ONLY=1
            auto_calls: list[int] = []

            def _fast_sync(_sd: Path, _dirs: dict[str, Path]) -> str:
                auto_calls.append(1)
                return "↻ Sync OK (1 repos fetched, 0 PRs reconciled)"

            with mock.patch.dict(os.environ, {"AGY_OFFLINE_ONLY": "0"}, clear=False):
                buf_auto = io.StringIO()
                tui.run_watch_loop(
                    _make_args(state_dir, watch=0.25, sync_interval=10.0, no_auto_sync=False, no_color=True),
                    stream=buf_auto,
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=3,
                    term_size_override=(120, 22),
                    sync_fn=_fast_sync,
                )
            self.assertEqual(len(auto_calls), 1)
            self.assertIn("sync:", buf_auto.getvalue())

            # No auto-sync when `--no-auto-sync` or `AGY_OFFLINE_ONLY=1` or only pending items without locks
            auto_calls.clear()
            tui.run_watch_loop(
                _make_args(state_dir, watch=0.06, sync_interval=0.01, no_auto_sync=True, no_color=True),
                stream=io.StringIO(),
                use_tty_override=True,
                open_tty=False,
                max_iterations=2,
                sync_fn=_fast_sync,
            )
            self.assertEqual(auto_calls, [])

            with mock.patch.dict(os.environ, {"AGY_OFFLINE_ONLY": "1"}, clear=False):
                tui.run_watch_loop(
                    _make_args(state_dir, watch=0.06, sync_interval=0.01, no_auto_sync=False, no_color=True),
                    stream=io.StringIO(),
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=2,
                    sync_fn=_fast_sync,
                )
            self.assertEqual(auto_calls, [])

            state_idle = root / "state_idle"
            _write_state(
                state_idle,
                items=[
                    {
                        "conversation_id": "cccc3333-2222-4333-8444-555555555555",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Idle pending only",
                    }
                ],
                locks={},
            )
            with mock.patch.dict(os.environ, {"AGY_OFFLINE_ONLY": "0"}, clear=False):
                tui.run_watch_loop(
                    _make_args(state_idle, watch=0.06, sync_interval=0.01, no_auto_sync=False, no_color=True),
                    stream=io.StringIO(),
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=2,
                    sync_fn=_fast_sync,
                )
            self.assertEqual(auto_calls, [])

            # Manual [s] hotkey triggers async sync even on idle queue without blocking input
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, b"s")
                os.close(w_fd)
                w_fd = -1
                buf_s = io.StringIO()
                tui.run_watch_loop(
                    _make_args(state_idle, watch=0.2, no_auto_sync=True, no_color=True),
                    stream=buf_s,
                    use_tty_override=True,
                    open_tty=False,
                    tty_fd_override=r_fd,
                    max_iterations=3,
                    term_size_override=(120, 22),
                    sync_fn=_fast_sync,
                )
                self.assertEqual(len(auto_calls), 1)
                self.assertIn("Sync OK", buf_s.getvalue())
            finally:
                os.close(r_fd)
                if w_fd >= 0:
                    os.close(w_fd)

    def test_stat_dirty_check_immediate_wakeup_sigwinch_repo_cache_and_hash_guarded_board(self) -> None:
        """`stat()` dirty-check wakes `<350ms` on state/worktree edits, `SIGWINCH` restores, `_discover_repos` caches 60s, and board skips no-op writes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            board_path = root / "board.md"
            wt_dir = root / "worktree"
            git_meta = root / "real_gitdir"
            wt_dir.mkdir()
            git_meta.mkdir()
            (wt_dir / ".git").write_text(f"gitdir: {git_meta}\n", encoding="utf-8")
            (git_meta / "HEAD").write_text("ref: refs/heads/fix/demo\n", encoding="utf-8")
            (git_meta / "index").write_bytes(b"idx1")

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "11112222-3333-4444-8555-666666666666",
                        "priority": 10,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Initial title",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": "11112222-3333-4444-8555-666666666666",
                        "worktree_path": str(wt_dir),
                    }
                },
            )

            # 1. Worktree gitdir pointer fingerprint changes when index updates
            locks_loaded, _ = tui._load_locks(state_dir)
            fp1 = tui._snapshot_state_fingerprint(state_dir, locks_loaded)
            (git_meta / "index").write_bytes(b"idx2-updated")
            fp2 = tui._snapshot_state_fingerprint(state_dir, locks_loaded)
            self.assertNotEqual(fp1, fp2)

            # 2. Immediate dirty-check wakeup: even with watch=5.0s, editing dev_queue.json wakes frame 2 in <0.6s
            def _mutate_queue_soon() -> None:
                time.sleep(0.06)
                _write_state(
                    state_dir,
                    items=[
                        {
                            "conversation_id": "11112222-3333-4444-8555-666666666666",
                            "priority": 10,
                            "status": "in_progress",
                            "repo_slugs": ["acme/repo-a"],
                            "merged_repos": [],
                            "title": "Mutated live title",
                        }
                    ],
                )

            mut_t = threading.Thread(target=_mutate_queue_soon, daemon=True)
            t0 = time.monotonic()
            mut_t.start()
            buf_wake = io.StringIO()
            tui.run_watch_loop(
                _make_args(state_dir, watch=5.0, no_auto_sync=True, no_color=True),
                stream=buf_wake,
                use_tty_override=True,
                open_tty=False,
                max_iterations=2,
                term_size_override=(120, 22),
            )
            elapsed = time.monotonic() - t0
            mut_t.join(timeout=1.0)
            self.assertLess(elapsed, 1.0)
            self.assertIn("Mutated live title", buf_wake.getvalue())

            # 3. SIGWINCH handler installed & restored cleanly
            sigwinch = getattr(signal, "SIGWINCH", None)
            if sigwinch is not None:
                custom_winch = lambda _s, _f: None  # noqa: E731
                orig_winch = signal.getsignal(sigwinch)
                try:
                    signal.signal(sigwinch, custom_winch)
                    tui.run_watch_loop(
                        _make_args(state_dir, watch=0.2, no_auto_sync=True, no_color=True),
                        stream=io.StringIO(),
                        use_tty_override=True,
                        open_tty=False,
                        max_iterations=1,
                        term_size_override=(100, 20),
                    )
                    self.assertIs(signal.getsignal(sigwinch), custom_winch)
                finally:
                    signal.signal(sigwinch, orig_winch)

            # 4. Repo discovery TTL cache skips duplicate _discover_repos calls until hints change or TTL expires
            repo_cache: dict[str, object] = {"ttl": 60.0}
            with mock.patch.object(
                tui,
                "_discover_repos",
                wraps=tui._discover_repos,
            ) as spy_discover:
                tui.render(
                    _make_args(state_dir, no_color=True),
                    is_tty_override=False,
                    repo_cache=repo_cache,  # type: ignore[arg-type]
                )
                tui.render(
                    _make_args(state_dir, no_color=True),
                    is_tty_override=False,
                    repo_cache=repo_cache,  # type: ignore[arg-type]
                )
                self.assertEqual(spy_discover.call_count, 1)

                # Adding a new repo slug invalidates cache immediately
                _write_state(
                    state_dir,
                    items=[
                        {
                            "conversation_id": "11112222-3333-4444-8555-666666666666",
                            "priority": 10,
                            "status": "pending",
                            "repo_slugs": ["acme/repo-a", "acme/repo-new"],
                            "merged_repos": [],
                            "title": "Added repo",
                        }
                    ],
                )
                tui.render(
                    _make_args(state_dir, no_color=True),
                    is_tty_override=False,
                    repo_cache=repo_cache,  # type: ignore[arg-type]
                )
                self.assertEqual(spy_discover.call_count, 2)

            # 5. Hash-guarded board write skips redundant disk writes unless content changes or force_board_write=True
            tui.render(
                _make_args(state_dir, write_board=str(board_path), no_color=True),
                is_tty_override=False,
            )
            self.assertTrue(board_path.is_file())
            mtime_1 = board_path.stat().st_mtime_ns
            with mock.patch.object(tui.os, "replace", wraps=os.replace) as spy_replace:
                tui.render(
                    _make_args(state_dir, write_board=str(board_path), no_color=True),
                    is_tty_override=False,
                )
                self.assertEqual(spy_replace.call_count, 0)
                self.assertEqual(board_path.stat().st_mtime_ns, mtime_1)

                # [b] hotkey forces write even when content is unchanged
                with mock.patch.object(tui, "_focus_antigravity_app", return_value=True):
                    tui._handle_board_hotkey(_make_args(state_dir, write_board=str(board_path)), state_dir)
                self.assertEqual(spy_replace.call_count, 1)

    def test_orchestrator_liveness_badge_and_transient_io_recovery_preserving_runtime_errors(self) -> None:
        """Header renders `orch:<id8> (idle ...)` from transcript mtime, and watch loop recovers from torn JSON/SQLite/OS errors."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            orch_cid = "444cef60-2222-4333-8444-555555555555"
            tpath = brain_dir / orch_cid / ".system_generated" / "logs" / "transcript.jsonl"
            tpath.parent.mkdir(parents=True)
            tpath.write_text("{}\n", encoding="utf-8")

            now_ts = 1_800_000_000.0
            stale_mtime = now_ts - (3 * 3600 + 18 * 60)
            os.utime(tpath, (stale_mtime, stale_mtime))

            _write_state(
                state_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": "aaaa9999-2222-4333-8444-555555555555",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Waiting on orchestrator",
                    }
                ],
            )

            out_stale, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=False, plain_urls=True),
                brain_dir=brain_dir,
                is_tty_override=True,
                term_width_override=140,
                now_ts_override=now_ts,
                sync_status_badge="sync:12s",
            )
            self.assertIn("444cef60", out_stale)
            self.assertIn("(idle 3h18m)", out_stale)
            self.assertIn("\033[1;33m(idle 3h18m)\033[0m", out_stale)
            self.assertIn("sync:12s", out_stale)

            # Active (<2m) orchestrator shows green `(active ...)`
            os.utime(tpath, (now_ts - 25.0, now_ts - 25.0))
            out_active, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=False, plain_urls=True),
                brain_dir=brain_dir,
                is_tty_override=True,
                term_width_override=140,
                now_ts_override=now_ts,
            )
            self.assertIn("(active 25s)", out_active)
            self.assertIn("\033[0;32m(active 25s)\033[0m", out_active)

            # Transient torn JSON on tick 1 -> recoverable warning frame -> valid state recovered on tick 2
            (state_dir / "dev_queue.json").write_text("{truncated-json", encoding="utf-8")

            def _heal_json() -> None:
                time.sleep(0.05)
                _write_state(
                    state_dir,
                    orchestrator_id=orch_cid,
                    items=[
                        {
                            "conversation_id": "aaaa9999-2222-4333-8444-555555555555",
                            "priority": 10,
                            "status": "pending",
                            "repo_slugs": ["acme/repo-a"],
                            "merged_repos": [],
                            "title": "Healed queue item",
                        }
                    ],
                )

            heal_t = threading.Thread(target=_heal_json, daemon=True)
            heal_t.start()
            buf_io = io.StringIO()
            rc_io = tui.run_watch_loop(
                _make_args(state_dir, watch=0.3, no_auto_sync=True, no_color=True),
                stream=buf_io,
                use_tty_override=True,
                open_tty=False,
                max_iterations=2,
                term_size_override=(120, 22),
            )
            heal_t.join(timeout=1.0)
            self.assertEqual(rc_io, 0)
            io_frames = buf_io.getvalue()
            self.assertIn("Transient read error (JSONDecodeError)", io_frames)
            self.assertIn("Healed queue item", io_frames)

            # Transient OSError and sqlite3.OperationalError also recover cleanly without crashing
            with mock.patch.object(
                tui,
                "render",
                side_effect=[OSError("resource busy"), sqlite3.OperationalError("database is locked")],
            ):
                buf_os_db = io.StringIO()
                rc_os_db = tui.run_watch_loop(
                    _make_args(state_dir, watch=0.05, no_auto_sync=True, no_color=True),
                    stream=buf_os_db,
                    use_tty_override=True,
                    open_tty=False,
                    max_iterations=2,
                    term_size_override=(120, 22),
                )
                self.assertEqual(rc_os_db, 0)
                self.assertIn("Transient read error (OSError)", buf_os_db.getvalue())
                self.assertIn("Transient read error (OperationalError)", buf_os_db.getvalue())

    def test_dual_desktop_and_cli_orchestrator_liveness_and_proposal_fallback(self) -> None:
        """TUI resolves orchestrator liveness and HITL proposal `.md` files across both Desktop and CLI `brain/` dirs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake_home = root / "home"
            desktop_brain = fake_home / ".gemini" / "antigravity" / "brain"
            cli_brain = fake_home / ".gemini" / "antigravity-cli" / "brain"
            state_dir = root / "state"
            board_path = root / "board.md"

            orch_cid = "8888aaaa-2222-4333-8444-555555555555"
            hitl_cid = "9999bbbb-2222-4333-8444-555555555555"
            now_ts = 1_800_400_000.0

            # Write orchestrator transcript ONLY under `antigravity-cli/brain`
            cli_tpath = cli_brain / orch_cid / ".system_generated" / "logs" / "transcript.jsonl"
            cli_tpath.parent.mkdir(parents=True)
            cli_tpath.write_text("{}\n", encoding="utf-8")
            os.utime(cli_tpath, (now_ts - 18.0, now_ts - 18.0))

            # Write HITL proposal artifact ONLY under `antigravity-cli/brain`
            cli_hitl_dir = cli_brain / hitl_cid
            cli_hitl_dir.mkdir(parents=True)
            (cli_hitl_dir / "cli_proposal.md").write_text("# CLI Proposal\n", encoding="utf-8")

            _write_state(
                state_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": hitl_cid,
                        "priority": 10,
                        "status": "hitl_blocked",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "CLI HITL item",
                    }
                ],
            )

            # Direct helper check with home_dir=fake_home and brain_dir=None
            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIR": ""}, clear=False):
                os.environ.pop("AGY_BRAIN_DIR", None)
                badge_txt, badge_col = tui._resolve_orchestrator_liveness(
                    orch_cid,
                    brain_dir=None,
                    counts={"hitl_blocked": 1},
                    now_ts=now_ts,
                    home_dir=fake_home,
                )
                self.assertEqual(badge_txt, "(active 18s)")
                self.assertEqual(badge_col, "0;32")

                # Empty brain_dir stays 100% hermetic
                self.assertEqual(
                    tui._resolve_orchestrator_liveness(
                        orch_cid,
                        brain_dir="",
                        counts={"hitl_blocked": 1},
                        now_ts=now_ts,
                        home_dir=fake_home,
                    ),
                    ("", ""),
                )

            # Render passing `[desktop_brain, cli_brain]` surfaces active CLI badge and `cli_proposal.md`
            out_dual, _, _, _, _, _ = tui.render(
                _make_args(state_dir, write_board=str(board_path), no_color=True, plain_urls=True),
                brain_dir=[desktop_brain, cli_brain],
                is_tty_override=True,
                term_width_override=140,
                now_ts_override=now_ts,
            )
            self.assertIn("(active 18s)", out_dual)
            self.assertIn("[cli_proposal.md](file://", board_path.read_text(encoding="utf-8"))


def _n_slot_lock(n_slots: int, *, max_slots: int | None, cid_prefix: str) -> dict[str, object]:
    """Build a hermetic N-slot repository lock (Slot #1 mirrored at top level, Slots #2..#N awaiting base merge)."""
    slots: dict[str, dict[str, object]] = {}
    for s_id in range(1, n_slots + 1):
        slots[str(s_id)] = {
            "slot_id": s_id,
            "active": True,
            "status": "DEVELOPING" if s_id == 1 else "READY_AWAITING_BASE_MERGE",
            "branch_name": f"fix/20270115-0{s_id}0000-task-s{s_id}",
            "conversation_id": f"{cid_prefix}{s_id}11111-2222-4333-8444-555555555555",
            "target_files": [f"src/module_{s_id}.py"],
            "section_anchors": [f"def handler_{s_id}"],
            "landing_order": s_id,
            "overlap_tier": "disjoint",
            "last_activity_at": "2027-01-15T09:59:00Z",
        }
    lock: dict[str, object] = {
        "active": True,
        "slot_id": 1,
        **{k: v for k, v in slots["1"].items() if k != "slot_id"},
        "active_slots_count": n_slots,
        "slots": slots,
    }
    if max_slots is not None:
        lock["max_slots"] = max_slots
    return lock


def _legacy_two_slot_lock() -> dict[str, object]:
    """Legacy lock shape: flat Slot #1 fields plus only `slots["2"]` and no `max_slots`."""
    return {
        "active": True,
        "status": "DEVELOPING",
        "branch_name": "fix/20270115-010000-legacy-s1",
        "conversation_id": "ee111111-2222-4333-8444-555555555555",
        "last_activity_at": "2027-01-15T09:59:00Z",
        "slots": {
            "2": {
                "active": True,
                "status": "READY_AWAITING_BASE_MERGE",
                "branch_name": "fix/20270115-020000-legacy-s2",
                "conversation_id": "ee222222-2222-4333-8444-555555555555",
                "last_activity_at": "2027-01-15T09:59:00Z",
            }
        },
    }


class TestDevQueueTuiNSlots(unittest.TestCase):
    """N-slot (`1..4`, default 3) occupancy, `S1..S4` labels, and legacy 2-slot lock compatibility."""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("AGY_DEV_MAX_SLOTS_PER_REPO", None)

    def test_resolve_max_repo_slots_clamps_and_defaults_to_three(self) -> None:
        self.assertEqual(tui.DEFAULT_MAX_REPO_SLOTS, 3)
        self.assertEqual(tui.MAX_REPO_SLOTS_CAP, 4)
        self.assertEqual(tui._resolve_max_repo_slots(None), 3)
        self.assertEqual(tui._resolve_max_repo_slots(2), 2)
        self.assertEqual(tui._resolve_max_repo_slots("4"), 4)
        self.assertEqual(tui._resolve_max_repo_slots(9), 4)
        self.assertEqual(tui._resolve_max_repo_slots(0), 3)
        self.assertEqual(tui._resolve_max_repo_slots("bad"), 3)
        with mock.patch.dict(os.environ, {"AGY_DEV_MAX_SLOTS_PER_REPO": "1"}):
            self.assertEqual(tui._resolve_max_repo_slots(None), 1)
        self.assertEqual([tui._slot_label(i) for i in range(1, 5)], ["S1", "S2", "S3", "S4"])

    def test_normalize_three_four_and_legacy_two_slot_locks(self) -> None:
        three = tui._normalize_repo_lock_slots("owner/repo-three", _n_slot_lock(3, max_slots=None, cid_prefix="aa"))
        self.assertEqual(three["max_slots"], 3)
        self.assertEqual(three["active_slots_count"], 3)
        self.assertEqual(three["slot_occupancy"], "3/3")
        self.assertEqual(sorted(three["slots"]), ["1", "2", "3"])
        self.assertEqual([s["slot_id"] for s in three["slots_list"]], [1, 2, 3])

        four = tui._normalize_repo_lock_slots("owner/repo-four", _n_slot_lock(4, max_slots=4, cid_prefix="bb"))
        self.assertEqual(four["slot_occupancy"], "4/4")
        self.assertEqual([s["slot_id"] for s in four["slots_list"]], [1, 2, 3, 4])
        self.assertEqual(tui._lock_slot_occupancy(four), "4/4")

        over = tui._normalize_repo_lock_slots("owner/repo-over", _n_slot_lock(3, max_slots=2, cid_prefix="cc"))
        self.assertEqual(over["slot_occupancy"], "3/3", "capacity never under-reports active slots")

        legacy = tui._normalize_repo_lock_slots("owner/repo-legacy", _legacy_two_slot_lock())
        self.assertEqual([s["slot_id"] for s in legacy["slots_list"]], [1, 2])
        self.assertEqual(legacy["slot_occupancy"], "2/3")
        self.assertEqual(legacy["slots"]["1"]["branch_name"], "fix/20270115-010000-legacy-s1")
        legacy_explicit = tui._normalize_repo_lock_slots("owner/repo-legacy", {**_legacy_two_slot_lock(), "max_slots": 2})
        self.assertEqual(legacy_explicit["slot_occupancy"], "2/2")
        self.assertEqual(tui._lock_slot_occupancy({"active": True, "status": "DEVELOPING"}), "1/3")

    def test_secondary_slot_progress_sorted_across_n_slots(self) -> None:
        cache: dict[str, dict[str, object]] = {
            "owner/repo#slot4": {"pct": 40},
            "owner/repo": {"pct": 10},
            "owner/repo#slot2": {"pct": 20},
            "owner/repo#slot3": {"pct": 30},
            "owner/repo-other#slot2": {"pct": 99},
            "owner/repo#slotX": {"pct": 0},
        }
        pairs = tui._secondary_slot_progress("owner/repo", cache)
        self.assertEqual([p[0] for p in pairs], [2, 3, 4])
        self.assertEqual([p[1]["pct"] for p in pairs], [20, 30, 40])

    def test_render_header_json_and_locks_cli_for_three_four_and_legacy_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_state(
                state_dir,
                items=[],
                locks={
                    "owner/repo-three": _n_slot_lock(3, max_slots=3, cid_prefix="aa"),
                    "owner/repo-four": _n_slot_lock(4, max_slots=4, cid_prefix="bb"),
                    "owner/repo-legacy": _legacy_two_slot_lock(),
                },
            )
            now_ts = tui._flow_opt.parse_iso_timestamp("2027-01-15T10:00:00Z")
            out_txt, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True, no_opt=True),
                brain_dir="",
                is_tty_override=False,
                term_width_override=220,
                now_ts_override=now_ts,
            )
            self.assertIn("[3/3]", out_txt)
            self.assertIn("[4/4]", out_txt)
            self.assertIn("[2/3]", out_txt)
            self.assertIn("·S2:await", out_txt)
            self.assertIn("·S3:await", out_txt)
            self.assertIn("·S4:await", out_txt)
            self.assertNotIn("/2]", out_txt)

            json_txt, _, _, _, _, _ = tui.render(
                _make_args(state_dir, json=True, no_opt=True),
                brain_dir="",
                is_tty_override=False,
                now_ts_override=now_ts,
            )
            payload = json.loads(json_txt)
            four_lk = payload["locks"]["owner/repo-four"]
            self.assertEqual(four_lk["slot_occupancy"], "4/4")
            self.assertEqual(sorted(four_lk["slots_progress"]), ["1", "2", "3", "4"])
            self.assertEqual([s["slot_id"] for s in four_lk["slots_list"]], [1, 2, 3, 4])
            self.assertIn("slot2_progress", four_lk, "legacy slot2_progress key preserved")
            legacy_lk = payload["locks"]["owner/repo-legacy"]
            self.assertEqual(sorted(legacy_lk["slots_progress"]), ["1", "2"])

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = tui._run_subcommand_locks(["--state-dir", str(state_dir), "--no-color"])
            self.assertEqual(rc, 0)
            locks_out = buf.getvalue()
            self.assertIn("owner/repo-four  [4/4]", locks_out)
            self.assertIn("Slot #4", locks_out)
            self.assertIn("owner/repo-legacy  [2/3]", locks_out)


def _pb_varint(val: int) -> bytes:
    """Encode non-negative integer as protobuf unsigned LEB128 varint."""
    out = bytearray()
    n = int(val)
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _pb_field_bytes(field_num: int, payload: bytes) -> bytes:
    """Encode a protobuf wire_type=2 (length-delimited) field."""
    return _pb_varint((field_num << 3) | 2) + _pb_varint(len(payload)) + payload


def _pb_field_varint(field_num: int, val: int) -> bytes:
    """Encode a protobuf wire_type=0 (varint) field."""
    return _pb_varint((field_num << 3) | 0) + _pb_varint(val)


def _build_raw_summary_field8(
    *,
    tool_name: str = "",
    questions_f56: list[str] | None = None,
    questions_f140: list[str] | None = None,
    include_f8: bool = True,
    opaque_f8_only: bool = False,
) -> bytes:
    """Synthesize a `conversation_summaries.raw_summary` blob matching Antigravity field `8` layout."""
    base = _pb_field_bytes(1, b"summary-header") + _pb_field_varint(2, 42)
    if not include_f8:
        return base
    if opaque_f8_only:
        return base + _pb_field_bytes(8, _pb_field_varint(99, 1))

    f1_parts: list[bytes] = []
    if tool_name:
        f4 = _pb_field_bytes(2, tool_name.encode("utf-8"))
        f5 = _pb_field_bytes(4, f4)
        f1_parts.append(_pb_field_bytes(5, f5))
    if questions_f56:
        q_msgs = b"".join(_pb_field_bytes(1, _pb_field_bytes(1, q.encode("utf-8"))) for q in questions_f56)
        f22 = _pb_field_bytes(22, q_msgs)
        f1_parts.append(_pb_field_bytes(56, f22))
    if questions_f140:
        q_raw = b"".join(_pb_field_bytes(1, q.encode("utf-8")) for q in questions_f140)
        f1_parts.append(_pb_field_bytes(140, q_raw))

    f1_payload = b"".join(f1_parts) if f1_parts else _pb_field_varint(9, 1)
    return base + _pb_field_bytes(8, _pb_field_bytes(1, f1_payload))


class TestLiveWaitingUserConversations(unittest.TestCase):
    """Hermetic tests for stdlib protobuf wire decoding and live waiting input surfacing in TUI & board."""

    def test_protobuf_decoder_parses_ask_question_f56_f140_approval_and_corrupt_blobs(self) -> None:
        multi_q = _build_raw_summary_field8(
            tool_name="ask_question",
            questions_f56=["Deploy to staging now?", "Enable canary rollout?"],
        )
        parsed_multi = tui._parse_waiting_step_from_raw_summary(multi_q)
        self.assertIsNotNone(parsed_multi)
        assert parsed_multi is not None
        self.assertEqual(parsed_multi["waiting_kind"], "ask_question")
        self.assertEqual(parsed_multi["waiting_tool"], "ask_question")
        self.assertEqual(parsed_multi["question_count"], 2)
        self.assertEqual(parsed_multi["question_preview"], "Deploy to staging now?")
        self.assertTrue(parsed_multi["has_readable_text"])

        f140_q = _build_raw_summary_field8(
            tool_name="ask_question",
            questions_f140=["Confirm database migration plan?"],
        )
        parsed_f140 = tui._parse_waiting_step_from_raw_summary(f140_q)
        assert parsed_f140 is not None
        self.assertEqual(parsed_f140["waiting_kind"], "ask_question")
        self.assertEqual(parsed_f140["question_count"], 1)
        self.assertEqual(parsed_f140["question_preview"], "Confirm database migration plan?")

        approval = _build_raw_summary_field8(tool_name="run_command")
        parsed_app = tui._parse_waiting_step_from_raw_summary(approval)
        assert parsed_app is not None
        self.assertEqual(parsed_app["waiting_kind"], "approval")
        self.assertEqual(parsed_app["waiting_tool"], "run_command")
        self.assertEqual(parsed_app["question_count"], 0)
        self.assertTrue(parsed_app["has_readable_text"])

        no_f8 = _build_raw_summary_field8(include_f8=False)
        self.assertIsNone(tui._parse_waiting_step_from_raw_summary(no_f8))
        self.assertIsNone(tui._parse_waiting_step_from_raw_summary(None))
        self.assertIsNone(tui._parse_waiting_step_from_raw_summary(b""))
        self.assertIsNone(tui._parse_waiting_step_from_raw_summary(b"\xff\xff\xff\x00"))

    def test_load_waiting_conversations_filters_and_transcript_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agy_dir = root / "agy"
            brain_dir = agy_dir / "brain"
            ann_dir = agy_dir / "annotations"
            db_path = agy_dir / "conversation_summaries.db"
            brain_dir.mkdir(parents=True, exist_ok=True)
            ann_dir.mkdir(parents=True, exist_ok=True)

            cid_ask = "11111111-1111-4111-8111-111111111111"
            cid_app = "22222222-2222-4222-8222-222222222222"
            cid_fb = "33333333-3333-4333-8333-333333333333"
            cid_nof8 = "44444444-4444-4444-8444-444444444444"
            cid_idle = "55555555-5555-4555-8555-555555555555"
            cid_killed = "66666666-6666-4666-8666-666666666666"
            cid_child = "77777777-7777-4777-8777-777777777777"
            cid_arch = "88888888-8888-4888-8888-888888888888"

            # Archived annotation excludes cid_arch, while non-archived annotation keeps cid_ask
            (ann_dir / f"{cid_arch}.pbtxt").write_text("archived: true\n", encoding="utf-8")
            (ann_dir / f"{cid_ask}.pbtxt").write_text("archived: false\npinned: true\n", encoding="utf-8")

            # Transcript fallback for cid_fb (f8 exists without readable text) and cid_nof8 (must NOT be read)
            fb_tp = brain_dir / cid_fb / ".system_generated" / "logs" / "transcript.jsonl"
            fb_tp.parent.mkdir(parents=True, exist_ok=True)
            fb_tp.write_text(
                json.dumps(
                    {
                        "name": "ask_question",
                        "arguments": {"questions": [{"question": "Fallback question from transcript?"}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            nof8_tp = brain_dir / cid_nof8 / ".system_generated" / "logs" / "transcript.jsonl"
            nof8_tp.parent.mkdir(parents=True, exist_ok=True)
            nof8_tp.write_text(
                json.dumps({"name": "ask_question", "arguments": {"questions": [{"question": "Old question?"}]}}) + "\n",
                encoding="utf-8",
            )

            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        status TEXT,
                        title TEXT,
                        parent_conversation_id TEXT,
                        killed INTEGER,
                        last_modified_time TEXT,
                        raw_summary BLOB
                    )
                    """
                )
                rows = [
                    (
                        cid_ask,
                        "CASCADE_RUN_STATUS_RUNNING",
                        f"Refactor {os.sep}Users{os.sep}realperson{os.sep}secret{os.sep}app",
                        "",
                        0,
                        "2026-11-25T10:03:00Z",
                        _build_raw_summary_field8(
                            tool_name="ask_question",
                            questions_f56=[f"Approve schema in {os.sep}Users{os.sep}realperson{os.sep}repo?", "Also update docs?"],
                        ),
                    ),
                    (
                        cid_app,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Build Runner",
                        "",
                        0,
                        "2026-11-25T10:02:00Z",
                        _build_raw_summary_field8(tool_name="run_command"),
                    ),
                    (
                        cid_fb,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Opaque Step Chat",
                        "",
                        0,
                        "2026-11-25T10:01:00Z",
                        _build_raw_summary_field8(tool_name="ask_question", opaque_f8_only=False),
                    ),
                    (
                        cid_nof8,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Active Coding No Prompt",
                        "",
                        0,
                        "2026-11-25T10:04:00Z",
                        _build_raw_summary_field8(include_f8=False),
                    ),
                    (
                        cid_idle,
                        "CASCADE_RUN_STATUS_IDLE",
                        "Idle Chat",
                        "",
                        0,
                        "2026-11-25T10:05:00Z",
                        _build_raw_summary_field8(tool_name="ask_question", questions_f56=["Stale?"]),
                    ),
                    (
                        cid_killed,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Killed Chat",
                        "",
                        1,
                        "2026-11-25T10:05:00Z",
                        _build_raw_summary_field8(tool_name="run_command"),
                    ),
                    (
                        cid_child,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Subagent Chat",
                        cid_ask,
                        0,
                        "2026-11-25T10:05:00Z",
                        _build_raw_summary_field8(tool_name="run_command"),
                    ),
                    (
                        cid_arch,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Archived Chat",
                        "",
                        0,
                        "2026-11-25T10:05:00Z",
                        _build_raw_summary_field8(tool_name="run_command"),
                    ),
                ]
                conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                conn.commit()

            waiting = tui.load_waiting_user_conversations(db_path=db_path, brain_dir=brain_dir)
            self.assertEqual([w["conversation_id"] for w in waiting], [cid_ask, cid_app, cid_fb])
            self.assertNotIn("realperson", waiting[0]["sidebar_title"])
            self.assertNotIn("realperson", waiting[0]["question_preview"])
            self.assertIn("$HOME/repo", waiting[0]["question_preview"])
            self.assertEqual(waiting[0]["question_count"], 2)
            self.assertEqual(waiting[1]["waiting_kind"], "approval")
            self.assertEqual(waiting[1]["waiting_tool"], "run_command")
            self.assertEqual(waiting[2]["waiting_kind"], "ask_question")
            self.assertEqual(waiting[2]["question_preview"], "Fallback question from transcript?")

            # Minimal backwards-compatible DB without status/raw_summary returns [] cleanly
            min_db = root / "minimal.db"
            with sqlite3.connect(str(min_db)) as mconn:
                mconn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
                mconn.execute("INSERT INTO conversation_summaries VALUES (?, ?)", (cid_ask, "Minimal"))
                mconn.commit()
            self.assertEqual(tui.load_waiting_user_conversations(db_path=min_db, brain_dir=brain_dir), [])

    def test_tui_render_board_and_open_hitl_surface_live_waiting_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "agy" / "brain"
            db_path = root / "agy" / "conversation_summaries.db"
            board_path = root / "dev_queue_board.md"
            brain_dir.mkdir(parents=True, exist_ok=True)

            live_cid = "aaaaaaaa-9999-4111-8111-111111111111"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        status TEXT,
                        title TEXT,
                        parent_conversation_id TEXT,
                        killed INTEGER,
                        last_modified_time TEXT,
                        raw_summary BLOB
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        live_cid,
                        "CASCADE_RUN_STATUS_RUNNING",
                        "Live Question Chat",
                        "",
                        0,
                        "2026-11-25T10:10:00Z",
                        _build_raw_summary_field8(
                            tool_name="ask_question",
                            questions_f56=["Proceed with deployment?", "Notify Slack?"],
                        ),
                    ),
                )
                conn.commit()

            # Empty dev_queue.json still surfaces live waiting prompt in TUI, board, and --open-hitl h1
            _write_state(state_dir, items=[], locks={})
            out, _, hitl_convs, _, _, _ = tui.render(
                _make_args(state_dir, write_board=str(board_path), no_color=True, plain_urls=True),
                conv_db_path=db_path,
                brain_dir=brain_dir,
                is_tty_override=True,
                term_width_override=140,
            )
            self.assertIn("hitl:1", out)
            self.assertIn("HITL — ANTIGRAVITY APP VALIDATION", out)
            self.assertIn("QUESTION (2): Proceed with deployment? (+1 more)", out)
            self.assertIn(1, hitl_convs)
            self.assertEqual(hitl_convs[1][1], live_cid)
            self.assertEqual(hitl_convs[1][2], "Live Question Chat")

            board_md = board_path.read_text(encoding="utf-8")
            self.assertIn("`HITL: 1`", board_md)
            self.assertIn(f"[Live Question Chat](conversation://{live_cid})", board_md)
            self.assertIn("QUESTION (2): Proceed with deployment? (+1 more)", board_md)

            with (
                mock.patch.dict(os.environ, {"AGY_CONVERSATION_SUMMARIES_DB": str(db_path), "AGY_NAV_INTENT_ACK_WAIT_SECONDS": "0"}),
                mock.patch.object(tui, "_focus_antigravity_app", return_value=True),
                mock.patch.object(tui, "_copy_to_clipboard", return_value=True),
            ):
                rc = tui.main(["--state-dir", str(state_dir), "--open-hitl", "h1"])
            self.assertEqual(rc, 0)
            nav_data = json.loads((state_dir / tui.NAV_INTENT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(nav_data["conversation_id"], live_cid)
            self.assertEqual(nav_data["hotkey"], "h1")
            self.assertEqual(nav_data["sidebar_title"], "Live Question Chat")


if __name__ == "__main__":
    unittest.main()
