#!/usr/bin/env python3
"""Reusable 5-Language Static Engineering, Santa Lockdown, Zero-PII & Plugin Auditor (`project_audit.py`).

Validates any repository or Antigravity plugin against core engineering pillars in
100% pure Python 3 stdlib + `bash -n` (Santa Lockdown safe — zero Node/npm/ruff/shellcheck binaries required):
  - Python AST & safety (syntax, `F821` undefined names, bare `except:`, `eval`/`exec`, unsafe `yaml.load`, mutable defaults, `shell=True`)
  - POSIX / Bash / Zsh shell (`bash -n`/`zsh -n`, strict mode on standalone scripts, unquoted `rm -rf $VAR`)
  - JavaScript (`eval`/`new Function`, `document.write`, `javascript:` URI, `debugger;`)
  - HTML5 (`html.parser` duplicate IDs, `javascript:` URI, `img` alt, `target=_blank` noopener, inline handlers, button type, external SRI)
  - CSS3 (balanced braces ignoring comments/strings, `!important` overuse)
  - Zero-PII & secret leak guardrails across all 5 languages + docs/configs
  - Santa Lockdown binary download & execution safety (pipes to shell/interpreters, `releases/download` + `chmod +x`, unapproved managers, `.pre-commit-config.yaml` binary languages)
  - Antigravity plugin/rule/skill/agent contracts (`plugin.json`, `<24,000` byte ceiling, YAML frontmatter, optional Copilot `<= 4,000` byte cap)
"""

from __future__ import annotations

import argparse
import ast
import builtins
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

MAX_MARKDOWN_INSTRUCTION_BYTES = 24000
MAX_COPILOT_INSTRUCTIONS_BYTES = 4000
MAX_SKILL_DESCRIPTION_CHARS = 1800
TEST_ARBITRARY_SLEEP_MIN_SECONDS = 0.25
ALLOW_SLEEP_COMMENT_RE = re.compile(r"#\s*allow-sleep:")
MAX_CSS_IMPORTANT_PER_FILE = 10
SKILL_HEAVY_BYTES_THRESHOLD = 12000
SKILL_HEAVY_BASH_BLOCKS_THRESHOLD = 4
FENCED_SHELL_BLOCK_RE = re.compile(r"(?m)^```(?:bash|sh|zsh|shell)\b")
SKILL_SCRIPT_OR_CLIENT_REF_RE = re.compile(
    r"(?:clients/[a-zA-Z0-9_.-]+\.py|skills/[a-zA-Z0-9_.-]+/scripts/[a-zA-Z0-9_.-]+\.(?:py|sh)|deterministic-wrapper\s*:\s*true)",
    re.IGNORECASE,
)
SIDECAR_PREGATE_SCRIPT_RE = re.compile(
    r"(?:^|[\s/\"'])(?:[a-zA-Z0-9_.-]*?(?:precheck|pregate|pre_gate|watchdog|gate_runner)[a-zA-Z0-9_.-]*\.(?:py|sh))(?:\b|[\s\"'])",
    re.IGNORECASE,
)
SIDECAR_INLINE_AGENTAPI_RE = re.compile(r"(?:^|[\s;&|\"'(])(?:[^\s;&|\"'(]*/)?agentapi(?:\b|[\s\"'])")
SHELL_INTERPRETER_BASENAMES = frozenset({"sh", "bash", "zsh", "dash"})
COPILOT_INSTRUCTIONS_REL_PATH = ".github/copilot-instructions.md"
REQUIREMENTS_MANIFEST_FILENAME = "requirements.json"
REQUIREMENTS_SCHEMA_VERSION = "1.0"
VALID_REQUIREMENT_MECHANISMS = frozenset(
    {
        "mcp_bridge",
        "python_client",
        "native_oauth_plugin",
        "system_binary",
    }
)
DOTENV_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "htmlcov",
        ".tox",
    }
)

CODE_AND_DOC_SUFFIXES = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".bats",
        ".js",
        ".mjs",
        ".html",
        ".htm",
        ".css",
        ".md",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".example",
        ".txt",
    }
)

SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh"})
JS_SUFFIXES = frozenset({".js", ".mjs"})
HTML_SUFFIXES = frozenset({".html", ".htm"})

LOCAL_USER_PATH_RE = re.compile(r"/Users/([a-zA-Z0-9._-]{2,})/")
ALLOWLISTED_PLACEHOLDER_USERS = frozenset(
    {"user", "username", "your_user", "alice", "bob", "runner", "developer", "tam"}
)

SECRET_TOKEN_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

SANTA_PIPE_RE = re.compile(
    r"(?:"
    r"\b(?:curl|wget)\s+[^\n|;]*\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh|python3?|perl|ruby)\b"
    r"|(?:^|[;&|]\s*)(?:mise\s+(?:install|use)|uv\s+(?:tool|python)\s+install|cargo\s+binstall)\b"
    r")",
    re.MULTILINE,
)

SANTA_BINARY_DOWNLOAD_CHAIN_RE = re.compile(
    r"\b(?:curl|wget)\b[^\n]*(?:releases/download/|(?:-o|-O)\s+\S+)[^\n]*(?:&&|;)[^\n]*\bchmod\s+(?:[a-z]*\+x|[0-7]*[1357][0-7]{0,2})\b"
)
SANTA_RELEASES_URL_RE = re.compile(r"\b(?:curl|wget)\b[^\n]*releases/download/")
SANTA_CHMOD_EXEC_RE = re.compile(r"\bchmod\s+(?:[a-z]*\+x|[0-7]*[1357][0-7]{0,2})\b")
SANTA_PRECOMMIT_BINARY_LANG_RE = re.compile(
    r"^\s*language:\s*(rust|golang|node|conda|docker|docker_image)\b",
    re.IGNORECASE,
)

SHELL_STRICT_MODE_RE = re.compile(r"\bset\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b")
SHELL_UNQUOTED_RM_VAR_RE = re.compile(
    r"\brm\s+-[a-zA-Z]*(?:rf|fr)[a-zA-Z]*\s+\$\{?[A-Za-z_][A-Za-z0-9_]*\}?(?:\s|/|;|$)"
)
SHELL_SOURCED_GUARD_RE = re.compile(r"\b(?:BASH_SOURCE|ZSH_EVAL_CONTEXT)\b")

JS_UNSAFE_EVAL_RE = re.compile(r"\b(?:eval\s*\(|new\s+Function\s*\()")
JS_DOCUMENT_WRITE_RE = re.compile(r"\bdocument\.write(?:ln)?\s*\(")
JS_JAVASCRIPT_URI_RE = re.compile(r"""["'`]\s*javascript:""", re.IGNORECASE)
JS_DEBUGGER_RE = re.compile(r"\bdebugger\s*(?:;|$)")

CSS_IMPORTANT_RE = re.compile(r"!\s*important\b", re.IGNORECASE)
HTML_INLINE_EVENT_ATTR_RE = re.compile(r"^on[a-z]{2,}$")
HTML_URI_ATTRS = frozenset({"href", "src", "action", "formaction"})
EXTERNAL_ORIGIN_RE = re.compile(r"^(?:https?:)?//(?!(?:localhost|127\.0\.0\.1)(?:[:/]|$))", re.IGNORECASE)


@dataclass
class AuditFinding:
    """Single finding produced by `audit_repository`."""

    severity: str  # "error" | "warning"
    rule_id: str
    file: str
    line: int = 0
    message: str = ""


