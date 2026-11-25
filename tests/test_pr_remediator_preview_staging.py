#!/usr/bin/env python3
"""Zero-credential hermetic unit tests for Slot 1 `tmp/dev_ui_preview` subtree staging and pruning."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
for _p in (CLIENTS_DIR, PR_REM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Prevent inherited git hook GIT_* variables from hijacking temp-repo subprocesses during tests.
for _git_hook_var in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
):
    os.environ.pop(_git_hook_var, None)

import dev_common  # noqa: E402
import git_client  # noqa: E402
import pr_remediator as prs  # noqa: E402


def _init_org_bare_and_clone_repo(
    remotes_org: pathlib.Path,
    checkouts_dir: pathlib.Path,
    name: str = "tower-app",
) -> tuple[pathlib.Path, pathlib.Path]:
    """Create a hermetic bare origin remote and local clone with an initial commit on `main`."""
    bare = remotes_org / f"{name}.git"
    clone = checkouts_dir / name
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Acme Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "acme-bot@example.com"], check=True, capture_output=True)
    (clone / "README.md").write_text("# Acme Tower App\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "chore: initial commit"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return bare, clone


def _commit_worktree_changes(wt_path: pathlib.Path, message: str = "feat: update web bundle") -> None:
    """Stage and commit all worktree modifications locally."""
    subprocess.run(["git", "-C", str(wt_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m", message], check=True, capture_output=True)


class TestPrRemediatorPreviewStaging(unittest.TestCase):
    """Hermetic tests for Slot 1 `tmp/dev_ui_preview` subtree staging, pruning, and Slot 2 isolation."""

    def test_slot1_stages_web_css_and_js_subtrees_skips_garbage_and_touches_utime(self) -> None:
        """Slot 1 staging must copy web/css and web/js subtrees (symlinks=False), ignore cache artifacts, and touch os.utime on sub-assets."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "tower-app")
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo)})

            start_res = prs.start_remediation_branch_and_lock(
                conversation_id="11111111-2222-4333-8444-555555555555",
                action_id="preview-subtrees",
                rca_summary="Modular web/css and web/js subtrees omitted from dev_ui_preview",
                proposed_fix="Copy web/css and web/js subtrees and touch utime",
                create_worktree=True,
                branch_timestamp="20261123-101000",
                workdir=repo,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(start_res["status"], "ok")
            self.assertEqual(start_res["slot_id"], 1)
            wt_path = pathlib.Path(start_res["worktree_path"])

            # Populate modular web/ bundle + api/ with nested subdirs, a symlink, and ignored files.
            (wt_path / "web" / "css").mkdir(parents=True, exist_ok=True)
            (wt_path / "web" / "js" / "core").mkdir(parents=True, exist_ok=True)
            (wt_path / "web" / "js" / "__pycache__").mkdir(parents=True, exist_ok=True)
            (wt_path / "api" / "__pycache__").mkdir(parents=True, exist_ok=True)

            (wt_path / "web" / "index.html").write_text("<html><body>Tower</body></html>\n", encoding="utf-8")
            (wt_path / "web" / "styles.css").write_text("body { margin: 0; }\n", encoding="utf-8")
            (wt_path / "web" / "favicon.svg").write_text("<svg></svg>\n", encoding="utf-8")
            # Intentionally omit dashboard.js to verify optional top-level files are handled cleanly.
            (wt_path / "web" / "css" / "tokens.css").write_text(":root { --bg: #111; }\n", encoding="utf-8")
            (wt_path / "web" / "css" / "base.css").write_text(".hidden { display: none; }\n", encoding="utf-8")
            (wt_path / "web" / "css" / ".DS_Store").write_text("junk", encoding="utf-8")
            (wt_path / "web" / "js" / "core" / "state.mjs").write_text("export const state = {};\n", encoding="utf-8")
            (wt_path / "web" / "js" / "core" / "bad.pyc").write_bytes(b"\x00pyc")
            (wt_path / "web" / "js" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00pyc")
            (wt_path / "web" / "js" / "core" / "linked.mjs").symlink_to(wt_path / "web" / "js" / "core" / "state.mjs")
            (wt_path / "api" / "server.py").write_text("APP_VERSION = 1\n", encoding="utf-8")
            (wt_path / "api" / "__pycache__" / "server.pyc").write_bytes(b"\x00pyc")

            _commit_worktree_changes(wt_path, "feat(web): modular css and js")

            touched_paths: list[pathlib.Path] = []
            real_utime = os.utime

            def _record_utime(
                path: os.PathLike[str] | str,
                times: tuple[float, float] | None = None,
                **kwargs: object,
            ) -> None:
                if times is None and "ns" not in kwargs:
                    touched_paths.append(pathlib.Path(path))
                real_utime(path, times, **kwargs)  # type: ignore[arg-type]

            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "stopped", "stopped": True}) as mock_stop,
                mock.patch.object(prs, "restart_pro_api_server", return_value={"status": "ok"}) as mock_restart,
                mock.patch.object(prs, "build_pro_dashboard_ui_url", return_value="http://127.0.0.1:8787/ui#health"),
                mock.patch.object(prs.os, "utime", side_effect=_record_utime),
            ):
                staged = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="feat(web): modular css and js",
                    executive_summary="Stage web/css and web/js subtrees.",
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs,
                )

            self.assertEqual(staged["status"], "ok")
            mock_stop.assert_called_once()
            mock_restart.assert_called_once()

            preview_dir = dev_common.resolve_state_dir(portfolio_dir=pdir) / "tmp" / "dev_ui_preview"
            self.assertTrue((preview_dir / "index.html").is_file())
            self.assertTrue((preview_dir / "styles.css").is_file())
            self.assertTrue((preview_dir / "favicon.svg").is_file())
            self.assertFalse((preview_dir / "dashboard.js").exists())

            self.assertEqual((preview_dir / "css" / "tokens.css").read_text(encoding="utf-8"), ":root { --bg: #111; }\n")
            self.assertEqual((preview_dir / "css" / "base.css").read_text(encoding="utf-8"), ".hidden { display: none; }\n")
            self.assertFalse((preview_dir / "css" / ".DS_Store").exists())

            staged_state = preview_dir / "js" / "core" / "state.mjs"
            staged_linked = preview_dir / "js" / "core" / "linked.mjs"
            self.assertTrue(staged_state.is_file())
            self.assertTrue(staged_linked.is_file())
            self.assertFalse(staged_linked.is_symlink(), "symlinks=False must dereference staged sub-assets")
            self.assertEqual(staged_linked.read_text(encoding="utf-8"), "export const state = {};\n")
            self.assertFalse((preview_dir / "js" / "core" / "bad.pyc").exists())
            self.assertFalse((preview_dir / "js" / "__pycache__").exists())

            self.assertTrue((preview_dir / "api" / "server.py").is_file())
            self.assertFalse((preview_dir / "api" / "__pycache__").exists())

            expected_touched = {
                preview_dir / "index.html",
                preview_dir / "styles.css",
                preview_dir / "favicon.svg",
                preview_dir / "css" / "tokens.css",
                preview_dir / "css" / "base.css",
                preview_dir / "js" / "core" / "state.mjs",
                preview_dir / "js" / "core" / "linked.mjs",
            }
            self.assertEqual(set(touched_paths), expected_touched)

    def test_slot1_prunes_stale_css_js_and_api_across_branches(self) -> None:
        """Slot 1 staging must prune stale css, js, and api files/dirs even when absent in the new branch."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "tower-app")
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo)})

            start_res = prs.start_remediation_branch_and_lock(
                conversation_id="22222222-3333-4444-8555-666666666666",
                action_id="preview-prune",
                rca_summary="Stale preview subdirs lingered across branches",
                proposed_fix="Prune preview_dir/css, preview_dir/js, and preview_dir/api before copying",
                create_worktree=True,
                branch_timestamp="20261123-101500",
                workdir=repo,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(start_res["status"], "ok")
            wt_path = pathlib.Path(start_res["worktree_path"])

            # Pre-seed stale artifacts from an earlier branch inside preview_dir.
            preview_dir = dev_common.resolve_state_dir(portfolio_dir=pdir) / "tmp" / "dev_ui_preview"
            (preview_dir / "css").mkdir(parents=True, exist_ok=True)
            (preview_dir / "css" / "stale_old.css").write_text("/* stale */\n", encoding="utf-8")
            (preview_dir / "js" / "legacy").mkdir(parents=True, exist_ok=True)
            (preview_dir / "js" / "legacy" / "old.js").write_text("// stale\n", encoding="utf-8")
            (preview_dir / "api").mkdir(parents=True, exist_ok=True)
            (preview_dir / "api" / "stale_endpoint.py").write_text("OLD = True\n", encoding="utf-8")

            # New branch has web/index.html and web/css/new.css ONLY (no web/js/ and no api/).
            (wt_path / "web" / "css").mkdir(parents=True, exist_ok=True)
            (wt_path / "web" / "index.html").write_text("<html><body>Clean</body></html>\n", encoding="utf-8")
            (wt_path / "web" / "css" / "new.css").write_text(".ok { color: green; }\n", encoding="utf-8")
            _commit_worktree_changes(wt_path, "feat(web): remove legacy js and api")

            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "stopped", "stopped": True}),
                mock.patch.object(prs, "restart_pro_api_server", return_value={"status": "ok"}),
                mock.patch.object(prs, "build_pro_dashboard_ui_url", return_value="http://127.0.0.1:8787/ui#health"),
            ):
                staged = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="feat(web): remove legacy js and api",
                    executive_summary="Verify stale subtrees are pruned.",
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs,
                )

            self.assertEqual(staged["status"], "ok")
            self.assertTrue((preview_dir / "css" / "new.css").is_file())
            self.assertFalse((preview_dir / "css" / "stale_old.css").exists())
            self.assertFalse((preview_dir / "js").exists(), "Absent web/js must prune stale preview_dir/js")
            self.assertFalse((preview_dir / "api").exists(), "Absent api/ must prune stale preview_dir/api")

    def test_slot2_does_not_overwrite_dev_ui_preview(self) -> None:
        """Slot 2 staging must leave Slot 1's tmp/dev_ui_preview completely untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "tower-app")
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo)})

            # Acquire Slot 1 on docs/ so Slot 2 can acquire web/ + api/ cleanly.
            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="33333333-4444-4555-8666-777777777777",
                action_id="slot1-docs",
                rca_summary="Slot 1 docs change",
                proposed_fix="Update docs",
                target_files=["README.md"],
                create_worktree=True,
                branch_timestamp="20261123-102000",
                workdir=repo,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(s1["status"], "ok")
            self.assertEqual(s1["slot_id"], 1)

            s2 = prs.start_remediation_branch_and_lock(
                conversation_id="44444444-5555-4666-8777-888888888888",
                action_id="slot2-web",
                rca_summary="Slot 2 web change",
                proposed_fix="Must not touch dev_ui_preview",
                target_files=["web/index.html", "web/css/slot2.css"],
                create_worktree=True,
                allow_slot2=True,
                branch_timestamp="20261123-102100",
                workdir=repo,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(s2["status"], "ok")
            self.assertEqual(s2["slot_id"], 2)
            wt2 = pathlib.Path(s2["worktree_path"])

            # Populate Slot 1 preview_dir sentinels.
            preview_dir = dev_common.resolve_state_dir(portfolio_dir=pdir) / "tmp" / "dev_ui_preview"
            (preview_dir / "css").mkdir(parents=True, exist_ok=True)
            (preview_dir / "js").mkdir(parents=True, exist_ok=True)
            (preview_dir / "api").mkdir(parents=True, exist_ok=True)
            (preview_dir / "index.html").write_text("SLOT1_INDEX", encoding="utf-8")
            (preview_dir / "css" / "slot1.css").write_text("SLOT1_CSS", encoding="utf-8")
            (preview_dir / "js" / "slot1.mjs").write_text("SLOT1_JS", encoding="utf-8")
            (preview_dir / "api" / "slot1.py").write_text("SLOT1_API", encoding="utf-8")

            # Populate Slot 2 worktree with a completely different web/ + api/ bundle.
            (wt2 / "web" / "css").mkdir(parents=True, exist_ok=True)
            (wt2 / "web" / "js").mkdir(parents=True, exist_ok=True)
            (wt2 / "api").mkdir(parents=True, exist_ok=True)
            (wt2 / "web" / "index.html").write_text("SLOT2_INDEX", encoding="utf-8")
            (wt2 / "web" / "css" / "slot2.css").write_text("SLOT2_CSS", encoding="utf-8")
            (wt2 / "web" / "js" / "slot2.mjs").write_text("SLOT2_JS", encoding="utf-8")
            (wt2 / "api" / "slot2.py").write_text("SLOT2_API = 1\n", encoding="utf-8")
            _commit_worktree_changes(wt2, "feat(web): slot 2 bundle")

            with (
                mock.patch.object(prs, "stop_dev_api_server") as mock_stop,
                mock.patch.object(prs, "restart_pro_api_server") as mock_restart,
                mock.patch.object(prs, "build_pro_dashboard_ui_url", return_value="http://127.0.0.1:8787/ui#health"),
            ):
                staged = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="feat(web): slot 2 bundle",
                    executive_summary="Slot 2 must not touch Slot 1 preview.",
                    workdir=wt2,
                    portfolio_dir=pdir,
                    git_client=vcs,
                )

            self.assertEqual(staged["status"], "ok")
            mock_stop.assert_not_called()
            mock_restart.assert_not_called()

            self.assertEqual((preview_dir / "index.html").read_text(encoding="utf-8"), "SLOT1_INDEX")
            self.assertEqual((preview_dir / "css" / "slot1.css").read_text(encoding="utf-8"), "SLOT1_CSS")
            self.assertEqual((preview_dir / "js" / "slot1.mjs").read_text(encoding="utf-8"), "SLOT1_JS")
            self.assertEqual((preview_dir / "api" / "slot1.py").read_text(encoding="utf-8"), "SLOT1_API")
            self.assertFalse((preview_dir / "css" / "slot2.css").exists())
            self.assertFalse((preview_dir / "js" / "slot2.mjs").exists())
            self.assertFalse((preview_dir / "api" / "slot2.py").exists())


if __name__ == "__main__":
    unittest.main()
