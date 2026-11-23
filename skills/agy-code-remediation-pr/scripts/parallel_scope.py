#!/usr/bin/env python3
"""Deterministic 0-LLM AST, ES2022 JS & HTML/CSS Section Blast-Radius Overlap Analyzer (`parallel_scope.py`).

Pure Python 3 standard-library module (Santa Lockdown compliant, Unix portable, Zero-PII) providing:
  1. Python `ast` import-graph, symbol/call extraction, and comment-banner section spans.
  2. ES2022+ JavaScript import/export graph, function/class/method brace spans, and DOM/CSS token references.
  3. HTML5 (`html.parser`) landmark/`id`/`data-section` spans and CSS3 `:root`/rule/banner spans.
  4. 4-tier blast-radius overlap classification (`NONE`, `TRANSITIVE`, `INTERFACE_SHARED`, `DEEP_SHARED`)
     with pragmatic section-level disjointness for monoliths so Slot #2 can develop safely in parallel
     when touching disjoint functions/sections/line ranges (guarded by post-merge rebase-reverify).
  5. Deterministic `landing_order` ranking: `depends_on` -> `pr_open`/review maturity -> numeric `priority`
     -> smaller blast radius -> `conversation_id` tie-breaker.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Mapping, Sequence
import contextlib
from dataclasses import asdict, dataclass, field
import heapq
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
from typing import Any

OVERLAP_NONE = "NONE"
OVERLAP_TRANSITIVE = "TRANSITIVE"
OVERLAP_INTERFACE_SHARED = "INTERFACE_SHARED"
OVERLAP_DEEP_SHARED = "DEEP_SHARED"

OVERLAP_TIERS: tuple[str, ...] = (
    OVERLAP_NONE,
    OVERLAP_TRANSITIVE,
    OVERLAP_INTERFACE_SHARED,
    OVERLAP_DEEP_SHARED,
)
OVERLAP_TIER_SEVERITY: dict[str, int] = {
    OVERLAP_NONE: 0,
    OVERLAP_TRANSITIVE: 1,
    OVERLAP_INTERFACE_SHARED: 2,
    OVERLAP_DEEP_SHARED: 3,
}
PARALLEL_SAFE_TIERS: frozenset[str] = frozenset({OVERLAP_NONE, OVERLAP_TRANSITIVE})

MONOLITH_MIN_LINES = 250
KNOWN_MONOLITH_BASENAMES: frozenset[str] = frozenset(
    {
        "pr_remediator.py",
        "dashboard.js",
        "portfolio.py",
        "styles.css",
        "index.html",
        "wiz_common.py",
        "dev_common.py",
        "git_client.py",
    }
)

SKIP_SCAN_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "tmp",
    }
)

_ANCHOR_LINE_RANGE_RE = re.compile(r"^L?(\d+)(?:\s*-\s*L?(\d+))?$", re.IGNORECASE)
_BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_#.-][A-Za-z0-9_#.:/-]{2,80})`")
_PY_BANNER_RE = re.compile(r"^\s*#\s*(?:={3,}|-{3,})\s*(.+?)\s*(?:={3,}|-{3,})?\s*$")
_JS_CSS_BANNER_RE = re.compile(
    r"^\s*(?://\s*(?:={3,}|-{3,}|#region)\s*(.+?)\s*(?:={3,}|-{3,})?|/\*\s*(?:={3,}|-{3,})\s*(.+?)\s*(?:={3,}|-{3,})?\s*\*/)\s*$",
    re.IGNORECASE,
)
_HTML_COMMENT_BANNER_RE = re.compile(
    r"<!--\s*(?:={2,}|-{2,}|SECTION\s*:)\s*(.+?)\s*(?:={2,}|-{2,})?\s*-->",
    re.IGNORECASE,
)

_JS_IMPORT_FROM_RE = re.compile(
    r"""(?m)^\s*import\s+(?:type\s+)?(.+?)\s+from\s+['"]([^'"]+)['"]""",
)
_JS_SIDE_EFFECT_IMPORT_RE = re.compile(
    r"""(?m)^\s*import\s+['"]([^'"]+)['"]""",
)
_JS_EXPORT_FROM_RE = re.compile(
    r"""(?m)^\s*export\s+(.+?)\s+from\s+['"]([^'"]+)['"]""",
)
_JS_DYNAMIC_IMPORT_RE = re.compile(
    r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""",
)
_JS_EXPORT_DECL_RE = re.compile(
    r"""(?m)^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)""",
)
_JS_EXPORT_LIST_RE = re.compile(
    r"""(?m)^\s*export\s*\{([^}]+)\}(?!\s*from)""",
)
_JS_TOP_FUNC_RE = re.compile(
    r"""^\s*(export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(""",
)
_JS_TOP_CLASS_RE = re.compile(
    r"""^\s*(export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b""",
)
_JS_TOP_ARROW_OR_FN_VAR_RE = re.compile(
    r"""^\s*(export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*\{""",
)
_JS_CLASS_METHOD_RE = re.compile(
    r"""^\s*(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(#?[A-Za-z_$][A-Za-z0-9_$]*)\s*\([^;]*\)\s*\{""",
)
_JS_CALL_IDENT_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_JS_DOM_ID_RE = re.compile(
    r"""(?:getElementById\s*\(\s*['"]([^'"]+)['"]|querySelector(?:All)?\s*\(\s*['"]#([A-Za-z0-9_:-]+))"""
)
_CSS_CUSTOM_PROP_DEF_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
_CSS_CUSTOM_PROP_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?['"]?([^'")\s;]+)['"]?\s*\)?""",
    re.IGNORECASE,
)
_JS_KEYWORDS: frozenset[str] = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "function",
        "return",
        "typeof",
        "await",
        "import",
        "super",
        "constructor",
    }
)
_HTML_SECTION_TAGS: frozenset[str] = frozenset(
    {"header", "nav", "main", "section", "article", "aside", "footer", "dialog", "template", "form"}
)
_HTML_VOID_TAGS: frozenset[str] = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_CLEAN_GIT_ENV_STRIP: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    }
)
_NON_REPO_PREFIX_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sh",
        ".bash",
        ".zsh",
        ".bats",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".rst",
        ".txt",
        ".sql",
        ".xml",
        ".svg",
        ".ini",
        ".cfg",
        ".lock",
        ".env",
    }
)
_REPO_PREFIX_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{2,}(?:/[A-Za-z0-9_.-]+)?$")
_SINGLE_COLON_RE = re.compile(r"(?<!:):(?!:)")
_GENERIC_FS_OWNER_SEGMENTS: frozenset[str] = frozenset(
    {"tmp", "var", "repos", "remotes", "git", "bare", "scratch", "worktrees"}
)


def split_repo_prefixed_target(raw_spec: str) -> tuple[str, str]:
    """Split an optional `<repo_name>:` or `<owner>/<repo_name>:` prefix from a target file/anchor string.

    Returns `(repo_prefix, remainder)`. When no repository prefix is present (for example
    `pkg/portfolio.py:15-42`, `pkg/portfolio.py::func`, `./pkg/cli.py`, or `#anchor`),
    returns `("", stripped_raw_spec)`.
    """
    text = str(raw_spec or "").strip()
    if not text or text.startswith((".", "/", "~", "#")):
        return ("", text)

    m_colon = _SINGLE_COLON_RE.search(text)
    if not m_colon:
        return ("", text)

    cand_prefix = text[: m_colon.start()].strip()
    rest = text[m_colon.end() :].strip()
    if not cand_prefix or not rest or rest.startswith("//"):
        return ("", text)
    if _ANCHOR_LINE_RANGE_RE.match(rest):
        return ("", text)
    if not _REPO_PREFIX_TOKEN_RE.match(cand_prefix):
        return ("", text)

    parts = cand_prefix.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return ("", text)
    repo_tok = parts[-1].removesuffix(".git")
    if len(repo_tok) < 2:
        return ("", text)
    lower_tok = repo_tok.lower()
    if any(lower_tok.endswith(ext) for ext in _NON_REPO_PREFIX_SUFFIXES):
        return ("", text)

    norm_prefix = f"{parts[0]}/{repo_tok}" if len(parts) == 2 else repo_tok
    return (norm_prefix, rest)


def _repo_prefix_matches_slug(repo_prefix: str, target_repo_slug: str) -> bool:
    """Return True when `repo_prefix` belongs to `target_repo_slug` (or when either is unscoped)."""
    pref = str(repo_prefix or "").strip().strip("/").removesuffix(".git").lower()
    targ = str(target_repo_slug or "").strip().strip("/").removesuffix(".git").lower()
    if not pref or not targ or targ == "default":
        return True
    if "/" in pref and "/" in targ:
        return pref == targ
    return pref.rsplit("/", 1)[-1] == targ.rsplit("/", 1)[-1]


def _infer_repo_slug_from_git_dir(repo_dir: Path | None) -> str:
    """Infer `owner/repo` (or `repo`) from `remote.origin.url` when `repo_dir` is a Git repository."""
    if repo_dir is None or not (repo_dir / ".git").exists():
        return ""
    clean_env = {k: v for k, v in os.environ.items() if k not in _CLEAN_GIT_ENV_STRIP}
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "config", "--get", "remote.origin.url"],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    raw_url = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if raw_url:
        cleaned = raw_url.rstrip("/").removesuffix(".git")
        # SSH form `git@github.com:owner/repo`
        if ":" in cleaned and "://" not in cleaned:
            tail = cleaned.split(":", 1)[1].strip("/")
            segs = [s for s in tail.split("/") if s]
            if len(segs) >= 2:
                return f"{segs[-2]}/{segs[-1]}".lower()
            if len(segs) == 1:
                return segs[0].lower()
        else:
            segs = [s for s in re.sub(r"^[a-zA-Z0-9+.-]+://[^/]*/?", "", cleaned).split("/") if s]
            if len(segs) >= 2:
                owner_cand, repo_cand = segs[-2], segs[-1]
                if owner_cand.lower() not in _GENERIC_FS_OWNER_SEGMENTS:
                    return f"{owner_cand}/{repo_cand}".lower()
                return repo_cand.lower()
            if len(segs) == 1:
                return segs[0].lower()
    return repo_dir.name.lower()


def normalize_target_files_for_repo(
    target_files: Sequence[str],
    *,
    repo_slug: str = "",
    repo_dir: Path | str | None = None,
) -> list[str]:
    """Normalize `<repo>:` / `<owner>/<repo>:` prefixes and filter out paths explicitly targeting other repositories."""
    r_dir = Path(repo_dir).expanduser() if repo_dir else None
    eff_slug = str(repo_slug or "").strip() or _infer_repo_slug_from_git_dir(r_dir)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in target_files or ():
        raw_str = str(raw_item or "").strip()
        if not raw_str:
            continue
        pref, rest = split_repo_prefixed_target(raw_str)
        if pref and eff_slug and not _repo_prefix_matches_slug(pref, eff_slug):
            continue
        rel, lrange, sym = parse_target_anchor_string(
            rest if pref else raw_str,
            repo_dir=r_dir,
            repo_slug=eff_slug,
        )
        if not rel and not sym:
            continue
        if lrange is not None:
            rebuilt = f"{rel}#L{lrange[0]}-L{lrange[1]}" if lrange[0] != lrange[1] else f"{rel}#L{lrange[0]}"
        elif sym and rel:
            rebuilt = f"{rel}#{sym}"
        elif sym:
            rebuilt = f"#{sym}"
        else:
            rebuilt = rel
        if rebuilt and rebuilt not in seen:
            seen.add(rebuilt)
            normalized.append(rebuilt)
    return normalized


def _normalize_rel_path(raw_path: str, repo_dir: Path | None = None) -> str:
    """Normalize a file path to forward-slash repository-relative form."""
    cleaned = str(raw_path or "").strip().replace("\\", "/")
    if not cleaned:
        return ""
    if repo_dir is not None:
        with contextlib.suppress(OSError, ValueError):
            cand = Path(cleaned).expanduser()
            if cand.is_absolute():
                cleaned = str(cand.resolve().relative_to(repo_dir.resolve())).replace("\\", "/")
    cleaned = re.sub(r"^\./+", "", cleaned).strip("/")
    return cleaned


def _normalize_anchor_token(token: str) -> str:
    """Normalize a section or symbol identifier for case-tolerant comparison."""
    return re.sub(r"\s+", " ", str(token or "").strip()).strip("#:")


def _slugify_section(title: str) -> str:
    """Convert a section banner title into a canonical lowercase slug."""
    return re.sub(r"[^a-z0-9]+", "-", str(title or "").strip().lower()).strip("-")


_HEX8_CID_RE = re.compile(r"^[0-9a-f]{8}$")


def _matches_cid(cand: str, ref: str) -> bool:
    """Return True when two conversation IDs match exactly or one is an 8-char hex short prefix of the other."""
    a = str(cand or "").strip().lower()
    b = str(ref or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if _HEX8_CID_RE.fullmatch(a) and b.startswith(a):
        return True
    if _HEX8_CID_RE.fullmatch(b) and a.startswith(b):
        return True
    return False


@dataclass(frozen=True)
class SectionSpan:
    """Contiguous named symbol or section span inside a source file."""

    kind: str
    name: str
    start_line: int
    end_line: int
    is_interface: bool = False
    calls: tuple[str, ...] = ()
    referenced_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize section span to JSON-friendly dict."""
        return asdict(self)