@dataclass
class AuditReport:
    """Structured result summary for a repository audit."""

    repo_dir: str
    passed: bool
    files_scanned: int = 0
    errors: list[AuditFinding] = field(default_factory=list)
    warnings: list[AuditFinding] = field(default_factory=list)
    checks_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dict."""
        return {
            "repo_dir": self.repo_dir,
            "passed": self.passed,
            "files_scanned": self.files_scanned,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "checks_summary": self.checks_summary,
        }


def _iter_repo_files(repo_dir: Path) -> list[Path]:
    """Enumerate trackable repository files, respecting standard ignored directories."""
    git_bin = shutil.which("git")
    if git_bin and (repo_dir / ".git").exists():
        proc = subprocess.run(
            [git_bin, "-C", str(repo_dir), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            out: list[Path] = []
            for raw_rel in proc.stdout.split(b"\0"):
                rel = raw_rel.decode("utf-8", errors="ignore").strip()
                if not rel:
                    continue
                p = repo_dir / rel
                if p.is_file() and not any(part in SKIP_DIR_NAMES for part in Path(rel).parts):
                    out.append(p)
            if out:
                return sorted(out)

    collected: list[Path] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIR_NAMES]
        root_path = Path(root)
        for fname in sorted(files):
            fpath = root_path / fname
            if fpath.is_file():
                collected.append(fpath)
    return collected


def _strip_hash_comment_lines(text: str) -> str:
    """Blank out `#`-only comment lines while preserving 1-based line numbers."""
    out_lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            out_lines.append("")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


_JS_REGEX_PREFIX_CHARS = frozenset("([{,;:=!?&|^~+*%<>")
_JS_REGEX_PREFIX_KEYWORDS = frozenset(
    {"return", "case", "throw", "typeof", "void", "delete", "in", "instanceof", "new", "of", "await", "yield"}
)


def _prev_js_allows_regex(src: str, pos: int) -> bool:
    """Return True when a `/` at `pos` can start a JS regex literal rather than division."""
    j = pos - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    ch = src[j]
    if ch in _JS_REGEX_PREFIX_CHARS or ch == "-":
        return True
    if ch.isalnum() or ch in ("_", "$"):
        k = j
        while k >= 0 and (src[k].isalnum() or src[k] in ("_", "$")):
            k -= 1
        word = src[k + 1 : j + 1]
        return word in _JS_REGEX_PREFIX_KEYWORDS
    return False


def _strip_js_comments_and_strings(src: str) -> tuple[str, str]:
    """Return `(no_comments, no_comments_or_strings)` preserving exact newline positions.

    Handles line/block comments, single/double-quoted strings, JS regex literals (so
    patterns like `/'/g` never corrupt string state), and `${...}` template interpolations.
    """
    no_comments: list[str] = []
    no_strings: list[str] = []
    n = len(src)
    i = 0
    state = "normal"  # normal | single | double | template | regex | regex_class | line_comment | block_comment
    tpl_brace_stack: list[int] = []

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if state == "normal":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                no_comments.append("  ")
                no_strings.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                no_comments.append("  ")
                no_strings.append("  ")
                i += 2
                continue
            if ch == "/" and _prev_js_allows_regex(src, i):
                state = "regex"
                no_comments.append(ch)
                no_strings.append(" ")
                i += 1
                continue
            if ch == "'":
                state = "single"
                no_comments.append(ch)
                no_strings.append(" ")
                i += 1
                continue
            if ch == '"':
                state = "double"
                no_comments.append(ch)
                no_strings.append(" ")
                i += 1
                continue
            if ch == "`":
                state = "template"
                no_comments.append(ch)
                no_strings.append(" ")
                i += 1
                continue
            if ch == "{" and tpl_brace_stack:
                tpl_brace_stack[-1] += 1
            elif ch == "}" and tpl_brace_stack:
                if tpl_brace_stack[-1] == 0:
                    tpl_brace_stack.pop()
                    state = "template"
                    no_comments.append(ch)
                    no_strings.append(" ")
                    i += 1
                    continue
                tpl_brace_stack[-1] -= 1
            no_comments.append(ch)
            no_strings.append(ch)
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "normal"
                no_comments.append("\n")
                no_strings.append("\n")
            else:
                no_comments.append(" ")
                no_strings.append(" ")
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "normal"
                no_comments.append("  ")
                no_strings.append("  ")
                i += 2
                continue
            no_comments.append("\n" if ch == "\n" else " ")
            no_strings.append("\n" if ch == "\n" else " ")
            i += 1
        elif state in ("regex", "regex_class"):
            if ch == "\\" and nxt:
                no_comments.append(ch)
                no_comments.append(nxt)
                no_strings.append(" ")
                no_strings.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            no_comments.append(ch)
            no_strings.append("\n" if ch == "\n" else " ")
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
                no_comments.append(ch)
                no_comments.append(nxt)
                no_strings.append(" ")
                no_strings.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            if ch == "$" and nxt == "{":
                tpl_brace_stack.append(0)
                state = "normal"
                no_comments.append("${")
                no_strings.append("  ")
                i += 2
                continue
            no_comments.append(ch)
            no_strings.append("\n" if ch == "\n" else " ")
            if ch == "`":
                state = "normal"
            i += 1
        else:
            quote = "'" if state == "single" else '"'
            if ch == "\\" and nxt:
                no_comments.append(ch)
                no_comments.append(nxt)
                no_strings.append(" ")
                no_strings.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            no_comments.append(ch)
            no_strings.append("\n" if ch == "\n" else " ")
            if ch == quote or ch == "\n":
                state = "normal"
            i += 1

    return "".join(no_comments), "".join(no_strings)


def _strip_css_comments_and_strings(src: str) -> str:
    """Replace CSS comments and string literals with spaces while preserving line numbers."""
    out: list[str] = []
    n = len(src)
    i = 0
    state = "normal"  # normal | single | double | block_comment

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if state == "normal":
            if ch == "/" and nxt == "*":
                state = "block_comment"
                out.append("  ")
                i += 2
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
            out.append(ch)
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "normal"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:
            quote = "'" if state == "single" else '"'
            if ch == "\\" and nxt:
                out.append(" ")
                out.append("\n" if nxt == "\n" else " ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            if ch == quote:
                state = "normal"
            i += 1

    return "".join(out)


def _is_safe_yaml_loader_node(node: ast.AST) -> bool:
    """Return True when an AST expression references a `SafeLoader` or `CSafeLoader`."""
    if isinstance(node, ast.Name):
        return node.id in {"SafeLoader", "CSafeLoader", "BaseLoader", "CBaseLoader"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"SafeLoader", "CSafeLoader", "BaseLoader", "CBaseLoader"}
    return False


def _is_subprocess_call(func: ast.AST) -> bool:
    """Return True when an AST call targets `subprocess.*`."""
    subprocess_fns = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id == "subprocess" and func.attr in subprocess_fns
    if isinstance(func, ast.Name):
        return func.id == "Popen"
    return False


_PYTHON_BASE_BUILTINS: frozenset[str] = frozenset(dir(builtins)) | frozenset(
    {
        "__all__",
        "__annotations__",
        "__builtins__",
        "__cached__",
        "__class__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__path__",
        "__spec__",
    }
)


def _check_python_undefined_names(
    tree: ast.Module,
    rel: str,
    errors: list[AuditFinding],
) -> None:
    """Detect F821 undefined variable names in pure Python AST respecting lexical, class, comprehension, and annotation scopes."""
    comp_types = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    scope_types: tuple[type[ast.AST], ...] = (
        ast.Module,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
        *comp_types,
    )
    type_alias_cls = getattr(ast, "TypeAlias", None)
    if type_alias_cls is not None:
        scope_types = (*scope_types, type_alias_cls)

    global_defs: set[str] = set()
    for gnode in ast.walk(tree):
        if isinstance(gnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            declared_globals: set[str] = set()
            stored_names: set[str] = set()
            for sub in ast.walk(gnode):
                if isinstance(sub, ast.Global):
                    declared_globals.update(sub.names)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    stored_names.add(sub.id)
            global_defs.update(declared_globals & stored_names)

    reported: set[tuple[int, int, str]] = set()

    def _collect_comp_walrus_targets(comp_node: ast.AST, target_defs: set[str]) -> None:
        """Bind PEP 572 `:=` targets inside comprehensions into the enclosing non-comprehension scope."""
        walk_stack = list(ast.iter_child_nodes(comp_node))
        while walk_stack:
            cur = walk_stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(cur, ast.NamedExpr) and isinstance(cur.target, ast.Name):
                target_defs.add(cur.target.id)
            walk_stack.extend(ast.iter_child_nodes(cur))

    def _visit_scope(
        scope_node: ast.AST,
        enclosing_defs: set[str],
        annotation_defs: set[str],
    ) -> None:
        local_defs: set[str] = set()
        if isinstance(scope_node, ast.Module):
            local_defs.update(global_defs)

        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for arg in (
                *scope_node.args.posonlyargs,
                *scope_node.args.args,
                *scope_node.args.kwonlyargs,
            ):
                local_defs.add(arg.arg)
            if scope_node.args.vararg:
                local_defs.add(scope_node.args.vararg.arg)
            if scope_node.args.kwarg:
                local_defs.add(scope_node.args.kwarg.arg)

        type_param_names: set[str] = set()
        for tp in getattr(scope_node, "type_params", ()):
            tp_name = getattr(tp, "name", None)
            if tp_name:
                type_param_names.add(str(tp_name))
                local_defs.add(str(tp_name))

        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_defs.add(scope_node.name)

        has_star_import = False
        child_scopes: list[ast.AST] = []
        load_nodes: list[tuple[ast.Name, bool]] = []
        outer_load_nodes: list[tuple[ast.Name, set[str]]] = []
        ann_roots: set[int] = set()

        def _seed_outer_expr(expr: ast.AST, extra_allowed: set[str] | None = None) -> None:
            allowed_extra = extra_allowed or set()
            estk = [expr]
            while estk:
                enode = estk.pop()
                if isinstance(enode, scope_types):
                    if isinstance(enode, comp_types):
                        if not isinstance(scope_node, comp_types):
                            _collect_comp_walrus_targets(enode, local_defs)
                        if enode.generators:
                            _seed_outer_expr(enode.generators[0].iter, allowed_extra)
                    child_scopes.append(enode)
                    continue
                if isinstance(enode, ast.Name):
                    if isinstance(enode.ctx, ast.Store):
                        local_defs.add(enode.id)
                    elif isinstance(enode.ctx, ast.Load):
                        outer_load_nodes.append((enode, allowed_extra))
                estk.extend(reversed(list(ast.iter_child_nodes(enode))))

        skipped_child_ids: set[int] = set()
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in scope_node.decorator_list:
                skipped_child_ids.add(id(dec))
            for dflt in (*scope_node.args.defaults, *(d for d in scope_node.args.kw_defaults if d is not None)):
                skipped_child_ids.add(id(dflt))
            if scope_node.returns is not None:
                ann_roots.add(id(scope_node.returns))
            for arg in (
                *scope_node.args.posonlyargs,
                *scope_node.args.args,
                *scope_node.args.kwonlyargs,
                *([scope_node.args.vararg] if scope_node.args.vararg else []),
                *([scope_node.args.kwarg] if scope_node.args.kwarg else []),
            ):
                if arg.annotation is not None:
                    ann_roots.add(id(arg.annotation))
        elif isinstance(scope_node, ast.Lambda):
            for dflt in (*scope_node.args.defaults, *(d for d in scope_node.args.kw_defaults if d is not None)):
                skipped_child_ids.add(id(dflt))
        elif isinstance(scope_node, ast.ClassDef):
            for dec in scope_node.decorator_list:
                skipped_child_ids.add(id(dec))
            for base_expr in (*scope_node.bases, *(kw.value for kw in scope_node.keywords)):
                skipped_child_ids.add(id(base_expr))
                _seed_outer_expr(base_expr, type_param_names)
        elif isinstance(scope_node, comp_types) and scope_node.generators:
            skipped_child_ids.add(id(scope_node.generators[0].iter))

        stack: list[tuple[ast.AST, bool]] = []
        for ch in reversed(list(ast.iter_child_nodes(scope_node))):
            if id(ch) in skipped_child_ids:
                continue
            stack.append((ch, id(ch) in ann_roots))

        while stack:
            node, in_ann = stack.pop()
            if id(node) in skipped_child_ids:
                continue
            if isinstance(node, scope_types):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        _seed_outer_expr(dec)
                    for dflt in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
                        _seed_outer_expr(dflt)
                    local_defs.add(node.name)
                elif isinstance(node, ast.Lambda):
                    for dflt in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
                        _seed_outer_expr(dflt)
                elif isinstance(node, ast.ClassDef):
                    for dec in node.decorator_list:
                        _seed_outer_expr(dec)
                    local_defs.add(node.name)
                elif type_alias_cls is not None and isinstance(node, type_alias_cls):
                    if isinstance(node.name, ast.Name):
                        local_defs.add(node.name.id)
                elif isinstance(node, comp_types):
                    if not isinstance(scope_node, comp_types):
                        _collect_comp_walrus_targets(node, local_defs)
                    if node.generators:
                        _seed_outer_expr(node.generators[0].iter)
                child_scopes.append(node)
                continue

            if isinstance(node, ast.AnnAssign) and node.annotation is not None:
                ann_roots.add(id(node.annotation))

            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    local_defs.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    load_nodes.append((node, in_ann))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        has_star_import = True
                    else:
                        local_defs.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                local_defs.add(node.name)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and getattr(node, "name", None):
                local_defs.add(str(node.name))
            elif isinstance(node, ast.MatchMapping) and getattr(node, "rest", None):
                local_defs.add(str(node.rest))

            for ch in reversed(list(ast.iter_child_nodes(node))):
                if id(ch) in skipped_child_ids:
                    continue
                stack.append((ch, in_ann or (id(ch) in ann_roots)))

        if has_star_import:
            return

        visible = enclosing_defs | local_defs
        visible_ann = visible | annotation_defs
        parent_visible = (
            enclosing_defs
            if isinstance(scope_node, ast.ClassDef)
            else visible
        )

        for o_node, extra_allowed in outer_load_nodes:
            if o_node.id not in (parent_visible | extra_allowed):
                key = (o_node.lineno, o_node.col_offset, o_node.id)
                if key not in reported:
                    reported.add(key)

        for load_node, in_ann in load_nodes:
            allowed = visible_ann if in_ann else visible
            if load_node.id not in allowed:
                key = (load_node.lineno, load_node.col_offset, load_node.id)
                if key not in reported:
                    reported.add(key)

        if isinstance(scope_node, ast.ClassDef):
            child_enclosing = enclosing_defs | {scope_node.name} | type_param_names
            child_ann = visible_ann
        else:
            child_enclosing = visible
            child_ann = visible_ann

        for child_scope in child_scopes:
            _visit_scope(child_scope, child_enclosing, child_ann)

    _visit_scope(tree, set(_PYTHON_BASE_BUILTINS), set())
    for lineno, _col, undef_name in sorted(reported):
        errors.append(
            AuditFinding(
                "error",
                "py:undefined-name",
                rel,
                lineno,
                f"F821 undefined name '{undef_name}' (missing import or declaration).",
            )
        )


def _is_hermetic_test_file(rel: str) -> bool:
    """Return True when `rel` matches `tests/**/test_*.py`."""
    norm = rel.replace(os.sep, "/")
    while norm.startswith("./"):
        norm = norm[2:]
    parts = Path(norm).parts
    return len(parts) >= 2 and parts[0] == "tests" and parts[-1].startswith("test_") and parts[-1].endswith(".py")


def _check_python_test_arbitrary_sleep(
    tree: ast.AST,
    src: str,
    rel: str,
    warnings: list[AuditFinding],
) -> None:
    """Warn on numeric `time.sleep >= 0.25s` in `tests/**/test_*.py` unless suppressed with `# allow-sleep:`."""
    if not _is_hermetic_test_file(rel):
        return

    time_module_aliases: set[str] = set()
    sleep_fn_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    time_module_aliases.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "sleep":
                    sleep_fn_aliases.add(alias.asname or "sleep")

    if not time_module_aliases and not sleep_fn_aliases:
        return

    src_lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_sleep_call = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in time_module_aliases
        ) or (isinstance(node.func, ast.Name) and node.func.id in sleep_fn_aliases)
        if not is_sleep_call:
            continue

        arg_node: ast.AST | None = node.args[0] if node.args else None
        if arg_node is None:
            for kw in node.keywords:
                if kw.arg in {"secs", "seconds"}:
                    arg_node = kw.value
                    break
        if (
            not isinstance(arg_node, ast.Constant)
            or not isinstance(arg_node.value, (int, float))
            or isinstance(arg_node.value, bool)
        ):
            continue

        sleep_seconds = float(arg_node.value)
        if sleep_seconds < TEST_ARBITRARY_SLEEP_MIN_SECONDS:
            continue

        end_lineno = getattr(node, "end_lineno", None) or node.lineno
        if any(
            ALLOW_SLEEP_COMMENT_RE.search(src_lines[ln - 1])
            for ln in range(node.lineno, end_lineno + 1)
            if 1 <= ln <= len(src_lines)
        ):
            continue

        warnings.append(
            AuditFinding(
                "warning",
                "py:test-arbitrary-sleep",
                rel,
                node.lineno,
                (
                    f"Avoid arbitrary sleep({sleep_seconds:g}) >= {TEST_ARBITRARY_SLEEP_MIN_SECONDS}s in tests; "
                    "use condition-based polling or add '# allow-sleep: <reason>'."
                ),
            )
        )


def _check_python_ast(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Validate Python AST syntax, F821 undefined names, bare except, eval/exec, unsafe yaml.load, mutable defaults, and shell=True."""
    try:
        src = fpath.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(AuditFinding("error", "py:read-error", rel, 0, f"Cannot read Python file: {exc}"))
        return

    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError as exc:
        errors.append(
            AuditFinding("error", "py:syntax-error", rel, exc.lineno or 0, f"SyntaxError: {exc.msg}")
        )
        return

    _check_python_undefined_names(tree, rel, errors)
    _check_python_test_arbitrary_sleep(tree, src, rel, warnings)

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            errors.append(
                AuditFinding("error", "py:bare-except", rel, node.lineno, "Bare 'except:' clause is forbidden.")
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
            for d_node in all_defaults:
                if isinstance(d_node, (ast.List, ast.Dict, ast.Set)):
                    warnings.append(
                        AuditFinding(
                            "warning",
                            "py:mutable-default",
                            rel,
                            d_node.lineno,
                            f"Mutable default argument in function '{node.name}'; prefer None + internal copy.",
                        )
                    )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                errors.append(
                    AuditFinding(
                        "error",
                        "py:unsafe-eval-exec",
                        rel,
                        node.lineno,
                        f"Dynamic '{node.func.id}()' execution is forbidden; use safe parsing or explicit dispatch.",
                    )
                )
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "yaml"
            ):
                loader_kw = next((kw for kw in node.keywords if kw.arg == "Loader"), None)
                if loader_kw is None or not _is_safe_yaml_loader_node(loader_kw.value):
                    errors.append(
                        AuditFinding(
                            "error",
                            "py:unsafe-yaml-load",
                            rel,
                            node.lineno,
                            "Unsafe 'yaml.load()' without SafeLoader; use 'yaml.safe_load()' instead.",
                        )
                    )
            elif _is_subprocess_call(node.func):
                shell_kw = next((kw for kw in node.keywords if kw.arg == "shell"), None)
                if (
                    shell_kw is not None
                    and isinstance(shell_kw.value, ast.Constant)
                    and shell_kw.value.value is True
                ):
                    warnings.append(
                        AuditFinding(
                            "warning",
                            "py:subprocess-shell-true",
                            rel,
                            node.lineno,
                            "Avoid 'subprocess(..., shell=True)'; pass explicit argument lists instead.",
                        )
                    )


