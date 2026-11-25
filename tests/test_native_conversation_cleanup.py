"""Hermetic Zero-PII unit tests for Native-First `ConversationService` cleanup and watchdog hourly auto-archive."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
WATCHDOG_DIR = REPO_ROOT / "sidecars" / "dev-queue-watchdog"
TESTS_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"

for _p in (CLIENTS_DIR, PR_REM_DIR, TESTS_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import conversation_service_client as csc  # noqa: E402
import merge_watcher as mw  # noqa: E402
import pr_remediator as prs  # noqa: E402
import selective_test_runner  # noqa: E402


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location("dev_queue_watchdog_native_test", WATCHDOG_DIR / "watchdog.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watchdog = _load_watchdog_module()


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeConversationServiceState:
    """Thread-safe in-memory state for a fake Connect HTTP+JSON `ConversationService` loopback server."""

    def __init__(
        self,
        *,
        expected_csrf: str = "mock-csrf-token-0000",
        conversations: list[dict[str, object]] | None = None,
        fail_methods: dict[str, tuple[int, str, str]] | None = None,
        page_size_cap: int = 100,
        on_delete: object | None = None,
    ) -> None:
        self.lock = threading.Lock()
        self.expected_csrf = expected_csrf
        self.conversations: list[dict[str, object]] = list(conversations or [])
        self.fail_methods: dict[str, tuple[int, str, str]] = dict(fail_methods or {})
        self.page_size_cap = page_size_cap
        self.on_delete = on_delete
        self.calls: list[dict[str, object]] = []


def _start_fake_ls_server(state: _FakeConversationServiceState) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start an isolated loopback `ThreadingHTTPServer` implementing `product.v1.ConversationService`."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            prefix = "/product.v1.ConversationService/"
            if not self.path.startswith(prefix):
                self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "unknown service"})
                return
            method = self.path[len(prefix) :]
            csrf_hdr = str(self.headers.get("x-antigravity-csrf-token") or "")
            proto_hdr = str(self.headers.get("Connect-Protocol-Version") or "")
            length = int(self.headers.get("Content-Length") or 0)
            body_raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(body_raw.decode("utf-8")) if body_raw else {}

            with state.lock:
                state.calls.append(
                    {
                        "method": method,
                        "body": body,
                        "csrf": csrf_hdr,
                        "connect_protocol": proto_hdr,
                    }
                )
                if csrf_hdr != state.expected_csrf or proto_hdr != "1":
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"code": "unauthenticated", "message": "bad auth/headers"})
                    return
                if method in state.fail_methods:
                    st_code, code, msg = state.fail_methods[method]
                    self._send_json(st_code, {"code": code, "message": msg})
                    return

                if method == "ListConversations":
                    include_archived = bool(body.get("includeArchived", False))
                    visible = [
                        c
                        for c in state.conversations
                        if include_archived or not bool((c.get("annotations") or {}).get("archived"))  # type: ignore[union-attr]
                    ]
                    ps = min(int(body.get("pageSize") or 100), state.page_size_cap)
                    tok = str(body.get("pageToken") or "")
                    offset = int(tok) if tok.isdigit() else 0
                    slice_items = visible[offset : offset + ps]
                    next_offset = offset + len(slice_items)
                    next_tok = str(next_offset) if next_offset < len(visible) else ""
                    self._send_json(HTTPStatus.OK, {"conversations": slice_items, "nextPageToken": next_tok})
                    return

                if method == "GetConversation":
                    cid = str(body.get("conversationId") or "").lower()
                    for conv in state.conversations:
                        if str(conv.get("id") or "").lower() == cid:
                            self._send_json(HTTPStatus.OK, conv)
                            return
                    self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "not found"})
                    return

                if method == "UpdateConversation":
                    cid = str(body.get("conversationId") or "").lower()
                    patch_ann = body.get("annotations") or {}
                    for conv in state.conversations:
                        if str(conv.get("id") or "").lower() == cid:
                            ann = dict(conv.get("annotations") or {})  # type: ignore[arg-type]
                            ann.update(patch_ann)
                            conv["annotations"] = ann
                            self._send_json(HTTPStatus.OK, conv)
                            return
                    self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "not found"})
                    return

                if method == "DeleteConversation":
                    cid = str(body.get("conversationId") or "").lower()
                    before = len(state.conversations)
                    state.conversations = [c for c in state.conversations if str(c.get("id") or "").lower() != cid]
                    if len(state.conversations) == before:
                        self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "not found"})
                        return
                    if callable(state.on_delete):
                        state.on_delete(cid)
                    self._send_json(HTTPStatus.OK, {})
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"code": "unimplemented", "message": f"unknown method {method}"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return httpd, thread, f"127.0.0.1:{port}"


def _seed_conv_artifacts(agy_root: Path, cid: str, *, mtime: float) -> dict[str, Path]:
    brain_dir = agy_root / "brain" / cid
    conv_dir = agy_root / "conversations"
    ann_dir = agy_root / "annotations"
    pres_dir = agy_root / "presence"
    brain_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)
    ann_dir.mkdir(parents=True, exist_ok=True)
    pres_dir.mkdir(parents=True, exist_ok=True)

    notes = brain_dir / "notes.md"
    db_p = conv_dir / f"{cid}.db"
    pb_p = conv_dir / f"{cid}.pb"
    ann_p = ann_dir / f"{cid}.pbtxt"
    pres_p = pres_dir / f"{cid}.lock"
    notes.write_text("# test notes\n", encoding="utf-8")
    db_p.write_bytes(b"sqlite-bytes")
    pb_p.write_bytes(b"pb-bytes")
    ann_p.write_text("k: v\n", encoding="utf-8")
    pres_p.write_bytes(b"lock")
    for p in (notes, brain_dir, db_p, pb_p, ann_p, pres_p):
        os.utime(p, (mtime, mtime))
    return {"brain_dir": brain_dir, "db": db_p, "pb": pb_p, "ann": ann_p, "pres": pres_p}


class TestConversationServiceClient(unittest.TestCase):
    """Verify loopback validation, CSRF redaction, Connect HTTP+JSON RPCs, and pagination."""

    def test_resolve_config_enforces_loopback_and_redacts_csrf(self) -> None:
        self.assertFalse(csc.resolve_conversation_service_config(env={})["configured"])
        self.assertEqual(csc.resolve_conversation_service_config(env={})["reason"], "missing_ls_address")

        missing_tok = csc.resolve_conversation_service_config(env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234"})
        self.assertFalse(missing_tok["configured"])
        self.assertEqual(missing_tok["reason"], "missing_csrf_token")

        for bad_addr in (
            "example.com:51234",
            "10.0.0.5:51234",
            "https://127.0.0.1:51234",
            "127.0.0.1",
            "127.0.0.1:99999",
            "http://127.0.0.1:51234/extra",
        ):
            cfg = csc.resolve_conversation_service_config(
                env={"ANTIGRAVITY_LS_ADDRESS": bad_addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-token"}
            )
            self.assertFalse(cfg["configured"], msg=f"Expected rejection for {bad_addr}")

        for good_addr, exp_url in (
            ("127.0.0.1:51234", "http://127.0.0.1:51234"),
            ("localhost:51234", "http://localhost:51234"),
            ("[::1]:51234", "http://[::1]:51234"),
            ("http://127.0.0.1:51234", "http://127.0.0.1:51234"),
        ):
            cfg = csc.resolve_conversation_service_config(
                env={"ANTIGRAVITY_LS_ADDRESS": good_addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-secret-csrf-0000"}
            )
            self.assertTrue(cfg["configured"], msg=f"Expected acceptance for {good_addr}")
            self.assertEqual(cfg["base_url"], exp_url)
            client = csc.ConversationServiceClient(
                env={"ANTIGRAVITY_LS_ADDRESS": good_addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-secret-csrf-0000"}
            )
            status = client.config_status()
            self.assertNotIn("csrf_token", status)
            self.assertNotIn("mock-secret-csrf-0000", json.dumps(status))
            self.assertTrue(status["has_csrf_token"])

    def test_loopback_http_json_rpcs_and_pagination(self) -> None:
        convs = [
            {
                "id": f"1000000{i}-1111-4111-8111-111111111111",
                "metadata": {"createTime": "2026-11-20T00:00:00Z"},
                "annotations": {"title": f"Chat {i}", "archived": False, "pinned": False, "starred": False},
                "state": {"runState": "RUN_STATE_IDLE", "updateTime": "2026-11-20T01:00:00Z"},
            }
            for i in range(3)
        ]
        state = _FakeConversationServiceState(conversations=convs, page_size_cap=2)
        httpd, thread, addr = _start_fake_ls_server(state)
        try:
            client = csc.ConversationServiceClient(
                env={"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-csrf-token-0000"}
            )
            listed = client.list_all_conversations(page_size=2)
            self.assertEqual(len(listed), 3)

            target_id = convs[0]["id"]
            got = client.get_conversation(str(target_id))
            self.assertEqual(got["id"], target_id)

            updated = client.update_conversation(str(target_id), archived=True)
            self.assertTrue(updated["annotations"]["archived"])

            # Now excluded when include_archived=False
            listed_unarchived = client.list_all_conversations(include_archived=False)
            self.assertEqual(len(listed_unarchived), 2)

            client.delete_conversation(str(target_id))
            listed_after_del = client.list_all_conversations(include_archived=True)
            self.assertEqual(len(listed_after_del), 2)

            with self.assertRaises(csc.ConversationServiceError) as ctx:
                client.get_conversation(str(target_id))
            self.assertEqual(ctx.exception.status_code, 404)
            self.assertEqual(ctx.exception.code, "not_found")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2.0)


class TestNativeFirstCleanupMergedConversations(unittest.TestCase):
    """Verify Native-First `DeleteConversation` while Desktop is active and safe deferrals."""

    def test_desktop_active_calls_delete_conversation_first_and_preserves_proto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            desktop_root = root / ".gemini" / "antigravity"
            cli_root = root / ".gemini" / "antigravity-cli"
            pdir.mkdir(parents=True)

            proto_path = desktop_root / "agyhub_summaries_proto.pb"
            proto_path.parent.mkdir(parents=True, exist_ok=True)
            proto_bytes = b"\x00\x01DO_NOT_MODIFY_PROTO\xff"
            proto_path.write_bytes(proto_bytes)

            now_ts = 1_800_000_000.0
            old_ts = now_ts - 7200.0
            merged_cid = "21111111-1111-4111-8111-111111111111"
            cli_cid = "22222222-2222-4222-8222-222222222222"

            d_art = _seed_conv_artifacts(desktop_root, merged_cid, mtime=old_ts)
            c_art = _seed_conv_artifacts(cli_root, cli_cid, mtime=old_ts)
            prs.enqueue_dev_queue_item(conversation_id=merged_cid, title="Merged desktop", status="merged", portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(conversation_id=cli_cid, title="Merged cli", status="merged", portfolio_dir=pdir)

            db_exists_during_rpc: list[bool] = []

            def _on_delete(cid: str) -> None:
                if cid == merged_cid:
                    db_exists_during_rpc.append(d_art["db"].exists())

            state = _FakeConversationServiceState(
                conversations=[{"id": merged_cid, "annotations": {}, "metadata": {}, "state": {"runState": "RUN_STATE_IDLE"}}],
                on_delete=_on_delete,
            )
            httpd, thread, addr = _start_fake_ls_server(state)
            desktop_ps = "  PID ARGS\n  555 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n"
            try:
                res = mw.cleanup_merged_conversations(
                    all_merged=True,
                    include_unaudited_check=False,
                    portfolio_dir=pdir,
                    antigravity_dirs=[desktop_root, cli_root],
                    now_ts=now_ts,
                    ps_output=desktop_ps,
                    env={"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-csrf-token-0000"},
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["desktop_active"])
            self.assertEqual(res["native_delete_count"], 1)
            self.assertEqual(res["deferred_desktop_count"], 0)
            self.assertEqual(set(res["deleted"]), {merged_cid, cli_cid})
            # DeleteConversation was invoked BEFORE disk artifacts were removed
            self.assertEqual(db_exists_during_rpc, [True])
            self.assertFalse(d_art["db"].exists())
            self.assertFalse(d_art["brain_dir"].exists())
            self.assertFalse(c_art["db"].exists())
            self.assertEqual(proto_path.read_bytes(), proto_bytes)
            # Only Desktop conversation called DeleteConversation; CLI used disk_rmtree
            del_calls = [c["body"]["conversationId"] for c in state.calls if c["method"] == "DeleteConversation"]
            self.assertEqual(del_calls, [merged_cid])

    def test_desktop_active_no_ls_and_delete_failure_defer_disk_rmtree_and_retain_cli_and_closed_desktop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            desktop_root = root / ".gemini" / "antigravity"
            cli_root = root / ".gemini" / "antigravity-cli"
            pdir.mkdir(parents=True)

            now_ts = 1_800_000_000.0
            old_ts = now_ts - 7200.0
            desk_cid = "31111111-1111-4111-8111-111111111111"
            cli_cid = "32222222-2222-4222-8222-222222222222"
            d_art = _seed_conv_artifacts(desktop_root, desk_cid, mtime=old_ts)
            c_art = _seed_conv_artifacts(cli_root, cli_cid, mtime=old_ts)
            prs.enqueue_dev_queue_item(conversation_id=desk_cid, title="Merged desk", status="merged", portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(conversation_id=cli_cid, title="Merged cli", status="merged", portfolio_dir=pdir)

            desktop_ps = "  PID ARGS\n  555 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n"

            # 1. Desktop active + NO LS env -> Desktop deferred (0 ghosts), CLI cleaned
            res_no_ls = mw.cleanup_merged_conversations(
                all_merged=True,
                include_unaudited_check=False,
                portfolio_dir=pdir,
                antigravity_dirs=[desktop_root, cli_root],
                now_ts=now_ts,
                ps_output=desktop_ps,
                env={},
            )
            self.assertEqual(res_no_ls["status"], "ok")
            self.assertEqual(res_no_ls["deferred_desktop_count"], 1)
            self.assertEqual(res_no_ls["deleted"], [cli_cid])
            self.assertTrue(d_art["db"].is_file())
            self.assertTrue(d_art["brain_dir"].is_dir())
            self.assertFalse(c_art["db"].exists())
            skipped_by_cid = {s["conversation_id"]: s for s in res_no_ls["skipped"]}
            self.assertIn("deferred_desktop_active_no_ls", skipped_by_cid[desk_cid]["reasons"])
            q_mid = {it["conversation_id"]: it["status"] for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
            self.assertEqual(q_mid[desk_cid], "merged")
            self.assertEqual(q_mid[cli_cid], "cleaned")

            # 2. Desktop active + DeleteConversation fails (HTTP 500) -> Desktop deferred, files untouched
            state_fail = _FakeConversationServiceState(
                conversations=[{"id": desk_cid, "annotations": {}, "metadata": {}, "state": {"runState": "RUN_STATE_IDLE"}}],
                fail_methods={"DeleteConversation": (500, "internal", "boom")},
            )
            httpd, thread, addr = _start_fake_ls_server(state_fail)
            try:
                res_fail = mw.cleanup_merged_conversations(
                    all_merged=True,
                    include_unaudited_check=False,
                    portfolio_dir=pdir,
                    antigravity_dirs=[desktop_root, cli_root],
                    now_ts=now_ts,
                    ps_output=desktop_ps,
                    env={"ANTIGRAVITY_LS_ADDRESS": addr, "ANTIGRAVITY_CSRF_TOKEN": "mock-csrf-token-0000"},
                )
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

            self.assertEqual(res_fail["status"], "ok")
            self.assertEqual(res_fail["deferred_desktop_count"], 1)
            self.assertEqual(res_fail["deleted_count"], 0)
            self.assertTrue(d_art["db"].is_file())
            skipped_fail = {s["conversation_id"]: s for s in res_fail["skipped"]}
            self.assertIn("conversation_service_delete_failed", skipped_fail[desk_cid]["reasons"])

            # 3. Desktop closed (ps_output="") -> direct disk cleanup works without LS
            res_closed = mw.cleanup_merged_conversations(
                all_merged=True,
                include_unaudited_check=False,
                portfolio_dir=pdir,
                antigravity_dirs=[desktop_root, cli_root],
                now_ts=now_ts,
                ps_output="",
                env={},
            )
            self.assertEqual(res_closed["status"], "ok")
            self.assertFalse(res_closed["desktop_active"])
            self.assertEqual(res_closed["deleted"], [desk_cid])
            self.assertFalse(d_art["db"].exists())
            self.assertFalse(d_art["brain_dir"].exists())

            # 4. Hermetic test override (antigravity_dirs set, ps_output=None) retains disk cleanup even if host Desktop is running
            hermetic_cid = "33333333-3333-4333-8333-333333333333"
            h_art = _seed_conv_artifacts(desktop_root, hermetic_cid, mtime=old_ts)
            prs.enqueue_dev_queue_item(conversation_id=hermetic_cid, title="Hermetic item", status="merged", portfolio_dir=pdir)
            with mock.patch.object(mw, "_detect_active_antigravity_processes", return_value=["pid=999:antigravity"]):
                res_herm = mw.cleanup_merged_conversations(
                    all_merged=True,
                    include_unaudited_check=False,
                    portfolio_dir=pdir,
                    antigravity_dirs=[desktop_root],
                    now_ts=now_ts,
                    env={},
                )
            self.assertEqual(res_herm["status"], "ok")
            self.assertTrue(res_herm["hermetic_test_override"])
            self.assertEqual(res_herm["deleted"], [hermetic_cid])
            self.assertFalse(h_art["db"].exists())


class TestAutoArchiveAndWatchdogIntegration(unittest.TestCase):
    """Verify `auto_archive_stale_conversations`, watchdog hourly cadence, and CLI/router wiring."""

    def test_auto_archive_stale_conversations_targets_and_strict_protections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            agy_dir = root / ".gemini" / "antigravity"
            pdir.mkdir(parents=True)
            agy_dir.mkdir(parents=True)

            now_ts = 1_800_500_000.0
            old_iso = _iso_from_epoch(now_ts - (5 * 86400.0))
            recent_iso = _iso_from_epoch(now_ts - (1 * 86400.0))

            cid_cancelled_compacted = "41111111-1111-4111-8111-111111111111"
            cid_batch_pending = "41112222-2222-4222-8222-222222222222"
            cid_adhoc_stale = "42221111-1111-4111-8111-111111111111"
            cid_adhoc_recent = "42222222-2222-4222-8222-222222222222"
            cid_pinned = "43331111-1111-4111-8111-111111111111"
            cid_starred = "43332222-2222-4222-8222-222222222222"
            cid_running = "44441111-1111-4111-8111-111111111111"
            cid_cancelling = "44442222-2222-4222-8222-222222222222"
            cid_orchestrator = "45551111-1111-4111-8111-111111111111"
            cid_rotated_orch = "45552222-2222-4222-8222-222222222222"
            cid_locked_worker = "46661111-1111-4111-8111-111111111111"
            cid_current_env = "47771111-1111-4111-8111-111111111111"

            prs.set_dev_queue_orchestrator(cid_orchestrator, portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(
                conversation_id=cid_cancelled_compacted,
                title="Absorbed task",
                status="cancelled",
                notes=f"Superseded / consolidated into {cid_batch_pending[:8]}.",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_batch_pending,
                title="Active replacement batch",
                status="pending",
                portfolio_dir=pdir,
            )
            prs.acquire_dev_pr_lock(
                conversation_id="46660000-0000-4000-8000-000000000000",
                action_id="locked-task",
                repo_slug="acme/repo",
                branch_name="fix/20261125-000000-test-46660000",
                worker_cid=cid_locked_worker,
                portfolio_dir=pdir,
            )

            conversations = [
                {
                    "id": cid_cancelled_compacted,
                    "metadata": {"createTime": recent_iso},
                    "annotations": {"title": "Absorbed PR task", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": recent_iso},
                },
                {
                    "id": cid_batch_pending,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Pending batch", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_adhoc_stale,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Old adhoc chat", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_adhoc_recent,
                    "metadata": {"createTime": recent_iso},
                    "annotations": {"title": "Recent adhoc chat", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": recent_iso},
                },
                {
                    "id": cid_pinned,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Pinned chat", "archived": False, "pinned": True, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_starred,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Starred chat", "archived": False, "pinned": False, "starred": True},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_running,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Running chat", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_RUNNING", "updateTime": old_iso},
                },
                {
                    "id": cid_cancelling,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Cancelling chat", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_CANCELLING", "updateTime": old_iso},
                },
                {
                    "id": cid_orchestrator,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Main Dev Queue Orchestrator", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_rotated_orch,
                    "metadata": {"createTime": old_iso, "tags": ["agy-dev-orchestrator"]},
                    "annotations": {"title": "[agy-doctor] Orchestrator Recovery", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_locked_worker,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Locked worker", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
                {
                    "id": cid_current_env,
                    "metadata": {"createTime": old_iso},
                    "annotations": {"title": "Caller chat", "archived": False, "pinned": False, "starred": False},
                    "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso},
                },
            ]

            state = _FakeConversationServiceState(conversations=conversations)
            httpd, thread, addr = _start_fake_ls_server(state)
            try:
                base_env = {
                    "ANTIGRAVITY_LS_ADDRESS": addr,
                    "ANTIGRAVITY_CSRF_TOKEN": "mock-csrf-token-0000",
                    "ANTIGRAVITY_CONVERSATION_ID": cid_current_env,
                }

                # 1. Dry-run executes zero UpdateConversation / DeleteConversation mutations
                dry = mw.auto_archive_stale_conversations(
                    include_unaudited_check=False,
                    dry_run=True,
                    portfolio_dir=pdir,
                    antigravity_dirs=[agy_dir],
                    now_ts=now_ts,
                    env=base_env,
                )
                self.assertEqual(dry["status"], "ok")
                self.assertEqual(set(dry["archived"]), {cid_cancelled_compacted, cid_adhoc_stale})
                self.assertEqual([c for c in state.calls if c["method"] in ("UpdateConversation", "DeleteConversation")], [])

                # 2. AGY_AUTO_ARCHIVE_ADHOC_DAYS=0 disables ad-hoc archiving while still archiving cancelled/compacted queue items
                zero_adhoc = mw.auto_archive_stale_conversations(
                    include_unaudited_check=False,
                    dry_run=True,
                    portfolio_dir=pdir,
                    antigravity_dirs=[agy_dir],
                    now_ts=now_ts,
                    env={**base_env, "AGY_AUTO_ARCHIVE_ADHOC_DAYS": "0"},
                )
                self.assertEqual(zero_adhoc["archived"], [cid_cancelled_compacted])

                # 3. Live run archives ONLY cid_cancelled_compacted and cid_adhoc_stale via UpdateConversation
                live = mw.auto_archive_stale_conversations(
                    include_unaudited_check=False,
                    dry_run=False,
                    portfolio_dir=pdir,
                    antigravity_dirs=[agy_dir],
                    now_ts=now_ts,
                    env=base_env,
                )
                self.assertEqual(live["status"], "ok")
                self.assertEqual(set(live["archived"]), {cid_cancelled_compacted, cid_adhoc_stale})
                self.assertEqual(live["by_category"]["cancelled_compacted_queue"], 1)
                self.assertEqual(live["by_category"]["stale_adhoc_chat"], 1)
                self.assertEqual([c for c in state.calls if c["method"] == "DeleteConversation"], [])
                upd_calls = [c for c in state.calls if c["method"] == "UpdateConversation"]
                self.assertEqual({c["body"]["conversationId"] for c in upd_calls}, {cid_cancelled_compacted, cid_adhoc_stale})
                for c in upd_calls:
                    self.assertEqual(c["body"]["annotations"], {"archived": True})
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

    def test_watchdog_hourly_cleanup_runs_native_delete_and_auto_archive_and_cli_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            agy_dir = root / ".gemini" / "antigravity"
            state_dir.mkdir(parents=True)
            agy_dir.mkdir(parents=True)

            now_ts = 1_800_600_000.0
            old_ts = now_ts - (5 * 86400.0)
            old_iso = _iso_from_epoch(old_ts)

            merged_cid = "51111111-1111-4111-8111-111111111111"
            stale_adhoc_cid = "52222222-2222-4222-8222-222222222222"
            _seed_conv_artifacts(agy_dir, merged_cid, mtime=old_ts)
            prs.enqueue_dev_queue_item(conversation_id=merged_cid, title="Merged item", status="merged", portfolio_dir=state_dir)

            state = _FakeConversationServiceState(
                conversations=[
                    {"id": merged_cid, "metadata": {"createTime": old_iso}, "annotations": {}, "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso}},
                    {"id": stale_adhoc_cid, "metadata": {"createTime": old_iso}, "annotations": {"title": "Stale chat"}, "state": {"runState": "RUN_STATE_IDLE", "updateTime": old_iso}},
                ]
            )
            httpd, thread, addr = _start_fake_ls_server(state)
            desktop_ps = "  PID ARGS\n  888 /Applications/Antigravity.app/Contents/MacOS/Antigravity\n"
            ls_env = {
                "ANTIGRAVITY_LS_ADDRESS": addr,
                "ANTIGRAVITY_CSRF_TOKEN": "mock-csrf-token-0000",
                "AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "3600",
            }
            try:
                res = watchdog.run_watchdog_cycle(
                    state_dir=state_dir,
                    antigravity_dirs=[agy_dir],
                    sync_git=False,
                    now_ts=now_ts,
                    ps_output=desktop_ps,
                    env=ls_env,
                )
                self.assertEqual(res["status"], "healthy")
                ac = res["auto_cleanup"]
                self.assertTrue(ac["ran"])
                self.assertEqual(ac["deleted_count"], 1)
                self.assertEqual(ac["native_delete_count"], 1)
                self.assertEqual(ac["archived_count"], 1)
                self.assertEqual(ac["auto_archive"]["archived"], [stale_adhoc_cid])

                persisted = json.loads((state_dir / watchdog.AUTO_CLEANUP_STATE_FILENAME).read_text(encoding="utf-8"))
                self.assertEqual(persisted["deleted_count"], 1)
                self.assertEqual(persisted["archived_count"], 1)

                # CLI subcommand `auto-archive-conversations --dry-run`
                buf = io.StringIO()
                with (
                    mock.patch.dict(os.environ, ls_env, clear=False),
                    contextlib.redirect_stdout(buf),
                ):
                    rc = prs.main(["auto-archive-conversations", "--portfolio-dir", str(state_dir), "--skip-unaudited-check", "--dry-run"])
                self.assertEqual(rc, 0)
                cli_payload = json.loads(buf.getvalue())
                self.assertEqual(cli_payload["status"], "ok")
                self.assertTrue(cli_payload["dry_run"])

                # CLI `cleanup-merged-conversations --auto-archive --adhoc-days 2 --dry-run`
                buf2 = io.StringIO()
                with (
                    mock.patch.dict(os.environ, ls_env, clear=False),
                    contextlib.redirect_stdout(buf2),
                ):
                    rc2 = prs.main(["cleanup-merged-conversations", "--auto-archive", "--adhoc-days", "2", "--skip-unaudited-check", "--dry-run"])
                self.assertEqual(rc2, 0)
                cli_payload2 = json.loads(buf2.getvalue())
                self.assertEqual(cli_payload2["status"], "ok")
                self.assertEqual(cli_payload2["adhoc_days"], 2.0)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2.0)

        # Router integrity & selective mapping
        self.assertEqual(selective_test_runner.audit_router_integrity(REPO_ROOT), [])
        self.assertIn("tests.test_native_conversation_cleanup", selective_test_runner.ALL_TEST_MODULES)
        self.assertIn(
            "tests.test_native_conversation_cleanup",
            selective_test_runner.select_test_modules(["clients/conversation_service_client.py"]),
        )
        self.assertIn(
            "tests.test_native_conversation_cleanup",
            selective_test_runner.select_test_modules(["sidecars/dev-queue-watchdog/watchdog.py"]),
        )


if __name__ == "__main__":
    unittest.main()
