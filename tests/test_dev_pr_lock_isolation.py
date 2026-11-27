"""Hermetic Zero-PII unit tests for `dev_pr_lock.json` isolation, idempotent migration, and authoritative precedence."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
CLIENTS_DIR = REPO_ROOT / "clients"
RUNNER_DIR = REPO_ROOT / "tests" / "scripts"
for _p in (str(SCRIPTS_DIR), str(TUI_DIR), str(CLIENTS_DIR), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dev_common  # noqa: E402
import lock_store  # noqa: E402
import merge_watcher  # noqa: E402
import pr_remediator as prs  # noqa: E402
import selective_test_runner  # noqa: E402


def _load_panel_server_module():
    spec = importlib.util.spec_from_file_location(
        "dev_queue_panel_server_under_test",
        REPO_ROOT / "sidecars" / "dev-queue-panel" / "server.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PANEL_SERVER = _load_panel_server_module()


class TestDevPrLockIsolation(unittest.TestCase):
    """Verify `dev_pr_lock.json` isolation, `0600` serialization, 1-time migration, and race immunity."""

    def test_dedicated_dev_pr_lock_persistence_permissions_and_zero_actions_state_creation(self) -> None:
        """Lock lifecycle persists exclusively to `0600` `dev_pr_lock.json` without creating `actions_state.json`."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid = "11112222-3333-4444-8555-666677778888"
            branch = "fix/20261127-220000-isolated-11112222"

            acq = lock_store.acquire_dev_pr_lock(
                conversation_id=cid,
                action_id="isolated-lock",
                branch_name=branch,
                summary="Isolate lock file",
                proposed_fix="Persist to dev_pr_lock.json",
                repo_slug="acme/repo-alpha",
                portfolio_dir=state_dir,
            )
            self.assertEqual(acq["status"], "ok")
            self.assertTrue(acq["acquired"])

            lock_path = state_dir / "dev_pr_lock.json"
            lock_guard = state_dir / "dev_pr_lock.json.lock"
            actions_path = state_dir / "actions_state.json"

            self.assertTrue(lock_path.is_file())
            self.assertTrue(lock_guard.is_file())
            self.assertFalse(actions_path.exists())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(lock_guard.stat().st_mode), 0o600)

            raw_doc = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertIn("acme/repo-alpha", raw_doc.get("dev_pr_locks", {}))
            self.assertTrue(raw_doc["dev_pr_locks"]["acme/repo-alpha"]["active"])

            hb = lock_store.record_dev_pr_lock_heartbeat(
                repo_slug="acme/repo-alpha",
                worker_cid="aaaa1111-2222-4333-8444-555566667777",
                reviewer_subagent_id="bbbb1111-2222-4333-8444-555566667777",
                stage_label="reviewing",
                portfolio_dir=state_dir,
            )
            self.assertEqual(hb["status"], "ok")
            fetched = lock_store.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug="acme/repo-alpha")
            self.assertEqual(fetched["stage_label"], "reviewing")
            self.assertEqual(fetched["reviewer_subagent_id"], "bbbb1111-2222-4333-8444-555566667777")
            self.assertFalse(actions_path.exists())

            rel = lock_store.release_dev_pr_lock(
                reason="BRANCH_MERGED_INTO_MAIN",
                portfolio_dir=state_dir,
                repo_slug="acme/repo-alpha",
            )
            self.assertEqual(rel["status"], "ok")
            self.assertFalse(lock_store.is_dev_pr_locked(portfolio_dir=state_dir, repo_slug="acme/repo-alpha"))
            self.assertFalse(actions_path.exists())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_lock_file_serialization_uses_dev_pr_lock_and_ignores_actions_state_lock(self) -> None:
        """`dev_pr_lock.json.lock` serializes writers while held `actions_state.json.lock` never blocks updates."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid = "22223333-4444-4555-8666-777788889999"
            lock_store.acquire_dev_pr_lock(
                conversation_id=cid,
                action_id="lock-contention",
                branch_name="fix/20261127-221000-lock-22223333",
                repo_slug="acme/repo-beta",
                portfolio_dir=state_dir,
            )

            # Holding `actions_state.json.lock` MUST NOT block `update_dev_pr_lock` once isolated
            actions_lock_fd = os.open(str(state_dir / "actions_state.json.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(actions_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                upd = lock_store.update_dev_pr_lock(
                    {"status": "AWAITING_AGENT_REVIEW"},
                    portfolio_dir=state_dir,
                    repo_slug="acme/repo-beta",
                )
                self.assertEqual(upd["status"], "ok")
                self.assertEqual(upd["lock"]["status"], "AWAITING_AGENT_REVIEW")
            finally:
                fcntl.flock(actions_lock_fd, fcntl.LOCK_UN)
                os.close(actions_lock_fd)

            # Holding `dev_pr_lock.json.lock` MUST serialize / time out bounded `file_lock`
            dev_lock_fd = os.open(str(state_dir / "dev_pr_lock.json.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(dev_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(TimeoutError):
                    with dev_common.file_lock(state_dir / "dev_pr_lock.json.lock", timeout_seconds=0.5):
                        pass
            finally:
                fcntl.flock(dev_lock_fd, fcntl.LOCK_UN)
                os.close(dev_lock_fd)

    def test_idempotent_one_time_migration_strips_legacy_keys_and_preserves_other_state(self) -> None:
        """Legacy `actions_state.json` locks migrate once into `dev_pr_lock.json` and strip legacy keys cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            actions_path = state_dir / "actions_state.json"
            lock_path = state_dir / "dev_pr_lock.json"

            legacy_payload = {
                "morning_health_check": {"last_run_date": "2026-11-27", "status": "ok"},
                "actions_log_audit_state": {
                    "watermark_iso": "2026-11-27T15:00:00Z",
                    "pending_tier2_proposals": {
                        "99990000-1111-4222-8333-444455556666": {"resolution": "STAGE_PROPOSED"}
                    },
                    "dev_pr_locks": {
                        "acme/repo-one": {
                            "active": True,
                            "repo_slug": "acme/repo-one",
                            "conversation_id": "aaaa0001-1111-4222-8333-444455556666",
                            "branch_name": "fix/20261127-150000-one-aaaa0001",
                            "status": "DEVELOPING",
                        }
                    },
                    "dev_pr_lock": {
                        "active": True,
                        "repo_slug": "acme/repo-legacy",
                        "conversation_id": "bbbb0002-1111-4222-8333-444455556666",
                        "branch_name": "fix/20261127-140000-leg-bbbb0002",
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                    },
                },
            }
            dev_common.atomic_write_json(actions_path, legacy_payload)

            # Read-only helpers read fallback without mutating disk
            ro_locks = lock_store.get_all_dev_pr_locks(portfolio_dir=state_dir)
            self.assertEqual(set(ro_locks.keys()), {"acme/repo-one", "acme/repo-legacy"})
            self.assertFalse(lock_path.exists())

            # Explicit 1-time migration creates `dev_pr_lock.json` and strips lock keys from `actions_state.json`
            mig1 = lock_store.migrate_legacy_dev_pr_locks(portfolio_dir=state_dir)
            self.assertEqual(mig1["status"], "ok")
            self.assertTrue(mig1["migrated"])
            self.assertTrue(lock_path.is_file())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

            after_actions = json.loads(actions_path.read_text(encoding="utf-8"))
            self.assertEqual(after_actions["morning_health_check"]["status"], "ok")
            self.assertEqual(
                after_actions["actions_log_audit_state"]["watermark_iso"],
                "2026-11-27T15:00:00Z",
            )
            self.assertIn(
                "99990000-1111-4222-8333-444455556666",
                after_actions["actions_log_audit_state"]["pending_tier2_proposals"],
            )
            self.assertNotIn("dev_pr_locks", after_actions["actions_log_audit_state"])
            self.assertNotIn("dev_pr_lock", after_actions["actions_log_audit_state"])

            migrated_locks = lock_store.get_all_dev_pr_locks(portfolio_dir=state_dir)
            self.assertEqual(set(migrated_locks.keys()), {"acme/repo-one", "acme/repo-legacy"})

            # Second migration call is a pure idempotent no-op
            mig2 = lock_store.migrate_legacy_dev_pr_locks(portfolio_dir=state_dir)
            self.assertEqual(mig2["status"], "ok")
            self.assertFalse(mig2["migrated"])

    def test_authoritative_dev_pr_lock_prevents_released_or_stale_resurrection_across_readers(self) -> None:
        """Once `dev_pr_lock.json` exists, stale `actions_state.json` locks never resurrect across any reader."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            wt_stale = root / "worktrees" / "pr-worktree-repo-stale-fix-deadbeef"
            wt_live = root / "worktrees" / "pr-worktree-repo-live-fix-cafebabe"
            wt_stale.mkdir(parents=True, exist_ok=True)
            wt_live.mkdir(parents=True, exist_ok=True)

            # Authoritative `dev_pr_lock.json`: `acme/repo-released` is inactive, `acme/repo-live` is active,
            # and `acme/repo-phantom` does not exist at all.
            dev_common.atomic_write_json(
                state_dir / "dev_pr_lock.json",
                {
                    "schema_version": 1,
                    "dev_pr_locks": {
                        "acme/repo-released": {
                            "active": False,
                            "repo_slug": "acme/repo-released",
                            "last_release_reason": "BRANCH_MERGED_INTO_MAIN",
                            "last_branch_name": "fix/20261127-100000-rel-deadbeef",
                            "last_conversation_id": "deadbeef-1111-4222-8333-444455556666",
                        },
                        "acme/repo-live": {
                            "active": True,
                            "repo_slug": "acme/repo-live",
                            "status": "PENDING_HUMAN_PR_VALIDATION",
                            "pr_gate_ready": True,
                            "branch_name": "fix/20261127-120000-live-cafebabe",
                            "worktree_path": str(wt_live),
                            "conversation_id": "cafebabe-1111-4222-8333-444455556666",
                        },
                    },
                },
            )

            # Stale `actions_state.json` claims `acme/repo-released` and `acme/repo-phantom` are still ACTIVE,
            # and claims `acme/repo-live` is still `AWAITING_AGENT_REVIEW`!
            dev_common.atomic_write_json(
                state_dir / "actions_state.json",
                {
                    "actions_log_audit_state": {
                        "dev_pr_locks": {
                            "acme/repo-released": {
                                "active": True,
                                "repo_slug": "acme/repo-released",
                                "status": "DEVELOPING",
                                "branch_name": "fix/20261127-100000-rel-deadbeef",
                                "worktree_path": str(wt_stale),
                                "conversation_id": "deadbeef-1111-4222-8333-444455556666",
                            },
                            "acme/repo-phantom": {
                                "active": True,
                                "repo_slug": "acme/repo-phantom",
                                "status": "DEVELOPING",
                                "branch_name": "fix/20261127-110000-phantom-feedface",
                                "worktree_path": str(wt_stale),
                                "conversation_id": "feedface-1111-4222-8333-444455556666",
                            },
                            "acme/repo-live": {
                                "active": True,
                                "repo_slug": "acme/repo-live",
                                "status": "AWAITING_AGENT_REVIEW",
                                "pr_gate_ready": False,
                                "branch_name": "fix/20261127-120000-live-cafebabe",
                                "worktree_path": str(wt_live),
                                "conversation_id": "cafebabe-1111-4222-8333-444455556666",
                            },
                        },
                        "dev_pr_lock": {
                            "active": True,
                            "repo_slug": "acme/repo-released",
                            "branch_name": "fix/20261127-100000-rel-deadbeef",
                        },
                    }
                },
            )

            # 1. `dev_common.load_dev_locks_from_dir`
            active_dc, all_dc = dev_common.load_dev_locks_from_dir(state_dir)
            self.assertEqual(set(active_dc.keys()), {"acme/repo-live"})
            self.assertEqual(active_dc["acme/repo-live"]["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(active_dc["acme/repo-live"]["pr_gate_ready"])
            self.assertNotIn("acme/repo-phantom", all_dc)
            self.assertFalse(all_dc["acme/repo-released"]["active"])

            # 2. `dev_common.get_active_dev_pr_lock_protections`
            prot_branches, prot_wts = dev_common.get_active_dev_pr_lock_protections(
                state_dir=state_dir,
                home_dir=root,
            )
            self.assertEqual(prot_branches, {"fix/20261127-120000-live-cafebabe"})
            self.assertEqual(prot_wts, {str(wt_live.resolve())})

            # 3. `DevLifecycleGuardrailEvaluator`
            evaluator = dev_common.DevLifecycleGuardrailEvaluator(home_dir=root, state_dir=state_dir)
            eval_map = evaluator._read_active_locks_map()
            self.assertEqual(set(eval_map.keys()), {"acme/repo-live"})
            self.assertEqual(eval_map["acme/repo-live"]["status"], "PENDING_HUMAN_PR_VALIDATION")

            # 4. `lock_store` readers
            self.assertEqual(set(lock_store.get_all_dev_pr_locks(portfolio_dir=state_dir).keys()), {"acme/repo-live"})
            self.assertFalse(lock_store.is_dev_pr_locked(portfolio_dir=state_dir, repo_slug="acme/repo-released"))
            self.assertEqual(
                lock_store.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug="acme/repo-live")["status"],
                "PENDING_HUMAN_PR_VALIDATION",
            )

            # 5. `merge_watcher.verify_branches_merged_into_base` does NOT treat released CID as active lock
            ver_released = merge_watcher.verify_branches_merged_into_base(
                conversation_ids=["deadbeef-1111-4222-8333-444455556666"],
                confirm_merged=True,
                portfolio_dir=state_dir,
            )
            self.assertNotEqual(ver_released.get("reason"), "active_dev_pr_lock_still_held")

            ver_live = merge_watcher.verify_branches_merged_into_base(
                conversation_ids=["cafebabe-1111-4222-8333-444455556666"],
                portfolio_dir=state_dir,
            )
            self.assertFalse(ver_live["all_merged"])
            self.assertEqual(ver_live["reason"], "active_dev_pr_lock_still_held")

    def test_concurrent_actions_state_overwrites_never_clobber_pass_transition_in_dev_pr_lock(self) -> None:
        """Reproduces RCA `1bc428a5`/`8822588a`: concurrent `actions_state.json` writes cannot roll back PASS."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid = "8822588a-1111-4222-8333-444455556666"
            branch = "fix/20261127-160000-race-8822588a"

            # Seed pre-migration `actions_state.json`
            stale_snapshot = {
                "morning_health_check": {"counter": 0},
                "actions_log_audit_state": {
                    "dev_pr_locks": {
                        "acme/repo-race": {
                            "active": True,
                            "repo_slug": "acme/repo-race",
                            "conversation_id": cid,
                            "branch_name": branch,
                            "status": "AWAITING_AGENT_REVIEW",
                            "pr_gate_ready": False,
                        }
                    }
                },
            }
            dev_common.atomic_write_json(state_dir / "actions_state.json", stale_snapshot)

            # First lock touch migrates `dev_pr_lock.json`
            hb = lock_store.record_dev_pr_lock_heartbeat(
                repo_slug="acme/repo-race",
                reviewer_subagent_id="rev00001-1111-4222-8333-444455556666",
                stage_label="reviewing",
                portfolio_dir=state_dir,
            )
            self.assertEqual(hb["status"], "ok")

            stop_flag = threading.Event()
            errors: list[Exception] = []

            def _tam_writer() -> None:
                i = 0
                while not stop_flag.is_set():
                    try:
                        snap = dict(stale_snapshot)
                        snap["morning_health_check"] = {"counter": i}
                        dev_common.atomic_write_json(state_dir / "actions_state.json", snap)
                        i += 1
                    except Exception as exc:  # pragma: no cover
                        errors.append(exc)
                        break

            writers = [threading.Thread(target=_tam_writer, daemon=True) for _ in range(3)]
            for w in writers:
                w.start()
            try:
                for round_idx in range(15):
                    sha = f"{round_idx:040x}"
                    res = lock_store.update_dev_pr_lock(
                        {
                            "status": "PENDING_HUMAN_PR_VALIDATION",
                            "pr_gate_ready": True,
                            "review_approved": True,
                            "approved_head_sha": sha,
                            "reviewed_head_sha": sha,
                            "reviewer_verdict": "PASS",
                            "reviewer_score": 97,
                        },
                        portfolio_dir=state_dir,
                        repo_slug="acme/repo-race",
                    )
                    self.assertEqual(res["status"], "ok")
                    cur = lock_store.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug="acme/repo-race")
                    self.assertEqual(cur["status"], "PENDING_HUMAN_PR_VALIDATION")
                    self.assertTrue(cur["pr_gate_ready"])
                    self.assertTrue(cur["review_approved"])
                    self.assertEqual(cur["approved_head_sha"], sha)
                    act_dc, _ = dev_common.load_dev_locks_from_dir(state_dir)
                    self.assertEqual(act_dc["acme/repo-race"]["status"], "PENDING_HUMAN_PR_VALIDATION")
            finally:
                stop_flag.set()
                for w in writers:
                    w.join(timeout=3.0)

            self.assertEqual(errors, [])

    def test_sidecar_panel_server_stat_watches_dev_pr_lock_json(self) -> None:
        """`sidecars/dev-queue-panel/server.py` cache signatures invalidate immediately when `dev_pr_lock.json` changes."""
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            st_dir.mkdir(parents=True, exist_ok=True)
            (st_dir / "dev_queue.json").write_text('{"items": []}\n', encoding="utf-8")
            (st_dir / "actions_state.json").write_text("{}\n", encoding="utf-8")

            sig_opt_before = PANEL_SERVER._compute_opt_hint_cache_signature(st_dir, {}, (), 3600)
            sig_ghost_before = PANEL_SERVER._compute_ghost_purge_cache_signature({"portfolio_dir": st_dir})

            lock_file = st_dir / "dev_pr_lock.json"
            lock_file.write_text('{"schema_version": 1, "dev_pr_locks": {}}\n', encoding="utf-8")

            sig_opt_created = PANEL_SERVER._compute_opt_hint_cache_signature(st_dir, {}, (), 3600)
            sig_ghost_created = PANEL_SERVER._compute_ghost_purge_cache_signature({"portfolio_dir": st_dir})
            self.assertNotEqual(sig_opt_before, sig_opt_created)
            self.assertNotEqual(sig_ghost_before, sig_ghost_created)

            time.sleep(0.01)
            lock_file.write_text(
                '{"schema_version": 1, "dev_pr_locks": {"acme/repo": {"active": true, "status": "DEVELOPING"}}}\n',
                encoding="utf-8",
            )
            sig_opt_updated = PANEL_SERVER._compute_opt_hint_cache_signature(st_dir, {}, (), 3600)
            sig_ghost_updated = PANEL_SERVER._compute_ghost_purge_cache_signature({"portfolio_dir": st_dir})
            self.assertNotEqual(sig_opt_created, sig_opt_updated)
            self.assertNotEqual(sig_ghost_created, sig_ghost_updated)

    def test_selective_test_runner_registers_dev_pr_lock_isolation(self) -> None:
        """`selective_test_runner` registers `tests.test_dev_pr_lock_isolation` with zero router integrity issues."""
        self.assertIn("tests.test_dev_pr_lock_isolation", selective_test_runner.ALL_TEST_MODULES)
        self.assertEqual(selective_test_runner.audit_router_integrity(REPO_ROOT), [])
        for changed in (
            "skills/agy-code-remediation-pr/scripts/lock_store.py",
            "clients/dev_common.py",
            "sidecars/dev-queue-panel/server.py",
            "tests/test_dev_pr_lock_isolation.py",
        ):
            selected = selective_test_runner.select_test_modules([changed])
            self.assertIn("tests.test_dev_pr_lock_isolation", selected, msg=f"Missing selection for {changed}")
        # Preserve 4566e488 CLI contract
        parsed = prs.parse_args(
            [
                "record-review",
                "--branch",
                "fix/20261127-220000-acme-11112222",
                "--reviewer-subagent-id",
                "11112222-3333-4444-8555-666677778888",
                "--verdict",
                "PASS",
                "--score",
                "96",
                "--summary",
                "Verified.",
            ]
        )
        self.assertEqual(parsed.subagent_id, "11112222-3333-4444-8555-666677778888")


if __name__ == "__main__":
    unittest.main()