def _check_secrets_and_pii(
    fpath: Path,
    rel: str,
    self_resolved: Path,
    errors: list[AuditFinding],
    warnings: list[AuditFinding],
) -> None:
    """Scan text file across all 5 languages for leaked secrets, local paths, and Santa binary violations."""
    if fpath.resolve() == self_resolved:
        return
    if fpath.suffix.lower() not in CODE_AND_DOC_SUFFIXES and fpath.name not in ("Makefile", ".gitignore"):
        return
    try:
        text = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    lines = text.splitlines()
    for label, pattern in SECRET_TOKEN_REGEXES:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            errors.append(
                AuditFinding("error", "sec:secret-pattern", rel, line_no, f"Detected potential {label}.")
            )

    for idx, line in enumerate(lines, start=1):
        for match in LOCAL_USER_PATH_RE.finditer(line):
            user_seg = match.group(1).lower()
            if user_seg not in ALLOWLISTED_PLACEHOLDER_USERS and not user_seg.startswith("<"):
                errors.append(
                    AuditFinding(
                        "error",
                        "sec:local-user-path",
                        rel,
                        idx,
                        f"Hardcoded machine path '/Users/{match.group(1)}/' must be parameterized with $HOME or ~.",
                    )
                )

    suffix = fpath.suffix.lower()
    is_shell_or_makefile = suffix in SHELL_SUFFIXES or suffix == ".bats" or fpath.name == "Makefile"
    is_workflow = ".github/workflows/" in rel

    if is_shell_or_makefile or suffix in {".yml", ".yaml", ".json", ".py"}:
        scan_text = _strip_hash_comment_lines(text) if is_shell_or_makefile else text
        match_pipe = SANTA_PIPE_RE.search(scan_text)
        if match_pipe:
            line_no = scan_text.count("\n", 0, match_pipe.start()) + 1
            errors.append(
                AuditFinding(
                    "error",
                    "sec:santa-unapproved-pipe",
                    rel,
                    line_no,
                    "Detected Santa-blocked binary pipe or manager (curl|sh, curl|python, mise, uv tool install, or cargo binstall).",
                )
            )

    if is_shell_or_makefile and not is_workflow:
        active_shell = _strip_hash_comment_lines(text)
        chain_match = SANTA_BINARY_DOWNLOAD_CHAIN_RE.search(active_shell)
        if chain_match:
            line_no = active_shell.count("\n", 0, chain_match.start()) + 1
            errors.append(
                AuditFinding(
                    "error",
                    "sec:santa-binary-download",
                    rel,
                    line_no,
                    "Detected local executable binary download (curl/wget + chmod +x); use declarative asdf or OS-signed tools under Santa Lockdown.",
                )
            )
        else:
            rel_match = SANTA_RELEASES_URL_RE.search(active_shell)
            if rel_match and SANTA_CHMOD_EXEC_RE.search(active_shell):
                line_no = active_shell.count("\n", 0, rel_match.start()) + 1
                errors.append(
                    AuditFinding(
                        "error",
                        "sec:santa-binary-download",
                        rel,
                        line_no,
                        "Detected GitHub releases/download fetch paired with chmod +x in local script; blocked under Santa Lockdown.",
                    )
                )

    if fpath.name in {".pre-commit-config.yaml", ".pre-commit-config.yml"}:
        for idx, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            m_lang = SANTA_PRECOMMIT_BINARY_LANG_RE.match(line)
            if m_lang:
                warnings.append(
                    AuditFinding(
                        "warning",
                        "sec:santa-precommit-binary-lang",
                        rel,
                        idx,
                        f"pre-commit hook uses 'language: {m_lang.group(1)}', which fetches standalone compiled toolchains blocked under Santa Lockdown.",
                    )
                )