@dataclass(frozen=True)
class FileStructure:
    """Extracted 0-LLM static structure, imports, symbols, and section anchors for one file."""

    rel_path: str
    language: str
    total_lines: int
    is_monolith: bool
    imports: tuple[str, ...] = ()
    resolved_local_imports: tuple[str, ...] = ()
    imported_symbols: dict[str, tuple[str, ...]] = field(default_factory=dict)
    exported_symbols: tuple[str, ...] = ()
    sections: tuple[SectionSpan, ...] = ()
    dom_ids: tuple[str, ...] = ()
    css_tokens: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize file structure to JSON-friendly dict."""
        data = asdict(self)
        data["sections"] = [s.to_dict() for s in self.sections]
        return data


@dataclass(frozen=True)
class TaskAnchorSpec:
    """Normalized candidate task specification for overlap and landing-order analysis."""

    conversation_id: str
    title: str = ""
    status: str = "pending"
    priority: int = 50
    depends_on: tuple[str, ...] = ()
    target_files: tuple[str, ...] = ()
    section_anchors: tuple[str, ...] = ()
    symbol_hints: tuple[str, ...] = ()
    branch_name: str = ""
    base_sha: str = ""
    worktree_path: str = ""
    pr_gate_ready: bool = False
    prose_context: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TaskAnchorSpec:
        """Construct a `TaskAnchorSpec` from a queue item, `dev_pr_lock` dict, or custom dict."""
        cid = str(data.get("conversation_id") or data.get("id") or data.get("lock_id") or "").strip()
        raw_prio = data.get("priority", 50)
        try:
            prio = int(raw_prio)
        except (TypeError, ValueError):
            prio = 50

        raw_anchors = data.get("section_anchors")
        flat_anchors: list[str] = []
        if isinstance(raw_anchors, Mapping):
            for fkey, vals in raw_anchors.items():
                if isinstance(vals, (list, tuple, set)):
                    for v in vals:
                        if str(v).strip():
                            flat_anchors.append(f"{fkey}#{str(v).strip()}")
                elif str(vals).strip():
                    flat_anchors.append(f"{fkey}#{str(vals).strip()}")
        elif isinstance(raw_anchors, (list, tuple, set)):
            flat_anchors.extend(str(x).strip() for x in raw_anchors if str(x).strip())
        elif isinstance(raw_anchors, str) and raw_anchors.strip():
            flat_anchors.append(raw_anchors.strip())

        raw_hints = data.get("symbol_hints") or ()
        hints_list = (
            [raw_hints.strip()]
            if isinstance(raw_hints, str) and raw_hints.strip()
            else [str(x).strip() for x in raw_hints if str(x).strip()]
        )

        raw_targets = data.get("target_files") or ()
        targets_list = (
            [raw_targets.strip()]
            if isinstance(raw_targets, str) and raw_targets.strip()
            else [str(x).strip() for x in raw_targets if str(x).strip()]
        )

        raw_deps = data.get("depends_on") or ()
        deps_list = (
            [raw_deps.strip()]
            if isinstance(raw_deps, str) and raw_deps.strip()
            else [str(x).strip() for x in raw_deps if str(x).strip()]
        )

        branch = str(data.get("branch_name") or "").strip()
        if not branch:
            bnames = data.get("branch_names")
            if isinstance(bnames, (list, tuple)) and bnames:
                branch = str(bnames[-1] or "").strip().split("::")[-1]

        base_sha = str(data.get("base_sha") or "").strip()
        worktree_path = str(data.get("worktree_path") or "").strip()

        prose_parts = [
            str(data.get("title") or ""),
            str(data.get("proposed_fix") or ""),
            str(data.get("rca_summary") or data.get("summary") or ""),
            str(data.get("anchored_context") or ""),
        ]
        prose = "\n".join(p for p in prose_parts if p.strip())

        return cls(
            conversation_id=cid,
            title=str(data.get("title") or data.get("proposal_title") or "").strip(),
            status=str(data.get("status") or "pending").strip(),
            priority=prio,
            depends_on=tuple(deps_list),
            target_files=tuple(targets_list),
            section_anchors=tuple(flat_anchors),
            symbol_hints=tuple(hints_list),
            branch_name=branch,
            base_sha=base_sha,
            worktree_path=worktree_path,
            pr_gate_ready=bool(data.get("pr_gate_ready", False)),
            prose_context=prose,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize task spec to JSON-friendly dict."""
        out = asdict(self)
        out.pop("prose_context", None)
        return out


@dataclass(frozen=True)
class ResolvedFileTouch:
    """Resolved file-level and section/line-level touch footprint for one task on one file."""

    rel_path: str
    whole_file: bool
    line_ranges: tuple[tuple[int, int], ...] = ()
    matched_sections: tuple[str, ...] = ()
    matched_symbols: tuple[str, ...] = ()
    touches_interface: bool = False
    called_symbols: tuple[str, ...] = ()
    referenced_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize resolved file touch to JSON-friendly dict."""
        return {
            "rel_path": self.rel_path,
            "whole_file": self.whole_file,
            "line_ranges": [[s, e] for s, e in self.line_ranges],
            "matched_sections": list(self.matched_sections),
            "matched_symbols": list(self.matched_symbols),
            "touches_interface": self.touches_interface,
            "called_symbols": list(self.called_symbols),
            "referenced_ids": list(self.referenced_ids),
        }


@dataclass(frozen=True)
class PairOverlapResult:
    """Structured 4-tier blast-radius overlap result between two candidate tasks."""

    task_a_id: str
    task_b_id: str
    overlap_tier: str
    parallel_safe: bool
    requires_rebase_reverify: bool
    monolith_disjoint: bool
    shared_files: tuple[str, ...] = ()
    disjoint_monolith_files: tuple[str, ...] = ()
    colliding_files: tuple[str, ...] = ()
    import_edges: tuple[str, ...] = ()
    interface_collisions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    landing_order: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize pair overlap result to JSON-friendly dict."""
        return {
            "task_a_id": self.task_a_id,
            "task_b_id": self.task_b_id,
            "overlap_tier": self.overlap_tier,
            "parallel_safe": self.parallel_safe,
            "requires_rebase_reverify": self.requires_rebase_reverify,
            "monolith_disjoint": self.monolith_disjoint,
            "shared_files": list(self.shared_files),
            "disjoint_monolith_files": list(self.disjoint_monolith_files),
            "colliding_files": list(self.colliding_files),
            "import_edges": list(self.import_edges),
            "interface_collisions": list(self.interface_collisions),
            "reasons": list(self.reasons),
            "landing_order": list(self.landing_order),
        }


