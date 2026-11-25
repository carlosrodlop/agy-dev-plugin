"""Hermetic Zero-PII unit tests for `sidecars/dev-queue-panel/` and `[h#]` `nav_intent` bridge."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
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
SIDECAR_DIR = REPO_ROOT / "sidecars" / "dev-queue-panel"
SERVER_SCRIPT = SIDECAR_DIR / "server.py"
SIDECAR_JSON = SIDECAR_DIR / "sidecar.json"
INDEX_HTML = SIDECAR_DIR / "index.html"
APP_JS = SIDECAR_DIR / "app.js"
STYLES_CSS = SIDECAR_DIR / "styles.css"
TUI_SCRIPT = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts" / "dev_queue_tui.py"
SKILL_MD = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md"
SELECTIVE_RUNNER = REPO_ROOT / "tests" / "scripts" / "selective_test_runner.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


panel_srv = _load_module("dev_queue_panel_server_under_test", SERVER_SCRIPT)
tui = _load_module("dev_queue_tui_sidecar_under_test", TUI_SCRIPT)
selective_runner = _load_module("selective_test_runner_under_test", SELECTIVE_RUNNER)


def _write_mock_conv_db(db_path: Path, titles: dict[str, str]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
        for cid, title in titles.items():
            conn.execute("INSERT OR REPLACE INTO conversation_summaries (conversation_id, title) VALUES (?, ?)", (cid, title))
        conn.commit()


def _write_mock_state(
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
                "updated_at": "2026-11-23T12:00:00Z",
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


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


class TestSidecarManifestAndFrontendContracts(unittest.TestCase):
    """Verify Antigravity 2.0 Sidecar manifest, Zero-Build frontend, SKILL.md, and router integrity."""

    def test_sidecar_json_aux_pane_manifest_contract(self) -> None:
        data = json.loads(SIDECAR_JSON.read_text(encoding="utf-8"))
        self.assertEqual(data.get("command"), "python3")
        self.assertEqual(data.get("args"), ["server.py"])
        self.assertTrue(data.get("has_web_ui"))
        self.assertEqual(data.get("restart_policy"), "always")
        self.assertEqual(data.get("agent_permissions", {}).get("allows_user_interactions"), False)
        views = data.get("ui_config", {}).get("views", [])
        entrypoints = [v.get("entrypoint") for v in views]
        self.assertIn("SIDECAR_UI_ENTRYPOINT_AUX_PANE", entrypoints)
        self.assertIn("SIDECAR_UI_ENTRYPOINT_SIDEBAR", entrypoints)
        for v in views:
            self.assertEqual(v.get("path"), "/")

    def test_vanilla_frontend_security_a11y_and_sdk_usage(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLES_CSS.read_text(encoding="utf-8")

        self.assertIn('<script src="/preload.js"></script>', html)
        self.assertIn('<script src="/app.js" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/styles.css">', html)
        self.assertNotIn("onclick=", html.lower())
        self.assertNotIn("javascript:", html.lower())
        # Verify distinct Blocked vs PR Ready chip labels & explanatory tooltips
        self.assertIn('data-section-filter="hitl_blocked"', html)
        self.assertIn('<span class="dq-kpi-label">Blocked</span>', html)
        self.assertIn("Blocked before PR:", html)
        self.assertIn('<span class="dq-kpi-label">PR Ready</span>', html)
        self.assertIn("ready for manual GitHub PR review and merge", html)
        self.assertIn("Blocked · Needs Input", html)
        self.assertIn("Open Blocked Chat", js)
        self.assertIn("Answer Chat", js)
        self.assertIn("Review Prompt", js)
        self.assertIn("QUESTION (", js)
        self.assertIn("APPROVAL", js)
        self.assertIn("dq-question-preview", js)
        self.assertIn(".dq-badge-question", css)
        self.assertIn(".dq-badge-approval", css)
        self.assertEqual(
            len(re.findall(r'data-section-filter="[^"]+"', html)),
            6,
            "compact 6-pill header must be preserved",
        )

        ids = re.findall(r'\bid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), "Duplicate HTML IDs found in index.html")

        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
            self.assertNotIn(unsafe_sink, js)
        self.assertIn('"use strict"', js)
        self.assertIn("window.sidecar.ui.toggleConversation", js)
        self.assertIn("window.sidecar.ui.toggleAuxPane", js)
        self.assertIn("fileView", js)
        self.assertIn("/api/nav-intent/ack", js)
        self.assertIn("/api/artifact-uri", js)
        self.assertIn("/api/cleanup/preview", js)
        self.assertIn("/api/cleanup/execute", js)
        self.assertIn('id="dq-clean-btn"', html)
        self.assertIn('id="dq-cleanup-panel"', html)

        self.assertEqual(css.count("{"), css.count("}"), "Unbalanced CSS braces in styles.css")
        self.assertIn(":root", css)
        self.assertIn('[data-theme="dark"]', css)
        self.assertIn("prefers-reduced-motion", css)

    def test_skill_md_documents_sidecar_pill_under_24kb_and_router_integrity(self) -> None:
        skill_bytes = SKILL_MD.read_bytes()
        self.assertLess(len(skill_bytes), 24000)
        skill_text = skill_bytes.decode("utf-8")
        self.assertIn("sidecar://agy-dev-plugin/dev-queue-panel/", skill_text)
        self.assertIn("dev_queue_nav_intent.json", skill_text)
        self.assertEqual(selective_runner.audit_router_integrity(REPO_ROOT), [])
        selected = selective_runner.select_test_modules(["sidecars/dev-queue-panel/server.py"])
        self.assertIn("tests.test_dev_queue_sidecar_panel", selected)


class TestSidecarServerSecurityAndState(unittest.TestCase):
    """Verify loopback binding, token fail-closed gate, Zero-PII scrubbing, artifact URI, and HTTP endpoints."""

    def test_loopback_port_and_token_validation_helpers(self) -> None:
        self.assertEqual(panel_srv.validate_bind_host("127.0.0.1"), "127.0.0.1")
        for bad_host in ("0.0.0.0", "::", "localhost", "192.168.1.10", ""):
            with self.assertRaises(ValueError):
                panel_srv.validate_bind_host(bad_host)

        with self.assertRaises(ValueError):
            panel_srv.resolve_web_port(None, env={})
        with self.assertRaises(ValueError):
            panel_srv.resolve_web_port(None, env={"ANTIGRAVITY_SIDECAR_WEB_PORT": "bad"})
        with self.assertRaises(ValueError):
            panel_srv.resolve_web_port(70000, env={})
        self.assertEqual(panel_srv.resolve_web_port(None, env={"ANTIGRAVITY_SIDECAR_WEB_PORT": "41923"}), 41923)

        mock_token = "mock-sidecar-ui-token-example"
        self.assertFalse(panel_srv.validate_sidecar_token("", header_token=mock_token))
        self.assertFalse(panel_srv.validate_sidecar_token(mock_token))
        self.assertFalse(panel_srv.validate_sidecar_token(mock_token, header_token="wrong-mock-token"))
        self.assertTrue(panel_srv.validate_sidecar_token(mock_token, header_token=mock_token))
        self.assertTrue(panel_srv.validate_sidecar_token(mock_token, auth_header=f"Bearer {mock_token}"))
        self.assertTrue(panel_srv.validate_sidecar_token(mock_token, query_token=mock_token))

    def test_zero_pii_scrubbing_and_preload_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake_home"
            sdk_preload = fake_home / ".gemini" / "antigravity" / "lib" / "node_modules" / "sidecar_sdk" / "preload.js"
            sdk_preload.parent.mkdir(parents=True, exist_ok=True)
            sdk_preload.write_text("window.__mockSdkPreload = true;\n", encoding="utf-8")

            loaded = panel_srv.resolve_preload_js(env={}, home_dir=fake_home)
            self.assertIn("__mockSdkPreload", loaded)

            empty_home = Path(tmp) / "empty_home"
            fallback = panel_srv.resolve_preload_js(env={}, home_dir=empty_home)
            self.assertIn("toggleConversation", fallback)

            mock_real_user_path = f"{os.sep}Users{os.sep}realperson{os.sep}secret"
            mock_home_alice_path = f"{os.sep}home{os.sep}alice{os.sep}work"
            mock_testuser_path = f"{os.sep}Users{os.sep}testuser{os.sep}sandbox"
            scrubbed = panel_srv.sanitize_zero_pii_payload(
                {
                    "msg": f"error in {fake_home}/repo and {mock_real_user_path} and {mock_home_alice_path}",
                    "safe_test": mock_testuser_path,
                },
                home_dir=fake_home,
            )
            self.assertNotIn("realperson", scrubbed["msg"])
            self.assertNotIn("alice", scrubbed["msg"])
            self.assertIn("$HOME/repo", scrubbed["msg"])
            self.assertEqual(scrubbed["safe_test"], mock_testuser_path)

    def test_http_server_endpoints_auth_readonly_etag_artifacts_and_nav_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            brain_dir = root / "brain"
            conv_db = root / "conversation_summaries.db"
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)

            hitl_cid = "aaaaaaaa-1111-4222-8333-444444444444"
            active_cid = "bbbbbbbb-1111-4222-8333-444444444444"
            worker_cid = "cccccccc-1111-4222-8333-444444444444"
            orch_cid = "11111111-2222-4333-8444-555555555555"

            _write_mock_conv_db(
                conv_db,
                {
                    hitl_cid: "AcmeCorp HITL Clarification",
                    active_cid: "Globex Active Implementation",
                    orch_cid: "Main Dev Queue Orchestrator",
                },
            )
            _write_mock_state(
                st_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": hitl_cid,
                        "priority": 10,
                        "status": "hitl_blocked",
                        "title": "Blocked on API scope",
                        "notes": "Choose option A or B",
                        "repo_slugs": ["acme-org/acme-repo"],
                    },
                    {
                        "conversation_id": active_cid,
                        "priority": 20,
                        "status": "in_progress",
                        "title": "Implement widget",
                        "repo_slugs": ["acme-org/globex-repo"],
                    },
                ],
                locks={
                    "acme-org/globex-repo": {
                        "active": True,
                        "slot_id": 1,
                        "conversation_id": active_cid,
                        "worker_cid": worker_cid,
                        "status": "DEVELOPING",
                        "stage_label": "coding",
                        "branch_name": "fix/20261123-120000-widget-bbbbbbbb",
                        "last_activity_at": "2026-11-23T12:00:00Z",
                    }
                },
            )

            conv_brain = brain_dir / hitl_cid
            conv_brain.mkdir(parents=True, exist_ok=True)
            (conv_brain / "implementation_plan.md").write_text("# Plan\n", encoding="utf-8")
            (conv_brain / "ignored.txt").write_text("not md", encoding="utf-8")
            outside_md = root / "outside_secret.md"
            outside_md.write_text("# Outside\n", encoding="utf-8")
            (conv_brain / "escape.md").symlink_to(outside_md)

            board_md = root / "dev_queue_board.md"
            board_md.write_text("# Board\n", encoding="utf-8")

            mock_token = "mock-panel-auth-token-000000"
            httpd = panel_srv.create_panel_server(
                port=0,
                ui_token=mock_token,
                state_dir=st_dir,
                conv_db_path=conv_db,
                brain_dir=brain_dir,
                board_override=str(board_md),
                home_dir=fake_home,
                env={},
            )
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}"

                # Static assets & CSP headers
                for asset_path in ("/", "/index.html", "/styles.css", "/app.js", "/preload.js"):
                    st, hdrs, _body = _http_request(f"{base}{asset_path}")
                    self.assertEqual(st, 200)
                    self.assertEqual(hdrs.get("Cache-Control"), "no-store")
                    self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff")
                    self.assertIn("default-src 'self'", hdrs.get("Content-Security-Policy", ""))

                # Fail-closed 401 without or with invalid token
                st_unauth, _, _ = _http_request(f"{base}/api/state")
                self.assertEqual(st_unauth, 401)
                st_bad, _, _ = _http_request(f"{base}/api/state", headers={"X-Sidecar-Token": "bad-mock-token"})
                self.assertEqual(st_bad, 401)

                # Read-only guard against agentapi mutation routes
                for mut_route in ("/_sidecar/send-message", "/_sidecar/new-conversation"):
                    st_forbid, _, body_forbid = _http_request(
                        f"{base}{mut_route}",
                        method="POST",
                        headers={"X-Sidecar-Token": mock_token},
                        body={"message": "nope"},
                    )
                    self.assertEqual(st_forbid, 403)
                    self.assertEqual(json.loads(body_forbid).get("error"), "read_only_sidecar")

                # Metadata route expected by preload.js
                st_meta, _, body_meta = _http_request(
                    f"{base}/_sidecar/get-conversation-metadata",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"conversationId": hitl_cid},
                )
                self.assertEqual(st_meta, 200)
                self.assertEqual(json.loads(body_meta), {"response": {}})

                # Full snapshot + ETag unchanged
                st_ok, _, body_ok = _http_request(
                    f"{base}/api/state",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_ok, 200)
                snap = json.loads(body_ok)
                self.assertTrue(snap["ok"])
                self.assertFalse(snap["unchanged"])
                self.assertEqual(len(snap["sections"]["hitl_blocked"]), 1)
                hitl_card = snap["sections"]["hitl_blocked"][0]
                self.assertEqual(hitl_card["hotkey"], "h1")
                self.assertEqual(hitl_card["sidebar_title"], "AcmeCorp HITL Clarification")
                self.assertEqual(hitl_card["artifacts"], [{"filename": "implementation_plan.md", "label": "implementation_plan.md"}])
                self.assertTrue(snap["board"]["available"])

                st_304, _, body_304 = _http_request(
                    f"{base}/api/state?etag={snap['etag']}",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_304, 200)
                self.assertTrue(json.loads(body_304)["unchanged"])

                # Artifact URI allowlist & symlink/traversal blocks
                st_art, _, body_art = _http_request(
                    f"{base}/api/artifact-uri",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"kind": "artifact", "conversation_id": hitl_cid, "filename": "implementation_plan.md"},
                )
                self.assertEqual(st_art, 200)
                self.assertTrue(json.loads(body_art)["uri"].startswith("file://"))

                st_brd, _, body_brd = _http_request(
                    f"{base}/api/artifact-uri",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"kind": "board"},
                )
                self.assertEqual(st_brd, 200)
                self.assertEqual(json.loads(body_brd)["label"], "dev_queue_board.md")

                for bad_art in (
                    {"kind": "artifact", "conversation_id": hitl_cid, "filename": "../outside_secret.md"},
                    {"kind": "artifact", "conversation_id": hitl_cid, "filename": "escape.md"},
                    {"kind": "artifact", "conversation_id": hitl_cid, "filename": "ignored.txt"},
                    {"kind": "artifact", "conversation_id": "not-a-uuid", "filename": "implementation_plan.md"},
                ):
                    st_bad_art, _, _ = _http_request(
                        f"{base}/api/artifact-uri",
                        method="POST",
                        headers={"X-Sidecar-Token": mock_token},
                        body=bad_art,
                    )
                    self.assertEqual(st_bad_art, 404)

                # Write nav_intent, inspect via /api/state, acknowledge via POST /api/nav-intent/ack
                intent = tui.write_nav_intent(
                    st_dir,
                    conversation_id=hitl_cid,
                    display_id=hitl_cid[:8],
                    sidebar_title="AcmeCorp HITL Clarification",
                    hotkey="h1",
                )
                st_nav, _, body_nav = _http_request(
                    f"{base}/api/nav-intent",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_nav, 200)
                self.assertEqual(json.loads(body_nav)["nav_intent"]["intent_id"], intent["intent_id"])

                st_ack_bad, _, _ = _http_request(
                    f"{base}/api/nav-intent/ack",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"intent_id": "wrong-id"},
                )
                self.assertEqual(st_ack_bad, 409)

                st_ack_ok, _, body_ack_ok = _http_request(
                    f"{base}/api/nav-intent/ack",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"intent_id": intent["intent_id"]},
                )
                self.assertEqual(st_ack_ok, 200)
                self.assertTrue(json.loads(body_ack_ok)["acknowledged"])

                st_nav_after, _, body_nav_after = _http_request(
                    f"{base}/api/nav-intent",
                    headers={"X-Sidecar-Token": mock_token},
                )
                self.assertEqual(st_nav_after, 200)
                self.assertIsNone(json.loads(body_nav_after)["nav_intent"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

    def test_empty_server_token_fails_closed_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = panel_srv.create_panel_server(port=0, ui_token="", state_dir=tmp, env={})
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                st, _, _ = _http_request(f"http://127.0.0.1:{port}/api/state")
                self.assertEqual(st, 401)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

    def test_http_cleanup_preview_and_execute_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            agy_dir = root / "agy"
            brain_dir = agy_dir / "brain"
            convs_dir = agy_dir / "conversations"
            brain_dir.mkdir(parents=True, exist_ok=True)
            convs_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)

            merged_cid = "81111111-1111-4111-8111-111111111111"
            active_cid = "82222222-2222-4222-8222-222222222222"
            old_ts = time.time() - 7200.0
            for cid in (merged_cid, active_cid):
                db_p = convs_dir / f"{cid}.db"
                db_p.write_bytes(b"sqlite-stub-" + cid.encode("utf-8"))
                os.utime(db_p, (old_ts, old_ts))

            ev_old = agy_dir / "sidecar_data" / "demo-panel" / "events" / "old.json"
            ev_old.parent.mkdir(parents=True, exist_ok=True)
            ev_old.write_text('{"ok":true}\n', encoding="utf-8")
            os.utime(ev_old, (time.time() - 30 * 3600.0, time.time() - 30 * 3600.0))

            _write_mock_state(
                st_dir,
                items=[
                    {"conversation_id": merged_cid, "priority": 20, "status": "merged", "title": "Merged item"},
                    {"conversation_id": active_cid, "priority": 10, "status": "pending", "title": "Pending item"},
                ],
                locks={},
            )

            mock_token = "mock-panel-cleanup-token-0000"
            httpd = panel_srv.create_panel_server(
                port=0,
                ui_token=mock_token,
                state_dir=st_dir,
                brain_dir=brain_dir,
                home_dir=fake_home,
                env={},
            )
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{port}"
                # Fail-closed 401 without token
                for route in ("/api/cleanup/preview", "/api/cleanup/execute"):
                    st_u, _, _ = _http_request(f"{base}{route}", method="POST", body={})
                    self.assertEqual(st_u, 401)
                    st_b, _, _ = _http_request(
                        f"{base}{route}",
                        method="POST",
                        headers={"X-Sidecar-Token": "wrong-token"},
                        body={},
                    )
                    self.assertEqual(st_b, 401)

                # Dry-run preview returns breakdown without deleting
                st_prev, _, body_prev = _http_request(
                    f"{base}/api/cleanup/preview",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"include_unaudited_check": False},
                )
                self.assertEqual(st_prev, 200)
                prev_data = json.loads(body_prev)
                self.assertTrue(prev_data["ok"])
                self.assertTrue(prev_data["dry_run"])
                self.assertEqual(prev_data["deleted"], [merged_cid])
                self.assertEqual(prev_data["sidecar_events_pruned_count"], 1)
                self.assertTrue((convs_dir / f"{merged_cid}.db").is_file())
                self.assertTrue(ev_old.is_file())

                # Execute deletes merged + old sidecar event while keeping active_cid
                st_exec, _, body_exec = _http_request(
                    f"{base}/api/cleanup/execute",
                    method="POST",
                    headers={"X-Sidecar-Token": mock_token},
                    body={"include_unaudited_check": False},
                )
                self.assertEqual(st_exec, 200)
                exec_data = json.loads(body_exec)
                self.assertTrue(exec_data["ok"])
                self.assertFalse(exec_data["dry_run"])
                self.assertEqual(exec_data["deleted"], [merged_cid])
                self.assertEqual(exec_data["sidecar_events_pruned_count"], 1)
                self.assertFalse((convs_dir / f"{merged_cid}.db").exists())
                self.assertFalse(ev_old.exists())
                self.assertTrue((convs_dir / f"{active_cid}.db").is_file())
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)


def _panel_n_slot_lock(n_slots: int, *, max_slots: int | None, cid_prefix: str) -> dict[str, object]:
    """Build a hermetic N-slot lock: Slot #1 mirrored at top level, Slots #2..#N `READY_AWAITING_BASE_MERGE`."""
    slots: dict[str, dict[str, object]] = {}
    for s_id in range(1, n_slots + 1):
        slots[str(s_id)] = {
            "slot_id": s_id,
            "active": True,
            "status": "DEVELOPING" if s_id == 1 else "READY_AWAITING_BASE_MERGE",
            "branch_name": f"fix/20261123-1{s_id}0000-task-s{s_id}",
            "conversation_id": f"{cid_prefix}{s_id}11111-1111-4222-8333-444444444444",
            "landing_order": s_id,
            "overlap_tier": "disjoint" if s_id == 1 else "same_file_disjoint_sections",
            "last_activity_at": "2026-11-23T12:00:00Z",
        }
    lock: dict[str, object] = {
        "active": True,
        "slot_id": 1,
        **{k: v for k, v in slots["1"].items() if k != "slot_id"},
        "slots": slots,
    }
    if max_slots is not None:
        lock["max_slots"] = max_slots
    return lock