def _check_js_file(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Audit JavaScript files for XSS/code-injection sinks and stray debugger statements."""
    try:
        src = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    no_comments, no_strings = _strip_js_comments_and_strings(src)
    for idx, (line_no_str, line_no_cmt) in enumerate(
        zip(no_strings.splitlines(), no_comments.splitlines()), start=1
    ):
        if JS_UNSAFE_EVAL_RE.search(line_no_str):
            errors.append(
                AuditFinding(
                    "error",
                    "js:unsafe-eval",
                    rel,
                    idx,
                    "Dynamic JS evaluation ('eval()' or 'new Function()') is forbidden.",
                )
            )
        if JS_DOCUMENT_WRITE_RE.search(line_no_str):
            errors.append(
                AuditFinding(
                    "error",
                    "js:document-write",
                    rel,
                    idx,
                    "'document.write()' is forbidden; use safe DOM APIs.",
                )
            )
        if JS_JAVASCRIPT_URI_RE.search(line_no_cmt):
            errors.append(
                AuditFinding(
                    "error",
                    "js:javascript-uri",
                    rel,
                    idx,
                    "'javascript:' URI is forbidden; attach event listeners safely in JS.",
                )
            )
        if JS_DEBUGGER_RE.search(line_no_str):
            warnings.append(
                AuditFinding(
                    "warning",
                    "js:debugger-statement",
                    rel,
                    idx,
                    "Remove stray 'debugger' statement before committing.",
                )
            )


class _HtmlAuditParser(HTMLParser):
    """Stdlib HTML5 parser auditing security, duplicate IDs, SRI, and accessibility hygiene."""

    def __init__(self, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
        super().__init__(convert_charrefs=False)
        self.rel = rel
        self.errors = errors
        self.warnings = warnings
        self.seen_ids: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs)

    def _inspect_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line_no = self.getpos()[0]
        tag_lower = tag.lower()
        attr_map: dict[str, str | None] = {}

        for raw_name, raw_val in attrs:
            name = raw_name.lower()
            attr_map[name] = raw_val
            val_str = (raw_val or "").strip()

            if name in HTML_URI_ATTRS and val_str.lower().startswith("javascript:"):
                self.errors.append(
                    AuditFinding(
                        "error",
                        "html:javascript-uri",
                        self.rel,
                        line_no,
                        f"Forbidden 'javascript:' URI in <{tag_lower} {name}=...>.",
                    )
                )

            if HTML_INLINE_EVENT_ATTR_RE.match(name):
                self.warnings.append(
                    AuditFinding(
                        "warning",
                        "html:inline-event-handler",
                        self.rel,
                        line_no,
                        f"Avoid inline event handler '{name}' on <{tag_lower}>; bind listeners in JS for CSP hygiene.",
                    )
                )

        elem_id = (attr_map.get("id") or "").strip()
        if elem_id:
            if elem_id in self.seen_ids:
                self.errors.append(
                    AuditFinding(
                        "error",
                        "html:duplicate-id",
                        self.rel,
                        line_no,
                        f"Duplicate HTML id='{elem_id}' (first declared at line {self.seen_ids[elem_id]}).",
                    )
                )
            else:
                self.seen_ids[elem_id] = line_no

        if tag_lower == "img" and "alt" not in attr_map:
            self.warnings.append(
                AuditFinding(
                    "warning",
                    "html:img-missing-alt",
                    self.rel,
                    line_no,
                    "<img> element is missing an 'alt' attribute (use alt=\"\" for decorative images).",
                )
            )

        if tag_lower == "button" and not (attr_map.get("type") or "").strip():
            self.warnings.append(
                AuditFinding(
                    "warning",
                    "html:button-missing-type",
                    self.rel,
                    line_no,
                    "<button> should declare an explicit type ('button', 'submit', or 'reset').",
                )
            )

        if (attr_map.get("target") or "").strip().lower() == "_blank":
            rel_tokens = set((attr_map.get("rel") or "").lower().split())
            if "noopener" not in rel_tokens:
                self.warnings.append(
                    AuditFinding(
                        "warning",
                        "html:blank-target-noopener",
                        self.rel,
                        line_no,
                        f"<{tag_lower} target=\"_blank\"> should include rel=\"noopener noreferrer\".",
                    )
                )

        if tag_lower == "script":
            src_val = (attr_map.get("src") or "").strip()
            if src_val and EXTERNAL_ORIGIN_RE.match(src_val) and not (attr_map.get("integrity") or "").strip():
                self.warnings.append(
                    AuditFinding(
                        "warning",
                        "html:external-script-no-sri",
                        self.rel,
                        line_no,
                        "External <script src=...> should be hosted locally or pinned with an 'integrity' SRI hash.",
                    )
                )
        elif tag_lower == "link":
            rel_tokens = set((attr_map.get("rel") or "").lower().split())
            href_val = (attr_map.get("href") or "").strip()
            if (
                "stylesheet" in rel_tokens
                and href_val
                and EXTERNAL_ORIGIN_RE.match(href_val)
                and not (attr_map.get("integrity") or "").strip()
            ):
                self.warnings.append(
                    AuditFinding(
                        "warning",
                        "html:external-script-no-sri",
                        self.rel,
                        line_no,
                        "External stylesheet <link href=...> should be hosted locally or pinned with an 'integrity' SRI hash.",
                    )
                )


def _check_html_file(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Audit HTML5 files using stdlib `html.parser`."""
    try:
        src = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    parser = _HtmlAuditParser(rel, errors, warnings)
    parser.feed(src)
    parser.close()


def _check_css_file(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Audit CSS stylesheets for balanced braces and `!important` specificity overuse."""
    try:
        src = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    cleaned = _strip_css_comments_and_strings(src)
    depth = 0
    unmatched_close_line = 0
    last_open_line = 0
    for idx, line in enumerate(cleaned.splitlines(), start=1):
        for ch in line:
            if ch == "{":
                if depth == 0:
                    last_open_line = idx
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0 and unmatched_close_line == 0:
                    unmatched_close_line = idx

    if unmatched_close_line or depth != 0:
        err_line = unmatched_close_line or last_open_line
        errors.append(
            AuditFinding(
                "error",
                "css:unbalanced-braces",
                rel,
                err_line,
                "Unbalanced CSS '{' / '}' braces detected.",
            )
        )

    important_count = len(CSS_IMPORTANT_RE.findall(cleaned))
    if important_count > MAX_CSS_IMPORTANT_PER_FILE:
        warnings.append(
            AuditFinding(
                "warning",
                "css:important-overuse",
                rel,
                0,
                f"Stylesheet uses '!important' {important_count} times (> {MAX_CSS_IMPORTANT_PER_FILE}); reduce selector specificity debt.",
            )
        )


def _check_skill_determinism(
    fpath: Path,
    rel: str,
    text: str,
    size_bytes: int,
    warnings: list[AuditFinding],
) -> None:
    """Advise extracting deterministic scripts when a large SKILL.md orchestrates many shell blocks without code backing."""
    if size_bytes < SKILL_HEAVY_BYTES_THRESHOLD:
        return
    bash_blocks = len(FENCED_SHELL_BLOCK_RE.findall(text))
    if bash_blocks < SKILL_HEAVY_BASH_BLOCKS_THRESHOLD:
        return
    scripts_dir = fpath.parent / "scripts"
    has_local_script = scripts_dir.is_dir() and any(
        child.is_file() and child.suffix.lower() in {".py", ".sh", ".bash", ".zsh"}
        for child in scripts_dir.iterdir()
    )
    if has_local_script or SKILL_SCRIPT_OR_CLIENT_REF_RE.search(text):
        return
    warnings.append(
        AuditFinding(
            "warning",
            "agy:skill-heavy-no-script",
            rel,
            0,
            (
                f"Skill is {size_bytes} bytes with {bash_blocks} shell blocks and no scripts/*.py helper or "
                "client delegation; extract repeatable steps into a deterministic script (deterministic-first-engineering.md)."
            ),
        )
    )


def _resolve_sidecar_effective_command(tokens: list[str]) -> tuple[str, list[str]]:
    """Unwrap leading `env` flags and `KEY=VALUE` assignments to return `(executable, remaining_args)`."""
    if not tokens:
        return "", []
    idx = 0
    if os.path.basename(tokens[0]) == "env":
        idx = 1
        while idx < len(tokens):
            tok = tokens[idx]
            if tok == "--":
                idx += 1
                break
            if tok in {"-u", "-C", "--unset", "--chdir"}:
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            if "=" in tok and not tok.startswith((".", "/", "$")):
                idx += 1
                continue
            break
    if idx >= len(tokens):
        return "", []
    return tokens[idx], tokens[idx + 1 :]


def _check_sidecar_determinism(
    rel: str,
    parsed_json: Any,
    errors: list[AuditFinding],
) -> None:
    """Enforce valid executables and mandatory 0-LLM pre-gates on scheduled `sidecar.json` definitions."""
    if not isinstance(parsed_json, dict):
        return
    builtin = str(parsed_json.get("builtin") or "").strip().lower()
    if builtin == "schedule":
        raw_args = parsed_json.get("args")
        if not isinstance(raw_args, list) or len(raw_args) < 2:
            errors.append(
                AuditFinding(
                    "error",
                    "agy:sidecar-invalid-executable",
                    rel,
                    0,
                    "Scheduled sidecar must provide a cron schedule in args[0] and an executable in args[1].",
                )
            )
            return
        exec_token = str(raw_args[1] or "").strip()
        if not exec_token or exec_token.startswith("-"):
            errors.append(
                AuditFinding(
                    "error",
                    "agy:sidecar-invalid-executable",
                    rel,
                    0,
                    f"Scheduled sidecar args[1] must be a valid executable, got {exec_token!r} (never a bare flag).",
                )
            )
            return
        str_tokens = [str(tok or "").strip() for tok in raw_args[1:]]
        eff_exec, eff_rest = _resolve_sidecar_effective_command(str_tokens)
        if not eff_exec or eff_exec.startswith("-"):
            errors.append(
                AuditFinding(
                    "error",
                    "agy:sidecar-invalid-executable",
                    rel,
                    0,
                    f"Scheduled sidecar resolved an invalid executable {eff_exec!r} after env wrapper.",
                )
            )
            return
        eff_base = os.path.basename(eff_exec)
        if eff_base == "agentapi":
            errors.append(
                AuditFinding(
                    "error",
                    "agy:schedule-missing-pregate",
                    rel,
                    0,
                    (
                        "Scheduled sidecar invokes 'agentapi' directly without a deterministic 0-LLM pre-gate runner; "
                        "run 0-LLM Python directly or wrap with action_precheck_runner.py (deterministic-first-engineering.md)."
                    ),
                )
            )
            return
        if eff_base in SHELL_INTERPRETER_BASENAMES:
            for r_idx, r_tok in enumerate(eff_rest):
                if r_tok.startswith("-") and not r_tok.startswith("--") and "c" in r_tok[1:]:
                    inline_cmd = eff_rest[r_idx + 1] if r_idx + 1 < len(eff_rest) else ""
                    if SIDECAR_INLINE_AGENTAPI_RE.search(inline_cmd) and not SIDECAR_PREGATE_SCRIPT_RE.search(inline_cmd):
                        errors.append(
                            AuditFinding(
                                "error",
                                "agy:schedule-missing-pregate",
                                rel,
                                0,
                                (
                                    "Scheduled shell sidecar invokes 'agentapi' inline without a deterministic 0-LLM pre-gate "
                                    "script (deterministic-first-engineering.md)."
                                ),
                            )
                        )
                    break
    elif "command" in parsed_json:
        cmd_token = str(parsed_json.get("command") or "").strip()
        if not cmd_token or cmd_token.startswith("-"):
            errors.append(
                AuditFinding(
                    "error",
                    "agy:sidecar-invalid-executable",
                    rel,
                    0,
                    f"Sidecar 'command' must be a valid executable, got {cmd_token!r}.",
                )
            )


def _extract_frontmatter_field(fm_block: str, field_name: str) -> str:
    """Extract a single-line or YAML block-scalar field value from frontmatter without PyYAML."""
    lines = fm_block.splitlines()
    prefix = f"{field_name}:"
    for idx, line in enumerate(lines):
        stripped_leading = line.lstrip()
        if line != stripped_leading or not line.startswith(prefix):
            continue
        raw_val = line[len(prefix) :].strip()
        if raw_val in {">", ">-", "|", "|-", ""}:
            block_lines: list[str] = []
            for cont in lines[idx + 1 :]:
                if cont == "":
                    block_lines.append("")
                    continue
                if cont[0] in {" ", "\t"}:
                    block_lines.append(cont.strip())
                    continue
                break
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
            if raw_val.startswith("|"):
                return "\n".join(block_lines).strip()
            return " ".join(part for part in block_lines if part).strip()
        if (raw_val.startswith('"') and raw_val.endswith('"')) or (
            raw_val.startswith("'") and raw_val.endswith("'")
        ):
            if len(raw_val) >= 2:
                return raw_val[1:-1].strip()
        return raw_val
    return ""


def _check_markdown_limits(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Verify Antigravity rules/skills/agents (24 KB + YAML frontmatter) and optional Copilot 4 KB hygiene cap."""
    normalized_rel = rel.replace(os.sep, "/")
    if normalized_rel.startswith("./"):
        normalized_rel = normalized_rel[2:]
    if normalized_rel == COPILOT_INSTRUCTIONS_REL_PATH:
        size_bytes = fpath.stat().st_size
        if size_bytes > MAX_COPILOT_INSTRUCTIONS_BYTES:
            warnings.append(
                AuditFinding(
                    "warning",
                    "agy:copilot-instructions-bloat",
                    COPILOT_INSTRUCTIONS_REL_PATH,
                    0,
                    (
                        f"Copilot instructions file is {size_bytes} bytes "
                        f"(> {MAX_COPILOT_INSTRUCTIONS_BYTES} byte hygiene cap); keep concise to limit AI credit usage."
                    ),
                )
            )
        return

    rel_parts = Path(normalized_rel).parts[:1]
    is_rule = rel_parts == ("rules",) and fpath.suffix == ".md"
    is_agent = rel_parts == ("agents",) and fpath.suffix == ".md"
    is_skill = fpath.name == "SKILL.md"
    if not (is_rule or is_skill or is_agent):
        return
    size_bytes = fpath.stat().st_size
    if size_bytes >= MAX_MARKDOWN_INSTRUCTION_BYTES:
        errors.append(
            AuditFinding(
                "error",
                "agy:markdown-size-limit",
                rel,
                0,
                f"Instruction file is {size_bytes} bytes (>= {MAX_MARKDOWN_INSTRUCTION_BYTES} byte AGY limit).",
            )
        )
    text = fpath.read_text(encoding="utf-8", errors="ignore")
    if is_skill:
        _check_skill_determinism(fpath, rel, text, size_bytes, warnings)
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        warnings.append(
            AuditFinding("warning", "agy:missing-frontmatter", rel, 1, "Missing YAML frontmatter block.")
        )
        return
    fm_block = text[4 : text.index("\n---\n", 4)]
    if is_skill:
        desc_text = _extract_frontmatter_field(fm_block, "description")
        if len(desc_text) > MAX_SKILL_DESCRIPTION_CHARS:
            warnings.append(
                AuditFinding(
                    "warning",
                    "agy:skill-description-bloat",
                    rel,
                    1,
                    (
                        f"SKILL.md description is {len(desc_text)} chars (> {MAX_SKILL_DESCRIPTION_CHARS} char cap); "
                        "keep frontmatter descriptions trigger-only ('Use when...')."
                    ),
                )
            )
    if is_agent:
        for req_key in ("name:", "description:"):
            if req_key not in fm_block:
                errors.append(
                    AuditFinding("error", "agy:agent-frontmatter", rel, 1, f"Agent missing required '{req_key}' frontmatter field.")
                )
        if "tools:" not in fm_block:
            warnings.append(
                AuditFinding("warning", "agy:agent-tools-missing", rel, 1, "Agent should declare explicit 'tools:' frontmatter list.")
            )


def _parse_dotenv_example_keys(dotenv_path: Path) -> set[str]:
    """Extract declared environment variable names from `.env.example` (ignoring `#` comment lines)."""
    try:
        text = dotenv_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    keys: set[str] = set()
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = DOTENV_ASSIGNMENT_RE.match(stripped)
        if m:
            keys.add(m.group(1))
    return keys


def _is_nonempty_str_list(val: Any, *, allow_empty: bool = False) -> bool:
    """Return True when `val` is a `list` of non-empty `str` items."""
    if not isinstance(val, list):
        return False
    if not allow_empty and len(val) == 0:
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in val)


def validate_plugin_requirements(repo_dir: Path | str) -> list[AuditFinding]:
    """Validate `requirements.json` against the 4 Plugin Requirements as Code invariants.

    Invariants enforced whenever `<repo_dir>/requirements.json` exists:
      1. Schema compliance (`schema_version == "1.0"`, valid `mechanism` in
         `mcp_bridge | python_client | native_oauth_plugin | system_binary`, required fields).
      2. 100% coverage of `clients/*_client.py` and `mcp_config.json` servers.
      3. 100% of `required_by_skills` exist under `skills/*/SKILL.md`.
      4. 100% of declared `env_vars` exist in `.env.example` (when `.env.example` exists).
    """
    resolved_repo = Path(repo_dir).expanduser().resolve()
    req_path = resolved_repo / REQUIREMENTS_MANIFEST_FILENAME
    if not req_path.is_file():
        return []

    findings: list[AuditFinding] = []
    try:
        rdata = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        findings.append(
            AuditFinding("error", "req:invalid-json", REQUIREMENTS_MANIFEST_FILENAME, 0, f"Invalid JSON: {exc}")
        )
        return findings

    if not isinstance(rdata, dict):
        findings.append(
            AuditFinding(
                "error",
                "req:schema",
                REQUIREMENTS_MANIFEST_FILENAME,
                0,
                "requirements.json root must be a JSON object.",
            )
        )
        return findings

    schema_version = rdata.get("schema_version")
    if not isinstance(schema_version, str) or schema_version != REQUIREMENTS_SCHEMA_VERSION:
        findings.append(
            AuditFinding(
                "error",
                "req:schema",
                REQUIREMENTS_MANIFEST_FILENAME,
                0,
                f"requirements.json must declare schema_version == '{REQUIREMENTS_SCHEMA_VERSION}' (got {schema_version!r}).",
            )
        )

    plugin_id = rdata.get("plugin_id")
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        findings.append(
            AuditFinding(
                "error",
                "req:schema",
                REQUIREMENTS_MANIFEST_FILENAME,
                0,
                "requirements.json must declare a non-empty string 'plugin_id'.",
            )
        )
        plugin_id_clean = ""
    else:
        plugin_id_clean = plugin_id.strip()
        plugin_manifest = resolved_repo / "plugin.json"
        if plugin_manifest.is_file():
            try:
                pdata = json.loads(plugin_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pdata = None
            if isinstance(pdata, dict):
                pname = str(pdata.get("name") or "").strip()
                if pname and plugin_id_clean != pname:
                    findings.append(
                        AuditFinding(
                            "error",
                            "req:schema",
                            REQUIREMENTS_MANIFEST_FILENAME,
                            0,
                            f"requirements.json plugin_id '{plugin_id_clean}' must match plugin.json name '{pname}'.",
                        )
                    )

    raw_items = rdata.get("requirements")
    if not isinstance(raw_items, list):
        findings.append(
            AuditFinding(
                "error",
                "req:schema",
                REQUIREMENTS_MANIFEST_FILENAME,
                0,
                "requirements.json must contain a 'requirements' array.",
            )
        )
        return findings

    seen_ids: set[str] = set()
    covered_client_scripts: set[str] = set()
    covered_mcp_servers: set[str] = set()

    skills_dir = resolved_repo / "skills"
    existing_skills = (
        {p.parent.name for p in skills_dir.glob("*/SKILL.md") if p.is_file()}
        if skills_dir.is_dir()
        else set()
    )

    dotenv_example_path = resolved_repo / ".env.example"
    has_dotenv_example = dotenv_example_path.is_file()
    declared_dotenv_keys = _parse_dotenv_example_keys(dotenv_example_path) if has_dotenv_example else set()

    mcp_config_path = resolved_repo / "mcp_config.json"
    repo_mcp_servers: set[str] = set()
    if mcp_config_path.is_file():
        try:
            mcp_data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mcp_data = None
        if isinstance(mcp_data, dict) and isinstance(mcp_data.get("mcpServers"), dict):
            repo_mcp_servers = {str(k).strip() for k in mcp_data["mcpServers"] if str(k).strip()}

    for idx, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"requirements[{idx}] must be a JSON object.",
                )
            )
            continue

        req_id_raw = entry.get("id")
        if not isinstance(req_id_raw, str) or not req_id_raw.strip():
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"requirements[{idx}] must declare a non-empty string 'id'.",
                )
            )
            req_label = f"requirements[{idx}]"
            req_id = ""
        else:
            req_id = req_id_raw.strip()
            req_label = req_id
            if req_id in seen_ids:
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Duplicate requirement id '{req_id}' in requirements.json.",
                    )
                )
            seen_ids.add(req_id)

        if "name" in entry and (not isinstance(entry["name"], str) or not entry["name"].strip()):
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"Requirement '{req_label}' field 'name' must be a non-empty string.",
                )
            )

        mechanism = entry.get("mechanism")
        if not isinstance(mechanism, str) or mechanism not in VALID_REQUIREMENT_MECHANISMS:
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    (
                        f"Requirement '{req_label}' has invalid mechanism {mechanism!r}; "
                        f"expected one of {sorted(VALID_REQUIREMENT_MECHANISMS)}."
                    ),
                )
            )
            mechanism = ""

        provider_plugin = entry.get("provider_plugin_id")
        if not isinstance(provider_plugin, str) or not provider_plugin.strip():
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"Requirement '{req_label}' must declare a non-empty string 'provider_plugin_id'.",
                )
            )
            provider_clean = ""
        else:
            provider_clean = provider_plugin.strip()

        req_by_plugins = entry.get("required_by_plugins")
        if not _is_nonempty_str_list(req_by_plugins, allow_empty=False):
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"Requirement '{req_label}' must declare a non-empty list of strings 'required_by_plugins'.",
                )
            )

        req_by_skills = entry.get("required_by_skills")
        if not _is_nonempty_str_list(req_by_skills, allow_empty=True):
            findings.append(
                AuditFinding(
                    "error",
                    "req:schema",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"Requirement '{req_label}' must declare 'required_by_skills' as a list of non-empty skill names.",
                )
            )
        else:
            for skill_name in req_by_skills:
                clean_skill = skill_name.strip()
                if clean_skill not in existing_skills:
                    findings.append(
                        AuditFinding(
                            "error",
                            "req:unknown-skill",
                            REQUIREMENTS_MANIFEST_FILENAME,
                            0,
                            (
                                f"Requirement '{req_label}' references unknown skill '{clean_skill}' "
                                f"(missing skills/{clean_skill}/SKILL.md)."
                            ),
                        )
                    )

        if "env_vars" in entry:
            env_vars_val = entry["env_vars"]
            if not _is_nonempty_str_list(env_vars_val, allow_empty=True):
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Requirement '{req_label}' field 'env_vars' must be a list of non-empty variable names.",
                    )
                )
            elif has_dotenv_example:
                for env_var in env_vars_val:
                    clean_env = env_var.strip()
                    if clean_env not in declared_dotenv_keys:
                        findings.append(
                            AuditFinding(
                                "error",
                                "req:missing-env-example",
                                REQUIREMENTS_MANIFEST_FILENAME,
                                0,
                                f"Requirement '{req_label}' declares env_var '{clean_env}' missing from .env.example.",
                            )
                        )

        is_local_provider = bool(plugin_id_clean and provider_clean == plugin_id_clean)
        client_script_raw = entry.get("client_script")
        norm_client_script = ""
        if client_script_raw is not None:
            if not isinstance(client_script_raw, str) or not client_script_raw.strip():
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Requirement '{req_label}' field 'client_script' must be a non-empty string.",
                    )
                )
            else:
                norm_client_script = client_script_raw.strip().replace("\\", "/")
                while norm_client_script.startswith("./"):
                    norm_client_script = norm_client_script[2:]

        if mechanism == "python_client":
            if is_local_provider and not norm_client_script:
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Local python_client requirement '{req_label}' must declare 'client_script' (e.g. 'clients/{req_id or 'example_client'}.py').",
                    )
                )
            elif norm_client_script:
                covered_client_scripts.add(norm_client_script)
                if is_local_provider and not norm_client_script.startswith(("$HOME/", "~/")):
                    if not (resolved_repo / norm_client_script).is_file():
                        findings.append(
                            AuditFinding(
                                "error",
                                "req:missing-client-file",
                                REQUIREMENTS_MANIFEST_FILENAME,
                                0,
                                f"Requirement '{req_label}' client_script '{norm_client_script}' does not exist on disk.",
                            )
                        )
        elif mechanism == "mcp_bridge":
            if norm_client_script:
                covered_client_scripts.add(norm_client_script)
                if is_local_provider and not norm_client_script.startswith(("$HOME/", "~/")):
                    if not (resolved_repo / norm_client_script).is_file():
                        findings.append(
                            AuditFinding(
                                "error",
                                "req:missing-client-file",
                                REQUIREMENTS_MANIFEST_FILENAME,
                                0,
                                f"Requirement '{req_label}' client_script '{norm_client_script}' does not exist on disk.",
                            )
                        )
            entry_servers: list[str] = []
            if "mcp_servers" in entry:
                if not _is_nonempty_str_list(entry["mcp_servers"], allow_empty=False):
                    findings.append(
                        AuditFinding(
                            "error",
                            "req:schema",
                            REQUIREMENTS_MANIFEST_FILENAME,
                            0,
                            f"Requirement '{req_label}' field 'mcp_servers' must be a non-empty list of server names.",
                        )
                    )
                else:
                    entry_servers.extend(s.strip() for s in entry["mcp_servers"])
            if "mcp_server" in entry:
                if not isinstance(entry["mcp_server"], str) or not entry["mcp_server"].strip():
                    findings.append(
                        AuditFinding(
                            "error",
                            "req:schema",
                            REQUIREMENTS_MANIFEST_FILENAME,
                            0,
                            f"Requirement '{req_label}' field 'mcp_server' must be a non-empty string.",
                        )
                    )
                else:
                    entry_servers.append(entry["mcp_server"].strip())
            if not entry_servers and req_id in repo_mcp_servers:
                entry_servers.append(req_id)
            if is_local_provider and not entry_servers:
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Local mcp_bridge requirement '{req_label}' must declare 'mcp_servers' (or 'mcp_server').",
                    )
                )
            for srv in entry_servers:
                covered_mcp_servers.add(srv)
                if is_local_provider and mcp_config_path.is_file() and srv not in repo_mcp_servers:
                    findings.append(
                        AuditFinding(
                            "error",
                            "req:unknown-mcp-server",
                            REQUIREMENTS_MANIFEST_FILENAME,
                            0,
                            f"Requirement '{req_label}' declares MCP server '{srv}' missing from mcp_config.json.",
                        )
                    )
        elif mechanism == "system_binary":
            has_bin_str = "binary" in entry
            has_bin_list = "binaries" in entry
            if has_bin_str and (not isinstance(entry["binary"], str) or not entry["binary"].strip()):
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Requirement '{req_label}' field 'binary' must be a non-empty string.",
                    )
                )
            if has_bin_list and not _is_nonempty_str_list(entry["binaries"], allow_empty=False):
                findings.append(
                    AuditFinding(
                        "error",
                        "req:schema",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Requirement '{req_label}' field 'binaries' must be a non-empty list of strings.",
                    )
                )

    clients_dir = resolved_repo / "clients"
    if clients_dir.is_dir():
        for client_file in sorted(clients_dir.glob("*_client.py")):
            if not client_file.is_file():
                continue
            rel_client = f"clients/{client_file.name}"
            if rel_client not in covered_client_scripts:
                findings.append(
                    AuditFinding(
                        "error",
                        "req:missing-client-coverage",
                        REQUIREMENTS_MANIFEST_FILENAME,
                        0,
                        f"Python client '{rel_client}' is not declared in requirements.json.",
                    )
                )

    for srv_name in sorted(repo_mcp_servers):
        if srv_name not in covered_mcp_servers:
            findings.append(
                AuditFinding(
                    "error",
                    "req:missing-mcp-coverage",
                    REQUIREMENTS_MANIFEST_FILENAME,
                    0,
                    f"MCP server '{srv_name}' in mcp_config.json is not declared with mechanism='mcp_bridge' in requirements.json.",
                )
            )

    return findings