@dataclass(frozen=True)
class BatchOverlapAnalysis:
    """Multi-task overlap matrix, blast-radius summary, and deterministic landing order."""

    tasks_count: int
    landing_order: tuple[str, ...]
    blast_radius_by_task: dict[str, dict[str, Any]]
    pairwise: tuple[PairOverlapResult, ...]
    parallel_slot2_candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize batch overlap analysis to JSON-friendly dict."""
        return {
            "status": "ok",
            "tasks_count": self.tasks_count,
            "landing_order": list(self.landing_order),
            "blast_radius_by_task": self.blast_radius_by_task,
            "pairwise": [p.to_dict() for p in self.pairwise],
            "parallel_slot2_candidates": list(self.parallel_slot2_candidates),
        }


# ==============================================================================
# Anchor Spec Parsing (`file.py#L10-L50`, `file.js#renderCard`, `file.py::func`)
# ==============================================================================


def parse_target_anchor_string(
    raw_spec: str,
    *,
    repo_dir: Path | None = None,
    repo_slug: str = "",
) -> tuple[str, tuple[int, int] | None, str]:
    """Parse a target file or section anchor string into `(rel_path, optional_line_range, symbol_or_section)`.

    Supported forms (including optional `<repo_name>:` or `<owner>/<repo_name>:` prefixes):
      - `agy-dev-plugin:tests/test_agy_dev_plugin.py`
      - `carlosrodlop/agy-dev-plugin:tests/test_agy_dev_plugin.py`
      - `web/dashboard.js#L120-L180` or `web/dashboard.js:120-180`
      - `skills/.../pr_remediator.py::record_pr_review_evaluation`
      - `web/dashboard.js#renderOverview`
      - `styles.css#root-tokens`
      - `clients/git_client.py`

    When `repo_slug` is provided and `raw_spec` carries an explicit prefix for a *different*
    repository, returns `("", None, "")` so cross-repo multi-repo entries never collide falsely.
    """
    text = str(raw_spec or "").strip()
    if not text:
        return ("", None, "")

    repo_prefix, stripped_text = split_repo_prefixed_target(text)
    if repo_prefix:
        if repo_slug and not _repo_prefix_matches_slug(repo_prefix, repo_slug):
            return ("", None, "")
        text = stripped_text
        if not text:
            return ("", None, "")

    file_part = text
    anchor_part = ""
    if "::" in text:
        file_part, anchor_part = text.split("::", 1)
    elif "#" in text:
        left, right = text.split("#", 1)
        if left.strip():
            file_part, anchor_part = left, right
        else:
            return ("", None, _normalize_anchor_token(right))
    elif ":" in text:
        left, right = text.rsplit(":", 1)
        if _ANCHOR_LINE_RANGE_RE.match(right.strip()) and left.strip():
            file_part, anchor_part = left, right

    rel_path = _normalize_rel_path(file_part, repo_dir=repo_dir)
    anchor_clean = anchor_part.strip()
    if not anchor_clean:
        return (rel_path, None, "")

    m_lines = _ANCHOR_LINE_RANGE_RE.match(anchor_clean)
    if m_lines:
        start_ln = max(1, int(m_lines.group(1)))
        end_ln = max(start_ln, int(m_lines.group(2) or start_ln))
        return (rel_path, (start_ln, end_ln), "")

    return (rel_path, None, _normalize_anchor_token(anchor_clean))


# ==============================================================================
# Language Structure & Section-Anchor Extractors (Python, ES2022 JS, HTML5, CSS3)
# ==============================================================================


def _build_repo_python_module_index(repo_dir: Path | None) -> dict[str, list[str]]:
    """Index repository `.py` files by dotted module path and bare module stem for fast resolution."""
    index: dict[str, list[str]] = {}
    if repo_dir is None or not repo_dir.is_dir():
        return index

    for root, dirs, files in os.walk(repo_dir, topdown=True, followlinks=False):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_SCAN_DIRS]
        root_p = Path(root)
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            fpath = root_p / fname
            try:
                rel = str(fpath.relative_to(repo_dir)).replace("\\", "/")
            except ValueError:
                continue
            no_ext = rel[:-3]
            if no_ext.endswith("/__init__"):
                pkg_dotted = no_ext[: -len("/__init__")].replace("/", ".")
                if pkg_dotted:
                    index.setdefault(pkg_dotted, []).append(rel)
                    index.setdefault(pkg_dotted.rsplit(".", 1)[-1], []).append(rel)
            else:
                dotted = no_ext.replace("/", ".")
                index.setdefault(dotted, []).append(rel)
                stem = Path(no_ext).name
                index.setdefault(stem, []).append(rel)
    return index


def _resolve_python_import_to_repo_file(
    module_name: str,
    level: int,
    importer_rel_path: str,
    repo_dir: Path | None,
    py_index: dict[str, list[str]],
) -> str:
    """Resolve a Python `import` / `from ... import` target to a repository-relative `.py` file if local."""
    importer_parent = Path(importer_rel_path).parent
    if level > 0:
        base_parts = list(importer_parent.parts)
        up = max(0, level - 1)
        if up <= len(base_parts):
            prefix_parts = base_parts[: len(base_parts) - up]
        else:
            prefix_parts = []
        mod_parts = [p for p in (module_name or "").split(".") if p]
        cand_base = "/".join([*prefix_parts, *mod_parts])
        for cand_rel in (f"{cand_base}.py", f"{cand_base}/__init__.py"):
            cand_norm = cand_rel.strip("/")
            if repo_dir is not None and (repo_dir / cand_norm).is_file():
                return cand_norm
        return ""

    clean_mod = (module_name or "").strip()
    if not clean_mod:
        return ""

    # 1. Exact sibling file in same directory (common in `clients/` and `skills/*/scripts/`)
    mod_rel = clean_mod.replace(".", "/")
    for local_cand in (
        importer_parent / f"{mod_rel}.py",
        importer_parent / mod_rel / "__init__.py",
        Path(f"{mod_rel}.py"),
        Path(mod_rel) / "__init__.py",
        Path("clients") / f"{mod_rel}.py",
        Path("src") / f"{mod_rel}.py",
        Path("src") / mod_rel / "__init__.py",
    ):
        norm_c = str(local_cand).replace("\\", "/").lstrip("./")
        if repo_dir is not None and (repo_dir / norm_c).is_file() and norm_c != importer_rel_path:
            return norm_c

    # 2. Repo-wide Python module index lookup
    for key in (clean_mod, clean_mod.split(".", 1)[0]):
        matches = [m for m in py_index.get(key, ()) if m != importer_rel_path]
        if len(matches) == 1:
            return matches[0]
    return ""


def _collect_ast_calls_and_attrs(node: ast.AST, module_aliases: dict[str, str]) -> tuple[tuple[str, ...], dict[str, set[str]]]:
    """Collect called function names and `alias.symbol` attribute usages within an AST subtree."""
    calls: list[str] = []
    seen_calls: set[str] = set()
    alias_attrs: dict[str, set[str]] = {}

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Name) and fn.id not in seen_calls:
                seen_calls.add(fn.id)
                calls.append(fn.id)
            elif isinstance(fn, ast.Attribute) and fn.attr not in seen_calls:
                seen_calls.add(fn.attr)
                calls.append(fn.attr)
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            base_id = child.value.id
            if base_id in module_aliases:
                alias_attrs.setdefault(module_aliases[base_id], set()).add(child.attr)

    return tuple(calls), alias_attrs


def extract_python_structure(
    source: str,
    rel_path: str,
    *,
    repo_dir: Path | None = None,
    py_index: dict[str, list[str]] | None = None,
) -> FileStructure:
    """Extract Python AST imports, exported symbols, function/class/method spans, and banner sections."""
    lines = source.splitlines()
    total_lines = len(lines)
    is_monolith = total_lines >= MONOLITH_MIN_LINES or Path(rel_path).name in KNOWN_MONOLITH_BASENAMES
    idx = py_index if py_index is not None else _build_repo_python_module_index(repo_dir)

    sections: list[SectionSpan] = []
    # Extract `# === Banner ===` sections first
    banner_hits: list[tuple[int, str]] = []
    for line_no, raw_ln in enumerate(lines, start=1):
        m_ban = _PY_BANNER_RE.match(raw_ln)
        if m_ban:
            title = m_ban.group(1).strip()
            if title and not set(title).issubset({"=", "-", "#", " "}):
                banner_hits.append((line_no, title))
    for i, (start_ln, title) in enumerate(banner_hits):
        end_ln = (banner_hits[i + 1][0] - 1) if (i + 1 < len(banner_hits)) else total_lines
        sections.append(
            SectionSpan(
                kind="banner",
                name=title,
                start_line=start_ln,
                end_line=max(start_ln, end_ln),
                is_interface=False,
            )
        )

    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return FileStructure(
            rel_path=rel_path,
            language="python",
            total_lines=total_lines,
            is_monolith=is_monolith,
            sections=tuple(sections),
        )

    raw_imports: list[str] = []
    resolved_imports: list[str] = []
    imported_symbols_set: dict[str, set[str]] = {}
    module_aliases: dict[str, str] = {}
    explicit_all: set[str] = set()
    exported: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                raw_imports.append(alias.name)
                resolved = _resolve_python_import_to_repo_file(alias.name, 0, rel_path, repo_dir, idx)
                if resolved:
                    if resolved not in resolved_imports:
                        resolved_imports.append(resolved)
                    imported_symbols_set.setdefault(resolved, set())
                    as_name = alias.asname or alias.name.split(".", 1)[0]
                    module_aliases[as_name] = resolved
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            raw_imports.append(f"{'.' * node.level}{mod_name}")
            resolved = _resolve_python_import_to_repo_file(mod_name, node.level, rel_path, repo_dir, idx)
            if resolved:
                if resolved not in resolved_imports:
                    resolved_imports.append(resolved)
                sym_bucket = imported_symbols_set.setdefault(resolved, set())
                for alias in node.names:
                    sym_bucket.add(alias.name)
                    if alias.asname:
                        module_aliases[alias.asname] = resolved
            else:
                # Handle `from pkg import submodule` where `pkg/submodule.py` is a local module
                for alias in node.names:
                    cand_sub = f"{mod_name}.{alias.name}".strip(".") if mod_name else alias.name
                    sub_resolved = _resolve_python_import_to_repo_file(cand_sub, node.level, rel_path, repo_dir, idx)
                    if sub_resolved:
                        if sub_resolved not in resolved_imports:
                            resolved_imports.append(sub_resolved)
                        imported_symbols_set.setdefault(sub_resolved, set())
                        module_aliases[alias.asname or alias.name] = sub_resolved
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                explicit_all.add(elt.value)
                    elif not target.id.startswith("_") and target.id.isupper():
                        exported.append(target.id)

    _, file_alias_attrs = _collect_ast_calls_and_attrs(tree, module_aliases)
    for mod_rel, attrs in file_alias_attrs.items():
        imported_symbols_set.setdefault(mod_rel, set()).update(attrs)
    for mod_rel, sym_set in imported_symbols_set.items():
        if not sym_set:
            # Whole-module import with dynamic/unrecognized access: treat as `*`
            sym_set.add("*")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start_ln = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end_ln = int(node.end_lineno or node.lineno)
            is_pub = (node.name in explicit_all) if explicit_all else (not node.name.startswith("_"))
            if is_pub and node.name not in exported:
                exported.append(node.name)
            fn_calls, _ = _collect_ast_calls_and_attrs(node, module_aliases)
            sections.append(
                SectionSpan(
                    kind="function",
                    name=node.name,
                    start_line=start_ln,
                    end_line=end_ln,
                    is_interface=is_pub,
                    calls=fn_calls,
                )
            )
        elif isinstance(node, ast.ClassDef):
            start_ln = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end_ln = int(node.end_lineno or node.lineno)
            is_pub = (node.name in explicit_all) if explicit_all else (not node.name.startswith("_"))
            if is_pub and node.name not in exported:
                exported.append(node.name)
            cls_calls, _ = _collect_ast_calls_and_attrs(node, module_aliases)
            sections.append(
                SectionSpan(
                    kind="class",
                    name=node.name,
                    start_line=start_ln,
                    end_line=end_ln,
                    is_interface=is_pub,
                    calls=cls_calls,
                )
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_start = min([item.lineno, *(d.lineno for d in item.decorator_list)])
                    m_end = int(item.end_lineno or item.lineno)
                    m_calls, _ = _collect_ast_calls_and_attrs(item, module_aliases)
                    m_pub = is_pub and (not item.name.startswith("_") or item.name == "__init__")
                    sections.append(
                        SectionSpan(
                            kind="method",
                            name=f"{node.name}.{item.name}",
                            start_line=m_start,
                            end_line=m_end,
                            is_interface=m_pub,
                            calls=m_calls,
                        )
                    )

    imported_frozen = {k: tuple(sorted(v)) for k, v in imported_symbols_set.items()}
    return FileStructure(
        rel_path=rel_path,
        language="python",
        total_lines=total_lines,
        is_monolith=is_monolith,
        imports=tuple(dict.fromkeys(raw_imports)),
        resolved_local_imports=tuple(resolved_imports),
        imported_symbols=imported_frozen,
        exported_symbols=tuple(dict.fromkeys(exported)),
        sections=tuple(sections),
    )


def _strip_js_comments_and_strings_keep_lines(src: str) -> str:
    """Blank out JS comments, strings, and regex literals while preserving newlines and braces."""
    out: list[str] = []
    n = len(src)
    i = 0
    state = "normal"
    tpl_stack: list[int] = []
    regex_prev_chars = frozenset("([{,;:=!?&|^~+*%<>-")
    regex_prev_kw = frozenset({"return", "case", "throw", "typeof", "void", "delete", "in", "instanceof", "new", "of", "await", "yield"})

    def _allows_regex(pos: int) -> bool:
        j = pos - 1
        while j >= 0 and src[j] in " \t\r\n":
            j -= 1
        if j < 0:
            return True
        ch_prev = src[j]
        if ch_prev in regex_prev_chars:
            return True
        if ch_prev.isalnum() or ch_prev in ("_", "$"):
            k = j
            while k >= 0 and (src[k].isalnum() or src[k] in ("_", "$")):
                k -= 1
            return src[k + 1 : j + 1] in regex_prev_kw
        return False

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == "normal":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                out.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                out.append("  ")
                i += 2
                continue
            if ch == "/" and _allows_regex(i):
                state = "regex"
                out.append(" ")
                i += 1
                continue
            if ch == "'":
                state = "single"
                out.append(" ")
                i += 1
                continue
            if ch == '"':
                state = "double"
                out.append(" ")
                i += 1
                continue
            if ch == "`":
                state = "template"
                out.append(" ")
                i += 1
                continue
            if ch == "{" and tpl_stack:
                tpl_stack[-1] += 1
            elif ch == "}" and tpl_stack:
                if tpl_stack[-1] == 0:
                    tpl_stack.pop()
                    state = "template"
                    out.append(" ")
                    i += 1
                    continue
                tpl_stack[-1] -= 1
            out.append(ch)
            i += 1
        elif state == "line_comment":
            out.append("\n" if ch == "\n" else " ")
            if ch == "\n":
                state = "normal"
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "normal"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        elif state in ("regex", "regex_class"):
            if ch == "\\" and nxt:
                out.append(" ")
                out.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == "\n":
                state = "normal"
            elif state == "regex":
                if ch == "[":
                    state = "regex_class"
                elif ch == "/":
                    state = "normal"
            elif ch == "]":
                state = "regex"
            i += 1
        elif state == "template":
            if ch == "\\" and nxt:
                out.append(" ")
                out.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            if ch == "$" and nxt == "{":
                tpl_stack.append(0)
                state = "normal"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == "`":
                state = "normal"
            i += 1
        else:
            q = "'" if state == "single" else '"'
            if ch == "\\" and nxt:
                out.append(" ")
                out.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == q or ch == "\n":
                state = "normal"
            i += 1
    return "".join(out)


def _resolve_relative_asset_import(specifier: str, importer_rel_path: str, repo_dir: Path | None) -> str:
    """Resolve a JS/HTML/CSS relative import specifier (`./foo.js`, `/web/app.js`) to a repo-relative path."""
    spec = str(specifier or "").strip()
    if not spec or re.match(r"^(?:https?:|data:|mailto:|javascript:|//)", spec, re.IGNORECASE):
        return ""
    spec = spec.split("?", 1)[0].split("#", 1)[0].strip()
    if not spec:
        return ""

    importer_parent = Path(importer_rel_path).parent
    candidates: list[Path] = []
    if spec.startswith("/"):
        rel_root = spec.lstrip("/")
        candidates.extend([Path(rel_root), Path("web") / rel_root, Path("src/web") / rel_root])
    elif spec.startswith("."):
        base_cand = importer_parent / spec
        candidates.extend(
            [
                base_cand,
                Path(f"{base_cand}.js"),
                Path(f"{base_cand}.mjs"),
                base_cand / "index.js",
            ]
        )
    else:
        # Bare specifier -> check local sibling only
        candidates.extend([importer_parent / spec, importer_parent / f"{spec}.js"])

    for cand in candidates:
        if repo_dir is not None:
            with contextlib.suppress(OSError, ValueError):
                abs_c = (repo_dir / cand).resolve()
                if abs_c.is_file():
                    return str(abs_c.relative_to(repo_dir.resolve())).replace("\\", "/")
        else:
            norm = os.path.normpath(str(cand)).replace("\\", "/").lstrip("./")
            if not norm.startswith(".."):
                return norm
    return ""


def _find_brace_block_end_line(stripped_lines: list[str], start_idx: int) -> int:
    """Find 1-based closing line of a `{ ... }` block starting at 0-based `start_idx`."""
    depth = 0
    opened = False
    for idx in range(start_idx, len(stripped_lines)):
        for ch in stripped_lines[idx]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return idx + 1
    return start_idx + 1


def extract_js_structure(
    source: str,
    rel_path: str,
    *,
    repo_dir: Path | None = None,
) -> FileStructure:
    """Extract ES2022+ JavaScript imports/exports, function/class/method spans, banners, and DOM/CSS refs."""
    raw_lines = source.splitlines()
    total_lines = len(raw_lines)
    is_monolith = total_lines >= MONOLITH_MIN_LINES or Path(rel_path).name in KNOWN_MONOLITH_BASENAMES
    stripped_src = _strip_js_comments_and_strings_keep_lines(source)
    stripped_lines = stripped_src.splitlines()

    raw_imports: list[str] = []
    resolved_imports: list[str] = []
    imported_symbols_set: dict[str, set[str]] = {}
    ns_aliases: dict[str, str] = {}

    for m_imp in _JS_IMPORT_FROM_RE.finditer(source):
        clause, spec = m_imp.group(1).strip(), m_imp.group(2).strip()
        raw_imports.append(spec)
        resolved = _resolve_relative_asset_import(spec, rel_path, repo_dir)
        if resolved:
            if resolved not in resolved_imports:
                resolved_imports.append(resolved)
            bucket = imported_symbols_set.setdefault(resolved, set())
            m_ns = re.search(r"\*\s+as\s+([A-Za-z_$][A-Za-z0-9_$]*)", clause)
            if m_ns:
                ns_aliases[m_ns.group(1)] = resolved
            if "{" in clause and "}" in clause:
                named = clause[clause.find("{") + 1 : clause.find("}")]
                for part in named.split(","):
                    orig = part.strip().split(" as ", 1)[0].strip()
                    if orig:
                        bucket.add(orig)
            rem = re.sub(r"\{[^}]*\}", "", clause).replace(",", " ").strip()
            if rem and not m_ns:
                bucket.add("default")

    for m_side in _JS_SIDE_EFFECT_IMPORT_RE.finditer(source):
        spec = m_side.group(1).strip()
        raw_imports.append(spec)
        resolved = _resolve_relative_asset_import(spec, rel_path, repo_dir)
        if resolved and resolved not in resolved_imports:
            resolved_imports.append(resolved)
            imported_symbols_set.setdefault(resolved, set()).add("*")

    for m_reexp in _JS_EXPORT_FROM_RE.finditer(source):
        clause, spec = m_reexp.group(1).strip(), m_reexp.group(2).strip()
        raw_imports.append(spec)
        resolved = _resolve_relative_asset_import(spec, rel_path, repo_dir)
        if resolved:
            if resolved not in resolved_imports:
                resolved_imports.append(resolved)
            bucket = imported_symbols_set.setdefault(resolved, set())
            if "*" in clause:
                bucket.add("*")
            elif "{" in clause and "}" in clause:
                named = clause[clause.find("{") + 1 : clause.find("}")]
                for part in named.split(","):
                    orig = part.strip().split(" as ", 1)[0].strip()
                    if orig:
                        bucket.add(orig)

    for m_dyn in _JS_DYNAMIC_IMPORT_RE.finditer(source):
        spec = m_dyn.group(1).strip()
        raw_imports.append(spec)
        resolved = _resolve_relative_asset_import(spec, rel_path, repo_dir)
        if resolved and resolved not in resolved_imports:
            resolved_imports.append(resolved)
            imported_symbols_set.setdefault(resolved, set()).add("*")

    for ns_ident, mod_rel in ns_aliases.items():
        attrs = set(re.findall(rf"\b{re.escape(ns_ident)}\.([A-Za-z_$][A-Za-z0-9_$]*)", stripped_src))
        imported_symbols_set.setdefault(mod_rel, set()).update(attrs or {"*"})

    exported: list[str] = []
    for m_ed in _JS_EXPORT_DECL_RE.finditer(stripped_src):
        sym = m_ed.group(1).strip()
        if sym and sym not in exported:
            exported.append(sym)
    for m_el in _JS_EXPORT_LIST_RE.finditer(stripped_src):
        for part in m_el.group(1).split(","):
            chunks = [c.strip() for c in part.strip().split(" as ") if c.strip()]
            if chunks:
                exp_name = chunks[-1]
                if exp_name not in exported:
                    exported.append(exp_name)

    sections: list[SectionSpan] = []
    banner_hits: list[tuple[int, str]] = []
    for line_no, raw_ln in enumerate(raw_lines, start=1):
        m_ban = _JS_CSS_BANNER_RE.match(raw_ln)
        if m_ban:
            title = (m_ban.group(1) or m_ban.group(2) or "").strip()
            if title:
                banner_hits.append((line_no, title))
    for i, (start_ln, title) in enumerate(banner_hits):
        end_ln = (banner_hits[i + 1][0] - 1) if (i + 1 < len(banner_hits)) else total_lines
        sections.append(SectionSpan(kind="banner", name=title, start_line=start_ln, end_line=max(start_ln, end_ln)))

    brace_depth = 0
    for idx_ln, s_line in enumerate(stripped_lines):
        line_no = idx_ln + 1
        if brace_depth == 0:
            m_fn = _JS_TOP_FUNC_RE.match(s_line) or _JS_TOP_ARROW_OR_FN_VAR_RE.match(s_line)
            m_cls = None if m_fn else _JS_TOP_CLASS_RE.match(s_line)
            if m_fn:
                is_exp = bool(m_fn.group(1)) or (m_fn.group(2) in exported)
                fn_name = m_fn.group(2)
                end_ln = _find_brace_block_end_line(stripped_lines, idx_ln)
                body_raw = "\n".join(raw_lines[idx_ln:end_ln])
                body_stripped = "\n".join(stripped_lines[idx_ln:end_ln])
                calls = tuple(
                    dict.fromkeys(
                        c for c in _JS_CALL_IDENT_RE.findall(body_stripped) if c not in _JS_KEYWORDS and c != fn_name
                    )
                )
                ref_ids = tuple(
                    dict.fromkeys(
                        [
                            *(f"#{m.group(1) or m.group(2)}" for m in _JS_DOM_ID_RE.finditer(body_raw)),
                            *_CSS_CUSTOM_PROP_DEF_RE.findall(body_raw),
                            *_CSS_CUSTOM_PROP_VAR_RE.findall(body_raw),
                        ]
                    )
                )
                sections.append(
                    SectionSpan(
                        kind="function",
                        name=fn_name,
                        start_line=line_no,
                        end_line=end_ln,
                        is_interface=is_exp,
                        calls=calls,
                        referenced_ids=ref_ids,
                    )
                )
            elif m_cls:
                is_exp = bool(m_cls.group(1)) or (m_cls.group(2) in exported)
                cls_name = m_cls.group(2)
                end_ln = _find_brace_block_end_line(stripped_lines, idx_ln)
                sections.append(
                    SectionSpan(
                        kind="class",
                        name=cls_name,
                        start_line=line_no,
                        end_line=end_ln,
                        is_interface=is_exp,
                    )
                )
                cls_depth = 0
                for m_idx in range(idx_ln, min(end_ln, len(stripped_lines))):
                    m_line = stripped_lines[m_idx]
                    if cls_depth == 1:
                        mm = _JS_CLASS_METHOD_RE.match(m_line)
                        if mm and mm.group(1) not in _JS_KEYWORDS:
                            m_name = mm.group(1)
                            m_end = _find_brace_block_end_line(stripped_lines, m_idx)
                            m_stripped = "\n".join(stripped_lines[m_idx:m_end])
                            m_calls = tuple(
                                dict.fromkeys(
                                    c for c in _JS_CALL_IDENT_RE.findall(m_stripped) if c not in _JS_KEYWORDS and c != m_name
                                )
                            )
                            sections.append(
                                SectionSpan(
                                    kind="method",
                                    name=f"{cls_name}.{m_name}",
                                    start_line=m_idx + 1,
                                    end_line=m_end,
                                    is_interface=is_exp and not m_name.startswith("#"),
                                    calls=m_calls,
                                )
                            )
                    cls_depth += m_line.count("{") - m_line.count("}")
        brace_depth += s_line.count("{") - s_line.count("}")
        if brace_depth < 0:
            brace_depth = 0

    all_dom_refs = tuple(
        dict.fromkeys(f"#{m.group(1) or m.group(2)}" for m in _JS_DOM_ID_RE.finditer(source))
    )
    return FileStructure(
        rel_path=rel_path,
        language="javascript",
        total_lines=total_lines,
        is_monolith=is_monolith,
        imports=tuple(dict.fromkeys(raw_imports)),
        resolved_local_imports=tuple(resolved_imports),
        imported_symbols={k: tuple(sorted(v)) for k, v in imported_symbols_set.items()},
        exported_symbols=tuple(exported),
        sections=tuple(sections),
        dom_ids=all_dom_refs,
    )


class _HTMLSectionExtractor(HTMLParser):
    """Stdlib `HTMLParser` collecting local asset imports, DOM IDs, and semantic/anchored element spans."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.asset_refs: list[str] = []
        self.dom_ids: list[str] = []
        self.completed_spans: list[SectionSpan] = []
        self._stack: list[tuple[str, str | None, int, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}
        line_no, _ = self.getpos()

        if tag_l == "script" and attr_map.get("src"):
            self.asset_refs.append(attr_map["src"])
        elif tag_l == "link" and attr_map.get("href") and "stylesheet" in attr_map.get("rel", "").lower():
            self.asset_refs.append(attr_map["href"])

        el_id = attr_map.get("id", "").strip()
        if el_id and f"#{el_id}" not in self.dom_ids:
            self.dom_ids.append(f"#{el_id}")

        data_sec = (attr_map.get("data-section") or attr_map.get("data-tab") or "").strip()
        sec_name: str | None = None
        is_iface = False
        if el_id:
            sec_name = f"#{el_id}"
            is_iface = True
        elif data_sec:
            sec_name = data_sec
        elif tag_l in _HTML_SECTION_TAGS:
            sec_name = tag_l

        if tag_l in _HTML_VOID_TAGS:
            if sec_name:
                self.completed_spans.append(
                    SectionSpan(kind="html_element", name=sec_name, start_line=line_no, end_line=line_no, is_interface=is_iface)
                )
            return

        self._stack.append((tag_l, sec_name, line_no, is_iface))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}
        line_no, _ = self.getpos()
        if tag_l == "link" and attr_map.get("href") and "stylesheet" in attr_map.get("rel", "").lower():
            self.asset_refs.append(attr_map["href"])
        el_id = attr_map.get("id", "").strip()
        if el_id:
            if f"#{el_id}" not in self.dom_ids:
                self.dom_ids.append(f"#{el_id}")
            self.completed_spans.append(
                SectionSpan(kind="html_element", name=f"#{el_id}", start_line=line_no, end_line=line_no, is_interface=True)
            )

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        end_line, _ = self.getpos()
        for idx in range(len(self._stack) - 1, -1, -1):
            open_tag, sec_name, start_line, is_iface = self._stack[idx]
            if open_tag == tag_l:
                del self._stack[idx:]
                if sec_name:
                    self.completed_spans.append(
                        SectionSpan(
                            kind="html_section",
                            name=sec_name,
                            start_line=start_line,
                            end_line=max(start_line, end_line),
                            is_interface=is_iface,
                        )
                    )
                break


