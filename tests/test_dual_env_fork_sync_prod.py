"""Hermetic Zero-PII tests for Org-Prod (:8787) vs Personal-Fork DEV (:8786) isolation,
APFS CoW state clone, Two-Step Cross-Fork PR promotion, and Manual Sync Prod + Rollback.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
REMEDIATOR_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
SIDECAR_PANEL_DIR = REPO_ROOT / "sidecars" / "dev-queue-panel"

for _p in (SIDECAR_PANEL_DIR, REMEDIATOR_DIR, CLIENTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_common  # noqa: E402
import git_client  # noqa: E402
import pr_remediator as prs  # noqa: E402
import server as panel_server  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
    }
    env.setdefault("GIT_AUTHOR_NAME", "Test Bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "bot@example.test")
    env.setdefault("GIT_COMMITTER_NAME", "Test Bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "bot@example.test")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


def _init_bare_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return path


def _clone_and_seed_repo(bare_remote: Path, clone_dir: Path, files: dict[str, str], msg: str = "chore: seed") -> str:
    subprocess.run(
        ["git", "clone", str(bare_remote), str(clone_dir)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    _git(clone_dir, "checkout", "-B", "main")
    for rel, content in files.items():
        fp = clone_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    _git(clone_dir, "add", "-A")
    _git(clone_dir, "commit", "-m", msg)
    _git(clone_dir, "push", "-u", "origin", "main")
    return _git(clone_dir, "rev-parse", "HEAD").stdout.strip()


class TestDualEnvTopologyAndIsolation(unittest.TestCase):
    """Verify Org-Prod (:8787) vs Personal-Fork DEV (:8786) topology, ports, and deploy guardrails."""

    def test_default_ports_and_env_example_contract(self) -> None:
        self.assertEqual(dev_common.DEFAULT_PRO_PORT, 8787)
        self.assertEqual(dev_common.DEFAULT_DEV_PORT, 8786)
        self.assertEqual(dev_common.resolve_pro_port(env={}), 8787)
        self.assertEqual(dev_common.resolve_dev_port(env={}), 8786)
        self.assertEqual(dev_common.resolve_dev_port(env={"AGY_DEV_PORT": "8790"}), 8790)
        self.assertEqual(dev_common.resolve_runtime_env(env={}), "prod")
        self.assertEqual(dev_common.resolve_runtime_env(env={"AGY_RUNTIME_ENV": "dev"}), "dev")

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for var_name in (
            "AGY_PROD_REPOS_DIR",
            "AGY_DEV_REPOS_DIR",
            "AGY_PRO_PORT",
            "AGY_DEV_PORT",
            "AGY_RUNTIME_ENV",
            "AGY_DEV_PORTFOLIO_DIR",
        ):
            self.assertIn(var_name, env_example)
        self.assertIn('AGY_DEV_PORT="8786"', env_example)

    def test_classify_repo_checkout_plane_and_server_envs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            prod_repos = root / "org-prod"
            dev_repos = root / "fork-dev"
            (prod_repos / "agy-dev-plugin").mkdir(parents=True)
            (dev_repos / "agy-dev-plugin").mkdir(parents=True)

            env_map = {
                "AGY_PROD_REPOS_DIR": str(prod_repos),
                "AGY_DEV_REPOS_DIR": str(dev_repos),
            }
            self.assertEqual(
                dev_common.classify_repo_checkout_plane(prod_repos / "agy-dev-plugin", env=env_map, home_dir=home),
                "prod",
            )
            self.assertEqual(
                dev_common.classify_repo_checkout_plane(dev_repos / "agy-dev-plugin", env=env_map, home_dir=home),
                "dev_fork",
            )
            self.assertEqual(
                dev_common.classify_repo_checkout_plane(
                    root / "tmp" / "pr-worktree-agy-dev-plugin-fix-1234",
                    env=env_map,
                    home_dir=home,
                ),
                "dev_fork",
            )

            standalone_fork = root / "standalone-fork" / "agy-dev-plugin"
            standalone_fork.mkdir(parents=True)
            subprocess.run(["git", "init", str(standalone_fork)], capture_output=True, check=True)
            _git(standalone_fork, "remote", "add", "origin", "git@github.com:fork-user/agy-dev-plugin.git")
            _git(standalone_fork, "remote", "add", "upstream", "git@github.com:acme-org/agy-dev-plugin.git")
            self.assertEqual(
                dev_common.classify_repo_checkout_plane(standalone_fork, env={}, home_dir=home),
                "dev_fork",
            )

            dev_env = dev_common.build_dev_server_env(base_env=env_map, home_dir=home)
            self.assertEqual(dev_env["AGY_RUNTIME_ENV"], "dev")
            self.assertEqual(dev_env["PORT"], "8786")
            self.assertEqual(dev_env["AGY_DEV_PORT"], "8786")
            self.assertTrue(dev_env["WIZ_CUSTOMER_PORTFOLIO_DIR"].endswith("wiz-customer-portfolio-dev"))
            self.assertIn(str((dev_repos / "g-control-tower").resolve()), dev_env["PYTHONPATH"])
            self.assertNotIn(str(prod_repos.resolve()), dev_env["PYTHONPATH"])

            prod_env = dev_common.build_prod_server_env(base_env=env_map, home_dir=home)
            self.assertEqual(prod_env["AGY_RUNTIME_ENV"], "prod")
            self.assertEqual(prod_env["AGY_PRO_PORT"], "8787")
            self.assertTrue(prod_env["WIZ_CUSTOMER_PORTFOLIO_DIR"].endswith("wiz-customer-portfolio"))
            self.assertIn(str((prod_repos / "g-control-tower").resolve()), prod_env["PYTHONPATH"])
            self.assertNotIn(str(dev_repos.resolve()), prod_env["PYTHONPATH"])

    def test_deploy_home_and_pretool_guardrail_block_fork_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            fake_home.mkdir()
            dev_repos = root / "fork-dev"
            fork_plugin = dev_repos / "agy-dev-plugin"
            bare_fork = _init_bare_repo(root / "remotes" / "fork-user" / "agy-dev-plugin.git")
            _clone_and_seed_repo(
                bare_fork,
                fork_plugin,
                {"plugin.json": json.dumps({"name": "agy-dev-plugin", "version": "0.1.0"})},
            )

            vcs = git_client.GitVcsIntegrationClient(
                env_overrides={"AGY_DEV_REPOS_DIR": str(dev_repos)},
            )
            dep_res = vcs.deploy_repo_to_home(
                repo_dir=fork_plugin,
                home_dir=fake_home,
                require_base_branch="main",
            )
            self.assertEqual(dep_res["status"], "skipped")
            self.assertFalse(dep_res["deployed"])
            self.assertEqual(dep_res["reason"], "blocked_fork_repo_deploy_home")
            self.assertFalse(dep_res["control_tower_restarted"])
            self.assertFalse((fake_home / ".gemini" / "config" / "plugins" / "agy-dev-plugin").exists())

            with mock.patch.dict(os.environ, {"AGY_DEV_REPOS_DIR": str(dev_repos)}, clear=False):
                evaluator = dev_common.DevLifecycleGuardrailEvaluator(home_dir=fake_home)
                res_install = evaluator.evaluate_pre_tool_use(
                    {
                        "tool_name": "run_command",
                        "cwd": str(fork_plugin),
                        "tool_input": {"CommandLine": "./install.sh"},
                    }
                )
                self.assertEqual(res_install["decision"], "deny")
                self.assertEqual(res_install["check_id"], "pretool:parallel-worker-deploy-home")

                res_deploy = evaluator.evaluate_pre_tool_use(
                    {
                        "tool_name": "run_command",
                        "cwd": str(fork_plugin),
                        "tool_input": {"CommandLine": "python3 clients/git_client.py deploy-home"},
                    }
                )
                self.assertEqual(res_deploy["decision"], "deny")
                self.assertEqual(res_deploy["check_id"], "pretool:parallel-worker-deploy-home")

    def test_lock_release_and_merge_watcher_never_restart_prod_8787_or_deploy_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "portfolio"
            pdir.mkdir(parents=True)
            bare_fork = _init_bare_repo(root / "remotes" / "fork-user" / "agy-dev-plugin.git")
            fork_repo = root / "fork-dev" / "agy-dev-plugin"
            _clone_and_seed_repo(
                bare_fork,
                fork_repo,
                {"plugin.json": json.dumps({"name": "agy-dev-plugin", "version": "0.1.0"})},
            )
            cid = "11111111-2222-4333-8444-555555555555"
            branch = "fix/20261126-120000-fork-iso-11111111"
            _git(fork_repo, "checkout", "-b", branch)
            (fork_repo / "feature.txt").write_text("isolated fork work\n", encoding="utf-8")
            _git(fork_repo, "add", "feature.txt")
            _git(fork_repo, "commit", "-m", "feat(iso): fork change")
            _git(fork_repo, "push", "-u", "origin", branch)
            _git(fork_repo, "checkout", "main")
            _git(fork_repo, "merge", "--ff-only", branch)
            _git(fork_repo, "push", "origin", "main")
            _git(fork_repo, "push", "origin", "--delete", branch)

            prs.acquire_dev_pr_lock(
                conversation_id=cid,
                branch_name=branch,
                repo_slug="fork-user/agy-dev-plugin",
                target_repo_dir=str(fork_repo),
                portfolio_dir=pdir,
            )
            prs.update_dev_pr_lock(
                {
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "branch_pushed": True,
                },
                portfolio_dir=pdir,
                repo_slug="fork-user/agy-dev-plugin",
                branch_name=branch,
            )

            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(fork_repo)})
            with (
                mock.patch.object(prs, "restart_pro_api_server") as mock_restart_8787,
                mock.patch.object(
                    prs,
                    "reload_dev_api_server",
                    return_value={"status": "ok", "reloaded": True, "port": 8786},
                ) as mock_reload_8786,
                mock.patch.object(vcs, "deploy_repo_to_home") as mock_deploy_home,
            ):
                watch_res = prs.run_pr_merge_watcher_loop(
                    poll_interval_seconds=0.05,
                    max_iterations=2,
                    portfolio_dir=pdir,
                    git_client=vcs,
                    auto_cleanup=False,
                )

            self.assertEqual(watch_res["status"], "ok")
            self.assertTrue(watch_res["released"])
            self.assertTrue(watch_res["prod_isolated"])
            self.assertFalse(watch_res["prod_restarted"])
            mock_restart_8787.assert_not_called()
            mock_deploy_home.assert_not_called()
            mock_reload_8786.assert_called_once()


class TestApfsDevStateCloneAndReload(unittest.TestCase):
    """Verify APFS CoW state clone (`cp -cR` + `shutil.copytree` fallback) and DEV `:8786` reload."""

    def test_clone_dev_state_apfs_darwin_and_copytree_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            src = home / ".local" / "share" / "wiz-customer-portfolio"
            dst = home / ".local" / "share" / "wiz-customer-portfolio-dev"
            (src / "cache").mkdir(parents=True)
            (src / "customer_portfolio.json").write_text('{"accounts": ["AcmeCorp"]}\n', encoding="utf-8")
            (src / "stale.lock").write_text("lock", encoding="utf-8")
            (src / "tmp" / "pr-worktree-dummy").mkdir(parents=True)
            (src / "tmp" / "pr-worktree-dummy" / "file.txt").write_text("temp", encoding="utf-8")

            same_res = dev_common.clone_dev_state_apfs(src_dir=src, dst_dir=src, home_dir=home)
            self.assertEqual(same_res["status"], "error")
            self.assertFalse(same_res["cloned"])

            cp_calls: list[list[str]] = []

            def _fake_darwin_cp(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                cp_calls.append(list(cmd))
                import shutil as _sh

                _sh.copytree(cmd[2], cmd[3])
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            res_apfs = dev_common.clone_dev_state_apfs(
                src_dir=src,
                dst_dir=dst,
                home_dir=home,
                system_name="Darwin",
                run_cmd=_fake_darwin_cp,
            )
            self.assertEqual(res_apfs["status"], "ok")
            self.assertTrue(res_apfs["cloned"])
            self.assertEqual(res_apfs["method"], "apfs_cow_cp_cR")
            self.assertEqual(len(cp_calls), 1)
            self.assertEqual(cp_calls[0][1], "-cR")
            self.assertTrue((dst / "customer_portfolio.json").is_file())
            self.assertFalse((dst / "stale.lock").exists())
            self.assertFalse((dst / "tmp" / "pr-worktree-dummy").exists())
            meta_file = dst / dev_common.DEV_CLONE_META_FILENAME
            self.assertTrue(meta_file.is_file())
            self.assertEqual(meta_file.stat().st_mode & 0o777, 0o600)

            res_skip = dev_common.clone_dev_state_apfs(src_dir=src, dst_dir=dst, home_dir=home)
            self.assertEqual(res_skip["status"], "ok")
            self.assertFalse(res_skip["cloned"])
            self.assertTrue(res_skip["already_initialized"])

            (src / "customer_portfolio.json").write_text('{"accounts": ["AcmeCorp", "Globex"]}\n', encoding="utf-8")
            res_linux = dev_common.clone_dev_state_apfs(
                src_dir=src,
                dst_dir=dst,
                force_refresh=True,
                home_dir=home,
                system_name="Linux",
            )
            self.assertEqual(res_linux["status"], "ok")
            self.assertTrue(res_linux["cloned"])
            self.assertTrue(res_linux["refreshed"])
            self.assertEqual(res_linux["method"], "copytree_fallback")
            self.assertIn("Globex", (dst / "customer_portfolio.json").read_text(encoding="utf-8"))

    def test_try_reload_dev_api_server_uses_port_8786_and_fork_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / ".local" / "share" / "wiz-customer-portfolio").mkdir(parents=True)
            dev_repos = root / "fork-dev"
            ct_api = dev_repos / "g-control-tower" / "api"
            ct_api.mkdir(parents=True)
            (ct_api / "server.py").write_text("# stub\n", encoding="utf-8")

            captured: dict[str, object] = {}

            def _fake_runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                captured["cmd"] = list(cmd)
                captured["cwd"] = kwargs.get("cwd")
                captured["env"] = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
                return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

            res = dev_common.try_reload_dev_api_server(
                dev_repos_dir=dev_repos,
                home_dir=home,
                env={"AGY_DEV_REPOS_DIR": str(dev_repos)},
                _allow_in_tests=True,
                run_cmd=_fake_runner,
            )
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["reloaded"])
            self.assertEqual(res["port"], 8786)
            self.assertIn("8786", captured["cmd"])  # type: ignore[arg-type]
            child_env = captured["env"]  # type: ignore[assignment]
            self.assertEqual(child_env["AGY_RUNTIME_ENV"], "dev")  # type: ignore[index]
            self.assertIn(str((dev_repos / "g-control-tower").resolve()), child_env["PYTHONPATH"])  # type: ignore[index]


class TestCrossForkPromotionAndSyncProdRollback(unittest.TestCase):
    """Verify Two-Step Fork->Org PR promotion, Manual Sync Prod smoke-check abort, and 1-command Rollback."""

    def test_build_vcs_cross_fork_pr_url_ssh_and_https(self) -> None:
        url, ref = git_client.build_vcs_cross_fork_pr_url(
            "git@github.com:acme-org/agy-dev-plugin.git",
            "https://github.com/fork-user/agy-dev-plugin.git",
            base_branch="main",
            head_branch="main",
            title="chore(sync): promote fork main",
        )
        self.assertEqual(ref, "acme-org:main...fork-user:agy-dev-plugin:main")
        self.assertTrue(
            url.startswith("https://github.com/acme-org/agy-dev-plugin/compare/main...fork-user:agy-dev-plugin:main?")
        )
        self.assertIn("expand=1", url)

    def test_promote_fork_to_org_pr_syncs_upstream_and_emits_compare_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            bare_upstream = _init_bare_repo(root / "remotes" / "acme-org" / "agy-dev-plugin.git")
            bare_fork = _init_bare_repo(root / "remotes" / "fork-user" / "agy-dev-plugin.git")

            seed_clone = root / "seed"
            _clone_and_seed_repo(bare_upstream, seed_clone, {"README.md": "# Seed\n"})
            _git(seed_clone, "remote", "add", "fork", str(bare_fork))
            _git(seed_clone, "push", "fork", "main")

            dev_repos = root / "fork-dev"
            fork_clone = dev_repos / "agy-dev-plugin"
            subprocess.run(
                ["git", "clone", str(bare_fork), str(fork_clone)],
                capture_output=True,
                check=True,
            )
            _git(fork_clone, "remote", "add", "upstream", str(bare_upstream))

            # Advance upstream first -> promote_fork_to_org_pr should fast-forward clean behind fork clone.
            (seed_clone / "UPSTREAM.md").write_text("org hotfix\n", encoding="utf-8")
            _git(seed_clone, "add", "UPSTREAM.md")
            _git(seed_clone, "commit", "-m", "fix: org hotfix")
            _git(seed_clone, "push", "origin", "main")

            res_behind = git_client.promote_fork_to_org_pr(dev_repos_dir=dev_repos, home_dir=home)
            self.assertEqual(res_behind["status"], "ok")
            self.assertEqual(res_behind["actionable_count"], 0)
            self.assertTrue(res_behind["repos"][0]["synced_local_upstream"])
            _git(fork_clone, "push", "origin", "main")

            # Now merge a feature commit on fork main -> promote_fork_to_org_pr emits Cross-Fork PR URL.
            (fork_clone / "FORK_FEATURE.py").write_text("VALUE = 42\n", encoding="utf-8")
            _git(fork_clone, "add", "FORK_FEATURE.py")
            _git(fork_clone, "commit", "-m", "feat(fork): ready for org promotion")
            _git(fork_clone, "push", "origin", "main")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = prs.main(["promote-org-pr", "--dev-repos-dir", str(dev_repos)])
            self.assertEqual(rc, 0)
            cli_out = json.loads(buf.getvalue())
            self.assertEqual(cli_out["status"], "ok")
            self.assertEqual(cli_out["actionable_count"], 1)
            repo_entry = cli_out["repos"][0]
            self.assertEqual(repo_entry["ahead_count"], 1)
            self.assertTrue(repo_entry["has_unpromoted_commits"])
            self.assertEqual(repo_entry["compare_ref"], "acme-org:main...fork-user:agy-dev-plugin:main")

    def test_sync_prod_smoke_check_abort_and_one_command_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            fake_home.mkdir()
            state_dir = fake_home / ".local" / "share" / "wiz-customer-portfolio"
            state_dir.mkdir(parents=True)

            # Seed 3 Org remotes & Prod clones (`agy-dev-plugin`, `agy-tam-plugin`, `g-control-tower`).
            org_remotes = root / "org-remotes"
            prod_repos = root / "org-prod"
            work_push = root / "org-publisher"

            bare_dev = _init_bare_repo(org_remotes / "agy-dev-plugin.git")
            bare_tam = _init_bare_repo(org_remotes / "agy-tam-plugin.git")
            bare_ct = _init_bare_repo(org_remotes / "g-control-tower.git")

            sha_dev_v1 = _clone_and_seed_repo(
                bare_dev,
                work_push / "agy-dev-plugin",
                {
                    "plugin.json": json.dumps({"name": "agy-dev-plugin", "version": "1.0.0"}),
                    "clients/dev_common.py": 'DEV_API_VERSION = "v1"\n',
                },
                msg="feat: dev v1",
            )
            sha_tam_v1 = _clone_and_seed_repo(
                bare_tam,
                work_push / "agy-tam-plugin",
                {
                    "plugin.json": json.dumps({"name": "wiz-tam-plugin", "version": "1.0.0"}),
                    "clients/wiz_common.py": 'SHARED_CONTRACT = "v1"\n',
                },
                msg="feat: tam v1",
            )
            sha_ct_v1 = _clone_and_seed_repo(
                bare_ct,
                work_push / "g-control-tower",
                {
                    "api/__init__.py": "",
                    "api/server.py": (
                        "from dev_common import DEV_API_VERSION\n"
                        "from wiz_common import SHARED_CONTRACT\n"
                        "READY = (DEV_API_VERSION, SHARED_CONTRACT)\n"
                    ),
                },
                msg="feat: ct v1",
            )

            for bare_r, name in (
                (bare_dev, "agy-dev-plugin"),
                (bare_tam, "agy-tam-plugin"),
                (bare_ct, "g-control-tower"),
            ):
                subprocess.run(
                    ["git", "clone", str(bare_r), str(prod_repos / name)],
                    capture_output=True,
                    check=True,
                )

            # 1) Advance Org remotes cleanly (v2 compatible across repos) and run Sync Prod.
            tam_pub = work_push / "agy-tam-plugin"
            (tam_pub / "clients" / "wiz_common.py").write_text(
                'SHARED_CONTRACT = "v2"\nNEW_HELPER = True\n',
                encoding="utf-8",
            )
            _git(tam_pub, "add", "-A")
            _git(tam_pub, "commit", "-m", "feat: tam v2")
            _git(tam_pub, "push", "origin", "main")
            sha_tam_v2 = _git(tam_pub, "rev-parse", "HEAD").stdout.strip()

            ct_pub = work_push / "g-control-tower"
            (ct_pub / "api" / "server.py").write_text(
                "from dev_common import DEV_API_VERSION\n"
                "from wiz_common import SHARED_CONTRACT, NEW_HELPER\n"
                "READY = (DEV_API_VERSION, SHARED_CONTRACT, NEW_HELPER)\n",
                encoding="utf-8",
            )
            _git(ct_pub, "add", "-A")
            _git(ct_pub, "commit", "-m", "feat: ct v2 using NEW_HELPER")
            _git(ct_pub, "push", "origin", "main")
            sha_ct_v2 = _git(ct_pub, "rev-parse", "HEAD").stdout.strip()

            restart_calls: list[str] = []
            sync_ok = git_client.sync_prod_environment(
                prod_repos_dir=prod_repos,
                state_dir=state_dir,
                home_dir=fake_home,
                restart_prod_fn=lambda: (restart_calls.append("restart_8787") or True),
            )
            self.assertEqual(sync_ok["status"], "ok")
            self.assertTrue(sync_ok["synced"])
            self.assertTrue(sync_ok["smoke_check"]["ok"])
            self.assertTrue(sync_ok["control_tower_restarted"])
            self.assertEqual(len(restart_calls), 1)
            self.assertEqual(sync_ok["previous_shas"]["agy-tam-plugin"], sha_tam_v1)
            self.assertEqual(sync_ok["previous_shas"]["g-control-tower"], sha_ct_v1)
            self.assertEqual(sync_ok["current_shas"]["agy-tam-plugin"], sha_tam_v2)
            self.assertEqual(sync_ok["current_shas"]["g-control-tower"], sha_ct_v2)

            state_file = state_dir / dev_common.SYNC_PROD_STATE_FILENAME
            self.assertTrue(state_file.is_file())
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
            deployed_wiz_common = (
                fake_home / ".gemini" / "config" / "plugins" / "wiz-tam-plugin" / "clients" / "wiz_common.py"
            )
            self.assertIn('SHARED_CONTRACT = "v2"', deployed_wiz_common.read_text(encoding="utf-8"))

            # 2) Push a breaking cross-repo ImportError to Org (`agy-tam-plugin` drops `NEW_HELPER` needed by `g-control-tower`).
            (tam_pub / "clients" / "wiz_common.py").write_text('SHARED_CONTRACT = "v3_broken"\n', encoding="utf-8")
            _git(tam_pub, "add", "-A")
            _git(tam_pub, "commit", "-m", "refactor!: remove NEW_HELPER breaking g-control-tower")
            _git(tam_pub, "push", "origin", "main")

            restart_calls.clear()
            sync_broken = git_client.sync_prod_environment(
                prod_repos_dir=prod_repos,
                state_dir=state_dir,
                home_dir=fake_home,
                restart_prod_fn=lambda: (restart_calls.append("restart_8787") or True),
            )
            self.assertEqual(sync_broken["status"], "error")
            self.assertFalse(sync_broken["synced"])
            self.assertEqual(sync_broken["step"], "smoke_check")
            self.assertTrue(sync_broken["aborted_before_restart"])
            self.assertTrue(sync_broken["rolled_back_git"])
            self.assertFalse(sync_broken["control_tower_restarted"])
            self.assertEqual(restart_calls, [], "Broken cross-repo smoke-check MUST abort before restarting :8787")
            # Org clone & $HOME must remain intact on v2!
            head_after_abort = _git(prod_repos / "agy-tam-plugin", "rev-parse", "HEAD").stdout.strip()
            self.assertEqual(head_after_abort, sha_tam_v2)
            self.assertIn('SHARED_CONTRACT = "v2"', deployed_wiz_common.read_text(encoding="utf-8"))

            # 3) 1-Command Rollback restores v1 SHAs, passes smoke-check, redeploys $HOME, and restarts :8787 once.
            rb_res = git_client.rollback_prod_environment(
                prod_repos_dir=prod_repos,
                state_dir=state_dir,
                home_dir=fake_home,
                restart_prod_fn=lambda: (restart_calls.append("restart_8787") or True),
            )
            self.assertEqual(rb_res["status"], "ok")
            self.assertTrue(rb_res["rolled_back"])
            self.assertTrue(rb_res["control_tower_restarted"])
            self.assertEqual(len(restart_calls), 1)
            self.assertEqual(rb_res["current_shas"]["agy-dev-plugin"], sha_dev_v1)
            self.assertEqual(rb_res["current_shas"]["agy-tam-plugin"], sha_tam_v1)
            self.assertEqual(rb_res["current_shas"]["g-control-tower"], sha_ct_v1)
            self.assertIn('SHARED_CONTRACT = "v1"', deployed_wiz_common.read_text(encoding="utf-8"))


class TestSidecarDualEnvEndpoints(unittest.TestCase):
    """Verify Dev Queue Sidecar `/api/state` `dual_env` payload and fail-closed `/api/dual-env/*` routes."""

    def test_sidecar_dual_env_auth_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            fake_home.mkdir()
            state_dir = fake_home / ".local" / "share" / "wiz-customer-portfolio"
            state_dir.mkdir(parents=True)
            (state_dir / "customer_portfolio.json").write_text('{"accounts": []}\n', encoding="utf-8")

            bare_org = _init_bare_repo(root / "remotes" / "acme-org" / "agy-dev-plugin.git")
            bare_fork = _init_bare_repo(root / "remotes" / "fork-user" / "agy-dev-plugin.git")
            seed_dir = root / "seed"
            _clone_and_seed_repo(
                bare_org,
                seed_dir,
                {"plugin.json": json.dumps({"name": "agy-dev-plugin", "version": "1.0.0"})},
            )
            _git(seed_dir, "remote", "add", "fork", str(bare_fork))
            _git(seed_dir, "push", "fork", "main")

            prod_repos = root / "org-prod"
            dev_repos = root / "fork-dev"
            subprocess.run(["git", "clone", str(bare_org), str(prod_repos / "agy-dev-plugin")], capture_output=True, check=True)
            subprocess.run(["git", "clone", str(bare_fork), str(dev_repos / "agy-dev-plugin")], capture_output=True, check=True)
            _git(dev_repos / "agy-dev-plugin", "remote", "add", "upstream", str(bare_org))

            token = "test-dual-env-secret-token"
            srv = panel_server.create_panel_server(
                host="127.0.0.1",
                port=0,
                ui_token=token,
                state_dir=state_dir,
                home_dir=fake_home,
                env={
                    "AGY_PROD_REPOS_DIR": str(prod_repos),
                    "AGY_DEV_REPOS_DIR": str(dev_repos),
                },
            )
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                base_url = f"http://127.0.0.1:{port}"

                # Unauthenticated POST must fail-closed with 401.
                unauth_req = urllib.request.Request(
                    f"{base_url}/api/dual-env/sync-prod",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(unauth_req, timeout=5)
                self.assertEqual(ctx.exception.code, 401)

                def _authed(path: str, method: str = "GET", body: dict[str, object] | None = None) -> dict[str, object]:
                    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
                    req = urllib.request.Request(
                        f"{base_url}{path}",
                        data=data,
                        headers={"X-Sidecar-Token": token, "Content-Type": "application/json"},
                        method=method,
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]

                snap = _authed("/api/state")
                dual_env = snap["dual_env"]  # type: ignore[index]
                self.assertEqual(dual_env["prod_port"], 8787)  # type: ignore[index]
                self.assertEqual(dual_env["dev_port"], 8786)  # type: ignore[index]
                self.assertTrue(dual_env["prod_repos_configured"])  # type: ignore[index]
                self.assertTrue(dual_env["dev_repos_configured"])  # type: ignore[index]

                clone_resp = _authed("/api/dual-env/refresh-dev-state", method="POST", body={})
                self.assertTrue(clone_resp["ok"])
                self.assertTrue(clone_resp["dual_env"]["dev_state_cloned"])  # type: ignore[index]

                prom_resp = _authed("/api/dual-env/promote-org-pr", method="POST", body={})
                self.assertTrue(prom_resp["ok"])
                self.assertEqual(prom_resp["actionable_count"], 0)

                sync_resp = _authed("/api/dual-env/sync-prod", method="POST", body={})
                self.assertTrue(sync_resp["ok"])
                self.assertTrue(sync_resp["synced"])
                self.assertTrue(sync_resp["dual_env"]["can_rollback_prod"])  # type: ignore[index]

                rb_resp = _authed("/api/dual-env/rollback-prod", method="POST", body={})
                self.assertTrue(rb_resp["ok"])
                self.assertTrue(rb_resp["rolled_back"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
