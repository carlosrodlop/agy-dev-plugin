#!/usr/bin/env python3
"""Pure-stdlib Python AST Complexity & God-File Auditor (`complexity_audit.py`).

Santa-Lockdown safe, Unix portable (macOS & Linux), Zero-PII static analyzer
measuring per-file and per-function LOC, max control-flow nesting depth,
McCabe cyclomatic complexity, and god-file thresholds.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any

DEFAULT_MAX_FILE_LOC = 600
DEFAULT_MAX_FUNC_LOC = 80
DEFAULT_MAX_COMPLEXITY = 15
DEFAULT_MAX_NESTING = 5
DEFAULT_MAX_FILE_FUNCTIONS = 30

_LOCAL_HOME_PATH_RE = re.compile(r"^/?(?:Users|home)/(?!testuser\b|test\b|Shared\b)[^/]+/")

DEFAULT_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".tox",
        "build",
        "dist",
    }
)


@dataclass(frozen=True)
class ComplexityThresholds:
    """Configurable thresholds for complexity and god-file detection."""

    max_file_loc: int = DEFAULT_MAX_FILE_LOC
    max_func_loc: int = DEFAULT_MAX_FUNC_LOC
    max_complexity: int = DEFAULT_MAX_COMPLEXITY
    max_nesting: int = DEFAULT_MAX_NESTING
    max_file_functions: int = DEFAULT_MAX_FILE_FUNCTIONS


@dataclass(frozen=True)
class FunctionMetrics:
    """AST complexity metrics for a single Python function or method."""

    name: str
    lineno: int
    end_lineno: int
    loc: int
    cyclomatic_complexity: int
    max_nesting_depth: int


@dataclass(frozen=True)
class ComplexityViolation:
    """Structured threshold violation emitted by `complexity_audit.py`."""

    rule_id: str
    severity: str
    file: str
    function: str
    line: int
    value: int
    threshold: int
    message: str


@dataclass
class FileMetrics:
    """Aggregated AST complexity metrics for a single Python module."""

    path: str
    loc: int
    code_loc: int
    function_count: int
    class_count: int
    max_function_loc: int
    max_nesting_depth: int
    max_cyclomatic_complexity: int
    avg_cyclomatic_complexity: float
    is_god_file: bool
    functions: list[FunctionMetrics] = field(default_factory=list)
    violations: list[ComplexityViolation] = field(default_factory=list)


def _sanitize_rel_path(rel_path: str) -> str:
    """Ensure emitted relative paths never leak `/Users/<user>/` prefixes."""
    cleaned = str(rel_path or "").replace(os.sep, "/").lstrip("./")
    return _LOCAL_HOME_PATH_RE.sub("$HOME/", cleaned)


def _compute_code_loc(source: str) -> int:
    """Count non-empty, non-comment physical lines."""
    return sum(
        1
        for raw_line in source.splitlines()
        if raw_line.strip() and not raw_line.strip().startswith("#")
    )


def _count_cyclomatic_in_node(node: ast.AST) -> int:
    """Compute McCabe cyclomatic complexity within a single function scope."""
    complexity = 1
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    branch_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)
    match_case_cls = getattr(ast, "match_case", None)
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(cur, branch_nodes):
            complexity += 1
        elif isinstance(cur, ast.BoolOp):
            complexity += max(0, len(cur.values) - 1)
        elif isinstance(cur, ast.comprehension):
            complexity += 1 + len(cur.ifs)
        elif match_case_cls is not None and isinstance(cur, match_case_cls):
            complexity += 1
        stack.extend(ast.iter_child_nodes(cur))
    return complexity


def _if_nesting_depth(stmt: ast.If, depth: int) -> int:
    """Compute max nesting depth for an `if / elif / else` chain without inflating `elif`."""
    max_depth = depth + 1
    cur_if: ast.If | None = stmt
    while cur_if is not None:
        branch_depth = depth + 1
        max_depth = max(max_depth, _compute_max_nesting_in_stmts(cur_if.body, branch_depth))
        if len(cur_if.orelse) == 1 and isinstance(cur_if.orelse[0], ast.If):
            cur_if = cur_if.orelse[0]
        else:
            if cur_if.orelse:
                max_depth = max(max_depth, _compute_max_nesting_in_stmts(cur_if.orelse, branch_depth))
            cur_if = None
    return max_depth


def _stmt_child_blocks(stmt: ast.stmt) -> list[Sequence[ast.stmt]]:
    """Return nested statement blocks for control-flow statements other than `ast.If`."""
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        return [blk for blk in (stmt.body, stmt.orelse) if blk]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body] if stmt.body else []
    try_star_cls = getattr(ast, "TryStar", None)
    if isinstance(stmt, ast.Try) or (try_star_cls is not None and isinstance(stmt, try_star_cls)):
        blocks: list[Sequence[ast.stmt]] = [stmt.body]
        blocks.extend(handler.body for handler in stmt.handlers)
        blocks.extend(blk for blk in (stmt.orelse, stmt.finalbody) if blk)
        return [blk for blk in blocks if blk]
    match_cls = getattr(ast, "Match", None)
    if match_cls is not None and isinstance(stmt, match_cls):
        return [case.body for case in stmt.cases if case.body]
    return []


def _compute_max_nesting_in_stmts(stmts: Sequence[ast.stmt], depth: int = 0) -> int:
    """Compute maximum control-flow nesting depth across a statement list."""
    max_depth = depth
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(stmt, ast.If):
            max_depth = max(max_depth, _if_nesting_depth(stmt, depth))
            continue
        child_blocks = _stmt_child_blocks(stmt)
        if child_blocks:
            next_depth = depth + 1
            max_depth = max(max_depth, next_depth)
            for blk in child_blocks:
                max_depth = max(max_depth, _compute_max_nesting_in_stmts(blk, next_depth))
    return max_depth


def _collect_functions_and_classes(tree: ast.Module) -> tuple[list[FunctionMetrics], int]:
    """Walk AST module in source order and collect per-function metrics + class count."""
    functions: list[FunctionMetrics] = []
    class_count = 0

    def _visit(node: ast.AST, prefix: tuple[str, ...]) -> None:
        nonlocal class_count
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                class_count += 1
                _visit(child, (*prefix, child.name))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual_name = ".".join((*prefix, child.name))
                start = int(getattr(child, "lineno", 1) or 1)
                end = int(getattr(child, "end_lineno", start) or start)
                functions.append(
                    FunctionMetrics(
                        name=qual_name,
                        lineno=start,
                        end_lineno=end,
                        loc=max(1, end - start + 1),
                        cyclomatic_complexity=_count_cyclomatic_in_node(child),
                        max_nesting_depth=_compute_max_nesting_in_stmts(child.body, 0),
                    )
                )
                _visit(child, (*prefix, child.name))
            else:
                _visit(child, prefix)

    _visit(tree, ())
    return functions, class_count


def _evaluate_file_violations(
    *,
    rel_path: str,
    total_loc: int,
    functions: Sequence[FunctionMetrics],
    cfg: ComplexityThresholds,
) -> tuple[bool, list[ComplexityViolation]]:
    """Evaluate god-file and per-function threshold violations."""
    violations: list[ComplexityViolation] = []
    is_god_file = False
    if total_loc > cfg.max_file_loc:
        is_god_file = True
        violations.append(
            ComplexityViolation(
                rule_id="complexity:god-file-loc",
                severity="warning",
                file=rel_path,
                function="",
                line=1,
                value=total_loc,
                threshold=cfg.max_file_loc,
                message=f"God-file candidate: {total_loc} LOC exceeds max_file_loc={cfg.max_file_loc}.",
            )
        )
    if len(functions) > cfg.max_file_functions:
        is_god_file = True
        violations.append(
            ComplexityViolation(
                rule_id="complexity:god-file-functions",
                severity="warning",
                file=rel_path,
                function="",
                line=1,
                value=len(functions),
                threshold=cfg.max_file_functions,
                message=f"God-file candidate: {len(functions)} functions exceeds max_file_functions={cfg.max_file_functions}.",
            )
        )

    fn_checks = (
        ("complexity:function-loc", "loc", cfg.max_func_loc, "is {val} LOC (> {thr})."),
        ("complexity:cyclomatic", "cyclomatic_complexity", cfg.max_complexity, "has cyclomatic complexity {val} (> {thr})."),
        ("complexity:nesting-depth", "max_nesting_depth", cfg.max_nesting, "has max nesting depth {val} (> {thr})."),
    )
    for fn in functions:
        for rule_id, attr, thr, tmpl in fn_checks:
            val = int(getattr(fn, attr))
            if val > thr:
                violations.append(
                    ComplexityViolation(
                        rule_id=rule_id,
                        severity="warning",
                        file=rel_path,
                        function=fn.name,
                        line=fn.lineno,
                        value=val,
                        threshold=thr,
                        message=f"Function `{fn.name}` {tmpl.format(val=val, thr=thr)}",
                    )
                )
    return is_god_file, violations


def analyze_python_source(
    source: str,
    *,
    rel_path: str,
    thresholds: ComplexityThresholds | None = None,
) -> FileMetrics:
    """Analyze Python source text using stdlib `ast` and return `FileMetrics`."""
    cfg = thresholds or ComplexityThresholds()
    clean_rel = _sanitize_rel_path(rel_path)
    tree = ast.parse(source, filename=clean_rel)
    total_loc = len(source.splitlines())
    code_loc = _compute_code_loc(source)
    functions, class_count = _collect_functions_and_classes(tree)
    is_god_file, violations = _evaluate_file_violations(
        rel_path=clean_rel,
        total_loc=total_loc,
        functions=functions,
        cfg=cfg,
    )
    avg_cyc = round(sum(f.cyclomatic_complexity for f in functions) / len(functions), 2) if functions else 0.0
    return FileMetrics(
        path=clean_rel,
        loc=total_loc,
        code_loc=code_loc,
        function_count=len(functions),
        class_count=class_count,
        max_function_loc=max((f.loc for f in functions), default=0),
        max_nesting_depth=max((f.max_nesting_depth for f in functions), default=0),
        max_cyclomatic_complexity=max((f.cyclomatic_complexity for f in functions), default=0),
        avg_cyclomatic_complexity=avg_cyc,
        is_god_file=is_god_file,
        functions=sorted(functions, key=lambda f: (-f.cyclomatic_complexity, -f.loc, f.lineno)),
        violations=violations,
    )


def _format_rel_path(fpath: Path, anchor: Path | None) -> str:
    """Format a safe relative POSIX path anchored to `anchor` (falling back to filename)."""
    if anchor is not None:
        try:
            return _sanitize_rel_path(str(fpath.relative_to(anchor)))
        except ValueError:
            pass
    return fpath.name


def _discover_python_files(
    targets: Sequence[Path | str],
    *,
    base_dir: Path | None = None,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Collect sorted `(abs_path, rel_posix_path)` pairs and missing-target errors."""
    discovered: dict[Path, str] = {}
    missing: list[str] = []
    anchor = base_dir.resolve() if base_dir is not None else None

    for raw in targets:
        cand = Path(raw).expanduser()
        if not cand.is_absolute() and base_dir is not None:
            cand = base_dir / cand
        resolved = cand.resolve()
        if not resolved.exists():
            missing.append(_sanitize_rel_path(str(raw)))
            continue
        if resolved.is_file():
            if resolved.suffix.lower() == ".py":
                discovered[resolved] = _format_rel_path(resolved, anchor)
            continue
        dir_anchor = anchor or resolved
        for dirpath, dirnames, filenames in os.walk(resolved):
            dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs and not d.startswith("."))
            for fname in sorted(filenames):
                if fname.endswith(".py"):
                    fpath = Path(dirpath) / fname
                    discovered[fpath] = _format_rel_path(fpath, dir_anchor)

    return sorted(discovered.items(), key=lambda pair: pair[1]), missing