def extract_html_structure(
    source: str,
    rel_path: str,
    *,
    repo_dir: Path | None = None,
) -> FileStructure:
    """Extract HTML5 `<script>`/`<link>` local assets, `id` contracts, and landmark/section spans."""
    lines = source.splitlines()
    total_lines = len(lines)
    is_monolith = total_lines >= MONOLITH_MIN_LINES or Path(rel_path).name in KNOWN_MONOLITH_BASENAMES

    parser = _HTMLSectionExtractor()
    with contextlib.suppress(Exception):
        parser.feed(source)
        parser.close()

    sections: list[SectionSpan] = list(parser.completed_spans)
    banner_hits: list[tuple[int, str]] = []
    for line_no, raw_ln in enumerate(lines, start=1):
        m_ban = _HTML_COMMENT_BANNER_RE.search(raw_ln)
        if m_ban:
            banner_hits.append((line_no, m_ban.group(1).strip()))
    for i, (start_ln, title) in enumerate(banner_hits):
        end_ln = (banner_hits[i + 1][0] - 1) if (i + 1 < len(banner_hits)) else total_lines
        sections.append(SectionSpan(kind="banner", name=title, start_line=start_ln, end_line=max(start_ln, end_ln)))

    resolved_imports: list[str] = []
    imp_syms: dict[str, tuple[str, ...]] = {}
    for ref in parser.asset_refs:
        resolved = _resolve_relative_asset_import(ref, rel_path, repo_dir)
        if resolved and resolved not in resolved_imports:
            resolved_imports.append(resolved)
            imp_syms[resolved] = ("*",)

    return FileStructure(
        rel_path=rel_path,
        language="html",
        total_lines=total_lines,
        is_monolith=is_monolith,
        imports=tuple(dict.fromkeys(parser.asset_refs)),
        resolved_local_imports=tuple(resolved_imports),
        imported_symbols=imp_syms,
        exported_symbols=tuple(parser.dom_ids),
        sections=tuple(sections),
        dom_ids=tuple(parser.dom_ids),
    )


