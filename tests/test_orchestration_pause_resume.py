#!/usr/bin/env python3
"""Hermetic unit tests for Orchestration Pause / Continue (Graceful Drain) controls."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
CLIENTS_DIR = REPO_ROOT / "clients"
PANEL_DIR = REPO_ROOT / "sidecars" / "dev-queue-panel"
WATCHDOG_DIR = REPO_ROOT / "sidecars" / "dev-queue-watchdog"

for _p in (CLIENTS_DIR, SCRIPTS_DIR, TUI_DIR):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

import dev_common  # noqa: E402
import dev_queue_doctor as dq_doc  # noqa: E402
import dev_queue_tui as dq_tui  # noqa: E402
import orchestration_control as orch_ctrl  # noqa: E402
import pr_remediator as pr_rem  # noqa: E402


def _load_module(path: Path, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {mod_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clean_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("GIT_"):
            env.pop(k, None)
    return env


def _init_temp_git_repo(root: Path, slug: str = "acme/demo-repo") -> Path:
    repo_dir = root / slug.split("/")[-1]
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = _clean_git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, env=env, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Bot"], cwd=repo_dir, env=env, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, env=env, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"git@github.com:{slug}.git"],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
    )
    (repo_dir / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=repo_dir, env=env, check=True, capture_output=True)
    return repo_dir


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return (int(resp.status), json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        return (int(exc.code), json.loads(raw) if raw.strip() else {})


class TestOrchestrationControlState(unittest.TestCase):
    """Verify atomic 0600 pause/resume state, Zero-PII scrubbing, and two-phase Graceful Drain summaries."""

    def test_pause_resume_0600_and_zero_pii_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            init_ctrl = orch_ctrl.load_orchestration_control(state_dir=st_dir)
            self.assertFalse(init_ctrl["paused"])
            self.assertFalse((st_dir / orch_ctrl.ORCHESTRATION_CONTROL_FILENAME).exists())

            p_res = orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                reason="Customer call /Users/alice/secret/mail.txt",
                source="sidecar_panel",
                ps_output="",
                now_ts=1763974800.0,
            )
            self.assertTrue(p_res["paused"])
            self.assertTrue(p_res["changed"])
            self.assertTrue(p_res["control"]["chrome_devtools_disabled"])
            self.assertNotIn("/Users/alice", p_res["control"]["reason"])
            self.assertIn("$HOME/secret/mail.txt", p_res["control"]["reason"])
            self.assertEqual(p_res["control"]["source"], "sidecar_panel")

            ctrl_file = st_dir / orch_ctrl.ORCHESTRATION_CONTROL_FILENAME
            self.assertTrue(ctrl_file.is_file())
            mode_bits = stat.S_IMODE(ctrl_file.stat().st_mode)
            self.assertEqual(mode_bits, 0o600)

            # Re-pausing is idempotent (changed=False) and preserves original paused_at
            p_res2 = orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                source="cli",
                ps_output="",
                now_ts=1763974900.0,
            )
            self.assertFalse(p_res2["changed"])
            self.assertEqual(p_res2["control"]["paused_at"], p_res["control"]["paused_at"])

            # Resuming generates fresh unhandled resume_nonce
            r_res = orch_ctrl.resume_orchestration(
                state_dir=st_dir,
                source="sidecar_panel",
                request_wake=True,
                now_ts=1763975000.0,
            )
            self.assertFalse(r_res["paused"])
            self.assertTrue(r_res["changed"])
            nonce = r_res["resume_nonce"]
            self.assertTrue(nonce)
            self.assertTrue(orch_ctrl.has_unhandled_resume_request(r_res["control"]))

            # Marking handled acknowledges nonce cleanly and rejects mismatches/repeats
            bad_ack = orch_ctrl.mark_resume_handled(state_dir=st_dir, resume_nonce="wrong-nonce", handled_by="test")
            self.assertFalse(bad_ack["acknowledged"])
            good_ack = orch_ctrl.mark_resume_handled(state_dir=st_dir, resume_nonce=nonce, handled_by="wait_for_unlock")
            self.assertTrue(good_ack["acknowledged"])
            self.assertFalse(orch_ctrl.has_unhandled_resume_request(good_ack["control"]))
            dup_ack = orch_ctrl.mark_resume_handled(state_dir=st_dir, resume_nonce=nonce, handled_by="watchdog")
            self.assertFalse(dup_ack["acknowledged"])

    def test_summarize_orchestration_phase_draining_vs_paused(self) -> None:
        paused_ctrl = {"paused": True, "mode": "graceful_drain", "reason": "Customer focus"}
        developing_locks = {
            "acme/repo-a": {"active": True, "status": "DEVELOPING", "pr_gate_ready": False},
            "acme/repo-b": {"active": True, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True},
        }
        draining = orch_ctrl.summarize_orchestration_phase(paused_ctrl, developing_locks)
        self.assertEqual(draining["phase"], "draining")
        self.assertEqual(draining["ongoing_count"], 1)
        self.assertTrue(draining["new_dispatches_blocked"])
        self.assertIn("DRAINING", draining["banner_text"])
        self.assertIn("1 ongoing task", draining["banner_text"])

        pr_ready_only_locks = {
            "acme/repo-b": {"active": True, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True},
        }
        fully_paused = orch_ctrl.summarize_orchestration_phase(paused_ctrl, pr_ready_only_locks)
        self.assertEqual(fully_paused["phase"], "paused")
        self.assertEqual(fully_paused["ongoing_count"], 0)
        self.assertTrue(fully_paused["chrome_devtools_disabled"])
        self.assertIn("PAUSED", fully_paused["banner_text"])
        self.assertIn("Chrome DevTools OFF", fully_paused["banner_text"])
        self.assertIn("Watchdog silent", fully_paused["banner_text"])

    def test_pause_stops_only_chrome_devtools_mcp_and_enforces_pretool_gate(self) -> None:
        fake_ps = "\n".join(
            [
                "1018 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222",
                "1345 /Applications/Antigravity.app/Contents/Resources/bin/language_server --standalone",
                "44201 node /Applications/Antigravity.app/Contents/Resources/app.asar.unpacked/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js",
                "44202 npx chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222",
                "73732 python3 watchdog.py",
            ]
        )
        killed: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            p_res = orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                reason="Customer work",
                source="sidecar_panel",
                ps_output=fake_ps,
                kill_fn=lambda pid, sig: killed.append((pid, sig)),
            )
            self.assertEqual(p_res["chrome_devtools"]["status"], "stopped")
            self.assertEqual(p_res["chrome_devtools"]["stopped_pids"], [44201, 44202])
            self.assertEqual([pid for pid, _ in killed], [44201, 44202])

            evaluator = dev_common.DevLifecycleGuardrailEvaluator(state_dir=st_dir)
            # Chrome DevTools MCP calls and CLI are blocked while paused
            deny_mcp = evaluator.evaluate_pre_tool_use(
                {"tool_name": "call_mcp_tool", "tool_input": {"ServerName": "chrome_devtools", "ToolName": "list_pages"}}
            )
            self.assertEqual(deny_mcp["decision"], "deny")
            self.assertIn("Chrome DevTools", deny_mcp["reason"])

            deny_eager = evaluator.evaluate_pre_tool_use(
                {"tool_name": "mcp_chrome_devtools_take_snapshot", "tool_input": {}}
            )
            self.assertEqual(deny_eager["decision"], "deny")

            deny_cli = evaluator.evaluate_pre_tool_use(
                {"tool_name": "run_command", "tool_input": {"CommandLine": "node $CHROME_DEVTOOLS_MCP_JS --help"}}
            )
            self.assertEqual(deny_cli["decision"], "deny")

            # Customer TAM / non-Chrome-DevTools MCP and normal shell commands remain 100% allowed
            allow_wiz = evaluator.evaluate_pre_tool_use(
                {"tool_name": "call_mcp_tool", "tool_input": {"ServerName": "wiz-tam-plugin_wiz-mcp", "ToolName": "execute"}}
            )
            self.assertEqual(allow_wiz["decision"], "allow")
            allow_shell = evaluator.evaluate_pre_tool_use(
                {"tool_name": "run_command", "tool_input": {"CommandLine": "python3 -c 'print(1)'"}}
            )
            self.assertEqual(allow_shell["decision"], "allow")

            # Resuming re-enables Chrome DevTools MCP cleanly
            orch_ctrl.resume_orchestration(state_dir=st_dir, source="sidecar_panel", request_wake=False)
            allow_after = evaluator.evaluate_pre_tool_use(
                {"tool_name": "call_mcp_tool", "tool_input": {"ServerName": "chrome_devtools", "ToolName": "list_pages"}}
            )
            self.assertEqual(allow_after["decision"], "allow")


class TestQueueWorktreeWatcherAndDoctorPauseGates(unittest.TestCase):
    """Verify Graceful Drain gates across queue-next, start-remediation, watchers, doctor, and watchdog."""

    def test_queue_next_blocks_new_dispatches_while_paused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            pr_rem.enqueue_dev_queue_item(
                conversation_id="11111111-1111-4111-8111-111111111111",
                title="Pending task A",
                priority=20,
                repo_slugs=["acme/repo-a"],
                status="pending",
                portfolio_dir=st_dir,
            )
            before = pr_rem.get_next_dev_queue_item(portfolio_dir=st_dir, skip_locked_repos=True)
            self.assertFalse(before["paused"])
            self.assertIsNotNone(before["next_unlocked_item"])
            self.assertIn("acme/repo-a", before["runnable_by_repo"])

            orch_ctrl.pause_orchestration(state_dir=st_dir, source="cli", stop_chrome_devtools=False)
            during = pr_rem.get_next_dev_queue_item(portfolio_dir=st_dir, skip_locked_repos=True)
            self.assertTrue(during["paused"])
            self.assertFalse(during["has_next"])
            self.assertIsNone(during["next_unlocked_item"])
            self.assertEqual(during["runnable_by_repo"], {})
            self.assertEqual(during["slot2_candidates"], [])

            orch_ctrl.resume_orchestration(state_dir=st_dir, source="cli", request_wake=False)
            after = pr_rem.get_next_dev_queue_item(portfolio_dir=st_dir, skip_locked_repos=True)
            self.assertFalse(after["paused"])
            self.assertIn("acme/repo-a", after["runnable_by_repo"])

    def test_start_remediation_blocks_new_lock_but_allows_reusing_ongoing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st_dir = root / "state"
            st_dir.mkdir()
            repo_dir = _init_temp_git_repo(root, "acme/demo-repo")

            # Acquire an active ongoing lock BEFORE pausing
            cid_ongoing = "22222222-2222-4222-8222-222222222222"
            res_init = pr_rem.start_remediation_branch_and_lock(
                conversation_id=cid_ongoing,
                action_id="ongoing-task",
                rca_summary="Ongoing work",
                proposed_fix="Finish ongoing task",
                create_worktree=True,
                workdir=repo_dir,
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_init["status"], "ok")

            # Pause orchestration (Graceful Drain)
            orch_ctrl.pause_orchestration(state_dir=st_dir, source="sidecar_panel", stop_chrome_devtools=False)

            # Ongoing active lock with --reuse-active MUST still succeed cleanly
            res_reuse = pr_rem.start_remediation_branch_and_lock(
                conversation_id=cid_ongoing,
                action_id="ongoing-task",
                rca_summary="Ongoing work round 2",
                proposed_fix="Apply review fix",
                create_worktree=True,
                reuse_active_branch=True,
                workdir=repo_dir,
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_reuse["status"], "ok")

            # A brand-new lock on a second FREE repo MUST be blocked with status="paused"
            repo2_dir = _init_temp_git_repo(root, "acme/other-repo")
            res_new = pr_rem.start_remediation_branch_and_lock(
                conversation_id="33333333-3333-4333-8333-333333333333",
                action_id="new-task",
                rca_summary="New work",
                proposed_fix="Should be blocked",
                create_worktree=True,
                workdir=repo2_dir,
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_new["status"], "paused")
            self.assertFalse(res_new["acquired"])
            self.assertEqual(res_new["error"], "orchestration_paused")

    def test_wait_for_unlock_sleeps_while_paused_and_wakes_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            pr_rem.enqueue_dev_queue_item(
                conversation_id="44444444-4444-4444-8444-444444444444",
                title="Pending work",
                priority=15,
                repo_slugs=["acme/repo-a"],
                status="pending",
                portfolio_dir=st_dir,
            )
            orch_ctrl.pause_orchestration(state_dir=st_dir, source="cli", stop_chrome_devtools=False)

            # While paused, even with unlocked runnable pending work, wait-for-unlock does NOT wake
            paused_res = pr_rem.wait_for_unlock_or_actionable_event(
                poll_interval_seconds=0.05,
                max_duration_seconds=5.0,
                max_iterations=2,
                portfolio_dir=st_dir,
                watched_paths_override=[],
            )
            self.assertEqual(paused_res["event"], "paused_max_iterations")
            self.assertTrue(paused_res["paused"])

            # Resume in background thread -> wait-for-unlock exits immediately with orchestration_resumed
            def _resume_soon() -> None:
                import time

                time.sleep(0.12)
                orch_ctrl.resume_orchestration(state_dir=st_dir, source="sidecar_panel", request_wake=True)

            t = threading.Thread(target=_resume_soon, daemon=True)
            t.start()
            resumed_res = pr_rem.wait_for_unlock_or_actionable_event(
                poll_interval_seconds=0.05,
                max_duration_seconds=5.0,
                max_iterations=40,
                portfolio_dir=st_dir,
                watched_paths_override=[],
            )
            t.join(timeout=2.0)
            self.assertEqual(resumed_res["event"], "orchestration_resumed")
            self.assertFalse(resumed_res["paused"])
            # Nonce was acknowledged by wait_for_unlock so watchdog won't double-wake
            ctrl_after = orch_ctrl.load_orchestration_control(state_dir=st_dir)
            self.assertFalse(orch_ctrl.has_unhandled_resume_request(ctrl_after))
            self.assertEqual(ctrl_after["resume_handled_by"], "wait_for_unlock")

    def test_watch_pr_merge_skips_git_sync_while_paused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            orch_ctrl.pause_orchestration(state_dir=st_dir, source="cli", stop_chrome_devtools=False)
            sync_calls: list[int] = []
            orig_sync = pr_rem.sync_dev_pr_lock_with_git
            try:
                pr_rem.sync_dev_pr_lock_with_git = lambda **kw: (sync_calls.append(1) or {"active": True})  # type: ignore[assignment]
                res = pr_rem.run_pr_merge_watcher_loop(
                    poll_interval_seconds=0.05,
                    max_duration_seconds=5.0,
                    max_iterations=2,
                    portfolio_dir=st_dir,
                    watched_paths_override=[],
                )
            finally:
                pr_rem.sync_dev_pr_lock_with_git = orig_sync
            self.assertTrue(res.get("paused"))
            self.assertEqual(res.get("reason"), "paused_max_iterations_reached")
            self.assertEqual(sync_calls, [])

    def test_doctor_and_watchdog_suppress_wake_while_paused_and_wake_on_resume(self) -> None:
        watchdog_mod = _load_module(WATCHDOG_DIR / "watchdog.py", "dev_queue_watchdog_pause_test")
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            pr_rem.enqueue_dev_queue_item(
                conversation_id="55555555-5555-4555-8555-555555555555",
                title="Queued task",
                priority=10,
                repo_slugs=["acme/repo-a"],
                status="pending",
                portfolio_dir=st_dir,
            )
            orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                reason="Customer email",
                source="sidecar_panel",
                stop_chrome_devtools=False,
            )

            diag = dq_doc.diagnose_orchestrator(state_dir=st_dir, sync_git=True, ps_output="")
            self.assertEqual(diag["overall_status"], "paused")
            self.assertTrue(diag["paused"])
            self.assertFalse(diag["action_required"])
            self.assertFalse(diag["needs_wake"])
            self.assertEqual(diag["anomalies"], [])
            self.assertEqual(diag["git_sync"].get("skipped_reason"), "orchestration_paused")

            # --check-only exits 0 while intentionally paused
            doc_check = dq_doc.run_queue_doctor(state_dir=st_dir, check_only=True, ps_output="")
            self.assertEqual(doc_check["exit_code"], 0)

            # trigger_orchestrator_wake refuses even with force=True while paused
            wake_blocked = dq_doc.trigger_orchestrator_wake(diag, state_dir=st_dir, force=True, dry_run=True)
            self.assertEqual(wake_blocked["status"], "skipped_paused")
            self.assertFalse(wake_blocked["woke"])

            # Rendered doctor report shows PAUSED banner
            rendered = dq_doc.render_doctor_report(diag, color=False)
            self.assertIn("[PAUSED]", rendered)
            self.assertIn("Customer email", rendered)

            # Watchdog cycle stays silent while paused
            wd_paused = watchdog_mod.run_watchdog_cycle(state_dir=st_dir, ps_output="")
            self.assertEqual(wd_paused["status"], "paused")
            self.assertFalse(wd_paused["woke"])

            # Now resume: if wait-for-unlock IS running, watchdog delegates cleanly without spawning
            orch_ctrl.resume_orchestration(state_dir=st_dir, source="sidecar_panel", request_wake=True)
            ps_with_waiter = "9999 python3 pr_remediator.py wait-for-unlock --orchestrator-id 1234\n"
            wd_delegated = watchdog_mod.run_watchdog_cycle(state_dir=st_dir, ps_output=ps_with_waiter)
            self.assertEqual(wd_delegated["status"], "resume_delegated_to_waiter")
            self.assertFalse(wd_delegated["woke"])

            # If wait-for-unlock is NOT running, watchdog triggers force wake and stamps resume_handled_nonce
            spawned_cmds: list[list[str]] = []

            class _FakeProc:
                pid = 42424

                def poll(self) -> int | None:
                    return None

            def _fake_popen(cmd: list[str], **_kw: Any) -> _FakeProc:
                spawned_cmds.append(list(cmd))
                return _FakeProc()

            wd_resumed = watchdog_mod.run_watchdog_cycle(
                state_dir=st_dir,
                sync_git=False,
                ps_output="",
                env={"ANTIGRAVITY_LS_ADDRESS": "localhost:49343"},
                which_fn=lambda name: "/usr/bin/agentapi" if name == "agentapi" else None,
                popen_fn=_fake_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(wd_resumed["status"], "resumed_woke")
            self.assertTrue(wd_resumed["woke"])
            self.assertEqual(len(spawned_cmds), 1)
            ctrl_post = orch_ctrl.load_orchestration_control(state_dir=st_dir)
            self.assertFalse(orch_ctrl.has_unhandled_resume_request(ctrl_post))
            self.assertEqual(ctrl_post["resume_handled_by"], "watchdog")


class TestSidecarPanelAndTuiPauseControls(unittest.TestCase):
    """Verify Dev Queue Sidecar Panel pause/resume API + static assets and agy-queue TUI/CLI controls."""

    def test_sidecar_panel_http_pause_resume_and_static_contract(self) -> None:
        panel_mod = _load_module(PANEL_DIR / "server.py", "dev_queue_panel_pause_test")
        html_txt = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
        js_txt = (PANEL_DIR / "app.js").read_text(encoding="utf-8")
        css_txt = (PANEL_DIR / "styles.css").read_text(encoding="utf-8")
        manifest = json.loads((PANEL_DIR / "sidecar.json").read_text(encoding="utf-8"))
        manifest_entrypoints = [v.get("entrypoint") for v in manifest.get("ui_config", {}).get("views", [])]
        self.assertIn("SIDECAR_UI_ENTRYPOINT_SIDEBAR", manifest_entrypoints)
        self.assertIn("SIDECAR_UI_ENTRYPOINT_AUX_PANE", manifest_entrypoints)

        self.assertIn('id="dq-pause-btn"', html_txt)
        self.assertIn('id="dq-pause-banner"', html_txt)
        self.assertIn('data-action="toggle-pause"', html_txt)
        self.assertIn('data-action="resume-dev"', html_txt)
        self.assertNotIn("onclick=", html_txt.lower())
        self.assertNotIn("innerHTML", js_txt)
        self.assertIn("/api/orchestration/pause", js_txt)
        self.assertIn("/api/orchestration/resume", js_txt)
        self.assertEqual(css_txt.count("{"), css_txt.count("}"))

        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            # Write an active DEVELOPING lock so pausing enters "draining" phase first
            pr_rem.acquire_dev_pr_lock(
                conversation_id="66666666-6666-4666-8666-666666666666",
                action_id="ongoing-panel-test",
                branch_name="fix/20261124-090000-ongoing-66666666",
                branch_timestamp="20261124-090000",
                summary="Ongoing test",
                proposed_fix="Fix",
                repo_slug="acme/repo-a",
                portfolio_dir=st_dir,
            )
            mock_token = "mock-panel-pause-token-123"
            httpd = panel_mod.create_panel_server(
                host="127.0.0.1",
                port=0,
                ui_token=mock_token,
                state_dir=st_dir,
            )
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            try:
                # Auth required on pause/resume
                st_401, _ = _http_json(f"{base}/api/orchestration/pause", method="POST", body={})
                self.assertEqual(st_401, 401)

                # Agentapi routes remain strictly 403 Forbidden
                st_403, body_403 = _http_json(
                    f"{base}/_sidecar/send-message",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"message": "blocked"},
                )
                self.assertEqual(st_403, 403)
                self.assertEqual(body_403.get("error"), "read_only_sidecar")

                # Initial state is active
                st_get1, snap1 = _http_json(f"{base}/api/state", headers={"X-Sidecar-Token": mock_token})
                self.assertEqual(st_get1, 200)
                self.assertFalse(snap1["orchestration"]["paused"])
                self.assertEqual(snap1["orchestration"]["phase"], "active")
                etag1 = snap1["etag"]

                # Pause -> enters draining phase because 1 DEVELOPING lock is active
                st_pause, body_pause = _http_json(
                    f"{base}/api/orchestration/pause",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"reason": "Customer mail"},
                )
                self.assertEqual(st_pause, 200)
                self.assertTrue(body_pause["paused"])
                self.assertEqual(body_pause["orchestration"]["phase"], "draining")
                self.assertEqual(body_pause["orchestration"]["ongoing_count"], 1)

                # /api/state reflects new ETag & draining banner
                st_get2, snap2 = _http_json(
                    f"{base}/api/state?etag={etag1}",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_get2, 200)
                self.assertFalse(snap2["unchanged"])
                self.assertTrue(snap2["orchestration"]["paused"])
                self.assertIn("DRAINING", snap2["orchestration"]["banner_text"])

                # Resume -> unpaused with resume_nonce stamped
                st_res, body_res = _http_json(
                    f"{base}/api/orchestration/resume",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={},
                )
                self.assertEqual(st_res, 200)
                self.assertFalse(body_res["paused"])
                self.assertTrue(body_res["resume_nonce"])
                self.assertEqual(body_res["orchestration"]["phase"], "active")
            finally:
                httpd.shutdown()
                httpd.server_close()
                t.join(timeout=2.0)

    def test_tui_banner_footer_and_cli_subcommands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            # CLI pause -> status -> resume --no-wake
            buf = io.StringIO()
            orig_stdout = sys.stdout
            try:
                sys.stdout = buf
                rc_p = dq_tui.main(["pause", "--state-dir", str(st_dir), "--reason", "Customer sync", "--json"])
            finally:
                sys.stdout = orig_stdout
            self.assertEqual(rc_p, 0)
            parsed_p = json.loads(buf.getvalue())
            self.assertTrue(parsed_p["paused"])
            self.assertEqual(parsed_p["orchestration"]["phase"], "paused")

            # Rendered dashboard includes PAUSED banner and JSON includes orchestration block
            ns_txt = dq_tui._build_parser().parse_args(["--state-dir", str(st_dir), "--no-color"])
            layout_out: dict[str, Any] = {}
            out_txt, *_ = dq_tui.render(ns_txt, layout_out=layout_out)
            self.assertIn("PAUSED", out_txt)
            self.assertIn("[P] Continue", out_txt)
            self.assertTrue(layout_out.get("orchestration_paused"))

            ns_json = dq_tui._build_parser().parse_args(["--state-dir", str(st_dir), "--json"])
            out_json, *_ = dq_tui.render(ns_json)
            parsed_j = json.loads(out_json)
            self.assertTrue(parsed_j["orchestration"]["paused"])

            # Footer renders [PAUSED] badge and [P] resume hint
            footer = dq_tui._build_watch_footer(
                interval=3.0,
                hitl_count=0,
                compact_pending=False,
                show_all=False,
                live_filter="",
                filter_input_active=False,
                overlay_mode="",
                awaiting_hitl_digit=False,
                sync_in_flight=False,
                status_msg="",
                orchestration_paused=True,
            )
            self.assertIn("[PAUSED]", footer)
            self.assertIn("[P] resume", footer)

            # CLI status & resume --no-wake
            buf2 = io.StringIO()
            try:
                sys.stdout = buf2
                rc_s = dq_tui.main(["status", "--state-dir", str(st_dir), "--json"])
            finally:
                sys.stdout = orig_stdout
            self.assertEqual(rc_s, 0)
            self.assertTrue(json.loads(buf2.getvalue())["orchestration"]["paused"])

            buf3 = io.StringIO()
            try:
                sys.stdout = buf3
                rc_r = dq_tui.main(["resume", "--state-dir", str(st_dir), "--no-wake", "--json"])
            finally:
                sys.stdout = orig_stdout
            self.assertEqual(rc_r, 0)
            parsed_r = json.loads(buf3.getvalue())
            self.assertFalse(parsed_r["paused"])
            self.assertEqual(parsed_r["wake"]["status"], "skipped_no_wake")


class TestOrchestrationHardStopControls(unittest.TestCase):
    """Verify non-destructive Hard Stop (Hard Freeze) across control state, PreToolUse, review_gate, panel, and TUI."""

    def test_hard_stop_state_process_scoping_and_resume_reset(self) -> None:
        fake_ps = "\n".join(
            [
                "459 /opt/python3 -m api.server serve",
                "1018 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222",
                "1345 /Applications/Antigravity.app/Contents/Resources/bin/language_server --standalone",
                "29872 python3 dev_queue_tui.py -w",
                "50864 python3 /path/sidecars/dev-queue-panel/server.py",
                "50865 python3 watchdog.py",
                "55334 python3 pr_remediator.py watch-pr-merge --interval 15",
                "64602 python3 pr_remediator.py wait-for-unlock --orchestrator-id 1234",
                "77101 pytest /tmp/state/tmp/pr-worktree-demo-repo-fix-20261126-demo/tests/test_demo.py",
                "77102 python3 -m uvicorn --app-dir /tmp/state/tmp/dev_ui_preview api.app:app --port 8788",
                "77103 node /app/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js",
            ]
        )
        killed: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            # First pause in Graceful Drain, then escalate to Hard Stop
            p_res = orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                reason="Live demo prep",
                source="sidecar_panel",
                stop_chrome_devtools=False,
                now_ts=1763974800.0,
            )
            self.assertFalse(orch_ctrl.is_hard_stop_active(state_dir=st_dir))

            hs_res = orch_ctrl.hard_stop_orchestration(
                state_dir=st_dir,
                reason="Live demo /Users/alice/notes.md",
                source="sidecar_panel",
                active_locks={
                    "acme/demo-repo": {
                        "active": True,
                        "status": "DEVELOPING",
                        "worktree_path": "/tmp/state/tmp/pr-worktree-demo-repo-fix-20261126-demo",
                    }
                },
                ps_output=fake_ps,
                kill_fn=lambda pid, sig: killed.append((pid, sig)),
                now_ts=1763974860.0,
            )
            self.assertTrue(hs_res["paused"])
            self.assertTrue(hs_res["hard_stopped"])
            self.assertTrue(hs_res["changed"])
            self.assertEqual(hs_res["mode"], orch_ctrl.PAUSE_MODE_HARD_STOP)
            self.assertEqual(hs_res["control"]["paused_at"], p_res["control"]["paused_at"])
            self.assertTrue(hs_res["control"]["hard_stopped_at"])
            self.assertNotIn("/Users/alice", hs_res["control"]["reason"])
            self.assertTrue(orch_ctrl.is_hard_stop_active(state_dir=st_dir))

            # Only worktree/preview/chrome-devtools-mcp processes killed; prod api.server serve (459) and sidecars untouched
            self.assertEqual(hs_res["chrome_devtools"]["stopped_pids"], [77103])
            self.assertEqual(hs_res["worktree_processes"]["stopped_pids"], [77101, 77102])
            self.assertNotIn(459, [pid for pid, _ in killed])
            self.assertNotIn(50864, [pid for pid, _ in killed])
            self.assertNotIn(50865, [pid for pid, _ in killed])
            self.assertNotIn(55334, [pid for pid, _ in killed])
            self.assertNotIn(64602, [pid for pid, _ in killed])

            # Idempotent second hard-stop call sets changed=False
            hs_res2 = orch_ctrl.hard_stop_orchestration(
                state_dir=st_dir,
                source="cli",
                stop_chrome_devtools=False,
                stop_worktree_processes=False,
                now_ts=1763974900.0,
            )
            self.assertFalse(hs_res2["changed"])
            self.assertTrue(hs_res2["was_hard_stopped"])

            # Phase summary reports hard_stopped preserving ongoing count
            summary = orch_ctrl.summarize_orchestration_phase(
                hs_res["control"],
                {"acme/demo-repo": {"active": True, "status": "DEVELOPING", "pr_gate_ready": False}},
            )
            self.assertEqual(summary["phase"], "hard_stopped")
            self.assertTrue(summary["hard_stopped"])
            self.assertEqual(summary["ongoing_count"], 1)
            self.assertIn("HARD STOPPED", summary["banner_text"])
            self.assertIn("preserved", summary["banner_text"])

            # Resume resets mode back to graceful_drain & clears hard_stopped_at
            r_res = orch_ctrl.resume_orchestration(state_dir=st_dir, source="sidecar_panel", request_wake=False)
            self.assertFalse(r_res["paused"])
            self.assertFalse(orch_ctrl.is_hard_stop_active(state_dir=st_dir))
            self.assertEqual(r_res["control"]["mode"], orch_ctrl.PAUSE_MODE_GRACEFUL_DRAIN)
            self.assertEqual(r_res["control"]["hard_stopped_at"], "")

    def test_pretool_hard_stop_freezes_active_workers_and_worktrees_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st_dir = root / "state"
            st_dir.mkdir()
            wt_dir = st_dir / "tmp" / "pr-worktree-demo-repo-fix-20261126-demo"
            wt_dir.mkdir(parents=True)

            worker_cid = "aaaaaaaa-1111-4111-8111-111111111111"
            reviewer_cid = "bbbbbbbb-2222-4222-8222-222222222222"
            unrelated_cid = "cccccccc-3333-4333-8333-333333333333"

            pr_rem.acquire_dev_pr_lock(
                conversation_id="dddddddd-4444-4444-8444-444444444444",
                action_id="demo-hard-stop",
                branch_name="fix/20261126-100000-demo-dddddddd",
                branch_timestamp="20261126-100000",
                worktree_path=str(wt_dir),
                worker_cid=worker_cid,
                summary="Test worker",
                proposed_fix="Test fix",
                repo_slug="acme/demo-repo",
                portfolio_dir=st_dir,
            )
            pr_rem.update_dev_pr_lock(
                repo_slug="acme/demo-repo",
                updates={"reviewer_subagent_id": reviewer_cid},
                portfolio_dir=st_dir,
            )

            evaluator = dev_common.DevLifecycleGuardrailEvaluator(state_dir=st_dir)

            # In Graceful Drain mode, active worker commands remain allowed
            orch_ctrl.pause_orchestration(state_dir=st_dir, stop_chrome_devtools=False)
            allow_drain = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": worker_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "python3 -m unittest"},
                }
            )
            self.assertEqual(allow_drain["decision"], "allow")

            # Escalate to Hard Stop -> worker & reviewer & worktree writes/cmds are immediately denied
            orch_ctrl.hard_stop_orchestration(
                state_dir=st_dir,
                stop_chrome_devtools=False,
                stop_worktree_processes=False,
            )

            deny_worker = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": worker_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "python3 -m unittest"},
                }
            )
            self.assertEqual(deny_worker["decision"], "deny")
            self.assertEqual(deny_worker.get("check_id"), "pretool:hard-stop-active-workers")

            deny_reviewer = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": reviewer_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "git diff"},
                }
            )
            self.assertEqual(deny_reviewer["decision"], "deny")

            deny_wt_write = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": unrelated_cid,
                    "tool_name": "edit_file",
                    "tool_input": {"Path": str(wt_dir / "README.md")},
                }
            )
            self.assertEqual(deny_wt_write["decision"], "deny")

            deny_wt_cmd = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": unrelated_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": f"git -C {wt_dir} status"},
                }
            )
            self.assertEqual(deny_wt_cmd["decision"], "deny")

            # Unrelated user conversation doing normal work MUST remain 100% allowed
            allow_unrelated = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": unrelated_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "python3 -c 'print(42)'"},
                }
            )
            self.assertEqual(allow_unrelated["decision"], "allow")

            # Resume control commands are NEVER blocked (zero self-lockout)
            allow_resume_cmd = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": worker_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "agy-queue resume"},
                }
            )
            self.assertEqual(allow_resume_cmd["decision"], "allow")

            # After resume, worker commands are allowed again
            orch_ctrl.resume_orchestration(state_dir=st_dir, request_wake=False)
            allow_post = evaluator.evaluate_pre_tool_use(
                {
                    "conversationId": worker_cid,
                    "tool_name": "run_command",
                    "tool_input": {"CommandLine": "python3 -m unittest"},
                }
            )
            self.assertEqual(allow_post["decision"], "allow")

    def test_hard_stop_blocks_reuse_active_review_and_pause_shields_pro_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            st_dir = root / "state"
            st_dir.mkdir()
            repo_dir = _init_temp_git_repo(root, "acme/demo-repo")
            cid_ongoing = "77777777-7777-4777-8777-777777777777"

            res_init = pr_rem.start_remediation_branch_and_lock(
                conversation_id=cid_ongoing,
                action_id="ongoing-hard-stop",
                rca_summary="Ongoing work",
                proposed_fix="Fix",
                create_worktree=True,
                workdir=repo_dir,
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_init["status"], "ok")

            # Hard stop blocks --reuse-active, prepare-review, and record-review while preserving lock
            orch_ctrl.hard_stop_orchestration(
                state_dir=st_dir,
                stop_chrome_devtools=False,
                stop_worktree_processes=False,
            )
            res_reuse = pr_rem.start_remediation_branch_and_lock(
                conversation_id=cid_ongoing,
                action_id="ongoing-hard-stop",
                rca_summary="Retry",
                proposed_fix="Retry",
                create_worktree=True,
                reuse_active_branch=True,
                workdir=repo_dir,
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_reuse["status"], "hard_stopped")
            self.assertEqual(res_reuse["error"], "orchestration_hard_stopped")

            res_stage = pr_rem.push_remediation_branch_and_build_reviewer_plan(
                title="fix(demo): test",
                workdir=res_init["worktree_path"],
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_stage["status"], "hard_stopped")

            res_rev = pr_rem.record_pr_review_evaluation(
                verdict="PASS",
                score=95,
                summary="Looks good",
                workdir=res_init["worktree_path"],
                portfolio_dir=st_dir,
            )
            self.assertEqual(res_rev["status"], "hard_stopped")

            # Lock & worktree are strictly preserved
            lk_after = pr_rem.get_dev_pr_lock(portfolio_dir=st_dir, repo_slug="acme/demo-repo")
            self.assertTrue(lk_after.get("active"))
            self.assertTrue(Path(res_init["worktree_path"]).is_dir())

            # Releasing lock while paused MUST NOT call restart_pro_api_server
            restart_calls: list[int] = []
            orig_restart = pr_rem.restart_pro_api_server
            orig_stop_dev = pr_rem.stop_dev_api_server
            try:
                pr_rem.restart_pro_api_server = lambda **kw: (restart_calls.append(1) or {"status": "ok"})  # type: ignore[assignment]
                pr_rem.stop_dev_api_server = lambda **kw: {"status": "ok", "stopped": False}  # type: ignore[assignment]
                pr_rem.release_dev_pr_lock(
                    reason="DEMO_CLEANUP",
                    repo_slug="acme/demo-repo",
                    portfolio_dir=st_dir,
                )
            finally:
                pr_rem.restart_pro_api_server = orig_restart
                pr_rem.stop_dev_api_server = orig_stop_dev
            self.assertEqual(restart_calls, [])

    def test_sidecar_panel_hard_stop_endpoint_and_cli_stop_subcommand(self) -> None:
        panel_mod = _load_module(PANEL_DIR / "server.py", "dev_queue_panel_hard_stop_test")
        html_txt = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
        js_txt = (PANEL_DIR / "app.js").read_text(encoding="utf-8")
        css_txt = (PANEL_DIR / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="dq-banner-hard-stop-btn"', html_txt)
        self.assertIn('id="dq-hard-stop-confirm-row"', html_txt)
        self.assertIn('data-action="hard-stop-prompt"', html_txt)
        self.assertIn('data-action="hard-stop-confirm"', html_txt)
        self.assertIn("/api/orchestration/hard-stop", js_txt)
        self.assertNotIn("onclick=", html_txt.lower())
        self.assertNotIn("innerHTML", js_txt)
        self.assertEqual(css_txt.count("{"), css_txt.count("}"))

        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            pr_rem.acquire_dev_pr_lock(
                conversation_id="88888888-8888-4888-8888-888888888888",
                action_id="ongoing-hard-stop-panel",
                branch_name="fix/20261126-100000-panel-88888888",
                branch_timestamp="20261126-100000",
                summary="Ongoing panel task",
                proposed_fix="Fix",
                repo_slug="acme/repo-a",
                portfolio_dir=st_dir,
            )
            mock_token = "mock-panel-hard-stop-token-456"
            httpd = panel_mod.create_panel_server(
                host="127.0.0.1",
                port=0,
                ui_token=mock_token,
                state_dir=st_dir,
            )
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{port}"
            try:
                # 401 without token
                st_401, _ = _http_json(f"{base}/api/orchestration/hard-stop", method="POST", body={})
                self.assertEqual(st_401, 401)

                # Pause -> Draining
                st_p, body_p = _http_json(
                    f"{base}/api/orchestration/pause",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"reason": "Demo"},
                )
                self.assertEqual(st_p, 200)
                self.assertEqual(body_p["orchestration"]["phase"], "draining")

                # Hard Stop -> Hard Stopped
                st_hs, body_hs = _http_json(
                    f"{base}/api/orchestration/hard-stop",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"reason": "Live demo"},
                )
                self.assertEqual(st_hs, 200)
                self.assertTrue(body_hs["hard_stopped"])
                self.assertEqual(body_hs["orchestration"]["phase"], "hard_stopped")
                self.assertIn("HARD STOPPED", body_hs["orchestration"]["banner_text"])

                # /api/state reflects hard_stopped
                st_get, snap = _http_json(f"{base}/api/state", headers={"X-Sidecar-Token": mock_token})
                self.assertEqual(st_get, 200)
                self.assertEqual(snap["orchestration"]["phase"], "hard_stopped")
                self.assertTrue(snap["orchestration"]["hard_stopped"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                t.join(timeout=2.0)

            # TUI banner & footer for hard_stopped + CLI `agy-queue stop`
            ns_txt = dq_tui._build_parser().parse_args(["--state-dir", str(st_dir), "--no-color"])
            out_txt, *_ = dq_tui.render(ns_txt)
            self.assertIn("HARD STOPPED", out_txt)
            self.assertIn("⏹", out_txt)

            footer_hs = dq_tui._build_watch_footer(
                interval=3.0,
                hitl_count=0,
                compact_pending=False,
                show_all=False,
                live_filter="",
                filter_input_active=False,
                overlay_mode="",
                awaiting_hitl_digit=False,
                sync_in_flight=False,
                status_msg="",
                orchestration_paused=True,
                orchestration_phase="hard_stopped",
            )
            self.assertIn("[HARD STOP]", footer_hs)

            footer_dr = dq_tui._build_watch_footer(
                interval=3.0,
                hitl_count=0,
                compact_pending=False,
                show_all=False,
                live_filter="",
                filter_input_active=False,
                overlay_mode="",
                awaiting_hitl_digit=False,
                sync_in_flight=False,
                status_msg="",
                orchestration_paused=True,
                orchestration_phase="draining",
            )
            self.assertIn("[S] hard-stop", footer_dr)

            buf_stop = io.StringIO()
            orig_stdout = sys.stdout
            try:
                sys.stdout = buf_stop
                rc_stop = dq_tui.main(["stop", "--state-dir", str(st_dir), "--reason", "Demo", "--json"])
            finally:
                sys.stdout = orig_stdout
            self.assertEqual(rc_stop, 0)
            parsed_stop = json.loads(buf_stop.getvalue())
            self.assertTrue(parsed_stop["hard_stopped"])
            self.assertEqual(parsed_stop["orchestration"]["phase"], "hard_stopped")


if __name__ == "__main__":
    unittest.main()