class TestSidecarPanelNSlots(unittest.TestCase):
    """Verify `[active/max]` occupancy (default 3, cap 4), sorted `S1..S4` slot rows, and legacy 2-slot compat."""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("AGY_DEV_MAX_SLOTS_PER_REPO", None)

    def test_resolve_panel_max_slots_clamp(self) -> None:
        self.assertEqual(panel_srv._resolve_panel_max_slots(None), 3)
        self.assertEqual(panel_srv._resolve_panel_max_slots(2), 2)
        self.assertEqual(panel_srv._resolve_panel_max_slots(7), 4)

    def test_build_panel_state_three_four_legacy_and_free_repo_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            four_lock = _panel_n_slot_lock(4, max_slots=4, cid_prefix="bb")
            # Shuffle slot insertion order to prove the panel sorts by `slot_id`.
            four_lock["slots"] = dict(reversed(list(four_lock["slots"].items())))  # type: ignore[union-attr]
            _write_mock_state(
                st_dir,
                items=[
                    {
                        "conversation_id": "dddddddd-1111-4222-8333-444444444444",
                        "priority": 30,
                        "status": "pending",
                        "title": "Queued follow-up",
                        "repo_slugs": ["owner/repo-free"],
                    }
                ],
                locks={
                    "owner/repo-three": _panel_n_slot_lock(3, max_slots=None, cid_prefix="aa"),
                    "owner/repo-four": four_lock,
                    "owner/repo-legacy": {
                        "active": True,
                        "status": "DEVELOPING",
                        "branch_name": "fix/20261123-110000-legacy-s1",
                        "conversation_id": "ee111111-1111-4222-8333-444444444444",
                        "last_activity_at": "2026-11-23T12:00:00Z",
                        "slots": {
                            "2": {
                                "active": True,
                                "status": "READY_AWAITING_BASE_MERGE",
                                "branch_name": "fix/20261123-120000-legacy-s2",
                                "conversation_id": "ee222222-1111-4222-8333-444444444444",
                            }
                        },
                    },
                },
            )
            snap = panel_srv.build_panel_state(
                state_dir=st_dir,
                conv_db_path=root / "missing.db",
                brain_dir=root / "brain",
                home_dir=fake_home,
                env={},
            )
            cards = {c["slug"]: c for c in snap["repos"]}

            three = cards["owner/repo-three"]
            self.assertEqual(three["slot_occupancy"], "3/3")
            self.assertEqual(three["max_slots"], 3)
            self.assertEqual(three["active_slots_count"], 3)
            self.assertEqual([s["slot_label"] for s in three["slots"]], ["S1", "S2", "S3"])

            four = cards["owner/repo-four"]
            self.assertEqual(four["slot_occupancy"], "4/4")
            self.assertEqual([s["slot_id"] for s in four["slots"]], [1, 2, 3, 4])
            self.assertEqual([s["landing_order"] for s in four["slots"]], [1, 2, 3, 4])
            self.assertEqual(four["slots"][3]["status"], "READY_AWAITING_BASE_MERGE")
            self.assertEqual(four["slots"][3]["overlap_tier"], "same_file_disjoint_sections")
            self.assertFalse(any(s["is_human_ready"] for s in four["slots"][1:]), "only Slot #1 may be PR-ready")

            legacy = cards["owner/repo-legacy"]
            self.assertEqual(legacy["slot_occupancy"], "2/3")
            self.assertEqual([s["slot_label"] for s in legacy["slots"]], ["S1", "S2"])
            self.assertEqual(legacy["slots"][1]["landing_order"], 2)

            free = cards["owner/repo-free"]
            self.assertFalse(free["locked"])
            self.assertEqual(free["slot_occupancy"], "0/3")
            self.assertEqual(free["max_slots"], 3)

    def test_app_js_renders_dynamic_slot_occupancy_without_hardcoded_two(self) -> None:
        js = APP_JS.read_text(encoding="utf-8")
        self.assertNotIn('"1/2"', js)
        self.assertIn("DEFAULT_MAX_REPO_SLOTS = 3", js)
        self.assertIn("MAX_REPO_SLOTS_CAP = 4", js)
        self.assertIn("function sortedRepoSlots(", js)
        self.assertIn("function repoSlotOccupancy(", js)
        self.assertIn("slot.slot_label", js)