def _check_hooks_and_plugin_manifest(
    resolved_repo: Path,
    errors: list[AuditFinding],
    warnings: list[AuditFinding],
) -> None:
    """Validate Antigravity `plugin.json` manifest bounds and flat/named `hooks.json` schema."""
    plugin_path = resolved_repo / "plugin.json"
    if plugin_path.is_file():
        try:
            pdata = json.loads(plugin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pdata = None
        if isinstance(pdata, dict):
            if "requirements" in pdata:
                errors.append(
                    AuditFinding(
                        "error",
                        "agy:plugin-json-unknown-key",
                        "plugin.json",
                        0,
                        "Do not add top-level 'requirements' to plugin.json (forbidden by Antigravity loader); use sibling requirements.json.",
                    )
                )
            prompts = pdata.get("suggestedPrompts")
            if isinstance(prompts, list) and len(prompts) > 3:
                warnings.append(
                    AuditFinding(
                        "warning",
                        "agy:suggested-prompts-limit",
                        "plugin.json",
                        0,
                        f"suggestedPrompts has {len(prompts)} entries; Antigravity recommends at most 3.",
                    )
                )

    hooks_path = resolved_repo / "hooks.json"
    if not hooks_path.is_file():
        return
    try:
        hdata = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(hdata, dict):
        errors.append(AuditFinding("error", "agy:hooks-schema", "hooks.json", 0, "hooks.json root must be a JSON object."))
        return
    if "hooks" in hdata and isinstance(hdata["hooks"], dict):
        errors.append(
            AuditFinding(
                "error",
                "agy:hooks-invalid-root-wrapper",
                "hooks.json",
                0,
                "hooks.json must map top-level named hook keys directly (remove deprecated outer 'hooks' wrapper).",
            )
        )
        return

    flat_events = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
    grouped_events = {"PreToolUse", "PostToolUse"}
    allowed_keys = flat_events | grouped_events | {"enabled"}
    for hook_name, hook_spec in hdata.items():
        if not isinstance(hook_spec, dict):
            errors.append(AuditFinding("error", "agy:hooks-schema", "hooks.json", 0, f"Hook '{hook_name}' must be an object."))
            continue
        for ev_key, ev_val in hook_spec.items():
            if ev_key not in allowed_keys:
                warnings.append(AuditFinding("warning", "agy:hooks-unknown-key", "hooks.json", 0, f"Unknown hook field '{ev_key}' under '{hook_name}'."))
            elif ev_key in flat_events:
                if not isinstance(ev_val, list) or any(not isinstance(it, dict) or "command" not in it for it in ev_val):
                    errors.append(
                        AuditFinding(
                            "error",
                            "agy:hooks-flat-event-schema",
                            "hooks.json",
                            0,
                            f"{hook_name}.{ev_key} must be a flat list of handler objects containing 'command' (no matcher wrapper).",
                        )
                    )
            elif ev_key in grouped_events:
                if not isinstance(ev_val, list) or any(
                    not isinstance(it, dict) or not isinstance(it.get("hooks"), list) for it in ev_val
                ):
                    errors.append(
                        AuditFinding(
                            "error",
                            "agy:hooks-grouped-event-schema",
                            "hooks.json",
                            0,
                            f"{hook_name}.{ev_key} must contain matcher groups with 'hooks' arrays.",
                        )
                    )


def _is_sourced_only_shell_library(text: str) -> bool:
    """Return True when a shell file is designed solely to be sourced (never put top-level `set -e` in it)."""
    lines = text.splitlines()
    first_line = lines[0].strip() if lines else ""
    if not first_line.startswith("#!"):
        return True
    header_window = "\n".join(lines[:15]).lower()
    if "sourced-only" in header_window or "shell-library" in header_window:
        return True
    active = _strip_hash_comment_lines(text)
    if SHELL_SOURCED_GUARD_RE.search(active) and not SHELL_STRICT_MODE_RE.search(active):
        return True
    return False


def _check_shell_and_workflows(
    fpath: Path,
    rel: str,
    errors: list[AuditFinding],
    warnings: list[AuditFinding],
) -> None:
    """Validate POSIX/Bash/Zsh syntax, strict mode, destructive quoting, and GitHub workflow runner pinning."""
    suffix = fpath.suffix.lower()
    if suffix in SHELL_SUFFIXES:
        shell_cmd = "zsh" if suffix == ".zsh" else "bash"
        shell_bin = shutil.which(shell_cmd)
        if shell_bin:
            proc = subprocess.run([shell_bin, "-n", str(fpath)], capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode != 0:
                errors.append(
                    AuditFinding(
                        "error",
                        "sh:syntax-error",
                        rel,
                        0,
                        f"{shell_cmd} -n failed: {(proc.stderr or proc.stdout).strip()}",
                    )
                )

        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return

        active = _strip_hash_comment_lines(text)
        if not _is_sourced_only_shell_library(text) and not SHELL_STRICT_MODE_RE.search(active):
            warnings.append(
                AuditFinding(
                    "warning",
                    "sh:missing-strict-mode",
                    rel,
                    1,
                    "Standalone shell script is missing 'set -euo pipefail' (or 'set -e').",
                )
            )
        for idx, line in enumerate(active.splitlines(), start=1):
            if SHELL_UNQUOTED_RM_VAR_RE.search(line):
                warnings.append(
                    AuditFinding(
                        "warning",
                        "sh:unquoted-rm-var",
                        rel,
                        idx,
                        "Unquoted variable in 'rm -rf'; double-quote and guard with ${VAR:?}.",
                    )
                )
    elif ".github/workflows/" in rel and suffix in {".yml", ".yaml"}:
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        if "runs-on: ubuntu-latest" in text:
            warnings.append(
                AuditFinding(
                    "warning",
                    "ci:unpinned-runner",
                    rel,
                    0,
                    "Pin 'runs-on: ubuntu-24.04' instead of mutable 'ubuntu-latest'.",
                )
            )


def audit_repository(repo_dir: Path | str, *, strict: bool = False) -> AuditReport:
    """Run all 5-language static engineering, Santa Lockdown, Zero-PII, and plugin contract checks."""
    resolved_repo = Path(repo_dir).expanduser().resolve()
    if not resolved_repo.is_dir():
        return AuditReport(
            repo_dir=str(resolved_repo),
            passed=False,
            errors=[AuditFinding("error", "repo:not-found", ".", 0, f"Directory not found: {resolved_repo}")],
        )

    files = _iter_repo_files(resolved_repo)
    self_resolved = Path(__file__).resolve()
    errors: list[AuditFinding] = []
    warnings: list[AuditFinding] = []
    checked_copilot_instructions = False

    for fpath in files:
        rel = str(fpath.relative_to(resolved_repo)).replace(os.sep, "/")
        suffix = fpath.suffix.lower()
        if rel == COPILOT_INSTRUCTIONS_REL_PATH:
            checked_copilot_instructions = True
        if suffix == ".py":
            _check_python_ast(fpath, rel, errors, warnings)
        elif suffix == ".json":
            try:
                parsed_json = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(AuditFinding("error", "json:invalid", rel, 0, f"Invalid JSON: {exc}"))
            else:
                if fpath.name == "sidecar.json":
                    _check_sidecar_determinism(rel, parsed_json, errors)
        elif suffix in JS_SUFFIXES:
            _check_js_file(fpath, rel, errors, warnings)
        elif suffix in HTML_SUFFIXES:
            _check_html_file(fpath, rel, errors, warnings)
        elif suffix == ".css":
            _check_css_file(fpath, rel, errors, warnings)

        _check_secrets_and_pii(fpath, rel, self_resolved, errors, warnings)
        _check_markdown_limits(fpath, rel, errors, warnings)
        _check_shell_and_workflows(fpath, rel, errors, warnings)

    copilot_path = resolved_repo / ".github" / "copilot-instructions.md"
    if not checked_copilot_instructions and copilot_path.is_file():
        _check_markdown_limits(copilot_path, COPILOT_INSTRUCTIONS_REL_PATH, errors, warnings)

    if (resolved_repo / "plugin.json").is_file():
        for expected in (".gitignore", ".tool-versions", "pyproject.toml", "install.sh"):
            if not (resolved_repo / expected).is_file():
                warnings.append(
                    AuditFinding("warning", "plugin:missing-standard-file", expected, 0, f"Recommended baseline file missing: {expected}")
                )
        _check_hooks_and_plugin_manifest(resolved_repo, errors, warnings)

    has_requirements_manifest = (resolved_repo / REQUIREMENTS_MANIFEST_FILENAME).is_file()
    if has_requirements_manifest:
        already_invalid_req_json = any(
            e.rule_id == "json:invalid" and e.file == REQUIREMENTS_MANIFEST_FILENAME for e in errors
        )
        for req_finding in validate_plugin_requirements(resolved_repo):
            if already_invalid_req_json and req_finding.rule_id == "req:invalid-json":
                continue
            if req_finding.severity == "warning":
                warnings.append(req_finding)
            else:
                errors.append(req_finding)

    passed = len(errors) == 0 and (len(warnings) == 0 if strict else True)
    return AuditReport(
        repo_dir=str(resolved_repo),
        passed=passed,
        files_scanned=len(files),
        errors=errors,
        warnings=warnings,
        checks_summary={
            "python_files": sum(1 for f in files if f.suffix.lower() == ".py"),
            "shell_files": sum(1 for f in files if f.suffix.lower() in SHELL_SUFFIXES),
            "js_files": sum(1 for f in files if f.suffix.lower() in JS_SUFFIXES),
            "html_files": sum(1 for f in files if f.suffix.lower() in HTML_SUFFIXES),
            "css_files": sum(1 for f in files if f.suffix.lower() == ".css"),
            "markdown_files": sum(1 for f in files if f.suffix.lower() == ".md"),
            "plugin_requirements_checked": has_requirements_manifest,
            "strict_mode": strict,
        },
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `project_audit.py`."""
    parser = argparse.ArgumentParser(
        prog="project_audit",
        description="5-language static engineering standards, Santa Lockdown, Zero-PII, and Antigravity plugin auditor.",
    )
    parser.add_argument("--repo", default=".", help="Path to target repository (default: current directory).")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    args = parser.parse_args(argv)

    report = audit_repository(args.repo, strict=args.strict)
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        status_label = "PASS" if report.passed else "FAIL"
        print(
            f"[project_audit] {status_label}: scanned {report.files_scanned} files in {report.repo_dir} "
            f"({len(report.errors)} errors, {len(report.warnings)} warnings)"
        )
        for item in (*report.errors, *report.warnings):
            loc = f"{item.file}:{item.line}" if item.line else item.file
            print(f"  [{item.severity.upper()}] {item.rule_id} @ {loc}: {item.message}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
