#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for Sidebar Ghost Purge (`merge_watcher.py` + `dev-queue-panel`)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
SIDECAR_DIR = REPO_ROOT / "sidecars" / "dev-queue-panel"

for _p in (CLIENTS_DIR, PR_REM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pr_remediator as prs  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


panel_srv = _load_module("dev_queue_panel_server_ghost_test", SIDECAR_DIR / "server.py")


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as err:
        return int(err.code), err.read()


def _seed_ghost_workspace(
    root: Path,
    *,
    live_cid: str = "11111111-1111-4111-8111-111111111111",
    ghost_db_cid: str = "22222222-2222-4222-8222-222222222222",
    ghost_app_only_cid: str = "33333333-3333-4333-8333-333333333333",
    running_cid: str = "44444444-4444-4444-8444-444444444444",
    include_running_in_db: bool = True,
    with_wal_sidecar: bool = False,
) -> dict[str, Path]:
    pdir = root / "state"
    agy_dir = root / ".gemini" / "antigravity"
    brain_dir = agy_dir / "brain"
    convs_dir = agy_dir / "conversations"
    pdir.mkdir(parents=True, exist_ok=True)
    brain_dir.mkdir(parents=True, exist_ok=True)
    convs_dir.mkdir(parents=True, exist_ok=True)

    # Live conversation disk artifacts
    (brain_dir / live_cid).mkdir(parents=True, exist_ok=True)
    (convs_dir / f"{live_cid}.db").write_bytes(b"live-db-stub")
    (convs_dir / f"{live_cid}.pb").write_bytes(b"live-pb-stub")

    # Undocumented binary summary proto MUST NEVER be touched
    proto_path = agy_dir / "agyhub_summaries_proto.pb"
    proto_path.write_bytes(b"\x00\x01PROTO_DO_NOT_TOUCH\xff")

    db_path = agy_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT '', "
        "parent_conversation_id TEXT NOT NULL DEFAULT '', last_modified_time TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, title, tokenize='unicode61 remove_diacritics 2')"
    )
    conn.execute("CREATE TABLE conversation_parents (conversation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL)")
    rows = [
        (live_cid, "CASCADE_RUN_STATUS_IDLE", "Live Conversation"),
        (ghost_db_cid, "CASCADE_RUN_STATUS_IDLE", "Deleted Ghost"),
    ]
    fts_rows = [(live_cid, "Live"), (ghost_db_cid, "Ghost")]
    if include_running_in_db:
        rows.append((running_cid, "CASCADE_RUN_STATUS_RUNNING", "Running Conversation"))
        fts_rows.append((running_cid, "Running"))
    conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, '', '', ?)", rows)
    conn.executemany("INSERT INTO conversations_fts (conversation_id, title) VALUES (?, ?)", fts_rows)
    conn.execute("INSERT INTO conversation_parents VALUES (?, ?)", (ghost_db_cid, live_cid))
    conn.commit()
    conn.close()

    if with_wal_sidecar:
        Path(f"{db_path}-wal").write_bytes(b"wal-frame-bytes")

    # Cross-platform app_storage.json in agy_dir (or platform dir)
    app_storage_path = agy_dir / "app_storage.json"
    app_payload = {
        "unrelated_setting": "keep-me",
        "pinned_conversations_order": json.dumps([live_cid, ghost_db_cid, ghost_app_only_cid]),
        f"antigravity-multi-conversation-layout-v3-{live_cid}": '{"rootNode":{"type":"leaf"}}',
        f"antigravity-multi-conversation-layout-v3-{ghost_db_cid}": '{"rootNode":{"type":"leaf"}}',
        f"antigravity-multi-conversation-layout-v3-{ghost_app_only_cid}+_new": '{"rootNode":{"type":"split"}}',
        f"antigravity-multi-conversation-layout-v3-_new+{ghost_app_only_cid}": '{"rootNode":{"type":"split"}}',
        f"antigravity-multi-conversation-layout-v3-{live_cid}+{ghost_app_only_cid}": '{"rootNode":{"type":"split"}}',
        "antigravity-multi-conversation-layout-v3-index": json.dumps(
            [
                [live_cid, live_cid],
                [ghost_db_cid, live_cid],
                [live_cid, ghost_app_only_cid],
            ]
        ),
        f"queued-messages-card-expanded-{live_cid}": "true",
        f"queued-messages-card-expanded-{ghost_app_only_cid}": "true",
    }
    app_storage_path.write_text(json.dumps(app_payload, indent=2), encoding="utf-8")

    (pdir / "dev_queue.json").write_text(
        json.dumps({"updated_at": "2026-11-24T15:00:00Z", "items": []}),
        encoding="utf-8",
    )
    return {
        "pdir": pdir,
        "agy_dir": agy_dir,
        "brain_dir": brain_dir,
        "db_path": db_path,
        "app_storage_path": app_storage_path,
        "proto_path": proto_path,
    }