class TestNavIntentBridgeInTui(unittest.TestCase):
    """Verify `0600` atomic `dev_queue_nav_intent.json` lifecycle and `--open-hitl` CLI wiring."""

    def test_nav_intent_0600_ttl_ack_and_trigger_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            st_dir.mkdir(parents=True, exist_ok=True)
            cid = "aaaaaaaa-1111-4222-8333-444444444444"
            now = time.time()

            intent = tui.write_nav_intent(
                st_dir,
                conversation_id=cid,
                display_id=cid[:8],
                sidebar_title="AcmeCorp Review",
                hotkey="h1",
                ttl_seconds=30,
                now_ts=now,
            )
            nav_path = st_dir / tui.NAV_INTENT_FILENAME
            self.assertTrue(nav_path.is_file())
            self.assertEqual(stat.S_IMODE(nav_path.stat().st_mode), 0o600)

            active = tui.read_active_nav_intent(st_dir, now_ts=now + 5)
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active["intent_id"], intent["intent_id"])

            # Expired past TTL
            self.assertIsNone(tui.read_active_nav_intent(st_dir, now_ts=now + 45))

            # Acknowledge and confirm no longer returned as active
            ack_res = tui.acknowledge_nav_intent(st_dir, intent["intent_id"], now_ts=now + 6)
            self.assertTrue(ack_res["acknowledged"])
            self.assertEqual(stat.S_IMODE(nav_path.stat().st_mode), 0o600)
            self.assertIsNone(tui.read_active_nav_intent(st_dir, now_ts=now + 7))

            # Trigger navigation with unacknowledged fallback calls clipboard + focus
            with (
                mock.patch.object(tui, "_focus_antigravity_app", return_value=True) as m_focus,
                mock.patch.object(tui, "_copy_to_clipboard", return_value=True) as m_copy,
            ):
                res = tui._trigger_hitl_navigation(
                    st_dir,
                    index=1,
                    cid_disp=cid[:8],
                    cid_full=cid,
                    ag_title="AcmeCorp Review",
                    ack_wait_seconds=0.0,
                )
            self.assertTrue(res["intent_written"])
            self.assertFalse(res["acknowledged"])
            self.assertTrue(res["copied"])
            self.assertTrue(res["focused"])
            m_focus.assert_called_once()
            m_copy.assert_called_once_with("AcmeCorp Review")

    def test_open_hitl_cli_writes_nav_intent_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            cid = "dddddddd-1111-4222-8333-444444444444"
            _write_mock_state(
                st_dir,
                items=[
                    {
                        "conversation_id": cid,
                        "priority": 10,
                        "status": "hitl_blocked",
                        "title": "Confirm schema",
                        "repo_slugs": ["acme-org/acme-repo"],
                    }
                ],
            )
            with (
                mock.patch.object(tui, "_focus_antigravity_app", return_value=True),
                mock.patch.object(tui, "_copy_to_clipboard", return_value=True),
                mock.patch.dict(os.environ, {"AGY_NAV_INTENT_ACK_WAIT_SECONDS": "0"}),
            ):
                rc = tui.main(["--state-dir", str(st_dir), "--open-hitl", "h1"])
            self.assertEqual(rc, 0)
            nav_path = st_dir / tui.NAV_INTENT_FILENAME
            self.assertTrue(nav_path.is_file())
            self.assertEqual(stat.S_IMODE(nav_path.stat().st_mode), 0o600)
            data = json.loads(nav_path.read_text(encoding="utf-8"))
            self.assertEqual(data["conversation_id"], cid)
            self.assertEqual(data["hotkey"], "h1")