def audit_paths(
    targets: Sequence[Path | str],
    *,
    base_dir: Path | str | None = None,
    thresholds: ComplexityThresholds | None = None,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
) -> dict[str, Any]:
    """Run AST complexity and god-file audit across Python files/directories."""
    cfg = thresholds or ComplexityThresholds()
    resolved_base = Path(base_dir).expanduser().resolve() if base_dir is not None else None
    eff_targets = list(targets) if targets else [resolved_base or Path(".")]
    files_list, missing = _discover_python_files(
        eff_targets,
        base_dir=resolved_base,
        exclude_dirs=exclude_dirs,
    )

    file_reports: list[FileMetrics] = []
    parse_errors: list[dict[str, Any]] = []
    all_violations: list[ComplexityViolation] = []

    for abs_path, rel_path in files_list:
        try:
            src = abs_path.read_text(encoding="utf-8")
            fm = analyze_python_source(src, rel_path=rel_path, thresholds=cfg)
        except SyntaxError as exc:
            parse_errors.append({"file": rel_path, "line": int(exc.lineno or 0), "error": f"SyntaxError: {exc.msg}"})
            continue
        except (OSError, UnicodeDecodeError) as exc:
            parse_errors.append({"file": rel_path, "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        file_reports.append(fm)
        all_violations.extend(fm.violations)

    god_files = [
        {
            "path": fm.path,
            "loc": fm.loc,
            "code_loc": fm.code_loc,
            "function_count": fm.function_count,
            "max_function_loc": fm.max_function_loc,
            "max_cyclomatic_complexity": fm.max_cyclomatic_complexity,
            "max_nesting_depth": fm.max_nesting_depth,
        }
        for fm in sorted(file_reports, key=lambda x: (-x.loc, x.path))
        if fm.is_god_file
    ]

    passed = not missing and not parse_errors and not all_violations
    return {
        "status": "error" if (missing or parse_errors) else ("ok" if passed else "violations"),
        "passed": passed,
        "thresholds": asdict(cfg),
        "summary": {
            "files_scanned": len(file_reports),
            "functions_scanned": sum(fm.function_count for fm in file_reports),
            "god_files_count": len(god_files),
            "violations_count": len(all_violations),
            "parse_errors_count": len(parse_errors),
            "missing_targets_count": len(missing),
            "max_file_loc_observed": max((fm.loc for fm in file_reports), default=0),
            "max_func_loc_observed": max((fm.max_function_loc for fm in file_reports), default=0),
            "max_complexity_observed": max((fm.max_cyclomatic_complexity for fm in file_reports), default=0),
            "max_nesting_observed": max((fm.max_nesting_depth for fm in file_reports), default=0),
        },
        "missing_targets": missing,
        "parse_errors": parse_errors,
        "god_files": god_files,
        "violations": [asdict(v) for v in all_violations],
        "files": [
            {
                "path": fm.path,
                "loc": fm.loc,
                "code_loc": fm.code_loc,
                "function_count": fm.function_count,
                "class_count": fm.class_count,
                "max_function_loc": fm.max_function_loc,
                "max_nesting_depth": fm.max_nesting_depth,
                "max_cyclomatic_complexity": fm.max_cyclomatic_complexity,
                "avg_cyclomatic_complexity": fm.avg_cyclomatic_complexity,
                "is_god_file": fm.is_god_file,
                "functions": [asdict(fn) for fn in fm.functions],
            }
            for fm in sorted(file_reports, key=lambda x: (-x.loc, -x.max_cyclomatic_complexity, x.path))
        ],
    }


def _render_text_report(report: dict[str, Any]) -> str:
    """Render concise human-readable summary of AST complexity metrics."""
    s = report["summary"]
    label = "PASS" if report["passed"] else ("ERROR" if report["status"] == "error" else "VIOLATIONS")
    lines = [
        (
            f"[complexity_audit] {label}: {s['files_scanned']} file(s), {s['functions_scanned']} function(s), "
            f"{s['god_files_count']} god-file(s), {s['violations_count']} violation(s) "
            f"(max_loc={s['max_file_loc_observed']}, max_func_loc={s['max_func_loc_observed']}, "
            f"max_cyc={s['max_complexity_observed']}, max_nest={s['max_nesting_observed']})"
        )
    ]
    for err in report.get("missing_targets") or []:
        lines.append(f"  [ERROR] missing target: {err}")
    for err in report.get("parse_errors") or []:
        lines.append(f"  [ERROR] {err.get('file')}:{err.get('line', 0)} {err.get('error')}")
    for v in report.get("violations") or []:
        loc = f"{v['file']}:{v['line']}" if v.get("line") else v["file"]
        lines.append(f"  [{v['severity'].upper()}] {v['rule_id']} @ {loc}: {v['message']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for `complexity_audit.py`."""
    parser = argparse.ArgumentParser(
        prog="complexity_audit",
        description="Pure-stdlib Python AST complexity, nesting depth, and god-file auditor.",
    )
    parser.add_argument("paths", nargs="*", help="Target Python files or directories (defaults to `--repo` or `.`).")
    parser.add_argument("--repo", default="", help="Optional repository root directory.")
    parser.add_argument("--max-file-loc", type=int, default=DEFAULT_MAX_FILE_LOC, help=f"God-file LOC threshold (default: {DEFAULT_MAX_FILE_LOC}).")
    parser.add_argument("--max-file-functions", type=int, default=DEFAULT_MAX_FILE_FUNCTIONS, help=f"God-file function count threshold (default: {DEFAULT_MAX_FILE_FUNCTIONS}).")
    parser.add_argument("--max-func-loc", type=int, default=DEFAULT_MAX_FUNC_LOC, help=f"Max LOC per function (default: {DEFAULT_MAX_FUNC_LOC}).")
    parser.add_argument("--max-complexity", type=int, default=DEFAULT_MAX_COMPLEXITY, help=f"Max McCabe cyclomatic complexity per function (default: {DEFAULT_MAX_COMPLEXITY}).")
    parser.add_argument("--max-nesting", type=int, default=DEFAULT_MAX_NESTING, help=f"Max control-flow nesting depth per function (default: {DEFAULT_MAX_NESTING}).")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON report.")
    parser.add_argument("--warn-only", dest="warn_only", action="store_true", help="Exit 0 even when threshold violations are detected (triage mode).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `complexity_audit.py`."""
    args = build_parser().parse_args(argv)
    base_dir = Path(args.repo).expanduser().resolve() if str(args.repo or "").strip() else None
    targets: list[Path | str] = list(args.paths) if args.paths else ([base_dir] if base_dir is not None else ["."])
    thresholds = ComplexityThresholds(
        max_file_loc=max(1, int(args.max_file_loc)),
        max_func_loc=max(1, int(args.max_func_loc)),
        max_complexity=max(1, int(args.max_complexity)),
        max_nesting=max(1, int(args.max_nesting)),
        max_file_functions=max(1, int(args.max_file_functions)),
    )
    report = audit_paths(targets, base_dir=base_dir, thresholds=thresholds)
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(_render_text_report(report))

    if report["missing_targets"] or report["parse_errors"]:
        return 2
    if not report["passed"] and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
