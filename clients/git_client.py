#!/usr/bin/env python3
"""Layer 1 VCS-Interoperable Native Git & SSH Integration Client (`git_vcs.py`).

Provides 100% VCS-interoperable (GitHub, GitLab, Bitbucket, Gitea, self-hosted Git) operations
using exclusively native `/usr/bin/git` and `/usr/bin/ssh` with zero proprietary REST API calls:
  - Repository & remote URL discovery (`resolve_repo_dir`, `resolve_remote_url`, `resolve_repo`)
  - Dynamic VCS provider & web compare/MR URL builder (`detect_vcs_provider_from_remote`, `build_vcs_manual_pr_url`)
  - Native SSH & Git authentication check (`check_ssh_git_auth`, `auth_status`, `probe_health`)
  - Unified branch diff, changed files & commit log (`get_branch_diff`)
  - Protected timestamped branch push (`push_branch` — refuses pushing `main`/`master`)
  - Remote branch existence & ancestor-merge inspection (`inspect_remote_branch_state`)
  - Native local & remote branch deletion (`delete_branch_native`)
  - Post-merge `main` fast-forward synchronization & remote-cleaned local branch/worktree pruning (`sync_and_clean_local_repo`)"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_CLIENTS_DIR = _Path(__file__).resolve().parent
if str(_CLIENTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CLIENTS_DIR))
import dev_common as _dev_common  # noqa: F401, E402

import argparse
import contextlib
import json
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Literal
import urllib.parse

from dev_common import (
    BaseIntegrationClient,
    HealthProbeResultDTO,
    load_env_into_os,
    try_restart_control_tower_api,
)

DEFAULT_GIT_REPO = "carlosrodlop/agy-dev-plugin"
VERSION_STAMP_RE = re.compile(r"^[0-9a-f]{7,12}-\d{8}$")
PEP440_VERSION_STAMP_RE = re.compile(r"^\d{8}\+g[0-9a-f]{7,12}$")


def resolve_repo_version_stamp(
    repo_dir: Path | str,
    ref: str = "HEAD",
    date_override: str = "",
) -> tuple[str, str]:
    """Resolve `(<short_sha>-<YYYYMMDD>, <YYYYMMDD>+g<short_sha>)` from a Git repository ref (UTC).

    Falls back to reading `plugin.json` or `api/__init__.py` when `.git` is unavailable.
    """
    target_dir = Path(repo_dir).expanduser().resolve()
    clean_date = re.sub(r"[^0-9]", "", (date_override or "").strip())[:8]
    short_sha = ""
    commit_date = ""

    if (target_dir / ".git").exists():
        env_utc = {**os.environ, "TZ": "UTC"}
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(target_dir),
                "log",
                "-1",
                "--format=%h:%cd",
                "--date=format-local:%Y%m%d",
                ref,
            ],
            env=env_utc,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and ":" in (proc.stdout or ""):
            raw_sha, raw_date = (proc.stdout or "").strip().split(":", 1)
            cand_sha = raw_sha.strip().lower()[:7]
            cand_date = re.sub(r"[^0-9]", "", raw_date.strip())[:8]
            if re.match(r"^[0-9a-f]{7,12}$", cand_sha):
                short_sha = cand_sha
            if len(cand_date) == 8:
                commit_date = cand_date

    if len(clean_date) == 8:
        commit_date = clean_date

    if short_sha and len(commit_date) == 8:
        return f"{short_sha}-{commit_date}", f"{commit_date}+g{short_sha}"

    plugin_json = target_dir / "plugin.json"
    if plugin_json.is_file():
        with contextlib.suppress(Exception):
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
            ver = str(data.get("version") or "").strip()
            if VERSION_STAMP_RE.match(ver):
                s_part, d_part = ver.split("-", 1)
                return ver, f"{d_part}+g{s_part}"

    api_init = target_dir / "api" / "__init__.py"
    if api_init.is_file():
        with contextlib.suppress(Exception):
            m_ver = re.search(
                r'(?:_FALLBACK_VERSION|__version__)\s*=\s*"([0-9a-f]{7,12}-\d{8})"',
                api_init.read_text(encoding="utf-8"),
            )
            if m_ver:
                ver = m_ver.group(1)
                s_part, d_part = ver.split("-", 1)
                return ver, f"{d_part}+g{s_part}"

    fallback_sha = short_sha or "0000000"
    fallback_date = commit_date if len(commit_date) == 8 else "19700101"
    return f"{fallback_sha}-{fallback_date}", f"{fallback_date}+g{fallback_sha}"


def stamp_plugin_json_bytes(raw_bytes: bytes, version_stamp: str) -> bytes:
    """Return formatted `plugin.json` UTF-8 bytes with `"version"` set to `version_stamp`."""
    if not VERSION_STAMP_RE.match((version_stamp or "").strip()):
        return raw_bytes
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return raw_bytes
        data["version"] = version_stamp.strip()
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except Exception:
        return raw_bytes


def stamp_repo_version_manifests(
    repo_dir: Path | str,
    branch_timestamp: str = "",
    base_ref: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Synchronize `plugin.json` / `api/__init__.py` (`<short_sha>-<YYYYMMDD>`) and `pyproject.toml` (`<YYYYMMDD>+g<short_sha>`).

    On `fix/<YYYYMMDD>-<HHMMSS>-*` branches, derives `<short_sha>` from `merge-base(HEAD, origin/main)` and
    `<YYYYMMDD>` from the branch timestamp so `pre-commit` is 100% idempotent across commits and CI runs.
    """
    target_dir = Path(repo_dir).expanduser().resolve()
    branch_name = GitVcsIntegrationClient._read_local_head_branch(target_dir) if (target_dir / ".git").exists() else ""
    m_fix_date = re.match(r"^fix/(\d{8})-\d{6}\b", branch_name)
    explicit_date = re.sub(r"[^0-9]", "", (branch_timestamp or "").strip())[:8]
    branch_date = explicit_date if len(explicit_date) == 8 else (m_fix_date.group(1) if m_fix_date else "")

    plugin_path = target_dir / "plugin.json"
    api_init_path = target_dir / "api" / "__init__.py"
    pyproject_path = target_dir / "pyproject.toml"
    if not plugin_path.is_file() and not api_init_path.is_file() and not pyproject_path.is_file():
        return {
            "status": "ok",
            "updated": False,
            "updated_files": [],
            "version": "",
            "pep440_version": "",
        }

    existing_ver = ""
    if plugin_path.is_file():
        with contextlib.suppress(Exception):
            existing_ver = str(json.loads(plugin_path.read_text(encoding="utf-8")).get("version") or "").strip()
    elif api_init_path.is_file():
        with contextlib.suppress(Exception):
            m_fb = re.search(
                r'(?:_FALLBACK_VERSION|__version__)\s*=\s*"([^"]+)"',
                api_init_path.read_text(encoding="utf-8"),
            )
            if m_fb:
                existing_ver = m_fb.group(1).strip()

    existing_pep440 = ""
    if pyproject_path.is_file():
        with contextlib.suppress(Exception):
            m_py = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_path.read_text(encoding="utf-8"))
            if m_py:
                existing_pep440 = m_py.group(1).strip()

    if not force and not branch_date and VERSION_STAMP_RE.match(existing_ver):
        s_part, d_part = existing_ver.split("-", 1)
        expected_pep = f"{d_part}+g{s_part}"
        if not pyproject_path.is_file() or existing_pep440 == expected_pep:
            return {
                "status": "ok",
                "updated": False,
                "updated_files": [],
                "version": existing_ver,
                "pep440_version": expected_pep,
            }

    resolved_ref = (base_ref or "").strip()
    if not resolved_ref and (target_dir / ".git").exists():
        if branch_name.startswith("fix/"):
            for cand_base in ("origin/main", "main"):
                proc_mb = subprocess.run(
                    ["git", "-C", str(target_dir), "merge-base", "HEAD", cand_base],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if proc_mb.returncode == 0 and (proc_mb.stdout or "").strip():
                    resolved_ref = (proc_mb.stdout or "").strip()
                    break
        if not resolved_ref:
            resolved_ref = "HEAD"

    version_stamp, pep440_stamp = resolve_repo_version_stamp(
        target_dir,
        ref=resolved_ref or "HEAD",
        date_override=branch_date,
    )
    updated_files: list[str] = []

    if plugin_path.is_file():
        raw_plugin = plugin_path.read_bytes()
        new_plugin = stamp_plugin_json_bytes(raw_plugin, version_stamp)
        if new_plugin != raw_plugin:
            plugin_path.write_bytes(new_plugin)
            updated_files.append("plugin.json")

    if api_init_path.is_file():
        raw_api = api_init_path.read_text(encoding="utf-8")
        if "_FALLBACK_VERSION" in raw_api:
            new_api = re.sub(
                r'(_FALLBACK_VERSION\s*=\s*)"[^"]*"',
                rf'\g<1>"{version_stamp}"',
                raw_api,
                count=1,
            )
        else:
            new_api = re.sub(
                r'(__version__\s*=\s*)"[^"]*"',
                rf'\g<1>"{version_stamp}"',
                raw_api,
                count=1,
            )
        if new_api != raw_api:
            api_init_path.write_text(new_api, encoding="utf-8")
            updated_files.append("api/__init__.py")

    if pyproject_path.is_file():
        raw_pyproj = pyproject_path.read_text(encoding="utf-8")
        new_pyproj = re.sub(
            r'(?m)^(version\s*=\s*)"[^"]*"',
            rf'\g<1>"{pep440_stamp}"',
            raw_pyproj,
            count=1,
        )
        if new_pyproj != raw_pyproj:
            pyproject_path.write_text(new_pyproj, encoding="utf-8")
            updated_files.append("pyproject.toml")

    return {
        "status": "ok",
        "updated": bool(updated_files),
        "updated_files": updated_files,
        "version": version_stamp,
        "pep440_version": pep440_stamp,
    }


def _maybe_restart_control_tower_after_deploy(
    deployed_files: list[str],
    target_home: Path | None = None,
    *,
    _allow_in_tests: bool = False,
) -> bool:
    """Restart the Control Tower daemon after real `$HOME` deploys that updated `.py`/`.json` files."""
    if (
        (not _allow_in_tests and ("unittest" in sys.modules or "pytest" in sys.modules))
        or os.environ.get("AGY_OFFLINE_ONLY") == "1"
        or os.environ.get("AGY_DISABLE_API_AUTOSTART") == "1"
        or os.environ.get("PRE_COMMIT_HOOK") == "1"
    ):
        return False
    if target_home is not None and Path(target_home).expanduser().resolve() != Path.home().resolve():
        return False
    if not any(str(f).endswith((".py", ".json")) for f in deployed_files):
        return False
    try:
        return bool(try_restart_control_tower_api())
    except Exception:
        return False


def parse_git_repo_slug(raw_url_or_slug: str) -> str:
    """Extract `owner/repo` from any Git remote URL (GitHub, GitLab, Bitbucket, etc.) or `owner/repo` string."""
    cleaned = (raw_url_or_slug or "").strip()
    if not cleaned:
        return DEFAULT_GIT_REPO
    m = re.search(r"[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", cleaned)
    if m and not cleaned.startswith("/"):
        return m.group(1)
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", cleaned):
        return cleaned.removesuffix(".git")
    return DEFAULT_GIT_REPO


def detect_vcs_provider_from_remote(remote_url: str) -> tuple[str, str, str]:
    """Return `(provider, web_base_url, repo_slug)` from any Git SSH or HTTPS remote URL.

    Works natively across GitHub, GitLab, Bitbucket, Gitea, and self-hosted Git remotes.
    """
    cleaned = (remote_url or "").strip()
    host = "github.com"
    slug = parse_git_repo_slug(cleaned)
    m_ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?$", cleaned)
    m_http = re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?$", cleaned)
    if m_ssh:
        host = m_ssh.group(1).strip()
        slug = m_ssh.group(2).strip().strip("/")
    elif m_http:
        host = m_http.group(1).strip()
        slug = m_http.group(2).strip().strip("/")

    host_lower = host.lower()
    if "gitlab" in host_lower:
        provider = "gitlab"
    elif "bitbucket" in host_lower:
        provider = "bitbucket"
    elif "github" in host_lower:
        provider = "github"
    else:
        provider = "generic-git"
    web_base = f"https://{host}/{slug}"
    return provider, web_base, slug


def build_vcs_manual_pr_url(
    branch_name: str = "",
    base_branch: str = "main",
    remote_url: str = "",
    *,
    head_branch: str = "",
    title: str = "",
    body: str = "",
) -> str:
    """Build a VCS-agnostic manual Pull Request / Merge Request compare URL from the native `git remote` URL."""
    if head_branch and not remote_url and ("@" in branch_name or "://" in branch_name):
        remote_url = branch_name
        resolved_branch = head_branch.strip()
    else:
        resolved_branch = (branch_name or head_branch).strip()
    provider, web_base, _ = detect_vcs_provider_from_remote(remote_url)
    enc_branch = urllib.parse.quote(resolved_branch, safe="/")
    enc_base = urllib.parse.quote((base_branch or "main").strip(), safe="/")
    clean_title = (title or "").strip()
    clean_body = (body or "").strip()
    if len(clean_body) > 5000:
        clean_body = clean_body[:4980].rstrip() + "\n...(truncated)"
    enc_title = urllib.parse.quote(clean_title, safe="") if clean_title else ""
    enc_body = urllib.parse.quote(clean_body, safe="") if clean_body else ""

    if provider == "gitlab":
        url = (
            f"{web_base}/-/merge_requests/new?"
            f"merge_request%5Bsource_branch%5D={enc_branch}&merge_request%5Btarget_branch%5D={enc_base}"
        )
        if enc_title:
            url += f"&merge_request%5Btitle%5D={enc_title}"
        if enc_body:
            url += f"&merge_request%5Bdescription%5D={enc_body}"
        return url
    if provider == "bitbucket":
        url = f"{web_base}/pull-requests/new?source={enc_branch}&dest={enc_base}"
        if enc_title:
            url += f"&title={enc_title}"
        if enc_body:
            url += f"&description={enc_body}"
        return url
    if provider == "github":
        url = f"{web_base}/compare/{enc_base}...{enc_branch}?expand=1"
        if enc_title:
            url += f"&title={enc_title}"
        if enc_body:
            url += f"&body={enc_body}"
        return url
    return f"{web_base}/tree/{enc_branch}"


class GitVcsIntegrationClient(BaseIntegrationClient):
    """Layer 1 VCS-Interoperable Native Git & SSH Integration Client (`git_client`)."""

    system_id = "git_client"
    display_name = "VCS-Interoperable Native Git & SSH Client"
    category = "adhoc_client"

    def __init__(
        self,
        repo: str = "",
        env_file: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        super().__init__(env_file=env_file, env_overrides=env_overrides)
        self._explicit_repo = repo.strip()
        self._explicit_repo_dir = str((env_overrides or {}).get("AGY_CONF_REPO_DIR") or "").strip()

    def resolve_repo_dir(self) -> Path:
        """Resolve the local Git repository path (`agy-dev-plugin` or active plugin checkout)."""
        configured = self.get_config("AGY_CONF_REPO_DIR", "")
        if configured and not configured.startswith("<"):
            p = Path(configured).expanduser()
            if (p / ".git").exists():
                return p
        candidate = Path(__file__).resolve()
        for parent in candidate.parents:
            if (parent / ".git").exists():
                return parent
        try:
            cwd = Path.cwd().resolve()
            for parent in (cwd, *cwd.parents):
                if (parent / ".git").exists():
                    return parent
        except OSError:
            pass
        default_dir = Path.home() / "Code" / "Github" / "carlosrodlop" / "agy-dev-plugin"
        if (default_dir / ".git").exists():
            return default_dir
        gh_root = Path.home() / "Code" / "Github"
        if gh_root.is_dir():
            for plugin_manifest in sorted(gh_root.glob("*/*/plugin.json")):
                repo_root = plugin_manifest.parent
                if (repo_root / ".git").exists():
                    return repo_root
        return default_dir

    def resolve_active_repo_dir(
        self,
        workdir: Path | str | None = None,
        prefer_cwd: bool = True,
    ) -> Path:
        """Resolve the target Git repository directory, honoring an explicit `workdir` or companion repository `Path.cwd()` before falling back to `resolve_repo_dir()`."""
        if workdir:
            cand = Path(str(workdir).replace("$HOME", str(Path.home()))).expanduser()
            if (cand / ".git").exists():
                return cand.resolve()
            for parent in cand.parents:
                if (parent / ".git").exists():
                    return parent.resolve()
        base_repo = self.resolve_repo_dir()
        if (
            prefer_cwd
            and not self._explicit_repo_dir
            and not hasattr(self.resolve_repo_dir, "_mock_name")
            and (base_repo / "plugin.json").is_file()
        ):
            try:
                cwd = Path.cwd().resolve()
                for cand_dir in (cwd, *cwd.parents):
                    if (cand_dir / ".git").exists():
                        if cand_dir != base_repo.resolve() and (cand_dir / "pyproject.toml").is_file():
                            return cand_dir
                        break
            except OSError:
                pass
        return base_repo

    @staticmethod
    def _read_local_head_branch(target_dir: Path) -> str:
        """Read current branch name from `.git/HEAD` without spawning a subprocess when possible."""
        head_file = target_dir / ".git" / "HEAD"
        if head_file.is_file():
            try:
                head_txt = head_file.read_text(encoding="utf-8", errors="ignore").strip()
                if head_txt.startswith("ref: refs/heads/"):
                    branch = head_txt[len("ref: refs/heads/") :].strip()
                    if branch and branch != ".invalid":
                        return branch
            except OSError:
                pass
        proc_cur = subprocess.run(
            ["git", "-C", str(target_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc_cur.stdout or "").strip()

    @staticmethod
    def _read_local_ref_sha(target_dir: Path, ref_rel_path: str) -> str:
        """Read a loose ref SHA directly from `.git/<ref_rel_path>` if present."""
        ref_file = target_dir / ".git" / ref_rel_path
        if ref_file.is_file():
            try:
                sha = ref_file.read_text(encoding="utf-8", errors="ignore").strip()
                if len(sha) == 40:
                    return sha
            except OSError:
                pass
        return ""

    def resolve_remote_url(self, remote: str = "origin", repo_dir: Path | None = None) -> str:
        """Return the configured Git remote URL using `.git/config` or native `/usr/bin/git`."""
        target_dir = repo_dir or self.resolve_repo_dir()
        cfg_file = target_dir / ".git" / "config"
        if cfg_file.is_file():
            try:
                cfg_text = cfg_file.read_text(encoding="utf-8", errors="ignore")
                m_sec = re.search(
                    rf'\[remote "{re.escape(remote)}"\]\s*\n((?:[ \t]+[^\n]*\n?)*)',
                    cfg_text,
                )
                if m_sec:
                    m_url = re.search(r"^\s*url\s*=\s*(.+)$", m_sec.group(1), re.MULTILINE)
                    if m_url and m_url.group(1).strip():
                        return m_url.group(1).strip()
            except OSError:
                pass
        if (target_dir / ".git").exists():
            try:
                res = subprocess.run(
                    ["git", "-C", str(target_dir), "remote", "get-url", remote],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                pass
        slug = parse_git_repo_slug(self._explicit_repo) if self._explicit_repo else DEFAULT_GIT_REPO
        return f"git@github.com:{slug}.git"

    def resolve_repo(self, repo_dir: Path | None = None) -> str:
        """Resolve the `owner/repo` slug from explicit argument or native `git remote get-url origin`."""
        if self._explicit_repo and repo_dir is None:
            return parse_git_repo_slug(self._explicit_repo)
        return parse_git_repo_slug(self.resolve_remote_url(repo_dir=repo_dir))

    def resolve_ssh_host_from_remote(self, remote_url: str = "") -> str:
        """Extract `git@<host>` target from the configured Git remote URL."""
        url = (remote_url or self.resolve_remote_url()).strip()
        m_ssh = re.match(r"^(?:ssh://)?(git@[^:/]+)[:/]", url)
        if m_ssh:
            return m_ssh.group(1)
        m_http = re.match(r"^https?://([^/]+)/", url)
        if m_http:
            return f"git@{m_http.group(1)}"
        return "git@github.com"

    def check_ssh_git_auth(self, timeout: float = 6.0) -> dict[str, Any]:
        """Verify SSH authentication against the configured Git remote host using `/usr/bin/ssh`."""
        ssh_key = Path.home() / ".ssh" / "id_ed25519"
        ssh_target = self.resolve_ssh_host_from_remote()
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]
        if ssh_key.is_file():
            cmd.extend(["-i", str(ssh_key), "-o", "IdentitiesOnly=yes"])
        cmd.extend(["-T", ssh_target])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            combined = f"{proc.stdout}\n{proc.stderr}".strip()
            m = re.search(
                r"(?:Hi|Welcome to GitLab,?\s*@?)\s*([A-Za-z0-9_.-]+)!?.*(?:successfully authenticated|!)",
                combined,
                re.IGNORECASE,
            )
            if m or proc.returncode == 0 or "successfully authenticated" in combined.lower():
                return {
                    "authenticated": True,
                    "username": m.group(1) if m else "git-ssh-user",
                    "ssh_target": ssh_target,
                    "ssh_key": str(ssh_key) if ssh_key.is_file() else "ssh-agent",
                    "message": combined.splitlines()[0] if combined else "SSH authenticated",
                }
            return {
                "authenticated": False,
                "ssh_target": ssh_target,
                "error": combined[:240] or f"ssh -T {ssh_target} exited with {proc.returncode}",
            }
        except Exception as exc:
            return {"authenticated": False, "ssh_target": ssh_target, "error": str(exc)}

    def auth_status(self, live: bool = True) -> dict[str, Any]:
        """Check native `/usr/bin/git`, local repository, and SSH authentication status."""
        t0 = time.monotonic()
        repo_dir = self.resolve_repo_dir()
        remote_url = self.resolve_remote_url(repo_dir=repo_dir)
        provider, web_base, repo_slug = detect_vcs_provider_from_remote(remote_url)
        git_bin = shutil.which("git") or "/usr/bin/git"
        ssh_key = Path.home() / ".ssh" / "id_ed25519"
        repo_exists = (repo_dir / ".git").exists()

        if not live:
            ok = bool(Path(git_bin).is_file() and (repo_exists or ssh_key.is_file()))
            elapsed_ms = int(round((time.monotonic() - t0) * 1000))
            return {
                "status": "ok" if ok else "error",
                "authenticated": ok,
                "auth_mode": "native_git_ssh_offline",
                "vcs_provider": provider,
                "repo": repo_slug,
                "remote_url": remote_url,
                "web_base_url": web_base,
                "repo_dir": str(repo_dir),
                "git_binary": git_bin,
                "ssh_key_file": str(ssh_key),
                "account": repo_slug.split("/")[0] if "/" in repo_slug else "git-local",
                "latency_ms": elapsed_ms,
                "details": f"Native git ({git_bin}) & repository ({repo_slug}) verified offline",
            }

        ssh_res = self.check_ssh_git_auth()
        elapsed_ms = int(round((time.monotonic() - t0) * 1000))
        if ssh_res.get("authenticated"):
            return {
                "status": "ok",
                "authenticated": True,
                "auth_mode": "native_git_ssh",
                "vcs_provider": provider,
                "repo": repo_slug,
                "remote_url": remote_url,
                "web_base_url": web_base,
                "repo_dir": str(repo_dir),
                "git_binary": git_bin,
                "ssh_key_file": str(ssh_key),
                "account": ssh_res.get("username", "git-ssh"),
                "latency_ms": elapsed_ms,
                "details": f"Native Git + SSH authenticated ({ssh_res.get('message', 'OK')}) for {repo_slug}",
            }
        return {
            "status": "error",
            "authenticated": False,
            "auth_mode": "none",
            "vcs_provider": provider,
            "repo": repo_slug,
            "remote_url": remote_url,
            "repo_dir": str(repo_dir),
            "ssh_key_file": str(ssh_key),
            "latency_ms": elapsed_ms,
            "error": f"Native Git SSH authentication failed: {ssh_res.get('error', 'SSH not configured')}",
        }

    def probe_health(self, live: bool = False) -> HealthProbeResultDTO:
        """Implement `BaseIntegrationClient.probe_health` for `agy-system-health-check`."""
        info = self.auth_status(live=live)
        st: Literal["ok", "degraded", "error"] = "ok" if info.get("status") == "ok" else "error"
        return HealthProbeResultDTO(
            system_id=self.system_id,
            display_name=self.display_name,
            category=self.category,
            status=st,
            authenticated_account=str(info.get("account") or ""),
            latency_ms=float(info.get("latency_ms") or 0.0),
            message=str(info.get("details") or info.get("error") or ""),
            details=info,
        )

    def build_manual_pr_url(
        self,
        branch_name: str = "",
        base_branch: str = "main",
        remote: str = "origin",
        repo_dir: Path | None = None,
        *,
        head_branch: str = "",
        title: str = "",
        body: str = "",
    ) -> str:
        """Build a VCS-interoperable manual Pull/Merge Request compare URL from the native `git remote`."""
        resolved_branch = (branch_name or head_branch).strip()
        remote_url = self.resolve_remote_url(remote=remote, repo_dir=repo_dir)
        return build_vcs_manual_pr_url(
            branch_name=resolved_branch,
            base_branch=base_branch,
            remote_url=remote_url,
            title=title,
            body=body,
        )

    def get_branch_diff(
        self,
        branch_name: str = "",
        base_branch: str = "main",
        repo_dir: Path | None = None,
        *,
        head_branch: str = "",
    ) -> dict[str, Any]:
        """Compute the unified diff, changed files, and commit log for `branch_name` vs `base_branch` using native `git`."""
        target_dir = repo_dir or self.resolve_repo_dir()
        clean_branch = (branch_name or head_branch).strip()
        clean_base = (base_branch or "main").strip()
        remote_url = self.resolve_remote_url(repo_dir=target_dir)
        provider, web_base, slug = detect_vcs_provider_from_remote(remote_url)

        if not (target_dir / ".git").exists():
            return {
                "status": "error",
                "error": f"Git repository not found at {target_dir}",
                "branch_name": clean_branch,
                "base_branch": clean_base,
            }

        # Prefer comparing against origin/<base_branch> if available so local base branch state never masks diffs
        base_ref = clean_base
        if self._read_local_ref_sha(target_dir, f"refs/remotes/origin/{clean_base}"):
            base_ref = f"origin/{clean_base}"
        else:
            proc_origin_base = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "--verify", f"origin/{clean_base}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_origin_base.returncode == 0:
                base_ref = f"origin/{clean_base}"

        range_spec = f"{base_ref}...{clean_branch}"
        proc_diff = subprocess.run(
            ["git", "-C", str(target_dir), "diff", range_spec],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc_diff.returncode != 0:
            range_spec = f"origin/{clean_base}...origin/{clean_branch}"
            proc_diff = subprocess.run(
                ["git", "-C", str(target_dir), "diff", range_spec],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

        proc_numstat = subprocess.run(
            ["git", "-C", str(target_dir), "diff", "--numstat", range_spec],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        proc_status = subprocess.run(
            ["git", "-C", str(target_dir), "diff", "--name-status", range_spec],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        log_range = range_spec.replace("...", "..")
        proc_log = subprocess.run(
            ["git", "-C", str(target_dir), "log", "--oneline", log_range],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        status_map: dict[str, str] = {}
        for line in (proc_status.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                st_code = parts[0].strip()[:1].upper()
                fname = parts[-1].strip()
                status_map[fname] = (
                    "added"
                    if st_code == "A"
                    else ("deleted" if st_code == "D" else ("renamed" if st_code == "R" else "modified"))
                )

        files_list: list[dict[str, Any]] = []
        total_additions = 0
        total_deletions = 0
        for line in (proc_numstat.stdout or "").splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 3:
                adds = int(parts[0]) if parts[0].isdigit() else 0
                dels = int(parts[1]) if parts[1].isdigit() else 0
                fname = parts[2].strip()
                total_additions += adds
                total_deletions += dels
                files_list.append(
                    {
                        "filename": fname,
                        "status": status_map.get(fname, "modified"),
                        "additions": adds,
                        "deletions": dels,
                    }
                )

        commits = [ln.strip() for ln in (proc_log.stdout or "").splitlines() if ln.strip()]
        return {
            "status": "ok",
            "vcs_provider": provider,
            "repo": slug,
            "remote_url": remote_url,
            "branch_name": clean_branch,
            "head_branch": clean_branch,
            "base_branch": clean_base,
            "manual_pr_url": build_vcs_manual_pr_url(clean_branch, clean_base, remote_url),
            "changed_files": len(files_list),
            "files_count": len(files_list),
            "additions": total_additions,
            "deletions": total_deletions,
            "commits": commits,
            "files": files_list,
            "diff": proc_diff.stdout or "",
            "web_base_url": web_base,
        }

    def push_branch(
        self,
        branch_name: str,
        remote: str = "origin",
        base_branch: str = "main",
        workdir: Path | None = None,
        *,
        set_upstream: bool = True,
        force_with_lease: bool = False,
        title: str = "",
        body: str = "",
        repo_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Push `branch_name` to `remote` using native `git push -u <remote> <branch_name>`."""
        _ = set_upstream
        target_dir = repo_dir or workdir or self.resolve_repo_dir()
        clean_branch = branch_name.strip()
        if not clean_branch or clean_branch in ("main", "master"):
            return {
                "status": "error",
                "error": f"Refusing to push protected default branch '{clean_branch}' as a remediation branch.",
            }
        remote_url = self.resolve_remote_url(remote=remote, repo_dir=target_dir)
        provider, _, slug = detect_vcs_provider_from_remote(remote_url)

        proc_sha = subprocess.run(
            ["git", "-C", str(target_dir), "rev-parse", clean_branch],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc_sha.returncode != 0:
            proc_sha = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        head_sha = (proc_sha.stdout or "").strip()

        push_cmd = ["git", "-C", str(target_dir), "push", "-u"]
        if force_with_lease:
            push_cmd.append("--force-with-lease")
        push_cmd.extend([remote, f"{clean_branch}:{clean_branch}"])
        proc_push = subprocess.run(
            push_cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc_push.returncode != 0:
            return {
                "status": "error",
                "branch_name": clean_branch,
                "remote": remote,
                "error": (proc_push.stderr or proc_push.stdout or "git push failed").strip(),
            }

        manual_pr_url = build_vcs_manual_pr_url(
            clean_branch,
            base_branch,
            remote_url,
            title=title,
            body=body,
        )
        return {
            "status": "ok",
            "vcs_provider": provider,
            "repo": slug,
            "remote": remote,
            "remote_url": remote_url,
            "branch_name": clean_branch,
            "base_branch": base_branch,
            "head_sha": head_sha,
            "pushed": True,
            "manual_pr_url": manual_pr_url,
            "output": f"{proc_push.stdout}\n{proc_push.stderr}".strip(),
        }

    def inspect_remote_branch_state(
        self,
        branch_name: str,
        base_branch: str = "main",
        remote: str = "origin",
        repo_dir: Path | None = None,
        fetch: bool = True,
    ) -> dict[str, Any]:
        """Inspect whether `branch_name` exists on `remote` or has been merged into `base_branch` using native `git`."""
        target_dir = repo_dir or self.resolve_repo_dir()
        clean_branch = branch_name.strip().removeprefix("refs/heads/")
        clean_base = (base_branch or "main").strip()
        if not (target_dir / ".git").exists():
            return {"status": "error", "error": f"Git repository not found at {target_dir}"}

        if fetch:
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(
                    ["git", "-C", str(target_dir), "fetch", remote, "--prune", "--quiet"],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=False,
                )

        ls_out = ""
        try:
            proc_ls = subprocess.run(
                ["git", "-C", str(target_dir), "ls-remote", "--heads", remote, clean_branch],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc_ls.returncode != 0:
                return {
                    "status": "error",
                    "branch_name": clean_branch,
                    "base_branch": clean_base,
                    "remote": remote,
                    "error": (
                        proc_ls.stderr
                        or proc_ls.stdout
                        or f"git ls-remote exited with {proc_ls.returncode}"
                    ).strip(),
                }
            ls_out = (proc_ls.stdout or "").strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "status": "error",
                "branch_name": clean_branch,
                "base_branch": clean_base,
                "remote": remote,
                "error": f"git ls-remote failed: {exc}",
            }
        exists_on_remote = bool(ls_out)
        remote_sha = ls_out.split()[0] if ls_out else ""

        base_remote_sha = self._read_local_ref_sha(target_dir, f"refs/remotes/{remote}/{clean_base}")
        if not base_remote_sha:
            proc_base_sha = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", f"{remote}/{clean_base}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            base_remote_sha = (proc_base_sha.stdout or "").strip() if proc_base_sha.returncode == 0 else ""

        merged_into_base = False
        local_branch_sha = self._read_local_ref_sha(target_dir, f"refs/heads/{clean_branch}")
        candidate_ref = remote_sha or local_branch_sha or (
            clean_branch if (target_dir / ".git" / "packed-refs").is_file() else ""
        )
        if candidate_ref and base_remote_sha:
            if candidate_ref == base_remote_sha:
                merged_into_base = True
            else:
                proc_anc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target_dir),
                        "merge-base",
                        "--is-ancestor",
                        candidate_ref,
                        f"{remote}/{clean_base}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if proc_anc.returncode == 0:
                    merged_into_base = True
                else:
                    proc_cherry = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target_dir),
                            "cherry",
                            f"{remote}/{clean_base}",
                            candidate_ref,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if proc_cherry.returncode == 0:
                        cherry_lines = [
                            ln.strip()
                            for ln in (proc_cherry.stdout or "").splitlines()
                            if ln.strip()
                        ]
                        if cherry_lines and not any(ln.startswith("+") for ln in cherry_lines):
                            merged_into_base = True

        return {
            "status": "ok",
            "branch_name": clean_branch,
            "base_branch": clean_base,
            "remote": remote,
            "exists_on_remote": exists_on_remote,
            "remote_exists": exists_on_remote,
            "remote_head_sha": remote_sha,
            "merged_into_base": merged_into_base,
        }

    def sync_and_clean_local_repo(
        self,
        base_branch: str = "main",
        remote: str = "origin",
        repo_dir: Path | None = None,
        protected_branches: list[str] | None = None,
        force_clean_branches: list[str] | None = None,
        fetch: bool = True,
        checkout_base_if_current_gone: bool = True,
        deploy_to_home: bool | None = None,
        home_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Synchronize local `base_branch` (`main`) with `<remote>/<base_branch>`, prune local branches/worktrees cleaned on `<remote>`, and atomically deploy `src/home/` (`main`) to `$HOME`."""
        clean_base = (base_branch or "main").strip()
        env_repo_dir = os.environ.get("AGY_CONF_REPO_DIR", "").strip()
        repo_cand = Path(env_repo_dir).expanduser() if env_repo_dir else None
        is_live_workstation_repo = bool(
            repo_cand
            and (
                (repo_cand / "plugin.json").is_file()
                or (repo_cand / "api" / "app.py").is_file()
            )
        )
        is_hermetic_offline = (
            os.environ.get("AGY_OFFLINE_ONLY") == "1"
            or os.environ.get("PRE_COMMIT_HOOK") == "1"
        )
        if (
            repo_dir is None
            and not self._explicit_repo_dir
            and is_hermetic_offline
            and (not env_repo_dir or is_live_workstation_repo)
        ):
            return {
                "status": "ok",
                "synced": False,
                "reason": "skipped_in_hermetic_test_mode",
                "base_branch": clean_base,
                "remote": remote,
                "deleted_local_branches": [],
                "deleted_worktrees": [],
            }
        target_dir = repo_dir or self.resolve_repo_dir()
        if not (target_dir / ".git").exists():
            return {
                "status": "error",
                "error": f"Git repository not found at {target_dir}",
                "base_branch": clean_base,
                "remote": remote,
            }

        post_merge_hook_installed = False
        if (target_dir / ".git").is_dir():
            git_cfg_path = target_dir / ".git" / "config"
            cfg_text = (
                git_cfg_path.read_text(encoding="utf-8", errors="ignore")
                if git_cfg_path.is_file()
                else ""
            )
            if "prune = true" not in cfg_text:
                subprocess.run(
                    ["git", "-C", str(target_dir), "config", "fetch.prune", "true"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            hooks_dir = target_dir / ".git" / "hooks"
            if hooks_dir.is_dir():
                post_merge_path = hooks_dir / "post-merge"
                post_merge_script = (
                    "#!/usr/bin/env sh\n"
                    "# Managed by agy-dev-plugin git_client.py — Pure Git Post-Merge Branch & Lock Cleaner\n"
                    'if [ "${AGY_OFFLINE_ONLY:-0}" = "1" ] || [ "${PRE_COMMIT_HOOK:-0}" = "1" ]; then\n'
                    "  exit 0\n"
                    "fi\n"
                    'for _pr_rem in '
                    '"$HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py" '
                    '"$HOME/.gemini/config/plugins/wiz-tam-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py" '
                    '"$HOME/.gemini/config/skills/agy-code-remediation-pr/scripts/pr_remediator.py"; do\n'
                    '  if [ -f "$_pr_rem" ]; then\n'
                    '    python3 "$_pr_rem" lock-status --sync-git >/dev/null 2>&1 &\n'
                    '    break\n'
                    '  fi\n'
                    'done\n'
                    "exit 0\n"
                )
                try:
                    existing_hook = (
                        post_merge_path.read_text(encoding="utf-8", errors="ignore")
                        if post_merge_path.is_file()
                        else ""
                    )
                    if (
                        not existing_hook
                        or "Managed by wiz-agy-as-code git_vcs.py" in existing_hook
                        or "Managed by agy-dev-plugin git_client.py" in existing_hook
                    ):
                        if existing_hook != post_merge_script:
                            post_merge_path.write_text(post_merge_script, encoding="utf-8")
                        os.chmod(post_merge_path, 0o755)
                        post_merge_hook_installed = True
                except OSError:
                    post_merge_hook_installed = False

        if fetch:
            subprocess.run(
                ["git", "-C", str(target_dir), "fetch", remote, "--prune", "--quiet"],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )

        proc_all_refs = subprocess.run(
            [
                "git",
                "-C",
                str(target_dir),
                "for-each-ref",
                "--format=%(refname)|%(refname:short)|%(objectname)|%(upstream:short)|%(upstream:track)",
                "refs/heads/",
                f"refs/remotes/{remote}/",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        remote_base_sha = ""
        initial_local_base_sha = ""
        remote_branches: set[str] = set()
        loc_ref_lines: list[str] = []
        prefix = f"{remote}/"
        remote_base_full_ref = f"refs/remotes/{remote}/{clean_base}"
        local_base_full_ref = f"refs/heads/{clean_base}"
        for raw_line in (proc_all_refs.stdout or "").splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.split("|")
            if len(parts) < 5:
                continue
            full_ref, ref_short, obj_sha, up_short, up_track = (
                parts[0].strip(),
                parts[1].strip(),
                parts[2].strip(),
                parts[3].strip(),
                parts[4].strip(),
            )
            if full_ref == remote_base_full_ref:
                remote_base_sha = obj_sha
            elif full_ref == local_base_full_ref:
                initial_local_base_sha = obj_sha
            if full_ref.startswith(f"refs/remotes/{remote}/"):
                b_name = (
                    ref_short[len(prefix) :].strip()
                    if ref_short.startswith(prefix)
                    else full_ref[len(f"refs/remotes/{remote}/") :].strip()
                )
                if b_name and b_name != "HEAD":
                    remote_branches.add(b_name)
            elif full_ref.startswith("refs/heads/"):
                loc_ref_lines.append(f"{ref_short}|{up_short}|{up_track}")

        if not remote_base_sha:
            return {
                "status": "ok",
                "synced": False,
                "reason": "no_remote_base_ref",
                "base_branch": clean_base,
                "remote": remote,
                "deleted_local_branches": [],
                "deleted_worktrees": [],
            }

        protected_set: set[str] = {"main", "master", clean_base}
        for pb in protected_branches or []:
            clean_pb = str(pb or "").strip().removeprefix("refs/heads/")
            if clean_pb:
                protected_set.add(clean_pb)

        force_clean_set: set[str] = {
            str(fb or "").strip().removeprefix("refs/heads/")
            for fb in (force_clean_branches or [])
            if str(fb or "").strip()
        } - {"main", "master", clean_base}

        needs_merged_check = any(
            (ln.split("|", 1)[0].strip() not in protected_set)
            and (ln.split("|", 1)[0].strip() not in force_clean_set)
            and not ln.split("|", 1)[0].strip().startswith("fix/")
            for ln in loc_ref_lines
            if ln.strip()
        )
        merged_into_remote_base: set[str] = set()
        if needs_merged_check:
            proc_merged = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target_dir),
                    "branch",
                    "--merged",
                    f"{remote}/{clean_base}",
                    "--format=%(refname:short)",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            merged_into_remote_base = {
                ln.strip() for ln in (proc_merged.stdout or "").splitlines() if ln.strip()
            }

        current_branch = self._read_local_head_branch(target_dir)

        proc_dirty = subprocess.run(
            ["git", "-C", str(target_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        is_dirty = bool((proc_dirty.stdout or "").strip())

        stale_branches: list[str] = []
        for line in loc_ref_lines:
            if not line.strip():
                continue
            parts = line.split("|")
            loc_br = parts[0].strip()
            upstream_short = parts[1].strip() if len(parts) > 1 else ""
            upstream_track = parts[2].strip() if len(parts) > 2 else ""
            if not loc_br:
                continue
            if loc_br in force_clean_set:
                stale_branches.append(loc_br)
                continue
            if loc_br in protected_set:
                continue

            upstream_gone = "[gone]" in upstream_track or (
                upstream_short.startswith(prefix)
                and upstream_short[len(prefix) :] not in remote_branches
            )
            if loc_br == current_branch and is_dirty and not upstream_gone:
                continue

            merged_and_remote_cleaned = (
                loc_br in merged_into_remote_base and loc_br not in remote_branches
            )
            orphan_fix_branch = loc_br.startswith("fix/") and loc_br not in remote_branches

            cherry_merged = False
            if (
                not (upstream_gone or merged_and_remote_cleaned or orphan_fix_branch)
                and loc_br not in remote_branches
            ):
                proc_cherry = subprocess.run(
                    ["git", "-C", str(target_dir), "cherry", f"{remote}/{clean_base}", loc_br],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if proc_cherry.returncode == 0:
                    has_unmerged = any(
                        ln.strip().startswith("+")
                        for ln in (proc_cherry.stdout or "").splitlines()
                    )
                    cherry_merged = not has_unmerged

            if upstream_gone or merged_and_remote_cleaned or orphan_fix_branch or cherry_merged:
                stale_branches.append(loc_br)

        deleted_worktrees: list[str] = []
        if (target_dir / ".git" / "worktrees").is_dir():
            proc_wt = subprocess.run(
                ["git", "-C", str(target_dir), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc_wt.returncode == 0:
                main_repo_resolved = str(target_dir.resolve())
                wt_blocks = (proc_wt.stdout or "").strip().split("\n\n")
                for block in wt_blocks:
                    wt_path_str = ""
                    wt_branch = ""
                    for ln in block.splitlines():
                        if ln.startswith("worktree "):
                            wt_path_str = ln[len("worktree ") :].strip()
                        elif ln.startswith("branch "):
                            wt_branch = ln[len("branch ") :].strip().removeprefix("refs/heads/")
                    if not wt_path_str or str(Path(wt_path_str).resolve()) == main_repo_resolved:
                        continue
                    if wt_branch in stale_branches or (
                        wt_branch not in protected_set and wt_branch not in remote_branches
                    ):
                        subprocess.run(
                            ["git", "-C", str(target_dir), "worktree", "remove", "--force", wt_path_str],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            check=False,
                        )
                        if Path(wt_path_str).exists():
                            shutil.rmtree(wt_path_str, ignore_errors=True)
                        deleted_worktrees.append(wt_path_str)
                subprocess.run(
                    ["git", "-C", str(target_dir), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

        stashed = False

        should_checkout_base = current_branch == clean_base or (
            checkout_base_if_current_gone
            and (current_branch in stale_branches or current_branch == "HEAD")
        )

        if should_checkout_base:
            if is_dirty:
                proc_stash = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target_dir),
                        "stash",
                        "push",
                        "--include-untracked",
                        "-m",
                        "git_vcs_sync_autostash",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                stashed = proc_stash.returncode == 0 and "No local changes to save" not in (
                    proc_stash.stdout or ""
                )

            if current_branch != clean_base:
                proc_co = subprocess.run(
                    ["git", "-C", str(target_dir), "checkout", clean_base],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if proc_co.returncode != 0:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(target_dir),
                            "checkout",
                            "-B",
                            clean_base,
                            f"{remote}/{clean_base}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                current_branch = clean_base

            if initial_local_base_sha != remote_base_sha:
                proc_ff = subprocess.run(
                    ["git", "-C", str(target_dir), "merge", "--ff-only", f"{remote}/{clean_base}"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if proc_ff.returncode != 0:
                    subprocess.run(
                        ["git", "-C", str(target_dir), "reset", "--hard", f"{remote}/{clean_base}"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )

            if stashed:
                subprocess.run(
                    ["git", "-C", str(target_dir), "stash", "pop", "--quiet"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
        elif initial_local_base_sha != remote_base_sha:
            subprocess.run(
                ["git", "-C", str(target_dir), "branch", "-f", clean_base, f"{remote}/{clean_base}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        deleted_local_branches: list[str] = []
        branches_to_delete = [br for br in stale_branches if br != current_branch]
        if branches_to_delete:
            proc_del = subprocess.run(
                ["git", "-C", str(target_dir), "branch", "-D", *branches_to_delete],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc_del.returncode == 0:
                deleted_local_branches.extend(branches_to_delete)
            else:
                for br in branches_to_delete:
                    proc_one = subprocess.run(
                        ["git", "-C", str(target_dir), "branch", "-D", br],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if proc_one.returncode == 0:
                        deleted_local_branches.append(br)

        local_base_sha = self._read_local_ref_sha(target_dir, f"refs/heads/{clean_base}")
        if not local_base_sha:
            proc_base_head = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", clean_base],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            local_base_sha = (proc_base_head.stdout or "").strip() if proc_base_head.returncode == 0 else ""

        if deploy_to_home is None:
            should_deploy = bool(
                current_branch == clean_base
                and (
                    home_dir is not None
                    or (
                        not is_hermetic_offline
                        and (
                            (target_dir / "plugin.json").is_file()
                            or (target_dir / "home").is_dir()
                            or (target_dir / "src" / "home").is_dir()
                        )
                    )
                )
            )
        else:
            should_deploy = bool(deploy_to_home and current_branch == clean_base)

        home_deploy: dict[str, Any] = {"status": "skipped", "reason": "not_requested"}
        if should_deploy:
            home_deploy = self.deploy_repo_to_home(
                repo_dir=target_dir,
                home_dir=home_dir,
                require_base_branch=clean_base,
            )

        return {
            "status": "ok",
            "synced": True,
            "base_branch": clean_base,
            "remote": remote,
            "current_branch": current_branch,
            "local_base_sha": local_base_sha,
            "remote_base_sha": remote_base_sha,
            "base_up_to_date": bool(local_base_sha and local_base_sha == remote_base_sha),
            "fetch_prune_enabled": True,
            "post_merge_hook_installed": post_merge_hook_installed,
            "deleted_local_branches": deleted_local_branches,
            "deleted_worktrees": deleted_worktrees,
            "home_deploy": home_deploy,
        }

    def resolve_repo_version_stamp(
        self,
        repo_dir: Path | None = None,
        ref: str = "HEAD",
        date_override: str = "",
    ) -> str:
        """Resolve `<short_sha>-<YYYYMMDD>` for `repo_dir`."""
        target_dir = repo_dir or self.resolve_repo_dir()
        ver_stamp, _ = resolve_repo_version_stamp(target_dir, ref=ref, date_override=date_override)
        return ver_stamp

    @staticmethod
    def _resolve_plugin_name(repo_dir: Path) -> str:
        """Read plugin `name` from `<repo_dir>/plugin.json`, defaulting to `repo_dir.name`."""
        manifest_path = repo_dir / "plugin.json"
        if manifest_path.is_file():
            with contextlib.suppress(Exception):
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                raw_name = str((data or {}).get("name") or "").strip()
                if raw_name and "/" not in raw_name and ".." not in raw_name:
                    return raw_name
        return "agy-dev-plugin"

    @staticmethod
    def _map_plugin_rel_to_home(rel_str: str, plugin_name: str = "wiz-tam-plugin") -> str:
        """Map a root-level Antigravity plugin relative path to its `$HOME` target path."""
        clean_name = (plugin_name or "wiz-tam-plugin").strip().strip("/")
        if (
            rel_str in ("plugin.json", "hooks.json", "mcp_config.json")
            or rel_str.startswith(("skills/", "clients/", "agents/", "rules/", "assets/"))
        ):
            return f".gemini/config/plugins/{clean_name}/{rel_str}"
        if clean_name == "wiz-tam-plugin" and rel_str in (".env.example", ".tool-versions", ".tool-plugins"):
            return rel_str
        return ""

    def deploy_repo_to_home(
        self,
        repo_dir: Path | None = None,
        home_dir: Path | None = None,
        require_base_branch: str = "main",
        prune_orphans: bool = True,
    ) -> dict[str, Any]:
        """Atomically deploy tracked plugin/customization files from the pinned `main` checkout into `$HOME` in `<15 ms` using `git ls-files`.

        Guarantees `origin/main` is the single source of truth:
          - Refuses to deploy to `$HOME` if `repo_dir` is checked out on an unmerged `fix/*` branch (`require_base_branch="main"`).
          - Expands `$HOME/` in `.gemini/config/mcp_config.json`, `.gemini/config/hooks.json`, plugin manifests, and `.gemini/config/sidecars/*.json`.
          - Stamps the live `HEAD` `<short_sha>-<YYYYMMDD>` into `.gemini/config/plugins/<plugin_name>/plugin.json`.
          - Applies deterministic permissions (`0755` for scripts/clients/bin, `0600` for `.ssh/*`, `0644` otherwise).
          - Prunes orphaned files in managed customization subdirectories of `$HOME` so rejected branch edits never linger.
        """
        target_dir = repo_dir or self.resolve_repo_dir()
        target_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
        src_home = (
            target_dir / "home"
            if (target_dir / "home").is_dir()
            else (target_dir / "src" / "home")
        )
        is_plugin_root_layout = (target_dir / "plugin.json").is_file() and not src_home.is_dir()
        plugin_name = self._resolve_plugin_name(target_dir) if is_plugin_root_layout else ""
        plugin_prefix = f".gemini/config/plugins/{plugin_name}" if plugin_name else ""
        if not (target_dir / ".git").exists() or (not is_plugin_root_layout and not src_home.is_dir()):
            return {
                "status": "skipped",
                "deployed": False,
                "reason": "no_src_home_dir",
                "repo_dir": str(target_dir),
                "home_dir": str(target_home),
                "deployed_files": [],
                "pruned_files": [],
                "pruned_orphans": [],
            }

        cur_branch = self._read_local_head_branch(target_dir)
        req_branch = (require_base_branch or "").strip()
        if req_branch and cur_branch != req_branch:
            return {
                "status": "skipped",
                "deployed": False,
                "reason": "not_on_base_branch",
                "current_branch": cur_branch,
                "required_branch": req_branch,
                "repo_dir": str(target_dir),
                "home_dir": str(target_home),
                "deployed_files": [],
                "pruned_files": [],
                "pruned_orphans": [],
            }

        rel_to_src_file: dict[str, Path] = {}
        if is_plugin_root_layout:
            proc_ls = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target_dir),
                    "ls-files",
                    "-z",
                    "--",
                    "plugin.json",
                    "hooks.json",
                    "mcp_config.json",
                    ".env.example",
                    ".tool-versions",
                    ".tool-plugins",
                    "skills",
                    "agents",
                    "rules",
                    "clients",
                    "assets",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc_ls.returncode == 0 and proc_ls.stdout:
                for raw_entry in proc_ls.stdout.split(b"\x00"):
                    if not raw_entry:
                        continue
                    entry_str = raw_entry.decode("utf-8", errors="ignore").replace("\\", "/").strip("/")
                    home_rel = self._map_plugin_rel_to_home(entry_str, plugin_name=plugin_name or "wiz-tam-plugin")
                    if home_rel:
                        rel_to_src_file[home_rel] = target_dir / entry_str
        else:
            rel_prefix = "home" if (target_dir / "home").is_dir() else "src/home"
            proc_ls = subprocess.run(
                ["git", "-C", str(target_dir), "ls-files", "-z", "--", rel_prefix],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if proc_ls.returncode == 0 and proc_ls.stdout:
                for raw_entry in proc_ls.stdout.split(b"\x00"):
                    if not raw_entry:
                        continue
                    entry_str = raw_entry.decode("utf-8", errors="ignore").replace("\\", "/")
                    if entry_str.startswith(f"{rel_prefix}/"):
                        rel_str = entry_str[len(rel_prefix) + 1 :].strip("/")
                        if rel_str:
                            rel_to_src_file[rel_str] = src_home / rel_str

            if not rel_to_src_file:
                for cand in sorted(src_home.rglob("*")):
                    if not cand.is_file():
                        continue
                    if "__pycache__" in cand.parts or cand.name.endswith((".pyc", ".DS_Store", ".old")) or ".bak." in cand.name:
                        continue
                    rel_str = str(cand.relative_to(src_home)).replace("\\", "/")
                    if rel_str:
                        rel_to_src_file[rel_str] = cand

        tracked_rel_paths = list(rel_to_src_file.keys())
        tracked_set = set(tracked_rel_paths)
        home_prefix_bytes = (str(target_home).rstrip("/") + "/").encode("utf-8")
        deployed_files: list[str] = []

        for rel_str in tracked_rel_paths:
            if rel_str == ".env":
                continue
            src_file = rel_to_src_file[rel_str]
            if not src_file.is_file():
                continue
            try:
                desired_bytes = src_file.read_bytes()
            except OSError:
                continue

            if plugin_prefix and rel_str == f"{plugin_prefix}/plugin.json":
                live_ver, _ = resolve_repo_version_stamp(target_dir, ref="HEAD")
                desired_bytes = stamp_plugin_json_bytes(desired_bytes, live_ver)

            if (
                rel_str
                in (
                    ".gemini/config/mcp_config.json",
                    ".gemini/config/hooks.json",
                )
                or (
                    plugin_prefix
                    and rel_str in (f"{plugin_prefix}/hooks.json", f"{plugin_prefix}/mcp_config.json")
                )
                or (rel_str.startswith(".gemini/config/sidecars/") and rel_str.endswith(".json"))
            ):
                desired_bytes = desired_bytes.replace(b"$HOME/", home_prefix_bytes)

            if (
                rel_str.startswith(".local/bin/")
                or (
                    (
                        rel_str.startswith(".gemini/config/clients/")
                        or (plugin_prefix and rel_str.startswith(f"{plugin_prefix}/clients/"))
                    )
                    and rel_str.endswith(".py")
                )
                or (
                    (
                        rel_str.startswith(".gemini/config/skills/")
                        or (plugin_prefix and rel_str.startswith(f"{plugin_prefix}/skills/"))
                    )
                    and "/scripts/" in rel_str
                    and rel_str.endswith(".py")
                )
                or (rel_str.startswith(".gemini/config/actions/") and "/scripts/" in rel_str and rel_str.endswith(".py"))
            ):
                mode = 0o755
            elif rel_str.startswith(".ssh/"):
                mode = 0o600
            else:
                mode = 0o644

            dest_file = target_home / rel_str
            needs_write = True
            if dest_file.is_file() and not dest_file.is_symlink():
                try:
                    if dest_file.stat().st_size == len(desired_bytes) and dest_file.read_bytes() == desired_bytes:
                        needs_write = False
                except OSError:
                    needs_write = True

            if needs_write:
                try:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp_dest = dest_file.with_name(f".{dest_file.name}.tmp.{os.getpid()}")
                    tmp_dest.write_bytes(desired_bytes)
                    os.chmod(tmp_dest, mode)
                    os.replace(tmp_dest, dest_file)
                    deployed_files.append(rel_str)
                except OSError:
                    pass
            else:
                try:
                    cur_mode = dest_file.stat().st_mode & 0o777
                    if cur_mode != mode:
                        os.chmod(dest_file, mode)
                except OSError:
                    pass

        pruned_files: list[str] = []
        if prune_orphans:
            legacy_root_gemini_md = target_home / ".gemini" / "GEMINI.md"
            if (
                legacy_root_gemini_md.is_file()
                and ".gemini/GEMINI.md" not in tracked_set
                and (
                    ".gemini/config/GEMINI.md" in tracked_set
                    or ".gemini/config/plugins/wiz-tam-plugin/rules/core-design-pillars.md" in tracked_set
                )
            ):
                try:
                    legacy_root_gemini_md.unlink()
                    pruned_files.append(".gemini/GEMINI.md")
                except OSError:
                    pass
            legacy_config_gemini_md = target_home / ".gemini" / "config" / "GEMINI.md"
            if (
                legacy_config_gemini_md.is_file()
                and ".gemini/config/GEMINI.md" not in tracked_set
                and ".gemini/config/plugins/wiz-tam-plugin/rules/core-design-pillars.md" in tracked_set
            ):
                try:
                    head_txt = legacy_config_gemini_md.read_text(encoding="utf-8", errors="ignore")[:200]
                    if "# Global Gemini / Antigravity Agent Guidelines" in head_txt:
                        legacy_config_gemini_md.unlink()
                        pruned_files.append(".gemini/config/GEMINI.md")
                except OSError:
                    pass
            legacy_hooks_json = target_home / ".gemini" / "config" / "hooks.json"
            if (
                legacy_hooks_json.is_file()
                and ".gemini/config/hooks.json" not in tracked_set
                and ".gemini/config/plugins/wiz-tam-plugin/hooks.json" in tracked_set
            ):
                try:
                    legacy_hooks_json.unlink()
                    pruned_files.append(".gemini/config/hooks.json")
                except OSError:
                    pass
            legacy_mcp_json = target_home / ".gemini" / "config" / "mcp_config.json"
            if (
                legacy_mcp_json.is_file()
                and ".gemini/config/mcp_config.json" not in tracked_set
                and ".gemini/config/plugins/wiz-tam-plugin/mcp_config.json" in tracked_set
            ):
                try:
                    legacy_mcp_json.unlink()
                    pruned_files.append(".gemini/config/mcp_config.json")
                except OSError:
                    pass
            if is_plugin_root_layout:
                legacy_skills_root = target_home / ".gemini" / "config" / "skills"
                if legacy_skills_root.is_dir():
                    for p_skill in (target_dir / "skills").iterdir() if (target_dir / "skills").is_dir() else ():
                        if p_skill.is_dir():
                            dup_skill_dir = legacy_skills_root / p_skill.name
                            if dup_skill_dir.exists() or dup_skill_dir.is_symlink():
                                shutil.rmtree(dup_skill_dir, ignore_errors=True)
                                with contextlib.suppress(OSError):
                                    dup_skill_dir.unlink()
                                pruned_files.append(f".gemini/config/skills/{p_skill.name}")
                    with contextlib.suppress(OSError):
                        legacy_skills_root.rmdir()
                legacy_clients_dir = target_home / ".gemini" / "config" / "clients"
                plugin_clients_dir = target_home / ".gemini" / "config" / "plugins" / "wiz-tam-plugin" / "clients"
                if plugin_name == "wiz-tam-plugin" and plugin_clients_dir.is_dir():
                    if legacy_clients_dir.is_dir() and not legacy_clients_dir.is_symlink():
                        shutil.rmtree(legacy_clients_dir, ignore_errors=True)
                    if not legacy_clients_dir.exists() and not legacy_clients_dir.is_symlink():
                        with contextlib.suppress(OSError):
                            legacy_clients_dir.parent.mkdir(parents=True, exist_ok=True)
                            legacy_clients_dir.symlink_to("plugins/wiz-tam-plugin/clients")
            obsolete_rel_dirs = (
                ".gemini/config/rules",
                ".gemini/config/plugins/wiz-copilot",
            )
            managed_rel_dirs = [
                ".config/shell",
                ".gemini/config/rules",
                ".gemini/config/clients",
                ".gemini/config/skills",
                ".gemini/config/actions",
                ".gemini/config/sidecars",
                ".gemini/config/plugins/wiz-copilot",
            ]
            if plugin_prefix:
                managed_rel_dirs.append(plugin_prefix)
            else:
                managed_rel_dirs.extend(
                    [
                        ".gemini/config/plugins/wiz-tam-plugin",
                        ".gemini/config/plugins/agy-dev-plugin",
                    ]
                )
            sibling_homes = [
                target_dir.parent / sib / "src" / "home"
                for sib in ("agy-tam-plugin", "agy-dev-plugin", "g-control-tower")
                if (target_dir.parent / sib / "src" / "home").is_dir()
            ]
            for m_dir in managed_rel_dirs:
                has_tracked_in_dir = any(r.startswith(f"{m_dir}/") for r in tracked_set)
                if m_dir not in obsolete_rel_dirs and not has_tracked_in_dir:
                    continue
                abs_m_dir = target_home / m_dir
                if not abs_m_dir.is_dir():
                    continue
                for existing_file in sorted(abs_m_dir.rglob("*")):
                    if not existing_file.is_file():
                        continue
                    fname = existing_file.name
                    if (
                        "__pycache__" in existing_file.parts
                        or fname.endswith((".pyc", ".DS_Store", ".old", ".local"))
                        or ".bak." in fname
                        or ".tmp." in fname
                    ):
                        continue
                    rel_existing = str(existing_file.relative_to(target_home)).replace("\\", "/")
                    if (
                        rel_existing not in tracked_set
                        and not (src_home / rel_existing).is_file()
                        and not any((sib_h / rel_existing).is_file() for sib_h in sibling_homes)
                    ):
                        try:
                            existing_file.unlink()
                            pruned_files.append(rel_existing)
                        except OSError:
                            pass
            for obsolete_dir_rel in (".gemini/config/rules", ".gemini/config/plugins/wiz-copilot"):
                if not any(r.startswith(f"{obsolete_dir_rel}/") for r in tracked_set):
                    obs_abs = target_home / obsolete_dir_rel
                    if obs_abs.is_dir():
                        shutil.rmtree(obs_abs, ignore_errors=True)

        restarted = _maybe_restart_control_tower_after_deploy(deployed_files, target_home=target_home)

        return {
            "status": "ok",
            "deployed": True,
            "repo_dir": str(target_dir),
            "home_dir": str(target_home),
            "branch": cur_branch,
            "tracked_count": len(tracked_set),
            "deployed_count": len(deployed_files),
            "updated_count": len(deployed_files),
            "unchanged_count": max(0, len(tracked_set) - len(deployed_files)),
            "deployed_files": deployed_files,
            "pruned_count": len(pruned_files),
            "pruned_files": pruned_files,
            "pruned_orphans": pruned_files,
            "control_tower_restarted": restarted,
        }

    def delete_branch_native(
        self,
        branch_name: str,
        remote: str = "origin",
        delete_remote: bool = True,
        delete_local: bool = True,
        repo_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Delete `branch_name` on remote and/or locally using pure native `git`."""
        target_dir = repo_dir or self.resolve_repo_dir()
        clean_branch = branch_name.strip().removeprefix("refs/heads/")
        if not clean_branch or clean_branch in ("main", "master"):
            return {"status": "skipped", "reason": "protected_default_branch"}

        remote_deleted = False
        local_deleted = False
        if delete_remote:
            proc_rem = subprocess.run(
                ["git", "-C", str(target_dir), "push", remote, "--delete", clean_branch],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            remote_deleted = proc_rem.returncode == 0
        if delete_local:
            proc_loc = subprocess.run(
                ["git", "-C", str(target_dir), "branch", "-D", clean_branch],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            local_deleted = proc_loc.returncode == 0
        return {
            "status": "ok",
            "branch_name": clean_branch,
            "remote_deleted": remote_deleted,
            "local_deleted": local_deleted,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `git_client.py`."""
    parser = argparse.ArgumentParser(
        prog="git_client",
        description="VCS-Interoperable Native Git & SSH client for timestamped branch management, diffs, pushes, and remote branch state.",
    )
    parser.add_argument("--repo", default="", help="Optional repository slug override.")
    sub = parser.add_subparsers(dest="subcommand")

    p_auth = sub.add_parser("auth-status", help="Check native Git binary, repository, and SSH authentication status.")
    p_auth.add_argument("--offline", action="store_true", help="Check local git binary and SSH key without network calls.")

    p_bdiff = sub.add_parser("branch-diff", help="Compute unified diff, changed files, and commits for a branch using native git.")
    p_bdiff.add_argument("--branch", required=True, help="Target feature branch name.")
    p_bdiff.add_argument("--base", default="main", help="Base branch name (default: main).")

    p_bpush = sub.add_parser("push-branch", help="Push a timestamped branch to origin using native git.")
    p_bpush.add_argument("--branch", required=True, help="Target feature branch name.")
    p_bpush.add_argument("--remote", default="origin", help="Git remote name (default: origin).")
    p_bpush.add_argument("--base", default="main", help="Base branch name (default: main).")

    p_bstate = sub.add_parser("branch-state", help="Inspect remote branch existence and merge status using native git.")
    p_bstate.add_argument("--branch", required=True, help="Target feature branch name.")
    p_bstate.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_bstate.add_argument("--remote", default="origin", help="Git remote name (default: origin).")
    p_bstate.add_argument("--no-fetch", action="store_true", help="Do not run git fetch before checking.")

    p_bdel = sub.add_parser("delete-branch", help="Delete a timestamped branch on remote and locally using native git.")
    p_bdel.add_argument("--branch", required=True, help="Target feature branch name.")
    p_bdel.add_argument("--remote", default="origin", help="Git remote name (default: origin).")
    p_bdel.add_argument("--local-only", action="store_true", help="Delete local branch only.")

    p_sync = sub.add_parser(
        "sync-repo",
        help="Synchronize local base branch (main) with remote and prune local branches/worktrees cleaned on remote using pure git.",
    )
    p_sync.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_sync.add_argument("--remote", default="origin", help="Git remote name (default: origin).")
    p_sync.add_argument("--protect", nargs="*", default=[], help="Optional active branch names to protect from pruning.")
    p_sync.add_argument("--no-fetch", action="store_true", help="Do not run git fetch --prune before cleaning.")

    p_deploy = sub.add_parser(
        "deploy-home",
        help="Atomically deploy tracked src/home/ files from the pinned main branch into $HOME using git ls-files.",
    )
    p_deploy.add_argument("--base", default="main", help="Required base branch name (default: main).")
    p_deploy.add_argument("--no-prune", action="store_true", help="Do not prune orphaned files in managed $HOME directories.")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `git_client.py`."""
    load_env_into_os()
    args = parse_args(argv)
    client = GitVcsIntegrationClient(repo=getattr(args, "repo", ""))

    if args.subcommand == "auth-status":
        res = client.auth_status(live=not getattr(args, "offline", False))
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "branch-diff":
        res = client.get_branch_diff(branch_name=args.branch, base_branch=args.base)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "push-branch":
        res = client.push_branch(branch_name=args.branch, remote=args.remote, base_branch=args.base)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "branch-state":
        res = client.inspect_remote_branch_state(
            branch_name=args.branch,
            base_branch=args.base,
            remote=args.remote,
            fetch=not getattr(args, "no_fetch", False),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "delete-branch":
        res = client.delete_branch_native(
            branch_name=args.branch,
            remote=args.remote,
            delete_remote=not getattr(args, "local_only", False),
            delete_local=True,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "sync-repo":
        res = client.sync_and_clean_local_repo(
            base_branch=args.base,
            remote=args.remote,
            protected_branches=list(getattr(args, "protect", []) or []),
            fetch=not getattr(args, "no_fetch", False),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "deploy-home":
        res = client.deploy_repo_to_home(
            require_base_branch=getattr(args, "base", "main"),
            prune_orphans=not getattr(args, "no_prune", False),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("ok", "skipped") else 1

    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