def _panel_pb_varint(val: int) -> bytes:
    out = bytearray()
    n = int(val)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _panel_pb_bytes(field_num: int, payload: bytes) -> bytes:
    return _panel_pb_varint((field_num << 3) | 2) + _panel_pb_varint(len(payload)) + payload


def _panel_raw_summary_field8(*, tool_name: str, questions: list[str] | None = None) -> bytes:
    f4 = _panel_pb_bytes(2, tool_name.encode("utf-8"))
    f1_parts = [_panel_pb_bytes(5, _panel_pb_bytes(4, f4))]
    if questions:
        q_msgs = b"".join(_panel_pb_bytes(1, _panel_pb_bytes(1, q.encode("utf-8"))) for q in questions)
        f1_parts.append(_panel_pb_bytes(56, _panel_pb_bytes(22, q_msgs)))
    return _panel_pb_bytes(1, b"hdr") + _panel_pb_bytes(8, _panel_pb_bytes(1, b"".join(f1_parts)))


class TestSidecarLiveWaitingPrompts(unittest.TestCase):
    """Verify deduplication, live_prompt synthesis, Zero-PII scrubbing, and Planning isolation."""

    def test_build_panel_state_surfaces_deduped_live_prompts_without_inflating_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            agy_dir = root / "agy"
            brain_dir = agy_dir / "brain"
            ann_dir = agy_dir / "annotations"
            conv_db = agy_dir / "conversation_summaries.db"
            fake_home = root / "home"
            brain_dir.mkdir(parents=True, exist_ok=True)
            ann_dir.mkdir(parents=True, exist_ok=True)
            fake_home.mkdir(parents=True, exist_ok=True)

            hitl_cid = "aaaaaaaa-1111-4222-8333-444444444444"
            live_ask_cid = "bbbbbbbb-1111-4222-8333-444444444444"
            live_app_cid = "cccccccc-1111-4222-8333-444444444444"
            pending_cid = "dddddddd-1111-4222-8333-444444444444"
            arch_cid = "eeeeeeee-1111-4222-8333-444444444444"

            (ann_dir / f"{arch_cid}.pbtxt").write_text("archived: true\n", encoding="utf-8")

            with sqlite3.connect(str(conv_db)) as conn:
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
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            hitl_cid,
                            "CASCADE_RUN_STATUS_RUNNING",
                            "Existing HITL Queue Task",
                            "",
                            0,
                            "2026-11-25T10:03:00Z",
                            _panel_raw_summary_field8(
                                tool_name="ask_question",
                                questions=["Approve existing HITL design?"],
                            ),
                        ),
                        (
                            live_ask_cid,
                            "CASCADE_RUN_STATUS_RUNNING",
                            "Ad-hoc Question Chat",
                            "",
                            0,
                            "2026-11-25T10:04:00Z",
                            _panel_raw_summary_field8(
                                tool_name="ask_question",
                                questions=[
                                    f"Proceed in {os.sep}Users{os.sep}realperson{os.sep}work{os.sep}app?",
                                    "Enable feature flag?",
                                ],
                            ),
                        ),
                        (
                            live_app_cid,
                            "CASCADE_RUN_STATUS_RUNNING",
                            "Ad-hoc Shell Approval",
                            "",
                            0,
                            "2026-11-25T10:02:00Z",
                            _panel_raw_summary_field8(tool_name="run_command"),
                        ),
                        (
                            arch_cid,
                            "CASCADE_RUN_STATUS_RUNNING",
                            "Archived Prompt",
                            "",
                            0,
                            "2026-11-25T10:05:00Z",
                            _panel_raw_summary_field8(tool_name="run_command"),
                        ),
                    ],
                )
                conn.commit()

            _write_mock_state(
                st_dir,
                items=[
                    {
                        "conversation_id": hitl_cid,
                        "priority": 10,
                        "status": "hitl_blocked",
                        "title": "HITL queue item",
                        "repo_slugs": ["acme/repo-a"],
                    },
                    {
                        "conversation_id": pending_cid,
                        "priority": 30,
                        "status": "pending",
                        "title": "Pending queue item",
                        "repo_slugs": ["acme/repo-a"],
                    },
                ],
                locks={},
            )
            queue_before = (st_dir / "dev_queue.json").read_text(encoding="utf-8")

            snap = panel_srv.build_panel_state(
                state_dir=st_dir,
                conv_db_path=conv_db,
                brain_dir=brain_dir,
                home_dir=fake_home,
                env={},
            )

            # Read-only guarantee: dev_queue.json never mutated
            self.assertEqual((st_dir / "dev_queue.json").read_text(encoding="utf-8"), queue_before)

            # Planning counts strictly isolated (only 2 queue items: hitl_cid + pending_cid)
            self.assertEqual(snap["planning"]["totals"]["open_count"], 2)
            self.assertEqual(snap["counts"]["hitl_blocked"], 1)

            # sec_hitl deduplicates hitl_cid (enriched) + appends 2 live_prompt cards with unified h1..h3
            sec_hitl = snap["sections"]["hitl_blocked"]
            self.assertEqual(len(sec_hitl), 3)
            self.assertEqual([c["hotkey"] for c in sec_hitl], ["h1", "h2", "h3"])
            self.assertEqual([c["conversation_id"] for c in sec_hitl], [hitl_cid, live_ask_cid, live_app_cid])

            enriched = sec_hitl[0]
            self.assertEqual(enriched["input_source"], "hitl_blocked")
            self.assertTrue(enriched["waiting_input"])
            self.assertEqual(enriched["waiting_kind"], "ask_question")
            self.assertEqual(enriched["question_count"], 1)

            synth_ask = sec_hitl[1]
            self.assertEqual(synth_ask["input_source"], "live_prompt")
            self.assertEqual(synth_ask["waiting_kind"], "ask_question")
            self.assertEqual(synth_ask["question_count"], 2)
            self.assertNotIn("realperson", synth_ask["question_preview"])
            self.assertIn("$HOME/work/app", synth_ask["question_preview"])

            synth_app = sec_hitl[2]
            self.assertEqual(synth_app["input_source"], "live_prompt")
            self.assertEqual(synth_app["waiting_kind"], "approval")
            self.assertEqual(synth_app["waiting_tool"], "run_command")
