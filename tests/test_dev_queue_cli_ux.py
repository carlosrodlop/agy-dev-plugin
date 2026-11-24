"""Hermetic Zero-PII tests for `agy-queue` CLI/TUI UX, live idle/stall semaphore, unified subcommands & <1ms TUI repaints."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
REM_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
TEST_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"

for _p in (CLIENTS_DIR, REM_SCRIPTS_DIR, TUI_SCRIPTS_DIR, TEST_SCRIPTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_common  # noqa: E402
import dev_flow_optimizer as flow_opt  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import git_client  # noqa: E402
import pr_remediator as prs  # noqa: E402
import selective_test_runner as srunner  # noqa: E402


def _make_args(state_dir: Path, **overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "watch": None,
        "all": False,
        "repo": "",
        "status": "",
        "sync": False,
        "sync_interval": 30.0,
        "no_auto_sync": True,
        "write_board": "",
        "open_hitl": "",
        "state_dir": str(state_dir),
        "repos_dir": "",
        "plain_urls": False,
        "no_opt": False,
        "json": False,
        "no_color": False,
        "debug": False,
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
                "updated_at": "2026-11-23T08:00:00Z",
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


def _write_transcript_mtime(brain_dir: Path, cid: str, mtime: float) -> Path:
    tpath = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text('{"step_index": 1}\n', encoding="utf-8")
    os.utime(tpath, (mtime, mtime))
    return tpath


class TestIdleStallSemaphoreAndReviewerTracking(unittest.TestCase):
    """Verify live 3-state inactivity semaphore, AWAITING_AGENT_REVIEW + reviewer_subagent_id, and source/elapsed telemetry."""

    def test_awaiting_agent_review_and_activity_sources_in_detect_stalled_lock(self) -> None:
        self.assertIn("AWAITING_AGENT_REVIEW", flow_opt.STALL_ELIGIBLE_LOCK_STATUSES)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain_dir = root / "brain"
            wt_dir = root / "wt"
            wt_dir.mkdir(parents=True)

            now_iso = "2026-11-23T10:00:00Z"
            acq_iso = "2026-11-23T09:30:00Z"  # 30m elapsed (1800s)
            now_ts = flow_opt.parse_iso_timestamp(now_iso)
            acq_ts = flow_opt.parse_iso_timestamp(acq_iso)

            worker_cid = "aaaa1111-1111-4111-8111-111111111111"
            reviewer_cid = "bbbb2222-2222-4222-8222-222222222222"

            # 1. Stale AWAITING_AGENT_REVIEW lock (30m idle) is detected as stalled with lock_meta source
            lock_rev = {
                "active": True,
                "status": "AWAITING_AGENT_REVIEW",
                "pr_gate_ready": False,
                "acquired_at": acq_iso,
                "updated_at": acq_iso,
                "last_activity_at": acq_iso,
                "worktree_path": str(wt_dir),
                "worker_cid": worker_cid,
                "reviewer_subagent_id": reviewer_cid,
            }
            det_stalled = flow_opt.detect_stalled_lock(lock_rev, now_ts=now_ts, brain_dir=brain_dir)
            self.assertTrue(det_stalled["stalled"])
            self.assertEqual(det_stalled["idle_seconds"], 1800)
            self.assertEqual(det_stalled["elapsed_seconds"], 1800)
            self.assertEqual(det_stalled["elapsed_label"], "30m")
            self.assertEqual(det_stalled["last_activity_source"], "lock_meta")
            self.assertEqual(det_stalled["reviewer_subagent_id"], reviewer_cid)

            # 2. Heartbeat update -> source == 'heartbeat'
            hb_iso = "2026-11-23T09:50:00Z"  # 10m ago
            det_hb = flow_opt.detect_stalled_lock(
                {**lock_rev, "last_activity_at": hb_iso, "stage_label": "reviewing"},
                now_ts=now_ts,
                brain_dir=brain_dir,
            )
            self.assertFalse(det_hb["stalled"])
            self.assertEqual(det_hb["idle_seconds"], 600)
            self.assertEqual(det_hb["idle_label"], "10m")
            self.assertEqual(det_hb["elapsed_seconds"], 1800)
            self.assertEqual(det_hb["last_activity_source"], "heartbeat")

            # 3. Newer worktree file edit -> source == 'worktree'
            wt_file = wt_dir / "mod.py"
            wt_file.write_text("X = 1\n", encoding="utf-8")
            os.utime(wt_file, (now_ts - 300.0, now_ts - 300.0))  # 5m ago
            det_wt = flow_opt.detect_stalled_lock(
                {**lock_rev, "last_activity_at": hb_iso},
                now_ts=now_ts,
                brain_dir=brain_dir,
            )
            self.assertEqual(det_wt["idle_seconds"], 300)
            self.assertEqual(det_wt["last_activity_source"], "worktree")

            # 4. Newer worker transcript -> source == 'transcript'
            _write_transcript_mtime(brain_dir, worker_cid, acq_ts + 1680.0)  # 2m ago
            det_worker = flow_opt.detect_stalled_lock(
                {**lock_rev, "last_activity_at": hb_iso},
                now_ts=now_ts,
                brain_dir=brain_dir,
            )
            self.assertFalse(det_worker["stalled"])
            self.assertEqual(det_worker["idle_seconds"], 120)
            self.assertEqual(det_worker["last_activity_source"], "transcript")

            # 5. Newer reviewer_subagent_id transcript -> source == 'reviewer_transcript' & clears stall
            _write_transcript_mtime(brain_dir, reviewer_cid, now_ts - 35.0)  # 35s ago
            det_rev_ok = flow_opt.detect_stalled_lock(
                {**lock_rev, "last_activity_at": acq_iso},
                now_ts=now_ts,
                brain_dir=brain_dir,
            )
            self.assertFalse(det_rev_ok["stalled"])
            self.assertEqual(det_rev_ok["idle_seconds"], 35)
            self.assertEqual(det_rev_ok["idle_label"], "35s")
            self.assertEqual(det_rev_ok["last_activity_source"], "reviewer_transcript")

    def test_three_state_semaphore_in_progress_and_repos_pill_and_json_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"

            now_iso = "2026-11-23T10:00:00Z"
            now_ts = flow_opt.parse_iso_timestamp(now_iso)

            cid_ok = "1111aaaa-0000-4000-8000-000000000001"
            cid_amber = "2222bbbb-0000-4000-8000-000000000002"
            cid_stall = "3333cccc-0000-4000-8000-000000000003"
            rev_sub_id = "9999dddd-0000-4000-8000-000000000009"
            _write_transcript_mtime(brain_dir, rev_sub_id, now_ts - 360.0)  # 6m idle (amber!)

            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_ok,
                        "priority": 10,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-ok"],
                        "merged_repos": [],
                        "title": "Fresh active worker",
                    },
                    {
                        "conversation_id": cid_amber,
                        "priority": 12,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-amber"],
                        "merged_repos": [],
                        "title": "Quiet reviewer at 6m",
                    },
                    {
                        "conversation_id": cid_stall,
                        "priority": 14,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-stall"],
                        "merged_repos": [],
                        "title": "Stalled reviewer at 20m",
                    },
                ],
                locks={
                    "acme/repo-ok": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": cid_ok,
                        "worker_cid": "77770001-0000-4000-8000-000000000001",
                        "stage_label": "testing",
                        "acquired_at": "2026-11-23T09:55:00Z",
                        "last_activity_at": "2026-11-23T09:59:15Z",  # 45s idle (<3m -> ok·45s)
                    },
                    "acme/repo-amber": {
                        "active": True,
                        "status": "AWAITING_AGENT_REVIEW",
                        "pr_gate_ready": False,
                        "conversation_id": cid_amber,
                        "reviewer_subagent_id": rev_sub_id,
                        "stage_label": "pr-review",
                        "acquired_at": "2026-11-23T09:42:00Z",
                        "last_activity_at": "2026-11-23T09:42:00Z",
                    },
                    "acme/repo-stall": {
                        "active": True,
                        "status": "AWAITING_AGENT_REVIEW",
                        "pr_gate_ready": False,
                        "conversation_id": cid_stall,
                        "acquired_at": "2026-11-23T09:40:00Z",
                        "last_activity_at": "2026-11-23T09:40:00Z",  # 20m idle (>=15m -> STALL 20m)
                    },
                },
            )

            out_color, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=False, plain_urls=True),
                brain_dir=brain_dir,
                is_tty_override=True,
                term_width_override=140,
                now_ts_override=now_ts,
            )
            plain = tui.ANSI_RE.sub("", out_color)

            # <3m -> ok·45s green; 3-14m -> idle 6m amber in BOTH row and REPOS pill; >=15m -> STALL 20m + banner
            self.assertIn("ok·45s", plain)
            self.assertIn("\033[0;32mok·45s\033[0m", out_color)
            self.assertIn("idle 6m", plain)
            self.assertIn("\033[1;33midle 6m\033[0m", out_color)
            self.assertIn("(rev · idle 6m)", plain)
            self.assertIn("\033[1;33m · idle 6m\033[0m", out_color)
            self.assertIn("STALL 20m", plain)
            self.assertIn("⚠ STALLED WORKER: repo-stall idle 20m", plain)

            # Zero hardcoded static fake ETAs anywhere in rendered output
            for fake_eta in ("~3m", "~5m", "~6m", "~10m"):
                self.assertNotIn(fake_eta, plain)

            # JSON output exposes full telemetry fields on both locks and items
            raw_json, _, _, _, _, _ = tui.render(
                _make_args(state_dir, json=True),
                brain_dir=brain_dir,
                now_ts_override=now_ts,
            )
            payload = json.loads(raw_json)
            self.assertEqual(payload["schema_version"], 1)
            lk_amber = payload["locks"]["acme/repo-amber"]
            self.assertEqual(lk_amber["idle_seconds"], 360)
            self.assertEqual(lk_amber["idle_label"], "6m")
            self.assertEqual(lk_amber["elapsed_seconds"], 1080)
            self.assertEqual(lk_amber["elapsed_label"], "18m")
            self.assertEqual(lk_amber["last_activity_source"], "reviewer_transcript")
            self.assertEqual(lk_amber["stage_label"], "pr-review")
            self.assertEqual(lk_amber["reviewer_subagent_id"], rev_sub_id)
            self.assertIn("acme/repo-stall", payload["stalled_locks"])

            items_by_cid = {it["conversation_id"]: it for it in payload["items"]}
            self.assertEqual(items_by_cid[cid_amber]["last_activity_source"], "reviewer_transcript")
            self.assertEqual(items_by_cid[cid_ok]["stage_label"], "testing")
            self.assertEqual(items_by_cid[cid_ok]["worker_cid"], "77770001-0000-4000-8000-000000000001")


class TestTerminalFormattingCliHygieneAndSubcommands(unittest.TestCase):
    """Verify 123-col non-truncation, English-only strings, partial-WIP 1/2 merged, unicodedata, NO_COLOR/TERM=dumb, SIGPIPE, NDJSON & subcommands."""

    def test_123_col_compact_osc8_english_strings_partial_wip_and_responsive_220_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            pr_cid = "00001111-1111-4222-8333-444444444444"
            partial_cid = "00002222-1111-4222-8333-555555555555"
            long_branch = "fix/20261123-000000-mock-queue-cli-tui-ux-long-branch-00001111"
            full_url = (
                f"https://github.com/acme/enterprise-platform-service/compare/main...{long_branch}"
                "?expand=1&title=feat%28queue%29%3A+ux&body=Automated+Verification+PASS+99%2F100"
            )
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": pr_cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/enterprise-platform-service", "acme/companion-gateway"],
                        "merged_repos": [],
                        "pr_urls": [full_url],
                        "title": "Cross-repo UX overhaul",
                    },
                    {
                        "conversation_id": partial_cid,
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/enterprise-platform-service", "acme/companion-gateway"],
                        "merged_repos": ["acme/enterprise-platform-service"],
                        "title": "Partially merged follow-up",
                    },
                ],
                locks={
                    "acme/enterprise-platform-service": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": pr_cid,
                        "branch_name": long_branch,
                        "manual_pr_url": full_url,
                    }
                },
            )

            with mock.patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}, clear=False):
                out_123, _, _, _, _, _ = tui.render(
                    _make_args(state_dir, no_color=False, plain_urls=False),
                    is_tty_override=True,
                    term_width_override=123,
                )
            # Every rendered line on a 123-col terminal fits within <=123 visible cells
            for ln in out_123.splitlines():
                self.assertLessEqual(
                    tui._visible_len(ln),
                    123,
                    msg=f"Line overflowed 123 columns ({tui._visible_len(ln)}): {tui.ANSI_RE.sub('', ln)}",
                )
            # Full unstripped URL preserved in OSC-8 target
            self.assertIn(f"\033]8;;{full_url}\007", out_123)
            plain_123 = tui.ANSI_RE.sub("", out_123)
            self.assertIn("PR 1/2 ready", plain_123)
            self.assertIn("part 1/2 · unlocks companion-gateway", plain_123)
            self.assertIn("[+verified]", plain_123)
            # Partially-merged PENDING row shows dim '1/2 merged (50%)'
            self.assertIn("1/2 merged (50%)", plain_123)
            self.assertIn("\033[2m1/2 merged (50%)\033[0m", out_123)

            # Zero Spanish strings
            for es_word in ("listo", "verificaciones", "desbloquea", "Abrir PR", "Abrir chat"):
                self.assertNotIn(es_word, plain_123)

            # Responsive bar_w scales past 140 up to 220 cols
            out_200, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=200,
            )
            self.assertEqual(tui._visible_len(out_200.splitlines()[0]), 200)
            out_260, _, _, _, _, _ = tui.render(
                _make_args(state_dir, no_color=True, plain_urls=True),
                is_tty_override=False,
                term_width_override=260,
            )
            self.assertEqual(tui._visible_len(out_260.splitlines()[0]), 220)

    def test_unicodedata_east_asian_and_combining_width_and_truncation(self) -> None:
        # Combining acute accent adds 0 cells: 'e\u0301' == 1 cell
        self.assertEqual(tui._visible_len("cafe\u0301"), 4)
        # Wide CJK characters take 2 cells each: 3 chars == 6 cells
        self.assertEqual(tui._visible_len("日本語"), 6)
        # Wide emoji takes 2 cells
        self.assertEqual(tui._visible_len("🚀ok"), 4)

        trunc_cjk = tui._trunc("日本語テスト", 7)
        self.assertLessEqual(tui._visible_len(trunc_cjk), 7)
        self.assertTrue(trunc_cjk.endswith("…"))
        self.assertEqual(trunc_cjk, "日本語…")  # 6 + 1 == 7 cells

        trunc_comb = tui._trunc("a\u0301b\u0301c\u0301d\u0301", 3)
        self.assertEqual(tui._visible_len(trunc_comb), 3)
        self.assertEqual(trunc_comb, "a\u0301b\u0301…")

    def test_no_color_term_dumb_version_status_choices_stderr_sync_ndjson_and_sigpipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "aaaa1111-2222-4333-8444-555555555555",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "title": "Sample item",
                    }
                ],
            )

            # 1. NO_COLOR=1 suppresses ANSI even on TTY
            with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm-256color"}, clear=False):
                out_nc, _, _, _, _, _ = tui.render(_make_args(state_dir), is_tty_override=True)
                self.assertNotIn("\033[", out_nc)

            # 2. TERM=dumb suppresses ANSI and OSC-8 even when stdout reports isatty()=True
            with (
                mock.patch.dict(os.environ, {"NO_COLOR": "", "TERM": "dumb"}, clear=False),
                mock.patch.object(tui.sys.stdout, "isatty", return_value=True),
            ):
                out_dumb, _, _, _, _, _ = tui.render(_make_args(state_dir))
                self.assertNotIn("\033[", out_dumb)
                self.assertNotIn("\033]8;;", out_dumb)

            # 3. --version prints version and exits 0; invalid --status exits 2
            ver_buf = io.StringIO()
            with self.assertRaises(SystemExit) as ver_exit, contextlib.redirect_stdout(ver_buf):
                tui.main(["--version"])
            self.assertEqual(ver_exit.exception.code, 0)
            self.assertIn("agy-queue", ver_buf.getvalue())

            with self.assertRaises(SystemExit) as bad_st, contextlib.redirect_stderr(io.StringIO()):
                tui.main(["--status", "not_a_real_status"])
            self.assertEqual(bad_st.exception.code, 2)

            # 4. --sync routes sync message to stderr so stdout stays pure JSON / table
            out_sync = io.StringIO()
            err_sync = io.StringIO()
            with (
                mock.patch.object(tui, "_run_live_sync", return_value="↻ Offline sync OK (0 repos fetched, 0 PRs reconciled)"),
                contextlib.redirect_stdout(out_sync),
                contextlib.redirect_stderr(err_sync),
            ):
                rc_sync = tui.main(["--state-dir", str(state_dir), "--sync", "--json", "--debug"])
            self.assertEqual(rc_sync, 0)
            parsed_sync_json = json.loads(out_sync.getvalue())
            self.assertEqual(parsed_sync_json["schema_version"], 1)
            self.assertIn("Offline sync OK", err_sync.getvalue())
            self.assertIn("[agy-queue:debug", err_sync.getvalue())

            # 5. --watch --json emits NDJSON lines with schema_version and incrementing seq
            ndjson_buf = io.StringIO()
            rc_nd = tui.run_watch_loop(
                _make_args(state_dir, watch=0.05, json=True),
                stream=ndjson_buf,
                max_iterations=2,
            )
            self.assertEqual(rc_nd, 0)
            nd_lines = [ln for ln in ndjson_buf.getvalue().splitlines() if ln.strip()]
            self.assertEqual(len(nd_lines), 2)
            rec1, rec2 = json.loads(nd_lines[0]), json.loads(nd_lines[1])
            self.assertEqual(rec1["schema_version"], 1)
            self.assertEqual(rec1["seq"], 1)
            self.assertEqual(rec2["seq"], 2)
            self.assertIn("emitted_at", rec1)

            # 6. SIGPIPE / BrokenPipeError handled cleanly in both dev_queue_tui and pr_remediator
            with (
                mock.patch.object(tui, "_main_dashboard", side_effect=BrokenPipeError()),
                mock.patch.object(tui, "_silence_broken_pipe_stdout", return_value=0) as spy_tui_pipe,
            ):
                self.assertEqual(tui.main(["--state-dir", str(state_dir)]), 0)
                spy_tui_pipe.assert_called_once()

            with (
                mock.patch.object(prs, "_main_impl", side_effect=BrokenPipeError()),
                mock.patch.object(prs, "_silence_broken_pipe_stdout", return_value=0) as spy_prs_pipe,
            ):
                self.assertEqual(prs.main(["queue-list"]), 0)
                spy_prs_pipe.assert_called_once()

    def test_unified_subcommands_and_deploy_home_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            cid = "c0c153ec-9999-4111-8222-333333333333"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid,
                        "priority": 16,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["clients/dev_common.py"],
                        "rca_summary": "Root cause details",
                        "proposed_fix": "Clean modular fix",
                        "title": "Unified subcommands item",
                    }
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": cid,
                        "worker_cid": "88881111-2222-4333-8444-555555555555",
                        "stage_label": "coding",
                        "branch_name": "fix/20261123-subcmd-c0c153ec",
                        "acquired_at": "2026-11-23T08:00:00Z",
                        "last_activity_at": "2026-11-23T08:02:00Z",
                    }
                },
            )

            # `list --status in_progress --json`
            buf_list = io.StringIO()
            with contextlib.redirect_stdout(buf_list):
                self.assertEqual(
                    dev_common.run_dev_queue_tui_cli(
                        ["list", "--state-dir", str(state_dir), "--status", "in_progress", "--json"]
                    ),
                    0,
                )
            self.assertEqual(len(json.loads(buf_list.getvalue())["items"]), 1)

            # `show <short_id>` (JSON + text inspector)
            buf_show_json = io.StringIO()
            with contextlib.redirect_stdout(buf_show_json):
                self.assertEqual(
                    tui.main(["show", "c0c153ec", "--state-dir", str(state_dir), "--json"]),
                    0,
                )
            shown = json.loads(buf_show_json.getvalue())
            self.assertEqual(shown["item"]["conversation_id"], cid)
            self.assertEqual(shown["stage_label"], "coding")
            self.assertEqual(shown["worker_cid"], "88881111-2222-4333-8444-555555555555")

            buf_show_txt = io.StringIO()
            with contextlib.redirect_stdout(buf_show_txt):
                self.assertEqual(
                    tui.main(["show", "c0c153ec", "--state-dir", str(state_dir), "--no-color"]),
                    0,
                )
            self.assertIn("ITEM DETAIL INSPECTOR", buf_show_txt.getvalue())
            self.assertIn("Root cause details", buf_show_txt.getvalue())

            # `locks --json` and `locks --no-color`
            buf_locks = io.StringIO()
            with contextlib.redirect_stdout(buf_locks):
                self.assertEqual(tui.main(["locks", "--state-dir", str(state_dir), "--json"]), 0)
            locks_data = json.loads(buf_locks.getvalue())
            self.assertEqual(locks_data["active_count"], 1)
            self.assertIn("acme/repo-a", locks_data["locks"])

            buf_locks_txt = io.StringIO()
            with contextlib.redirect_stdout(buf_locks_txt):
                self.assertEqual(tui.main(["locks", "--state-dir", str(state_dir), "--no-color"]), 0)
            self.assertIn("ACTIVE REPOSITORY LOCKS", buf_locks_txt.getvalue())

            # `optimize --json` and `optimize --no-color`
            buf_opt = io.StringIO()
            with contextlib.redirect_stdout(buf_opt):
                self.assertEqual(tui.main(["optimize", "--state-dir", str(state_dir), "--json"]), 0)
            self.assertIn("health_score", json.loads(buf_opt.getvalue()))

            buf_opt_txt = io.StringIO()
            with contextlib.redirect_stdout(buf_opt_txt):
                self.assertEqual(tui.main(["optimize", "--state-dir", str(state_dir), "--no-color"]), 0)
            self.assertIn("DEV FLOW OPTIMIZER", buf_opt_txt.getvalue())

            # `sync --offline --json`
            buf_sync = io.StringIO()
            with mock.patch.object(tui, "_run_live_sync", return_value="↻ Offline sync OK"), contextlib.redirect_stdout(buf_sync):
                self.assertEqual(tui.main(["sync", "--state-dir", str(state_dir), "--offline", "--json"]), 0)
            self.assertEqual(json.loads(buf_sync.getvalue())["status"], "ok")

            # `set-status <prefix> standby --notes ...` runs in-process without spawning pr_remediator.py
            buf_set = io.StringIO()
            with mock.patch.object(tui.subprocess, "run") as spy_sub, contextlib.redirect_stdout(buf_set):
                self.assertEqual(
                    tui.main(["set-status", "c0c153ec", "standby", "--notes", "Deferred for UX test", "--state-dir", str(state_dir)]),
                    0,
                )
            self.assertEqual(spy_sub.call_count, 0)
            q_updated = json.loads((state_dir / "dev_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(q_updated["items"][0]["status"], "standby")

            # `git_client.deploy_repo_to_home()` syncs ~/.local/bin/agy-queue and ~/.local/bin/agy-doctor symlinks
            src_repo = (root / "plugin-repo").resolve()
            fake_home = (root / "fake-home").resolve()
            src_repo.mkdir()
            fake_home.mkdir()
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Bot",
                "GIT_AUTHOR_EMAIL": "bot@acme.example",
                "GIT_COMMITTER_NAME": "Bot",
                "GIT_COMMITTER_EMAIL": "bot@acme.example",
            }
            subprocess.run(["git", "-C", str(src_repo), "init", "-b", "main"], env=git_env, check=True, capture_output=True)
            (src_repo / "plugin.json").write_text('{"name": "agy-dev-plugin", "version": "1.0.0"}\n', encoding="utf-8")
            (src_repo / "shell").mkdir()
            (src_repo / "shell" / "agy_dev_queue.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(src_repo), "add", "."], env=git_env, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(src_repo), "commit", "-m", "init"], env=git_env, check=True, capture_output=True)

            vcs = git_client.GitVcsIntegrationClient()
            dep = vcs.deploy_repo_to_home(repo_dir=src_repo, home_dir=fake_home)
            self.assertEqual(dep["status"], "ok")
            self.assertEqual(dep["synced_symlinks"], [".local/bin/agy-queue", ".local/bin/agy-doctor"])
            expected_target = str(fake_home / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "shell" / "agy_dev_queue.sh")
            self.assertEqual(os.readlink(fake_home / ".local" / "bin" / "agy-queue"), expected_target)
            self.assertEqual(os.readlink(fake_home / ".local" / "bin" / "agy-doctor"), expected_target)


class TestPureViewportRepaintAndInteractiveOverlays(unittest.TestCase):
    """Verify `<1ms` zero-I/O viewport repaints, `[Enter]`/`[/]`/`[x]`/`[?]` overlays, footer badges, and conditional `[h+#]`."""

    def test_navigation_filter_and_overlays_repaint_without_reinvoking_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            items = [
                {
                    "conversation_id": f"aaaa{i:04d}-1111-4222-8333-444444444444",
                    "priority": 10 + i,
                    "status": "in_progress" if i == 1 else "pending",
                    "repo_slugs": ["acme/alpha-service" if i % 2 else "acme/beta-gateway"],
                    "merged_repos": [],
                    "rca_summary": f"RCA for task {i}",
                    "title": f"Task number {i:02d} {'specialneedle' if i == 3 else 'routine'}",
                }
                for i in range(1, 10)
            ]
            _write_state(
                state_dir,
                items=items,
                locks={
                    "acme/alpha-service": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": items[0]["conversation_id"],
                        "worker_cid": "77778888-1111-4222-8333-444444444444",
                        "stage_label": "coding",
                        "acquired_at": "2026-11-23T09:00:00Z",
                        "last_activity_at": "2026-11-23T09:04:00Z",
                    }
                },
            )

            # Sequence of pure UI keys:
            # j, PgDn (\x1b[6~), PgUp (\x1b[5~), G, g, Enter (detail), Esc, x (optimizer), Esc, ? (help), Esc,
            # p (compact), /specialneedle\r (live filter), Ctrl+L (\x0c), q
            key_bytes = b"j\x1b[6~\x1b[5~Gg\r\x1bx\x1b?\x1bp/specialneedle\r\x0cq"
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, key_bytes)
                os.close(w_fd)
                w_fd = -1
                buf = io.StringIO()
                with mock.patch.object(tui, "render", wraps=tui.render) as spy_render:
                    rc = tui.run_watch_loop(
                        _make_args(state_dir, watch=10.0, no_color=True, plain_urls=True, no_auto_sync=True),
                        stream=buf,
                        use_tty_override=True,
                        open_tty=False,
                        tty_fd_override=r_fd,
                        max_iterations=40,
                        term_size_override=(130, 24),
                    )
                self.assertEqual(rc, 0)
                # Snapshot `render()` ran ONLY ONCE despite ~28 interactive viewport/overlay/filter repaints!
                self.assertEqual(spy_render.call_count, 1)

                frames = buf.getvalue()
                self.assertIn("ITEM DETAIL INSPECTOR", frames)
                self.assertIn("worker_cid=77778888-1111-4222-8333-444444444444", frames)
                self.assertIn("DEV FLOW OPTIMIZER", frames)
                self.assertIn("AGY-QUEUE INTERACTIVE HELP & SEMAPHORE LEGEND", frames)
                self.assertIn("[compact]", frames)
                self.assertIn("[filter:specialneedle]", frames)
                # Zero HITL items -> [h+#] Agy must be hidden from main footer!
                self.assertNotIn("[h+#] Agy", frames)
            finally:
                os.close(r_fd)
                if w_fd >= 0:
                    os.close(w_fd)

        # Verify [h+#] Agy IS shown when hitl_count > 0 and [all] badge renders when show_all=True
        footer_with_hitl = tui._build_watch_footer(
            interval=3.0,
            hitl_count=2,
            compact_pending=True,
            show_all=True,
            live_filter="gateway",
            filter_input_active=False,
            overlay_mode="",
            awaiting_hitl_digit=False,
            sync_in_flight=False,
            status_msg="",
        )
        self.assertIn("[h+#] Agy", footer_with_hitl)
        self.assertIn("[compact]", footer_with_hitl)
        self.assertIn("[all]", footer_with_hitl)
        self.assertIn("[filter:gateway]", footer_with_hitl)

        # Verify selective_test_runner routes TUI/UX edits (including all tui_*.py modules) and BATS
        self.assertIn("tests.test_dev_queue_cli_ux", srunner.ALL_TEST_MODULES)
        for rel_tui in (
            "skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_models.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_state.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_progress.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_render.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_board.py",
            "skills/agy-dev-queue-orchestrator/scripts/tui_watch.py",
        ):
            routed = srunner.select_test_modules([rel_tui])
            self.assertIn("tests.test_dev_queue_cli_ux", routed)
            self.assertIn("tests.test_dev_queue_tui", routed)
            self.assertTrue(srunner.should_run_bats([rel_tui]))

        # Verify lean facade size (<25 KB), @dataclass view-models, and render_dashboard() parity
        tui_path = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts" / "dev_queue_tui.py"
        self.assertLess(tui_path.stat().st_size, 25_000)
        for vm_name in ("QueueRowView", "RepoPillView", "DashboardSnapshot", "RenderResult", "WatchState"):
            self.assertTrue(hasattr(tui, vm_name), f"Missing view-model {vm_name}")
        with tempfile.TemporaryDirectory() as tmp_vm:
            st_vm = Path(tmp_vm) / "state"
            _write_state(st_vm, items=[])
            rr = tui.render_dashboard(_make_args(st_vm), is_tty_override=False, term_width_override=120)
            self.assertIsInstance(rr, tui.RenderResult)
            legacy_tuple = tui.render(_make_args(st_vm), is_tty_override=False, term_width_override=120)
            self.assertEqual(rr.state_dir, legacy_tuple[4])

        # Verify shared dev_common helpers (`cids_match`, `load_dev_locks_from_dir`, `discover_managed_repo_dirs`)
        self.assertTrue(dev_common.cids_match("c0c153ec", "c0c153ec-p2-dev-queue-tui-modularize"))
        self.assertFalse(dev_common.cids_match("c0c153ec-aaaa", "c0c153ec-bbbb"))
        with tempfile.TemporaryDirectory() as tmp_dc:
            st_dc = Path(tmp_dc) / "state"
            _write_state(
                st_dc,
                items=[],
                locks={"acme/repo-x": {"active": True, "status": "DEVELOPING", "conversation_id": "abcd1234"}},
            )
            act_lks, all_lks = dev_common.load_dev_locks_from_dir(st_dc)
            self.assertIn("acme/repo-x", act_lks)
            self.assertEqual(act_lks["acme/repo-x"]["slot_occupancy"], "1/3")
            self.assertIn("acme/repo-x", all_lks)


if __name__ == "__main__":
    unittest.main()
