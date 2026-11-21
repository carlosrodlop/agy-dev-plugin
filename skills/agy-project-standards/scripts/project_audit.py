#!/usr/bin/env python3
"""Reusable 5-Language Static Engineering, Santa Lockdown, Zero-PII & Plugin Auditor (`project_audit.py`).

Validates any repository or Antigravity plugin against core engineering pillars in
100% pure Python 3 stdlib + `bash -n` (Santa Lockdown safe — zero Node/npm/ruff/shellcheck binaries required):
  - Python AST & safety (syntax, bare `except:`, `eval`/`exec`, unsafe `yaml.load`, mutable defaults, `shell=True`)
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
MAX_CSS_IMPORTANT_PER_FILE = 10
COPILOT_INSTRUCTIONS_REL_PATH = ".github/copilot-instructions.md"

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


def _check_python_ast(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Validate Python AST syntax, bare except, eval/exec, unsafe yaml.load, mutable defaults, and shell=True."""
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
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        warnings.append(
            AuditFinding("warning", "agy:missing-frontmatter", rel, 1, "Missing YAML frontmatter block.")
        )
        return
    if is_agent:
        fm_block = text[4 : text.index("\n---\n", 4)]
        for req_key in ("name:", "description:"):
            if req_key not in fm_block:
                errors.append(
                    AuditFinding("error", "agy:agent-frontmatter", rel, 1, f"Agent missing required '{req_key}' frontmatter field.")
                )
        if "tools:" not in fm_block:
            warnings.append(
                AuditFinding("warning", "agy:agent-tools-missing", rel, 1, "Agent should declare explicit 'tools:' frontmatter list.")
            )


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
                json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(AuditFinding("error", "json:invalid", rel, 0, f"Invalid JSON: {exc}"))
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