class TestGhostCountingAndAppStoragePruning(unittest.TestCase):
    """Verify read-only ghost counting, app_storage.json pruning, 0600 backup, and proto protection."""

    def test_count_ghost_sidebar_entries_readonly_with_active_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, with_wal_sidecar=True)
            proto_before = paths["proto_path"].read_bytes()
            app_before = paths["app_storage_path"].read_text(encoding="utf-8")
            db_mtime_before = paths["db_path"].stat().st_mtime_ns

            res = prs.count_ghost_sidebar_entries(
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                now_ts=1_800_000_000.0,
            )

            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["has_ghosts"])
            self.assertEqual(res["db_ghost_count"], 1)
            self.assertEqual(res["db_ghost_cids"], ["22222222-2222-4222-8222-222222222222"])
            self.assertEqual(res["app_storage_ghost_count"], 2)
            self.assertEqual(res["total_ghost_cids"], 2)
            # pinned(2) + layout_keys(4) + layout_index_pairs(2) + expanded_keys(1) = 9
            self.assertEqual(res["app_storage_entries_count"], 9)
            self.assertEqual(
                res["app_storage_breakdown"],
                {"pinned": 2, "layout_keys": 4, "layout_index_pairs": 2, "expanded_keys": 1},
            )
            # Strictly zero writes occurred
            self.assertEqual(paths["proto_path"].read_bytes(), proto_before)
            self.assertEqual(paths["app_storage_path"].read_text(encoding="utf-8"), app_before)
            self.assertEqual(paths["db_path"].stat().st_mtime_ns, db_mtime_before)
            self.assertEqual(list(paths["agy_dir"].glob("*.bak.*")), [])

    def test_prune_ghost_conversation_summaries_cleans_app_storage_and_preserves_proto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_cid = "11111111-1111-4111-8111-111111111111"
            ghost_db_cid = "22222222-2222-4222-8222-222222222222"
            ghost_app_only_cid = "33333333-3333-4333-8333-333333333333"
            running_cid = "44444444-4444-4444-8444-444444444444"
            paths = _seed_ghost_workspace(
                root,
                live_cid=live_cid,
                ghost_db_cid=ghost_db_cid,
                ghost_app_only_cid=ghost_app_only_cid,
                running_cid=running_cid,
            )
            proto_before = paths["proto_path"].read_bytes()

            res = prs.prune_ghost_conversation_summaries(
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                ps_output="",
                now_ts=1_800_000_000.0,
            )

            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["pruned_cids"], [ghost_db_cid])
            self.assertEqual(res["app_storage_entries_removed"], 9)
            self.assertEqual(res["total_ghosts_pruned_count"], 2)
            self.assertEqual(len(res["backups"]), 2)
            for bkp in res["backups"]:
                bkp_p = Path(bkp)
                self.assertTrue(bkp_p.is_file())
                self.assertEqual(stat.S_IMODE(bkp_p.stat().st_mode), 0o600)

            self.assertEqual(stat.S_IMODE(paths["app_storage_path"].stat().st_mode), 0o600)
            cleaned_app = json.loads(paths["app_storage_path"].read_text(encoding="utf-8"))
            self.assertEqual(cleaned_app["unrelated_setting"], "keep-me")
            self.assertEqual(json.loads(cleaned_app["pinned_conversations_order"]), [live_cid])
            self.assertEqual(json.loads(cleaned_app["antigravity-multi-conversation-layout-v3-index"]), [[live_cid, live_cid]])
            self.assertIn(f"antigravity-multi-conversation-layout-v3-{live_cid}", cleaned_app)
            self.assertIn(f"queued-messages-card-expanded-{live_cid}", cleaned_app)
            self.assertNotIn(f"antigravity-multi-conversation-layout-v3-{ghost_db_cid}", cleaned_app)
            self.assertNotIn(f"antigravity-multi-conversation-layout-v3-{ghost_app_only_cid}+_new", cleaned_app)
            self.assertNotIn(f"antigravity-multi-conversation-layout-v3-_new+{ghost_app_only_cid}", cleaned_app)
            self.assertNotIn(f"antigravity-multi-conversation-layout-v3-{live_cid}+{ghost_app_only_cid}", cleaned_app)
            self.assertNotIn(f"queued-messages-card-expanded-{ghost_app_only_cid}", cleaned_app)

            # Running conversation & proto file are untouched
            verify_conn = sqlite3.connect(str(paths["db_path"]))
            rem_ids = {r[0] for r in verify_conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
            verify_conn.close()
            self.assertEqual(rem_ids, {live_cid, running_cid})
            self.assertEqual(paths["proto_path"].read_bytes(), proto_before)


class TestDetachedGhostPurgeWorkerAndGuards(unittest.TestCase):
    """Verify single-flight arm/cancel, TTL, fail-closed RUNNING/lock guard, and Darwin/Linux worker callbacks."""

    def test_single_flight_arm_ttl_expiry_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            spawned_cmds: list[list[str]] = []

            class _FakeProc:
                pid = 4242

            def _fake_popen(cmd, **kwargs):
                self.assertTrue(kwargs.get("start_new_session"))
                spawned_cmds.append(list(cmd))
                return _FakeProc()

            with mock.patch.dict(os.environ, {"AGY_GHOST_PURGE_ARM_TTL_SECONDS": "120"}):
                first = prs.arm_ghost_purge(
                    portfolio_dir=pdir,
                    force_spawn=True,
                    popen_fn=_fake_popen,
                    now_ts=1_800_000_000.0,
                )
                self.assertEqual(first["status"], "ok")
                self.assertTrue(first["armed"])
                self.assertFalse(first["already_armed"])
                self.assertEqual(len(spawned_cmds), 1)
                self.assertIn("--ghost-purge-worker", spawned_cmds[0])
                arm_file = pdir / prs.GHOST_PURGE_ARMED_FILENAME
                self.assertEqual(stat.S_IMODE(arm_file.stat().st_mode), 0o600)
                self.assertEqual(first["arm_state"]["ttl_seconds"], 120)

                # Single-flight second arm reuses existing arm_id
                second = prs.arm_ghost_purge(
                    portfolio_dir=pdir,
                    spawn_worker=False,
                    now_ts=1_800_000_010.0,
                )
                self.assertTrue(second["already_armed"])
                self.assertEqual(second["arm_state"]["arm_id"], first["arm_state"]["arm_id"])

                # Worker exits immediately when TTL expires
                expired_res = prs.run_ghost_purge_worker(
                    portfolio_dir=pdir,
                    clock_fn=lambda: 1_800_000_200.0,
                )
                self.assertEqual(expired_res["event"], "expired")
                self.assertFalse(arm_file.exists())

                # Re-arm and cancel
                prs.arm_ghost_purge(portfolio_dir=pdir, spawn_worker=False, now_ts=1_800_000_300.0)
                self.assertTrue(arm_file.exists())
                cancelled = prs.cancel_ghost_purge(portfolio_dir=pdir)
                self.assertTrue(cancelled["cancelled"])
                self.assertFalse(cancelled["armed"])
                self.assertFalse(arm_file.exists())

    def test_restart_and_purge_fail_closed_on_running_and_active_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=True)

            # 1. Blocked by RUNNING conversation in DB
            blocked_run = prs.restart_and_purge(
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                spawn_worker=False,
            )
            self.assertEqual(blocked_run["status"], "blocked")
            self.assertEqual(blocked_run["blocked_reason"], "running_conversations_active")

            # Remove RUNNING row from DB
            conn = sqlite3.connect(str(paths["db_path"]))
            conn.execute("DELETE FROM conversation_summaries WHERE status = 'CASCADE_RUN_STATUS_RUNNING'")
            conn.commit()
            conn.close()

            # 2. Blocked by active repo lock in DEVELOPING / NEEDS_REVISION_ITERATING / AWAITING_AGENT_REVIEW
            for lock_status in ("DEVELOPING", "NEEDS_REVISION_ITERATING", "AWAITING_AGENT_REVIEW"):
                (paths["pdir"] / "actions_state.json").write_text(
                    json.dumps(
                        {
                            "actions_log_audit_state": {
                                "dev_pr_locks": {
                                    "acme/repo": {
                                        "active": True,
                                        "status": lock_status,
                                        "conversation_id": "99999999-9999-4999-8999-999999999999",
                                    }
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                blocked_lk = prs.restart_and_purge(
                    portfolio_dir=paths["pdir"],
                    antigravity_dirs=[paths["agy_dir"]],
                    spawn_worker=False,
                )
                self.assertEqual(blocked_lk["status"], "blocked")
                self.assertEqual(blocked_lk["blocked_reason"], "active_dev_locks")

            # 3. Allowed when lock is PENDING_HUMAN_PR_VALIDATION and no RUNNING conversations
            (paths["pdir"] / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_locks": {
                                "acme/repo": {
                                    "active": True,
                                    "status": "PENDING_HUMAN_PR_VALIDATION",
                                    "conversation_id": "99999999-9999-4999-8999-999999999999",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            allowed = prs.restart_and_purge(
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                spawn_worker=False,
                now_ts=1_800_000_000.0,
            )
            self.assertEqual(allowed["status"], "ok")
            self.assertTrue(allowed["can_restart_now"])

    def test_worker_on_quit_and_restart_now_darwin_and_linux_callbacks(self) -> None:
        for plat, reopen_expected in (("darwin", True), ("linux", True)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = _seed_ghost_workspace(root, include_running_in_db=False)
                prs.arm_ghost_purge(
                    mode="restart_now",
                    portfolio_dir=paths["pdir"],
                    spawn_worker=False,
                    now_ts=1_800_000_000.0,
                )
                ps_calls = {"n": 0}
                quit_calls: list[tuple[str, list[str]]] = []
                reopen_calls: list[str] = []

                def _ps_runner() -> str:
                    ps_calls["n"] += 1
                    if ps_calls["n"] <= 2:
                        return "  PID ARGS\n  555 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n"
                    return ""

                def _quit_fn(p_name: str, procs: list[str]) -> bool:
                    quit_calls.append((p_name, list(procs)))
                    return True

                def _reopen_fn(p_name: str) -> bool:
                    reopen_calls.append(p_name)
                    return True

                w_res = prs.run_ghost_purge_worker(
                    mode="restart_now",
                    poll_interval_seconds=0.01,
                    quit_timeout_seconds=10.0,
                    portfolio_dir=paths["pdir"],
                    antigravity_dirs=[paths["agy_dir"]],
                    ps_runner=_ps_runner,
                    quit_fn=_quit_fn,
                    reopen_fn=_reopen_fn,
                    sleep_fn=lambda _s: None,
                    clock_fn=lambda: 1_800_000_001.0,
                    platform_name=plat,
                )
                self.assertEqual(w_res["status"], "ok")
                self.assertEqual(w_res["event"], "purged_and_reopened")
                self.assertEqual(len(quit_calls), 1)
                self.assertEqual(quit_calls[0][0], plat)
                self.assertEqual(reopen_calls, [plat])
                self.assertFalse((paths["pdir"] / prs.GHOST_PURGE_ARMED_FILENAME).exists())
                res_file = paths["pdir"] / prs.GHOST_PURGE_RESULT_FILENAME
                self.assertTrue(res_file.is_file())
                self.assertEqual(stat.S_IMODE(res_file.stat().st_mode), 0o600)
                saved = json.loads(res_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["pruned_count"], 1)
                self.assertEqual(saved["app_storage_entries_removed"], 9)
                self.assertEqual(saved["reopened"], reopen_expected)
                self.assertFalse(saved["acked"])

        # on_quit mode never calls quit or reopen
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=False)
            prs.arm_ghost_purge(
                mode="on_quit",
                portfolio_dir=paths["pdir"],
                spawn_worker=False,
                now_ts=1_800_000_000.0,
            )
            ps_step = {"n": 0}
            reopen_called: list[str] = []

            def _ps_on_quit() -> str:
                ps_step["n"] += 1
                return "  PID ARGS\n  777 antigravity\n" if ps_step["n"] == 1 else ""

            oq_res = prs.run_ghost_purge_worker(
                mode="on_quit",
                poll_interval_seconds=0.01,
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                ps_runner=_ps_on_quit,
                reopen_fn=lambda p: reopen_called.append(p) or True,
                sleep_fn=lambda _s: None,
                clock_fn=lambda: 1_800_000_002.0,
            )
            self.assertEqual(oq_res["event"], "purged_on_quit")
            self.assertEqual(reopen_called, [])
            self.assertFalse(oq_res["result"]["reopened"])

    def test_cli_flags_count_arm_cancel_and_restart_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=True)

            buf_count = io.StringIO()
            with contextlib.redirect_stdout(buf_count):
                rc_c = prs.main(
                    [
                        "prune-ghost-summaries",
                        "--count-only",
                        "--portfolio-dir",
                        str(paths["pdir"]),
                        "--summaries-db",
                        str(paths["db_path"]),
                        "--app-storage",
                        str(paths["app_storage_path"]),
                    ]
                )
            self.assertEqual(rc_c, 0)
            c_json = json.loads(buf_count.getvalue())
            self.assertEqual(c_json["db_ghost_count"], 1)

            buf_restart = io.StringIO()
            with contextlib.redirect_stdout(buf_restart):
                rc_r = prs.main(
                    [
                        "prune-ghost-summaries",
                        "--restart-and-purge",
                        "--portfolio-dir",
                        str(paths["pdir"]),
                        "--summaries-db",
                        str(paths["db_path"]),
                    ]
                )
            self.assertEqual(rc_r, 1)
            self.assertEqual(json.loads(buf_restart.getvalue())["status"], "blocked")

            buf_arm = io.StringIO()
            with (
                mock.patch("subprocess.Popen") as mock_popen,
                contextlib.redirect_stdout(buf_arm),
            ):
                mock_popen.return_value = mock.Mock(pid=9991)
                rc_a = prs.main(
                    [
                        "prune-ghost-summaries",
                        "--arm-ghost-purge",
                        "--portfolio-dir",
                        str(paths["pdir"]),
                        "--summaries-db",
                        str(paths["db_path"]),
                        "--app-storage",
                        str(paths["app_storage_path"]),
                    ]
                )
            self.assertEqual(rc_a, 0)
            self.assertTrue(json.loads(buf_arm.getvalue())["armed"])
            mock_popen.assert_called_once()
            self.assertTrue(mock_popen.call_args.kwargs.get("start_new_session"))

            buf_cancel = io.StringIO()
            with contextlib.redirect_stdout(buf_cancel):
                rc_x = prs.main(
                    [
                        "prune-ghost-summaries",
                        "--cancel-ghost-purge",
                        "--portfolio-dir",
                        str(paths["pdir"]),
                    ]
                )
            self.assertEqual(rc_x, 0)
            self.assertFalse(json.loads(buf_cancel.getvalue())["armed"])

    def test_linux_quit_targets_desktop_pids_only(self) -> None:
        ps_out = (
            "  PID ARGS\n"
            "  111 /opt/Antigravity/antigravity --no-sandbox\n"
            "  222 /usr/local/bin/agy chat --id abc\n"
            "  333 /home/testuser/.gemini/language_server_linux --port 9000\n"
        )
        detected = prs._detect_active_antigravity_processes(ps_output=ps_out)
        self.assertEqual(len(detected), 3)
        desktop_only = prs._filter_desktop_antigravity_procs(detected)
        self.assertEqual(desktop_only, ["pid=111:antigravity"])

        killed: list[tuple[int, int]] = []
        with mock.patch("os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))):
            ok = prs._default_quit_antigravity("linux", detected)
        self.assertTrue(ok)
        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][0], 111)

    def test_restart_now_quit_timeout_still_reopens_desktop_after_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=False, with_wal_sidecar=True)
            prs.arm_ghost_purge(
                mode="restart_now",
                portfolio_dir=paths["pdir"],
                spawn_worker=False,
                now_ts=1_800_000_000.0,
            )
            ps_step = {"n": 0}
            reopen_calls: list[str] = []

            def _ps_desktop_exits_but_agy_remains() -> str:
                ps_step["n"] += 1
                if ps_step["n"] == 1:
                    return (
                        "  PID ARGS\n"
                        "  501 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n"
                        "  502 /usr/local/bin/agy\n"
                    )
                # Desktop exited, leftover agy CLI process remains
                return "  PID ARGS\n  502 /usr/local/bin/agy\n"

            w_res = prs.run_ghost_purge_worker(
                mode="restart_now",
                poll_interval_seconds=0.01,
                quit_timeout_seconds=10.0,
                max_iterations=2,
                portfolio_dir=paths["pdir"],
                antigravity_dirs=[paths["agy_dir"]],
                ps_runner=_ps_desktop_exits_but_agy_remains,
                quit_fn=lambda _p, _procs: True,
                reopen_fn=lambda p: reopen_calls.append(p) or True,
                sleep_fn=lambda _s: None,
                clock_fn=lambda: 1_800_000_001.0,
                platform_name="darwin",
            )
            self.assertEqual(w_res["status"], "error")
            self.assertEqual(w_res["event"], "quit_timeout")
            self.assertTrue(w_res["reopened"])
            self.assertEqual(reopen_calls, ["darwin"])
            saved = json.loads((paths["pdir"] / prs.GHOST_PURGE_RESULT_FILENAME).read_text(encoding="utf-8"))
            self.assertTrue(saved["reopened"])
            self.assertEqual(saved["reason"], "quit_timeout")


class TestSidecarGhostPurgeEndpointsAndUI(unittest.TestCase):
    """Verify sidecar panel state, HTTP endpoints (auth, 409 guard, ack), Zero-PII, and UI/env contracts."""

    def test_sidecar_state_and_http_endpoints_zero_pii_and_409_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=True)
            fake_home = root / "home_testuser"
            fake_home.mkdir(parents=True, exist_ok=True)
            mock_token = "mock-ghost-purge-panel-token-0000"

            httpd = panel_srv.create_panel_server(
                port=0,
                ui_token=mock_token,
                state_dir=paths["pdir"],
                brain_dir=paths["brain_dir"],
                conv_db_path=paths["db_path"],
                home_dir=fake_home,
                env={},
            )
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}"

                # Auth required on all new POST routes
                for route in (
                    "/api/cleanup/ghosts/arm",
                    "/api/cleanup/ghosts/cancel",
                    "/api/cleanup/ghosts/ack-result",
                ):
                    st_unauth, _ = _http_request(f"{base}{route}", method="POST", body={})
                    self.assertEqual(st_unauth, 401)

                # GET /api/state exposes ghost_purge without leaks
                st_state, raw_state = _http_request(
                    f"{base}/api/state",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_state, 200)
                state_text = raw_state.decode("utf-8")
                self.assertNotIn(str(fake_home), state_text)
                state_json = json.loads(state_text)
                gp = state_json["ghost_purge"]
                self.assertEqual(gp["summary"]["db_ghost_count"], 1)
                self.assertEqual(gp["summary"]["app_storage_entries_count"], 9)
                self.assertFalse(gp["can_restart_now"])
                self.assertEqual(gp["blocked_reason"], "running_conversations_active")

                # POST /api/cleanup/preview includes ghost_purge
                st_prev, raw_prev = _http_request(
                    f"{base}/api/cleanup/preview",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"include_unaudited_check": False},
                )
                self.assertEqual(st_prev, 200)
                prev_json = json.loads(raw_prev)
                self.assertIn("ghost_purge", prev_json)
                self.assertEqual(prev_json["ghost_purge"]["summary"]["total_ghost_cids"], 2)

                # POST /api/cleanup/ghosts/arm mode=restart_now fails closed with HTTP 409
                st_409, raw_409 = _http_request(
                    f"{base}/api/cleanup/ghosts/arm",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"mode": "restart_now"},
                )
                self.assertEqual(st_409, 409)
                self.assertEqual(json.loads(raw_409)["blocked_reason"], "running_conversations_active")

                # POST /api/cleanup/ghosts/arm mode=on_quit succeeds even when RUNNING
                st_arm, raw_arm = _http_request(
                    f"{base}/api/cleanup/ghosts/arm",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"mode": "on_quit"},
                )
                self.assertEqual(st_arm, 200)
                arm_json = json.loads(raw_arm)
                self.assertTrue(arm_json["ghost_purge"]["armed"]["armed"])

                # POST /api/cleanup/ghosts/arm mode=restart_now while on_quit is armed fails closed with HTTP 409
                st_armed_409, raw_armed_409 = _http_request(
                    f"{base}/api/cleanup/ghosts/arm",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"mode": "restart_now"},
                )
                self.assertEqual(st_armed_409, 409)
                self.assertEqual(json.loads(raw_armed_409)["blocked_reason"], "armed_already")

                # POST /api/cleanup/ghosts/cancel disarms
                st_cancel, raw_cancel = _http_request(
                    f"{base}/api/cleanup/ghosts/cancel",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={},
                )
                self.assertEqual(st_cancel, 200)
                self.assertFalse(json.loads(raw_cancel)["ghost_purge"]["armed"]["armed"])

                # Clear RUNNING row and verify restart_now succeeds (200)
                conn = sqlite3.connect(str(paths["db_path"]))
                conn.execute("DELETE FROM conversation_summaries WHERE status = 'CASCADE_RUN_STATUS_RUNNING'")
                conn.commit()
                conn.close()
                st_ok_restart, raw_ok_restart = _http_request(
                    f"{base}/api/cleanup/ghosts/arm",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"mode": "restart_now"},
                )
                self.assertEqual(st_ok_restart, 200)
                self.assertTrue(json.loads(raw_ok_restart)["ghost_purge"]["can_restart_now"])

                # Seed result and ack via /api/cleanup/ghosts/ack-result
                prs._write_ghost_purge_result(
                    paths["pdir"],
                    status="ok",
                    mode="restart_now",
                    now_ts=time.time(),
                    prune_res={"pruned_count": 1, "app_storage_entries_removed": 8, "backups": ["b1", "b2"]},
                    reopened=True,
                )
                st_ack, raw_ack = _http_request(
                    f"{base}/api/cleanup/ghosts/ack-result",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={},
                )
                self.assertEqual(st_ack, 200)
                self.assertTrue(json.loads(raw_ack)["ghost_purge"]["last_result"]["acked"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

    def test_ui_markup_script_and_env_example_contracts(self) -> None:
        html = (SIDECAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDECAR_DIR / "app.js").read_text(encoding="utf-8")
        css = (SIDECAR_DIR / "styles.css").read_text(encoding="utf-8")
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("AGY_GHOST_PURGE_ARM_TTL_SECONDS", env_example)
        for dom_id in (
            "dq-ghost-result-banner",
            "dq-ghost-section",
            "dq-ghost-summary",
            "dq-ghost-arm-btn",
            "dq-ghost-restart-btn",
            "dq-ghost-armed-banner",
            "dq-ghost-cancel-btn",
            "dq-ghost-confirm-row",
            "dq-ghost-restart-confirm-btn",
        ):
            self.assertIn(f'id="{dom_id}"', html)
        for route in (
            "/api/cleanup/ghosts/arm",
            "/api/cleanup/ghosts/cancel",
            "/api/cleanup/ghosts/ack-result",
        ):
            self.assertIn(route, js)
        self.assertIn("renderGhostPurgeControls", js)
        self.assertIn("renderGhostResultBanner", js)
        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(unsafe_sink, js)
        self.assertIn(".dq-ghost-section", css)
        self.assertIn(".dq-ghost-result-banner", css)
        self.assertEqual(css.count("{"), css.count("}"))

    def test_build_panel_state_mtime_ttl_cache_avoids_redundant_scans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _seed_ghost_workspace(root, include_running_in_db=False)
            fake_home = root / "home_testuser"
            fake_home.mkdir(parents=True, exist_ok=True)
            repo_cache: dict[str, object] = {}

            with mock.patch.object(
                panel_srv._merge_watcher,
                "get_ghost_purge_status",
                wraps=panel_srv._merge_watcher.get_ghost_purge_status,
            ) as spy_status:
                snap1 = panel_srv.build_panel_state(
                    state_dir=paths["pdir"],
                    brain_dir=paths["brain_dir"],
                    conv_db_path=paths["db_path"],
                    repo_cache=repo_cache,
                    home_dir=fake_home,
                    now_ts=1_800_000_000.0,
                    env={},
                )
                snap2 = panel_srv.build_panel_state(
                    state_dir=paths["pdir"],
                    brain_dir=paths["brain_dir"],
                    conv_db_path=paths["db_path"],
                    repo_cache=repo_cache,
                    home_dir=fake_home,
                    now_ts=1_800_000_002.0,
                    env={},
                )
                self.assertEqual(spy_status.call_count, 1)
                self.assertEqual(snap1["ghost_purge"], snap2["ghost_purge"])

                # Mutating armed state invalidates cache via mtime signature
                prs.arm_ghost_purge(portfolio_dir=paths["pdir"], spawn_worker=False, now_ts=1_800_000_003.0)
                snap3 = panel_srv.build_panel_state(
                    state_dir=paths["pdir"],
                    brain_dir=paths["brain_dir"],
                    conv_db_path=paths["db_path"],
                    repo_cache=repo_cache,
                    home_dir=fake_home,
                    now_ts=1_800_000_004.0,
                    env={},
                )
                self.assertEqual(spy_status.call_count, 2)
                self.assertTrue(snap3["ghost_purge"]["armed"]["armed"])

                # TTL expiry also refreshes after 45s
                panel_srv.build_panel_state(
                    state_dir=paths["pdir"],
                    brain_dir=paths["brain_dir"],
                    conv_db_path=paths["db_path"],
                    repo_cache=repo_cache,
                    home_dir=fake_home,
                    now_ts=1_800_000_055.0,
                    env={},
                )
                self.assertEqual(spy_status.call_count, 3)


if __name__ == "__main__":
    unittest.main()
