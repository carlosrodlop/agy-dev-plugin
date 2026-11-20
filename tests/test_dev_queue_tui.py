"""Hermetic Zero-PII unit tests for `dev_queue_tui.py`, live HITL board sync, and `agy-queue` CLI."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
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
        """PR 1/2 renders full bright-yellow bar (`PR 1/2 listo`), PR 2/2 and 1/1 render bright-green, never 45% cyan."""
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
        self.assertIn("PR 1/2 listo", rendered_part)
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
        self.assertIn("PR 2/2 listo", rendered_final)
        self.assertIn("\033[1;32m", rendered_final)

        # Single-repo PR ready -> bright green "PR listo"
        item_single = {**item_part, "repo_slugs": ["acme/repo-a"]}
        prog_single = tui._compute_item_progress(item_single, locks_by_cid, {}, is_human_pr=True)
        self.assertIsNotNone(prog_single)
        assert prog_single is not None
        self.assertEqual(prog_single["mode"], "pr_final")
        rendered_single = tui._render_inline_progress(prog_single, 20, color_on=True)
        self.assertIn("PR listo", rendered_single)
        self.assertIn("\033[1;32m", rendered_single)

        # Active IN_PROGRESS development keeps numeric percentage + ETA
        item_dev = {
            "conversation_id": "bbbb1111-2222-4333-8444-555555555555",
            "priority": 20,
            "status": "in_progress",
            "repo_slugs": ["acme/repo-a"],
            "merged_repos": [],
            "title": "Active coding task",
        }
        lock_dev = {
            "active": True,
            "status": "DEVELOPING",
            "pr_gate_ready": False,
            "progress_pct": 35,
            "eta_minutes": 6,
            "conversation_id": item_dev["conversation_id"],
        }
        prog_dev = tui._compute_item_progress(
            item_dev,
            {item_dev["conversation_id"]: [("acme/repo-a", lock_dev)]},
            {},
            is_human_pr=False,
        )
        self.assertIsNotNone(prog_dev)
        assert prog_dev is not None
        self.assertEqual(prog_dev["mode"], "dev")
        rendered_dev = tui._render_inline_progress(prog_dev, 20, color_on=True)
        self.assertIn("35%", rendered_dev)
        self.assertIn("~6m", rendered_dev)
        self.assertNotIn("PR listo", rendered_dev)

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
            self.assertIn("PR 1/2 listo", out)
            self.assertIn("[1] repo-a (cccc1111 · parte 1/2 · desbloquea repo-b) [+verificaciones]:", out)
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

            # OSC-8 hyperlink preserves FULL URL while showing compact display URL
            self.assertIn(f"\033]8;;{full_url}\007{short_display}\033]8;;\007", out)
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
            self.assertIn(f"[Abrir PR]({escaped_expected})", board_md)
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
            self.assertNotIn("PR listo", out)
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
            self.assertIn(updated_item["status"], ("pr_open", "in_progress"))

            # Verify --fetch flag is passed to queue-sync ONLY when offline=False
            with mock.patch.object(tui.subprocess, "run") as mock_run:
                mock_run.return_value = mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"status": "ok", "reconciled_count": 2, "reconciled_items": [{}, {}]}),
                    stderr="",
                )
                msg_online = tui._run_live_sync(state_dir, {"acme/repo-a": repo_a}, offline=False)
                self.assertIn("Sync OK (1 repos fetched", msg_online)
                queue_sync_calls = [c.args[0] for c in mock_run.call_args_list if "queue-sync" in c.args[0]]
                self.assertEqual(len(queue_sync_calls), 1)
                self.assertIn("--fetch", queue_sync_calls[0])

                mock_run.reset_mock()
                msg_off = tui._run_live_sync(state_dir, {"acme/repo-a": repo_a}, offline=True)
                self.assertIn("Offline sync OK (0 repos fetched", msg_off)
                queue_sync_off = [c.args[0] for c in mock_run.call_args_list if "queue-sync" in c.args[0]]
                self.assertEqual(len(queue_sync_off), 1)
                self.assertNotIn("--fetch", queue_sync_off[0])

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
            self.assertTrue(any(f"[Abrir PR]({escaped})" in ln for ln in table_rows))

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


if __name__ == "__main__":
    unittest.main()