def _strip_css_comments_keep_lines(src: str) -> str:
    """Blank out CSS block comments and strings while preserving exact line numbers."""
    out: list[str] = []
    n = len(src)
    i = 0
    state = "normal"
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == "normal":
            if ch == "/" and nxt == "*":
                state = "comment"
                out.append("  ")
                i += 2
                continue
            if ch in ("'", '"'):
                state = ch
                out.append(" ")
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "comment":
            if ch == "*" and nxt == "/":
                state = "normal"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:
            if ch == "\\" and nxt:
                out.append(" ")
                out.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == state or ch == "\n":
                state = "normal"
            i += 1
    return "".join(out)


def extract_css_structure(
    source: str,
    rel_path: str,
    *,
    repo_dir: Path | None = None,
) -> FileStructure:
    """Extract CSS3 `@import` dependencies, `:root` custom-property tokens, banner sections, and rule spans."""
    raw_lines = source.splitlines()
    total_lines = len(raw_lines)
    is_monolith = total_lines >= MONOLITH_MIN_LINES or Path(rel_path).name in KNOWN_MONOLITH_BASENAMES
    stripped = _strip_css_comments_keep_lines(source)
    stripped_lines = stripped.splitlines()

    raw_imports: list[str] = []
    resolved_imports: list[str] = []
    imp_syms: dict[str, tuple[str, ...]] = {}
    for m_imp in _CSS_IMPORT_RE.finditer(source):
        spec = m_imp.group(1).strip()
        raw_imports.append(spec)
        resolved = _resolve_relative_asset_import(spec, rel_path, repo_dir)
        if resolved and resolved not in resolved_imports:
            resolved_imports.append(resolved)
            imp_syms[resolved] = ("*",)

    sections: list[SectionSpan] = []
    banner_hits: list[tuple[int, str]] = []
    for line_no, raw_ln in enumerate(raw_lines, start=1):
        m_ban = _JS_CSS_BANNER_RE.match(raw_ln)
        if m_ban:
            title = (m_ban.group(1) or m_ban.group(2) or "").strip()
            if title:
                banner_hits.append((line_no, title))
    for i, (start_ln, title) in enumerate(banner_hits):
        end_ln = (banner_hits[i + 1][0] - 1) if (i + 1 < len(banner_hits)) else total_lines
        sections.append(SectionSpan(kind="banner", name=title, start_line=start_ln, end_line=max(start_ln, end_ln)))

    depth = 0
    selector_buf: list[str] = []
    selector_start = 1
    defined_tokens: list[str] = []

    for idx_ln, s_line in enumerate(stripped_lines):
        line_no = idx_ln + 1
        if depth == 0:
            if not selector_buf and s_line.strip():
                selector_start = line_no
            if "{" in s_line:
                before_brace = s_line.split("{", 1)[0].strip()
                if before_brace:
                    selector_buf.append(before_brace)
                selector = re.sub(r"\s+", " ", " ".join(selector_buf)).strip()
                selector_buf = []
                end_ln = _find_brace_block_end_line(stripped_lines, idx_ln)
                block_raw = "\n".join(raw_lines[selector_start - 1 : end_ln])
                block_defs = _CSS_CUSTOM_PROP_DEF_RE.findall(block_raw)
                block_vars = _CSS_CUSTOM_PROP_VAR_RE.findall(block_raw)
                for tok in block_defs:
                    if tok not in defined_tokens:
                        defined_tokens.append(tok)
                is_root_tokens = ":root" in selector or "[data-theme" in selector or bool(block_defs)
                if selector:
                    sections.append(
                        SectionSpan(
                            kind="css_rule",
                            name=selector,
                            start_line=selector_start,
                            end_line=end_ln,
                            is_interface=is_root_tokens,
                            referenced_ids=tuple(dict.fromkeys([*block_defs, *block_vars])),
                        )
                    )
            elif ";" in s_line:
                selector_buf = []
            elif s_line.strip():
                selector_buf.append(s_line.strip())
        depth += s_line.count("{") - s_line.count("}")
        if depth < 0:
            depth = 0

    return FileStructure(
        rel_path=rel_path,
        language="css",
        total_lines=total_lines,
        is_monolith=is_monolith,
        imports=tuple(dict.fromkeys(raw_imports)),
        resolved_local_imports=tuple(resolved_imports),
        imported_symbols=imp_syms,
        exported_symbols=tuple(defined_tokens),
        sections=tuple(sections),
        css_tokens=tuple(defined_tokens),
    )


def extract_file_structure(
    file_path: Path | str,
    *,
    repo_dir: Path | None = None,
    source_override: str | None = None,
    py_index: dict[str, list[str]] | None = None,
) -> FileStructure:
    """Dispatch static structure & section-anchor extraction based on file suffix."""
    rel = _normalize_rel_path(str(file_path), repo_dir=repo_dir)
    abs_path: Path | None = None
    if source_override is None:
        cand = Path(str(file_path)).expanduser()
        if cand.is_file():
            abs_path = cand
        elif repo_dir is not None and rel and (repo_dir / rel).is_file():
            abs_path = repo_dir / rel

    if source_override is not None:
        src = source_override
    elif abs_path is not None:
        try:
            src = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            src = ""
    else:
        src = ""

    suffix = Path(rel).suffix.lower()
    if suffix == ".py":
        return extract_python_structure(src, rel, repo_dir=repo_dir, py_index=py_index)
    if suffix in (".js", ".mjs"):
        return extract_js_structure(src, rel, repo_dir=repo_dir)
    if suffix in (".html", ".htm"):
        return extract_html_structure(src, rel, repo_dir=repo_dir)
    if suffix == ".css":
        return extract_css_structure(src, rel, repo_dir=repo_dir)

    lines = src.splitlines()
    return FileStructure(
        rel_path=rel,
        language="other",
        total_lines=len(lines),
        is_monolith=len(lines) >= MONOLITH_MIN_LINES,
    )


# ==============================================================================
# Repository Import Graph & Git Diff Hunk Line Extraction
# ==============================================================================


def build_repository_import_graph(
    repo_dir: Path | str,
) -> tuple[dict[str, FileStructure], dict[str, tuple[str, ...]]]:
    """Scan trackable Python, JS, HTML, and CSS files in `repo_dir` and return `(structures, forward_imports)`."""
    root_dir = Path(repo_dir).expanduser().resolve()
    if not root_dir.is_dir():
        return {}, {}

    py_index = _build_repo_python_module_index(root_dir)
    structures: dict[str, FileStructure] = {}
    forward_graph: dict[str, tuple[str, ...]] = {}
    supported_ext = {".py", ".js", ".mjs", ".html", ".htm", ".css"}

    for cur_root, dirs, files in os.walk(root_dir, topdown=True, followlinks=False):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_SCAN_DIRS]
        cur_p = Path(cur_root)
        for fname in sorted(files):
            if Path(fname).suffix.lower() not in supported_ext:
                continue
            fpath = cur_p / fname
            try:
                rel = str(fpath.relative_to(root_dir)).replace("\\", "/")
            except ValueError:
                continue
            st = extract_file_structure(rel, repo_dir=root_dir, py_index=py_index)
            structures[rel] = st
            forward_graph[rel] = st.resolved_local_imports

    return structures, forward_graph


def _resolve_branch_worktree_dir(
    repo_dir: Path | None,
    *,
    branch_name: str = "",
    worktree_path: str = "",
    clean_env: dict[str, str] | None = None,
) -> Path | None:
    """Resolve an existing isolated/checked-out worktree directory for `(branch_name, worktree_path)`."""
    wt_raw = str(worktree_path or "").strip()
    if wt_raw:
        wt_exp = Path(wt_raw.replace("$HOME", str(Path.home()))).expanduser()
        if wt_exp.is_dir() and (wt_exp / ".git").exists():
            return wt_exp.resolve()

    clean_br = str(branch_name or "").strip().removeprefix("refs/heads/")
    if not clean_br or clean_br in ("main", "master", "HEAD") or repo_dir is None or not (repo_dir / ".git").exists():
        return None

    env = clean_env if clean_env is not None else {k: v for k, v in os.environ.items() if k not in _CLEAN_GIT_ENV_STRIP}
    wt_proc = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if wt_proc.returncode == 0 and wt_proc.stdout:
        target_ref = f"refs/heads/{clean_br}"
        cur_wt = ""
        for raw_ln in wt_proc.stdout.splitlines():
            ln = raw_ln.strip()
            if not ln:
                cur_wt = ""
            elif ln.startswith("worktree "):
                cur_wt = ln[9:].strip()
            elif ln.startswith("branch ") and cur_wt and ln[7:].strip() == target_ref:
                cand_wt = Path(cur_wt)
                if cand_wt.is_dir() and (cand_wt / ".git").exists():
                    return cand_wt.resolve()
    return None


