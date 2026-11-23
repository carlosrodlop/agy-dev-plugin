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
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0].get("entrypoint"), "SIDECAR_UI_ENTRYPOINT_AUX_PANE")
        self.assertEqual(views[0].get("path"), "/")

    def test_vanilla_frontend_security_a11y_and_sdk_usage(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        css = STYLES_CSS.read_text(encoding="utf-8")

        self.assertIn('<script src="/preload.js"></script>', html)
        self.assertIn('<script src="/app.js" defer></script>', html)
        self.assertIn('<link rel="stylesheet" href="/styles.css">', html)
        self.assertNotIn("onclick=", html.lower())
        self.assertNotIn("javascript:", html.lower())

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