def collect_git_changed_files(
    repo_dir: Path | None,
    *,
    branch_name: str = "",
    base_sha: str = "",
    worktree_path: str = "",
    base_branch: str = "main",
) -> list[str]:
    """Collect deduplicated changed file paths via `git diff --name-only <base_sha>...HEAD` plus staged/unstaged/untracked changes.

    Strictly scoped to active branch/worktree tasks (`branch_name` or `worktree_path` non-empty) so unbranched
    `pending` queue candidates never inherit working-tree files from `main`.
    """
    clean_br = str(branch_name or "").strip().removeprefix("refs/heads/")
    if clean_br in ("main", "master", "HEAD"):
        clean_br = ""

    clean_env = {k: v for k, v in os.environ.items() if k not in _CLEAN_GIT_ENV_STRIP}
    wt_dir = _resolve_branch_worktree_dir(
        repo_dir,
        branch_name=clean_br,
        worktree_path=worktree_path,
        clean_env=clean_env,
    )
    if not clean_br and wt_dir is None:
        return []

    eff_git_dir = wt_dir if wt_dir is not None else repo_dir
    if eff_git_dir is None or not (eff_git_dir / ".git").exists():
        return []

    base_ref = ""
    clean_base_sha = str(base_sha or "").strip()
    base_candidates = [c for c in (clean_base_sha, f"origin/{base_branch}", base_branch) if c]
    for cand_base in base_candidates:
        chk = subprocess.run(
            ["git", "-C", str(eff_git_dir), "rev-parse", "--verify", "--quiet", f"{cand_base}^{{commit}}"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if chk.returncode == 0:
            base_ref = cand_base
            break

    head_ref = ""
    if wt_dir is not None:
        head_ref = "HEAD"
    elif clean_br:
        for cand_br in (clean_br, f"origin/{clean_br}"):
            chk_br = subprocess.run(
                ["git", "-C", str(eff_git_dir), "rev-parse", "--verify", "--quiet", f"{cand_br}^{{commit}}"],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if chk_br.returncode == 0:
                head_ref = cand_br
                break

    seen: set[str] = set()
    changed: list[str] = []

    def _collect_nul(git_dir: Path, args: list[str]) -> None:
        proc = subprocess.run(
            ["git", "-C", str(git_dir), *args],
            env=clean_env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return
        for raw_entry in proc.stdout.split(b"\x00"):
            if not raw_entry:
                continue
            rel = _normalize_rel_path(raw_entry.decode("utf-8", errors="surrogateescape"))
            if not rel or "__pycache__/" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            if rel not in seen:
                seen.add(rel)
                changed.append(rel)

    if base_ref and head_ref:
        _collect_nul(eff_git_dir, ["diff", "--name-only", "-z", f"{base_ref}...{head_ref}"])

    if wt_dir is not None:
        _collect_nul(wt_dir, ["diff", "--cached", "--name-only", "-z"])
        _collect_nul(wt_dir, ["diff", "--name-only", "-z"])
        _collect_nul(wt_dir, ["ls-files", "--others", "--exclude-standard", "-z"])
    return changed


def _extract_git_branch_diff_line_ranges(
    repo_dir: Path | None,
    branch_name: str,
    base_branch: str = "main",
    *,
    base_sha: str = "",
    worktree_path: str = "",
) -> dict[str, list[tuple[int, int]]]:
    """Extract modified file line ranges from `git diff -U0 <base>...<branch>` (plus uncommitted worktree changes)."""
    clean_br = str(branch_name or "").strip().removeprefix("refs/heads/")
    if clean_br in ("main", "master", "HEAD"):
        clean_br = ""

    clean_env = {k: v for k, v in os.environ.items() if k not in _CLEAN_GIT_ENV_STRIP}
    wt_dir = _resolve_branch_worktree_dir(
        repo_dir,
        branch_name=clean_br,
        worktree_path=worktree_path,
        clean_env=clean_env,
    )
    eff_dir = wt_dir if wt_dir is not None else repo_dir
    if eff_dir is None or (not clean_br and wt_dir is None) or not (eff_dir / ".git").exists():
        return {}

    base_ref = ""
    clean_base_sha = str(base_sha or "").strip()
    for cand_base in [c for c in (clean_base_sha, f"origin/{base_branch}", base_branch) if c]:
        chk = subprocess.run(
            ["git", "-C", str(eff_dir), "rev-parse", "--verify", "--quiet", f"{cand_base}^{{commit}}"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if chk.returncode == 0:
            base_ref = cand_base
            break

    br_ref = "HEAD" if wt_dir is not None else ""
    if not br_ref and clean_br:
        for cand_br in (clean_br, f"origin/{clean_br}"):
            chk_br = subprocess.run(
                ["git", "-C", str(eff_dir), "rev-parse", "--verify", "--quiet", f"{cand_br}^{{commit}}"],
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if chk_br.returncode == 0:
                br_ref = cand_br
                break

    ranges_by_file: dict[str, list[tuple[int, int]]] = {}
    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

    def _parse_unified_zero(diff_stdout: str) -> None:
        current_file = ""
        for line in diff_stdout.splitlines():
            if line.startswith("+++ b/"):
                current_file = _normalize_rel_path(line[6:].strip())
                if current_file:
                    ranges_by_file.setdefault(current_file, [])
            elif line.startswith("+++ /dev/null"):
                current_file = ""
            elif line.startswith("@@ ") and current_file:
                m = hunk_re.match(line)
                if m:
                    old_start = max(1, int(m.group(1)))
                    old_cnt = int(m.group(2)) if m.group(2) is not None else 1
                    new_start = max(1, int(m.group(3)))
                    new_cnt = int(m.group(4)) if m.group(4) is not None else 1
                    start_ln = old_start if old_cnt > 0 else new_start
                    span_len = max(1, old_cnt if old_cnt > 0 else new_cnt)
                    ranges_by_file[current_file].append((start_ln, start_ln + span_len - 1))

    if base_ref and br_ref:
        proc = subprocess.run(
            ["git", "-C", str(eff_dir), "diff", "--unified=0", f"{base_ref}...{br_ref}"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            _parse_unified_zero(proc.stdout)

    if wt_dir is not None:
        wt_diff = subprocess.run(
            ["git", "-C", str(wt_dir), "diff", "--unified=0", "HEAD"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if wt_diff.returncode == 0 and wt_diff.stdout:
            _parse_unified_zero(wt_diff.stdout)
        untracked = subprocess.run(
            ["git", "-C", str(wt_dir), "ls-files", "--others", "--exclude-standard", "-z"],
            env=clean_env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if untracked.returncode == 0 and untracked.stdout:
            for raw_u in untracked.stdout.split(b"\x00"):
                if not raw_u:
                    continue
                rel_u = _normalize_rel_path(raw_u.decode("utf-8", errors="surrogateescape"))
                if rel_u and "__pycache__/" not in rel_u and not rel_u.endswith((".pyc", ".pyo")):
                    ranges_by_file.setdefault(rel_u, [])

    return ranges_by_file


# ==============================================================================
# Task Touch Resolution & 4-Tier Overlap Classification
# ==============================================================================


def _match_anchor_against_structure(
    anchor_token: str,
    structure: FileStructure,
) -> list[SectionSpan]:
    """Match a symbol/section/selector/banner anchor token against extracted `SectionSpan` entries."""
    clean = _normalize_anchor_token(anchor_token)
    if not clean:
        return []
    clean_lower = clean.lower()
    clean_slug = _slugify_section(clean)

    exact: list[SectionSpan] = []
    fuzzy: list[SectionSpan] = []
    for sec in structure.sections:
        sec_norm = _normalize_anchor_token(sec.name)
        sec_lower = sec_norm.lower()
        sec_slug = _slugify_section(sec.name)
        tail_method = sec_norm.rsplit(".", 1)[-1].lower() if "." in sec_norm else ""

        if sec_norm == clean or sec_lower == clean_lower or f"#{sec_lower}" == f"#{clean_lower}":
            exact.append(sec)
        elif tail_method and tail_method == clean_lower:
            exact.append(sec)
        elif clean_slug and sec_slug and (clean_slug == sec_slug or (len(clean_slug) >= 5 and clean_slug in sec_slug)):
            fuzzy.append(sec)
        elif sec.kind == "css_rule" and clean in sec.name.split():
            exact.append(sec)
    return exact or fuzzy


def resolve_task_touches(
    task: TaskAnchorSpec,
    *,
    repo_dir: Path | None = None,
    repo_slug: str = "",
    structures: dict[str, FileStructure] | None = None,
    py_index: dict[str, list[str]] | None = None,
) -> dict[str, ResolvedFileTouch]:
    """Resolve every touched file and its concrete section/symbol/line spans for `task`."""
    eff_repo_slug = str(repo_slug or "").strip() or _infer_repo_slug_from_git_dir(repo_dir)
    file_ranges: dict[str, list[tuple[int, int]]] = {}
    file_tokens: dict[str, list[str]] = {}
    global_tokens: list[str] = [_normalize_anchor_token(h) for h in task.symbol_hints if _normalize_anchor_token(h)]
    explicit_whole_files: set[str] = set()

    for raw_tf in task.target_files:
        rel, lrange, sym = parse_target_anchor_string(raw_tf, repo_dir=repo_dir, repo_slug=eff_repo_slug)
        if not rel:
            if sym:
                global_tokens.append(sym)
            continue
        file_ranges.setdefault(rel, [])
        file_tokens.setdefault(rel, [])
        if lrange is not None:
            file_ranges[rel].append(lrange)
        elif sym:
            file_tokens[rel].append(sym)
        else:
            explicit_whole_files.add(rel)

    for raw_sec in task.section_anchors:
        rel, lrange, sym = parse_target_anchor_string(raw_sec, repo_dir=repo_dir, repo_slug=eff_repo_slug)
        if rel:
            file_ranges.setdefault(rel, [])
            file_tokens.setdefault(rel, [])
            explicit_whole_files.discard(rel)
            if lrange is not None:
                file_ranges[rel].append(lrange)
            if sym:
                file_tokens[rel].append(sym)
        elif sym:
            global_tokens.append(sym)

    # Fall back to `git diff --name-only <base_sha>...HEAD` (+ staged/unstaged/untracked) when `target_files` is empty
    # or supplement active branch/worktree changes so overlap detection never misses live Slot #1 edits.
    if task.branch_name or task.worktree_path:
        git_changed_files = collect_git_changed_files(
            repo_dir,
            branch_name=task.branch_name,
            base_sha=task.base_sha,
            worktree_path=task.worktree_path,
        )
        for rel_git in git_changed_files:
            file_ranges.setdefault(rel_git, [])
            file_tokens.setdefault(rel_git, [])

    diff_ranges = _extract_git_branch_diff_line_ranges(
        repo_dir,
        task.branch_name,
        base_sha=task.base_sha,
        worktree_path=task.worktree_path,
    )
    for rel_diff, d_ranges in diff_ranges.items():
        file_ranges.setdefault(rel_diff, [])
        file_tokens.setdefault(rel_diff, [])
        if d_ranges:
            explicit_whole_files.discard(rel_diff)
            file_ranges[rel_diff].extend(d_ranges)

    # Extract prose backtick symbol hints strictly when matching known symbols on monolith files
    prose_tokens: list[str] = []
    if task.prose_context:
        for m_bt in _BACKTICK_SYMBOL_RE.finditer(task.prose_context):
            cand_tok = m_bt.group(1).strip()
            if "/" not in cand_tok and len(cand_tok) >= 4:
                prose_tokens.append(_normalize_anchor_token(cand_tok.rstrip("()")))

    resolved: dict[str, ResolvedFileTouch] = {}
    for rel in sorted(set(file_ranges.keys()) | set(file_tokens.keys())):
        st = (
            structures[rel]
            if (structures is not None and rel in structures)
            else extract_file_structure(rel, repo_dir=repo_dir, py_index=py_index)
        )
        ranges: list[tuple[int, int]] = list(file_ranges.get(rel) or [])
        matched_secs: list[str] = []
        matched_syms: list[str] = []
        touches_iface = False
        calls: list[str] = []
        ref_ids: list[str] = []

        tokens_for_file = [*file_tokens.get(rel, ()), *global_tokens]
        if not ranges and not tokens_for_file and st.is_monolith and prose_tokens:
            tokens_for_file = prose_tokens

        for tok in tokens_for_file:
            spans = _match_anchor_against_structure(tok, st)
            for sp in spans:
                ranges.append((sp.start_line, sp.end_line))
                if sp.name not in matched_secs:
                    matched_secs.append(sp.name)
                if sp.kind in ("function", "class", "method") and sp.name not in matched_syms:
                    matched_syms.append(sp.name)
                    if "." in sp.name:
                        matched_syms.append(sp.name.rsplit(".", 1)[-1])
                if sp.is_interface:
                    touches_iface = True
                for c in sp.calls:
                    if c not in calls:
                        calls.append(c)
                for rid in sp.referenced_ids:
                    if rid not in ref_ids:
                        ref_ids.append(rid)

        # Also map explicit/diff line ranges onto enclosing AST/JS/HTML/CSS spans
        if ranges:
            for r_start, r_end in list(ranges):
                for sp in st.sections:
                    if sp.kind == "banner":
                        continue
                    if max(r_start, sp.start_line) <= min(r_end, sp.end_line):
                        if sp.name not in matched_secs:
                            matched_secs.append(sp.name)
                        if sp.kind in ("function", "class", "method") and sp.name not in matched_syms:
                            matched_syms.append(sp.name)
                        if sp.is_interface:
                            touches_iface = True
                        for c in sp.calls:
                            if c not in calls:
                                calls.append(c)
                        for rid in sp.referenced_ids:
                            if rid not in ref_ids:
                                ref_ids.append(rid)

        whole_file = not bool(ranges)
        if whole_file:
            touches_iface = bool(st.exported_symbols or st.dom_ids or st.css_tokens or st.language != "other")
            matched_syms = list(st.exported_symbols)
            ref_ids = list(dict.fromkeys([*st.dom_ids, *st.css_tokens]))

        merged_ranges = _merge_line_intervals(ranges)
        resolved[rel] = ResolvedFileTouch(
            rel_path=rel,
            whole_file=whole_file,
            line_ranges=tuple(merged_ranges),
            matched_sections=tuple(matched_secs),
            matched_symbols=tuple(dict.fromkeys(matched_syms)),
            touches_interface=touches_iface,
            called_symbols=tuple(calls),
            referenced_ids=tuple(ref_ids),
        )
    return resolved


def _merge_line_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent 1-based `(start_line, end_line)` intervals."""
    if not intervals:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: list[tuple[int, int]] = [ordered[0]]
    for cur_s, cur_e in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if cur_s <= prev_e + 1:
            merged[-1] = (prev_s, max(prev_e, cur_e))
        else:
            merged.append((cur_s, cur_e))
    return merged


def _ranges_overlap(ranges_a: Sequence[tuple[int, int]], ranges_b: Sequence[tuple[int, int]]) -> bool:
    """Return True if any interval in `ranges_a` intersects any interval in `ranges_b`."""
    for a_s, a_e in ranges_a:
        for b_s, b_e in ranges_b:
            if max(a_s, b_s) <= min(a_e, b_e):
                return True
    return False


def _shortest_import_distance(
    start_file: str,
    target_files: set[str],
    forward_graph: Mapping[str, Sequence[str]],
) -> tuple[int | None, str]:
    """Return `(min_hops, matched_target_file)` from `start_file` to any file in `target_files` via `forward_graph`."""
    if not target_files:
        return (None, "")
    queue: deque[tuple[str, int]] = deque([(start_file, 0)])
    visited: set[str] = {start_file}
    while queue:
        cur, dist = queue.popleft()
        for nxt in forward_graph.get(cur, ()):
            if nxt in target_files:
                return (dist + 1, nxt)
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, dist + 1))
    return (None, "")


def _review_maturity_rank(task: TaskAnchorSpec) -> int:
    """Rank task maturity so further-along branches land first (`0` = PR open .. `5` = hold)."""
    st = task.status.strip().upper()
    if task.pr_gate_ready or st in ("PR_OPEN", "PENDING_HUMAN_PR_VALIDATION"):
        return 0
    if st == "READY_AWAITING_BASE_MERGE":
        return 1
    if st == "AWAITING_AGENT_REVIEW":
        return 2
    if st in ("IN_PROGRESS", "DEVELOPING", "NEEDS_REVISION_ITERATING", "NEEDS_REBASE_REVERIFY"):
        return 3
    if st == "PENDING":
        return 4
    return 5


def compute_blast_radius_metrics(
    touches: Mapping[str, ResolvedFileTouch],
    reverse_graph: Mapping[str, Sequence[str]],
    structures: Mapping[str, FileStructure],
) -> dict[str, Any]:
    """Compute deterministic blast-radius metrics for a task's resolved file touches."""
    files_count = len(touches)
    lines_span = 0
    for rel, touch in touches.items():
        if touch.whole_file:
            st = structures.get(rel)
            lines_span += max(50, st.total_lines if st is not None else 200)
        else:
            lines_span += sum((end - start + 1) for start, end in touch.line_ranges)

    # Transitive reverse dependents count
    dependents: set[str] = set()
    queue: deque[str] = deque(touches.keys())
    while queue:
        cur = queue.popleft()
        for rev_importer in reverse_graph.get(cur, ()):
            if rev_importer not in touches and rev_importer not in dependents:
                dependents.add(rev_importer)
                queue.append(rev_importer)

    interface_files = sum(1 for t in touches.values() if t.touches_interface)
    score = (files_count * 100) + (len(dependents) * 40) + (interface_files * 20) + min(lines_span, 2000)
    return {
        "files_count": files_count,
        "lines_span": lines_span,
        "reverse_dependents_count": len(dependents),
        "interface_files_count": interface_files,
        "blast_radius_score": score,
    }


def compute_landing_order(
    tasks: Sequence[TaskAnchorSpec | Mapping[str, Any]],
    *,
    repo_dir: Path | str | None = None,
    repo_slug: str = "",
    structures: dict[str, FileStructure] | None = None,
    forward_graph: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """Compute deterministic `landing_order` across candidate tasks.

    Precedence:
      1. `depends_on` topological order (dependencies land before dependents)
      2. `pr_open` / review maturity (`PENDING_HUMAN_PR_VALIDATION` / `pr_open` -> `READY_AWAITING_BASE_MERGE` -> `AWAITING_AGENT_REVIEW` -> `DEVELOPING` -> `pending`)
      3. Numeric `priority` (lower integer wins)
      4. Smaller blast radius (`blast_radius_score`, `files_count`, `lines_span`)
      5. `conversation_id` lexicographical tie-break
    """
    specs = [t if isinstance(t, TaskAnchorSpec) else TaskAnchorSpec.from_mapping(t) for t in tasks]
    if not specs:
        return []

    r_dir = Path(repo_dir).expanduser().resolve() if repo_dir else None
    if structures is None or forward_graph is None:
        if r_dir is not None and r_dir.is_dir():
            structures, forward_graph = build_repository_import_graph(r_dir)
        else:
            structures, forward_graph = {}, {}

    reverse_graph: dict[str, list[str]] = {}
    for src_f, dsts in forward_graph.items():
        for dst_f in dsts:
            reverse_graph.setdefault(dst_f, []).append(src_f)

    py_index = _build_repo_python_module_index(r_dir)
    sort_keys: dict[str, tuple[int, int, int, int, int, str]] = {}
    spec_by_id: dict[str, TaskAnchorSpec] = {}

    for idx_pos, sp in enumerate(specs):
        cid = sp.conversation_id or f"task-{idx_pos}"
        spec_by_id[cid] = sp
        touches = resolve_task_touches(sp, repo_dir=r_dir, repo_slug=repo_slug, structures=structures, py_index=py_index)
        metrics = compute_blast_radius_metrics(touches, reverse_graph, structures)
        unanchored_penalty = 100000 if not touches else 0
        sort_keys[cid] = (
            _review_maturity_rank(sp),
            int(sp.priority),
            int(metrics["blast_radius_score"]) + unanchored_penalty,
            int(metrics["files_count"]),
            int(metrics["lines_span"]),
            cid,
        )

    cids = list(spec_by_id.keys())
    in_degree: dict[str, int] = {c: 0 for c in cids}
    dependents_map: dict[str, list[str]] = {c: [] for c in cids}

    for cid, sp in spec_by_id.items():
        for raw_dep in sp.depends_on:
            for cand_cid in cids:
                if cand_cid != cid and _matches_cid(cand_cid, raw_dep):
                    if cid not in dependents_map[cand_cid]:
                        dependents_map[cand_cid].append(cid)
                        in_degree[cid] += 1

    heap: list[tuple[tuple[int, int, int, int, int, str], str]] = [
        (sort_keys[c], c) for c in cids if in_degree[c] == 0
    ]
    heapq.heapify(heap)

    ordered: list[str] = []
    visited: set[str] = set()
    while heap:
        _, chosen = heapq.heappop(heap)
        if chosen in visited:
            continue
        visited.add(chosen)
        ordered.append(chosen)
        for dep_child in dependents_map[chosen]:
            in_degree[dep_child] -= 1
            if in_degree[dep_child] == 0:
                heapq.heappush(heap, (sort_keys[dep_child], dep_child))

    # Deterministic cycle fallback if malformed circular depends_on is supplied
    if len(ordered) < len(cids):
        remaining = sorted((c for c in cids if c not in visited), key=lambda c: sort_keys[c])
        ordered.extend(remaining)
    return ordered


def analyze_pair_overlap(
    task_a: TaskAnchorSpec | Mapping[str, Any],
    task_b: TaskAnchorSpec | Mapping[str, Any],
    *,
    repo_dir: Path | str | None = None,
    repo_slug: str = "",
    structures: dict[str, FileStructure] | None = None,
    forward_graph: dict[str, tuple[str, ...]] | None = None,
) -> PairOverlapResult:
    """Classify blast-radius overlap between `task_a` and `task_b` into `NONE`, `TRANSITIVE`, `INTERFACE_SHARED`, or `DEEP_SHARED`."""
    spec_a = task_a if isinstance(task_a, TaskAnchorSpec) else TaskAnchorSpec.from_mapping(task_a)
    spec_b = task_b if isinstance(task_b, TaskAnchorSpec) else TaskAnchorSpec.from_mapping(task_b)
    r_dir = Path(repo_dir).expanduser().resolve() if repo_dir else None

    if structures is None or forward_graph is None:
        if r_dir is not None and r_dir.is_dir():
            structures, forward_graph = build_repository_import_graph(r_dir)
        else:
            structures, forward_graph = {}, {}

    order = tuple(
        compute_landing_order(
            [spec_a, spec_b],
            repo_dir=r_dir,
            repo_slug=repo_slug,
            structures=structures,
            forward_graph=forward_graph,
        )
    )

    # 1. Explicit dependency link -> DEEP_SHARED
    a_deps_b = any(_matches_cid(spec_b.conversation_id, d) for d in spec_a.depends_on)
    b_deps_a = any(_matches_cid(spec_a.conversation_id, d) for d in spec_b.depends_on)
    if a_deps_b or b_deps_a:
        return PairOverlapResult(
            task_a_id=spec_a.conversation_id,
            task_b_id=spec_b.conversation_id,
            overlap_tier=OVERLAP_DEEP_SHARED,
            parallel_safe=False,
            requires_rebase_reverify=True,
            monolith_disjoint=False,
            reasons=("explicit_depends_on",),
            landing_order=order,
        )

    py_index = _build_repo_python_module_index(r_dir)
    touches_a = resolve_task_touches(spec_a, repo_dir=r_dir, repo_slug=repo_slug, structures=structures, py_index=py_index)
    touches_b = resolve_task_touches(spec_b, repo_dir=r_dir, repo_slug=repo_slug, structures=structures, py_index=py_index)

    # Unanchored task cannot prove disjointness safely
    if not touches_a or not touches_b:
        return PairOverlapResult(
            task_a_id=spec_a.conversation_id,
            task_b_id=spec_b.conversation_id,
            overlap_tier=OVERLAP_DEEP_SHARED,
            parallel_safe=False,
            requires_rebase_reverify=True,
            monolith_disjoint=False,
            reasons=("unanchored_target_files",),
            landing_order=order,
        )

    max_tier = OVERLAP_NONE
    shared_files = sorted(set(touches_a.keys()) & set(touches_b.keys()))
    disjoint_monolith_files: list[str] = []
    colliding_files: list[str] = []
    import_edges: list[str] = []
    interface_collisions: list[str] = []
    reasons: list[str] = []

    def _escalate(tier: str, reason: str) -> None:
        nonlocal max_tier
        if OVERLAP_TIER_SEVERITY[tier] > OVERLAP_TIER_SEVERITY[max_tier]:
            max_tier = tier
        if reason and reason not in reasons:
            reasons.append(reason)

    # 2. Evaluate shared files (including monolith section-level disjointness)
    for shared_rel in shared_files:
        ta = touches_a[shared_rel]
        tb = touches_b[shared_rel]
        st = structures.get(shared_rel) or extract_file_structure(shared_rel, repo_dir=r_dir, py_index=py_index)

        if ta.whole_file or tb.whole_file:
            colliding_files.append(shared_rel)
            _escalate(OVERLAP_DEEP_SHARED, f"whole_file_collision:{shared_rel}")
            continue

        sec_set_a = {s.lower() for s in ta.matched_sections}
        sec_set_b = {s.lower() for s in tb.matched_sections}
        if _ranges_overlap(ta.line_ranges, tb.line_ranges) or (sec_set_a and (sec_set_a & sec_set_b)):
            colliding_files.append(shared_rel)
            _escalate(OVERLAP_DEEP_SHARED, f"overlapping_section_or_lines:{shared_rel}")
            continue

        # Ranges and named sections are strictly disjoint inside `shared_rel`!
        # Check intra-file symbol call or CSS `:root` token coupling between the two sections.
        syms_a = {s.rsplit(".", 1)[-1] for s in ta.matched_symbols} | set(ta.matched_symbols)
        syms_b = {s.rsplit(".", 1)[-1] for s in tb.matched_symbols} | set(tb.matched_symbols)
        a_calls_b = syms_b & set(ta.called_symbols)
        b_calls_a = syms_a & set(tb.called_symbols)
        shared_refs = (set(ta.referenced_ids) & set(tb.referenced_ids)) if st.language == "css" and (ta.touches_interface or tb.touches_interface) else set()

        if a_calls_b or b_calls_a or shared_refs:
            coupled = sorted(a_calls_b | b_calls_a | shared_refs)
            for sym_c in coupled:
                interface_collisions.append(f"{shared_rel}:{sym_c}")
            _escalate(OVERLAP_INTERFACE_SHARED, f"intra_file_interface_call:{shared_rel}")
        else:
            disjoint_monolith_files.append(shared_rel)
            _escalate(OVERLAP_TRANSITIVE, f"monolith_disjoint_sections:{shared_rel}")

    # 3. Evaluate cross-file import graph & HTML/CSS/JS interface contracts
    files_a_set = set(touches_a.keys())
    files_b_set = set(touches_b.keys())

    def _check_directional_imports(
        importers: Mapping[str, ResolvedFileTouch],
        upstreams: Mapping[str, ResolvedFileTouch],
        upstream_set: set[str],
    ) -> None:
        for imp_rel, imp_touch in importers.items():
            imp_struct = structures.get(imp_rel) or extract_file_structure(imp_rel, repo_dir=r_dir, py_index=py_index)
            # Direct 1-hop imports from `imp_rel`
            for dst_rel in imp_struct.resolved_local_imports:
                if dst_rel not in upstream_set or dst_rel == imp_rel:
                    continue
                edge_str = f"{imp_rel}->{dst_rel}"
                if edge_str not in import_edges:
                    import_edges.append(edge_str)
                up_touch = upstreams[dst_rel]
                imported_syms = set(imp_struct.imported_symbols.get(dst_rel, ("*",)))
                up_syms = {s.rsplit(".", 1)[-1] for s in up_touch.matched_symbols} | set(up_touch.matched_symbols)

                if up_touch.whole_file:
                    interface_collisions.append(f"{edge_str}:whole_module")
                    _escalate(OVERLAP_INTERFACE_SHARED, f"direct_import_whole_module:{edge_str}")
                elif "*" in imported_syms and up_touch.touches_interface:
                    # Check if HTML/JS/CSS references specific DOM IDs or CSS tokens
                    up_contracts = set(up_touch.referenced_ids) | set(up_touch.matched_sections)
                    imp_contracts = set(imp_touch.referenced_ids) | set(imp_struct.dom_ids)
                    if imp_struct.language in ("html", "javascript", "css") and up_contracts and imp_contracts:
                        overlap_contracts = up_contracts & imp_contracts
                        if overlap_contracts:
                            for oc in sorted(overlap_contracts):
                                interface_collisions.append(f"{edge_str}:{oc}")
                            _escalate(OVERLAP_INTERFACE_SHARED, f"shared_ui_contract:{edge_str}")
                        else:
                            _escalate(OVERLAP_TRANSITIVE, f"disjoint_ui_asset_import:{edge_str}")
                    else:
                        interface_collisions.append(f"{edge_str}:wildcard_interface")
                        _escalate(OVERLAP_INTERFACE_SHARED, f"direct_import_interface:{edge_str}")
                elif imported_syms & up_syms:
                    for sym_hit in sorted(imported_syms & up_syms):
                        interface_collisions.append(f"{edge_str}:{sym_hit}")
                    _escalate(OVERLAP_INTERFACE_SHARED, f"direct_imported_symbol:{edge_str}")
                else:
                    _escalate(OVERLAP_TRANSITIVE, f"direct_import_disjoint_symbols:{edge_str}")

            # Indirect >=2-hop transitive imports
            hops, hit_target = _shortest_import_distance(imp_rel, upstream_set - {imp_rel}, forward_graph)
            if hops is not None and hops >= 2:
                edge_str = f"{imp_rel}-[{hops} hops]->{hit_target}"
                if edge_str not in import_edges:
                    import_edges.append(edge_str)
                _escalate(OVERLAP_TRANSITIVE, f"transitive_import:{edge_str}")

    _check_directional_imports(touches_a, touches_b, files_b_set)
    _check_directional_imports(touches_b, touches_a, files_a_set)

    # Cross-file DOM `#id` and CSS `--token` contract collisions across distinct frontend files
    ui_langs = {"html", "javascript", "css"}
    for fa, ta in touches_a.items():
        sta = structures.get(fa) or extract_file_structure(fa, repo_dir=r_dir, py_index=py_index)
        if sta.language not in ui_langs:
            continue
        contracts_a = {
            tok
            for tok in (*ta.referenced_ids, *ta.matched_sections)
            if tok.startswith(("#", "--"))
        }
        if not contracts_a:
            continue
        for fb, tb in touches_b.items():
            if fa == fb:
                continue
            stb = structures.get(fb) or extract_file_structure(fb, repo_dir=r_dir, py_index=py_index)
            if stb.language not in ui_langs:
                continue
            contracts_b = {
                tok
                for tok in (*tb.referenced_ids, *tb.matched_sections)
                if tok.startswith(("#", "--"))
            }
            shared_ui = sorted(contracts_a & contracts_b)
            if shared_ui:
                for tok_ui in shared_ui:
                    col = f"{fa}<->{fb}:{tok_ui}"
                    if col not in interface_collisions:
                        interface_collisions.append(col)
                _escalate(OVERLAP_INTERFACE_SHARED, f"shared_dom_or_css_contract:{fa}<->{fb}")

    parallel_safe = max_tier in PARALLEL_SAFE_TIERS
    monolith_disjoint = bool(disjoint_monolith_files) and parallel_safe
    return PairOverlapResult(
        task_a_id=spec_a.conversation_id,
        task_b_id=spec_b.conversation_id,
        overlap_tier=max_tier,
        parallel_safe=parallel_safe,
        requires_rebase_reverify=(max_tier != OVERLAP_NONE),
        monolith_disjoint=monolith_disjoint,
        shared_files=tuple(shared_files),
        disjoint_monolith_files=tuple(disjoint_monolith_files),
        colliding_files=tuple(colliding_files),
        import_edges=tuple(import_edges),
        interface_collisions=tuple(dict.fromkeys(interface_collisions)),
        reasons=tuple(reasons) if reasons else ("disjoint_files_and_imports",),
        landing_order=order,
    )


def analyze_candidates_overlap(
    tasks: Sequence[TaskAnchorSpec | Mapping[str, Any]],
    *,
    repo_dir: Path | str | None = None,
    repo_slug: str = "",
    primary_slot_id: str = "",
) -> BatchOverlapAnalysis:
    """Analyze overlap across 2+ candidate tasks, compute blast-radius metrics, and rank `landing_order`."""
    specs = [t if isinstance(t, TaskAnchorSpec) else TaskAnchorSpec.from_mapping(t) for t in tasks]
    r_dir = Path(repo_dir).expanduser().resolve() if repo_dir else None
    if r_dir is not None and r_dir.is_dir():
        structures, forward_graph = build_repository_import_graph(r_dir)
    else:
        structures, forward_graph = {}, {}

    reverse_graph: dict[str, list[str]] = {}
    for src_f, dsts in forward_graph.items():
        for dst_f in dsts:
            reverse_graph.setdefault(dst_f, []).append(src_f)

    py_index = _build_repo_python_module_index(r_dir)
    blast_by_task: dict[str, dict[str, Any]] = {}
    for idx_pos, sp in enumerate(specs):
        cid = sp.conversation_id or f"task-{idx_pos}"
        touches = resolve_task_touches(sp, repo_dir=r_dir, repo_slug=repo_slug, structures=structures, py_index=py_index)
        metrics = compute_blast_radius_metrics(touches, reverse_graph, structures)
        blast_by_task[cid] = {
            **metrics,
            "touches": {rel: t.to_dict() for rel, t in touches.items()},
        }

    landing = tuple(
        compute_landing_order(
            specs,
            repo_dir=r_dir,
            repo_slug=repo_slug,
            structures=structures,
            forward_graph=forward_graph,
        )
    )
    pairwise: list[PairOverlapResult] = []
    for i in range(len(specs)):
        for j in range(i + 1, len(specs)):
            pairwise.append(
                analyze_pair_overlap(
                    specs[i],
                    specs[j],
                    repo_dir=r_dir,
                    repo_slug=repo_slug,
                    structures=structures,
                    forward_graph=forward_graph,
                )
            )

    anchor_cid = primary_slot_id.strip() or (landing[0] if landing else "")
    slot2_safe: list[str] = []
    if anchor_cid:
        for cand_cid in landing:
            if _matches_cid(cand_cid, anchor_cid):
                continue
            pair_match = next(
                (
                    p
                    for p in pairwise
                    if (_matches_cid(p.task_a_id, anchor_cid) and _matches_cid(p.task_b_id, cand_cid))
                    or (_matches_cid(p.task_b_id, anchor_cid) and _matches_cid(p.task_a_id, cand_cid))
                ),
                None,
            )
            if pair_match is not None and pair_match.parallel_safe:
                slot2_safe.append(cand_cid)

    return BatchOverlapAnalysis(
        tasks_count=len(specs),
        landing_order=landing,
        blast_radius_by_task=blast_by_task,
        pairwise=tuple(pairwise),
        parallel_slot2_candidates=tuple(slot2_safe),
    )
