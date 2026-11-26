"""Typed view-models, constants, and facade registry for `dev_queue_tui`."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys
from typing import Any

_SCRIPT_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_clients in (_SCRIPT_PLUGIN_ROOT / "clients", _INSTALLED_PLUGIN_ROOT / "clients"):
    if _cand_clients.is_dir():
        _clients_str = str(_cand_clients)
        if _clients_str not in sys.path:
            sys.path.insert(0, _clients_str)
        break

for _cand_opt in (
    _SCRIPT_PLUGIN_ROOT / "skills" / "agy-code-remediation-pr" / "scripts",
    _INSTALLED_PLUGIN_ROOT / "skills" / "agy-code-remediation-pr" / "scripts",
):
    if (_cand_opt / "dev_flow_optimizer.py").is_file():
        _opt_str = str(_cand_opt)
        if _opt_str not in sys.path:
            sys.path.insert(0, _opt_str)
        break

try:
    import dev_common as _dev_common  # type: ignore[import-not-found]
except ImportError:
    _dev_common = None

try:
    import dev_flow_optimizer as _flow_opt  # type: ignore[import-not-found]
except ImportError:
    _flow_opt = None

_git_client: Any = None
_git_client_loaded = False
_queue_doctor: Any = None
_queue_doctor_loaded = False
_lock_store: Any = None
_lock_store_loaded = False
_queue_store: Any = None
_queue_store_loaded = False
_queue_planner: Any = None
_queue_planner_loaded = False
_orch_ctrl_mod: Any = None
_orch_ctrl_loaded = False

_FACADE_MODULES: list[Any] = []
_FACADE_NAMESPACES: list[dict[str, Any]] = []


def _register_facade_module(mod: Any) -> None:
    """Register loaded `dev_queue_tui` facade module instances so `mock.patch.object` propagates cleanly."""
    if mod is not None and mod not in _FACADE_MODULES:
        _FACADE_MODULES.append(mod)


def _register_facade_namespace(ns: dict[str, Any] | None) -> None:
    """Register a facade module's `globals()` dict so `spec_from_file_location` loaders without `sys.modules` propagate patches."""
    if ns is not None and ns not in _FACADE_NAMESPACES:
        _FACADE_NAMESPACES.append(ns)


def _facade_attr(name: str, fallback: Any) -> Any:
    """Resolve `name` via loaded `dev_queue_tui` facade(s) so test monkeypatches propagate across `tui_*` modules."""
    for ns in reversed(_FACADE_NAMESPACES):
        val = ns.get(name, fallback)
        if val is not fallback:
            return val
    cur_mod = sys.modules.get("dev_queue_tui")
    if cur_mod is not None:
        val = getattr(cur_mod, name, fallback)
        if val is not fallback:
            return val
    for mod in reversed(_FACADE_MODULES):
        val = getattr(mod, name, fallback)
        if val is not fallback:
            return val
    return fallback


def _get_git_client() -> Any:
    """Lazy-load `git_client` when available."""
    global _git_client, _git_client_loaded
    if not _git_client_loaded:
        _git_client_loaded = True
        try:
            import git_client as _gc  # type: ignore[import-not-found]

            _git_client = _gc
        except ImportError:
            _git_client = None
    return _git_client


def _get_queue_doctor() -> Any:
    """Lazy-load `dev_queue_doctor` when available."""
    global _queue_doctor, _queue_doctor_loaded
    if not _queue_doctor_loaded:
        _queue_doctor_loaded = True
        try:
            import dev_queue_doctor as _qd  # type: ignore[import-not-found]

            _queue_doctor = _qd
        except ImportError:
            _queue_doctor = None
    return _queue_doctor


def _get_lock_store() -> Any:
    """Lazy-load `lock_store` from `agy-code-remediation-pr` when available."""
    global _lock_store, _lock_store_loaded
    if not _lock_store_loaded:
        _lock_store_loaded = True
        try:
            import lock_store as _ls  # type: ignore[import-not-found]

            _lock_store = _ls
        except ImportError:
            _lock_store = None
    return _lock_store


def _get_queue_store() -> Any:
    """Lazy-load `queue_store` from `agy-code-remediation-pr` when available."""
    global _queue_store, _queue_store_loaded
    if not _queue_store_loaded:
        _queue_store_loaded = True
        try:
            import queue_store as _qs  # type: ignore[import-not-found]

            _queue_store = _qs
        except ImportError:
            _queue_store = None
    return _queue_store


def _get_queue_planner() -> Any:
    """Lazy-load `dev_queue_planner` from `agy-code-remediation-pr` when available."""
    global _queue_planner, _queue_planner_loaded
    if not _queue_planner_loaded:
        _queue_planner_loaded = True
        with contextlib.suppress(ImportError, OSError, SyntaxError, RuntimeError):
            import dev_queue_planner as _qp  # type: ignore[import-not-found]

            _queue_planner = _qp
    return _queue_planner


def _get_orch_control() -> Any:
    """Lazy-load `orchestration_control` from `agy-code-remediation-pr` when available."""
    global _orch_ctrl_mod, _orch_ctrl_loaded
    if not _orch_ctrl_loaded:
        _orch_ctrl_loaded = True
        with contextlib.suppress(ImportError, OSError, SyntaxError, RuntimeError):
            import orchestration_control as _oc  # type: ignore[import-not-found]

            _orch_ctrl_mod = _oc
    return _orch_ctrl_mod


ANSI_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[A-Za-z]")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_CREATED_PR_PATH_RE = re.compile(r"/(?:pull|pulls|pull-requests|merge_requests)/\d+(?:/|$)")
_BOARD_UPDATED_LINE_RE = re.compile(r"^> \*\*Updated\*\*: `[^`]*`", re.MULTILINE)
_RELOAD_ENV_FLAG = "_AGY_QUEUE_RELOADED"

ALT_SCREEN_ENTER = "\033[?1049h\033[?25l\033[H\033[2J"
ALT_SCREEN_EXIT = "\033[?25h\033[?1049l"

JSON_SCHEMA_VERSION = 1
IDLE_AMBER_THRESHOLD_SECONDS = 180
DEFAULT_MAX_REPO_SLOTS: int = int(getattr(_dev_common, "DEFAULT_MAX_REPO_SLOTS", 3) or 3)
MAX_REPO_SLOTS_CAP: int = int(getattr(_dev_common, "MAX_REPO_SLOTS_CAP", 4) or 4)

NAV_INTENT_FILENAME = "dev_queue_nav_intent.json"
NAV_INTENT_LOCK_FILENAME = ".dev_queue_nav_intent.lock"
DEFAULT_NAV_INTENT_TTL_SECONDS = 60
DEFAULT_NAV_INTENT_ACK_WAIT_SECONDS = 0.12

STATUS_ORDER: dict[str, int] = {
    "pr_open": 0,
    "needs_rebase_reverify": 1,
    "ready_awaiting_base_merge": 1,
    "needs_agent_semantic_fix": 1,
    "needs_agent_conflict_resolution": 1,
    "in_progress": 1,
    "pending": 2,
    "hitl_blocked": 3,
    "standby": 4,
    "merged": 5,
    "cleaned": 6,
    "cancelled": 7,
    "rejected": 8,
}

VALID_QUEUE_STATUSES: tuple[str, ...] = tuple(STATUS_ORDER.keys())
AGY_QUEUE_SUBCOMMANDS: tuple[str, ...] = (
    "watch",
    "doctor",
    "optimize",
    "compact",
    "plan",
    "sync",
    "list",
    "show",
    "locks",
    "set-status",
    "pause",
    "stop",
    "resume",
    "status",
)

INACTIVE_STATUSES: frozenset[str] = frozenset({"merged", "cleaned", "cancelled", "rejected"})

HUMAN_READY_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "PENDING_HUMAN_PR_VALIDATION",
        "PR_OPEN",
        "REVIEW_PASSED",
        "REVIEW_APPROVED",
        "NEEDS_HUMAN_REVIEW",
    }
)

AGENT_REVIEW_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "AWAITING_AGENT_REVIEW",
        "NEEDS_REVISION_ITERATING",
        "BRANCH_PUSHED_AWAITING_AGENT_REVIEW",
        "BRANCH_PUSHED",
        "READY_FOR_PR",
    }
)

REBASE_OR_AWAIT_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "READY_AWAITING_BASE_MERGE",
        "NEEDS_REBASE_REVERIFY",
        "NEEDS_AGENT_SEMANTIC_FIX",
        "NEEDS_AGENT_CONFLICT_RESOLUTION",
    }
)

UNREADY_PR_GATE_LOCK_STATUSES: frozenset[str] = frozenset(
    set(
        getattr(
            _dev_common,
            "UNREADY_PR_GATE_LOCK_STATUSES",
            frozenset(
                {
                    "AWAITING_AGENT_REVIEW",
                    "NEEDS_REVISION_ITERATING",
                    "ESCALATED_MAX_REVIEW_ITERATIONS",
                }
            ),
        )
    )
    | set(REBASE_OR_AWAIT_LOCK_STATUSES)
)

_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "pr_open": ("1;33", "PR_OPEN"),
    "ready_awaiting_base_merge": ("1;34", "READY_AWAITING_BASE_MERGE"),
    "needs_rebase_reverify": ("1;35", "NEEDS_REBASE_REVERIFY"),
    "needs_agent_semantic_fix": ("1;31", "NEEDS_AGENT_SEMANTIC_FIX"),
    "needs_agent_conflict_resolution": ("1;31", "NEEDS_AGENT_CONFLICT_RESOLUTION"),
    "in_progress": ("1;36", "IN_PROGRESS"),
    "pending": ("0;37", "PENDING"),
    "hitl_blocked": ("1;35", "HITL_BLOCK"),
    "standby": ("2;37", "STANDBY"),
    "merged": ("0;32", "MERGED"),
    "cleaned": ("2;32", "CLEANED"),
    "cancelled": ("2;31", "CANCELLED"),
    "rejected": ("0;31", "REJECTED"),
}


@dataclass(slots=True)
class RepoPillView:
    """Typed view-model for a managed repository pill in the header and Markdown board."""

    slug: str
    label: str
    locked: bool
    occupancy: str = ""
    base_tag: str = ""
    is_human_ready: bool = False
    is_stalled: bool = False
    is_needs_rebase: bool = False
    is_awaiting_base: bool = False
    is_agent_review: bool = False
    is_escalated: bool = False
    is_amber: bool = False
    idle_seconds: int = 0
    idle_label: str = ""
    s2_suffix_plain: str = ""
    s2_is_amber: bool = False
    s2_idle_label: str = ""
    s2_tag: str = ""
    secondary_slots: list[tuple[str, bool, str]] = field(default_factory=list)
    any_secondary_amber: bool = False
    link_url: str = ""
    lock_has_pr: bool = False
    lock_progress: dict[str, object] = field(default_factory=dict)
    slot2_progress: dict[str, object] | None = None
    slots_progress: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(slots=True)
class QueueRowView:
    """Typed view-model for a single Dev Queue table row, avoiding ANSI scraping in viewport filters."""

    idx: int
    priority: int
    status: str
    raw_status: str
    band: str
    display_id: str
    conversation_id: str
    sidebar_title: str
    title: str
    repos_str: str
    repos_md: str
    repo_slugs: list[str] = field(default_factory=list)
    merged_repos: set[str] = field(default_factory=set)
    progress: dict[str, object] | None = None
    linked_locks: list[dict[str, Any]] = field(default_factory=list)
    pr_url: str = ""
    is_human_pr: bool = False
    link_kind: str = ""
    git_state: str = ""
    target_slug: str = ""
    raw_line: str = ""
    search_blob: str = ""
    item: dict[str, Any] = field(default_factory=dict)

    def to_record_dict(self) -> dict[str, Any]:
        """Serialize into the legacy dictionary shape expected by `layout_out['row_records']`."""
        return {
            "idx": self.idx,
            "band": self.band,
            "raw_line": self.raw_line,
            "item": dict(self.item),
            "effective_status": self.status,
            "display_id": self.display_id,
            "sidebar_title": self.sidebar_title,
            "repos_str": self.repos_str,
            "progress": self.progress,
            "linked_locks": self.linked_locks,
            "pr_url": self.pr_url if self.is_human_pr else "",
            "search_blob": self.search_blob,
        }


@dataclass(slots=True)
class DashboardSnapshot:
    """Typed snapshot of loaded queue items, locks, repositories, and computed telemetry."""

    state_dir: Path
    queue_data: dict[str, Any]
    items: list[dict[str, Any]]
    locks: dict[str, dict[str, Any]]
    all_locks: dict[str, dict[str, Any]]
    repo_web_bases: dict[str, tuple[str, str]]
    repo_local_dirs: dict[str, Path]
    effective_items: list[dict[str, Any]]
    filtered: list[dict[str, Any]]
    row_eff_statuses: list[str]
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]]
    lock_progress_cache: dict[str, dict[str, object]]
    counts: dict[str, int]
    all_managed_slugs: set[str]
    managed_list: list[str]
    label_map: dict[str, str]
    display_ids: dict[str, str]
    ag_titles: dict[str, str]
    item_urls_by_cid: dict[str, list[str]]
    orch_full: str = "-"
    orch_live_txt: str = ""
    orch_live_code: str = ""
    opt_hint: str = ""
    has_active_work: bool = False
    orch_phase: dict[str, Any] = field(default_factory=dict)
    repo_pills: list[RepoPillView] = field(default_factory=list)
    stalled_alerts: list[tuple[str, str, str]] = field(default_factory=list)
    waiting_convs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RenderResult:
    """Typed output bundle returned by `render_dashboard()` and projected into `render()` / `layout_out`."""

    text: str
    row_links: dict[int, tuple[str, str]]
    hitl_convs: dict[int, tuple[str, str, str]]
    first_pr_link: tuple[str, str] | None
    state_dir: Path
    repo_local_dirs: dict[str, Path]
    header_lines: list[str] = field(default_factory=list)
    active_row_lines: list[str] = field(default_factory=list)
    middle_row_lines: list[str] = field(default_factory=list)
    active_rows: list[QueueRowView] = field(default_factory=list)
    middle_rows: list[QueueRowView] = field(default_factory=list)
    sticky_footer_lines: list[str] = field(default_factory=list)
    row_records: list[dict[str, Any]] = field(default_factory=list)
    row_views: list[QueueRowView] = field(default_factory=list)
    all_pr_links: list[tuple[str, str]] = field(default_factory=list)
    has_active_work: bool = False
    orchestration_paused: bool = False
    orchestration_phase: str = "active"
    active_locks: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_locks: dict[str, dict[str, Any]] = field(default_factory=dict)
    items_snapshot: list[dict[str, Any]] = field(default_factory=list)
    color_on: bool = False
    use_osc8: bool = False
    bar_w: int = 124
    waiting_convs: list[dict[str, Any]] = field(default_factory=list)

    def to_legacy_tuple(
        self,
    ) -> tuple[
        str,
        dict[int, tuple[str, str]],
        dict[int, tuple[str, str, str]],
        tuple[str, str] | None,
        Path,
        dict[str, Path],
    ]:
        """Return the 6-element tuple signature expected by legacy `render()` callers."""
        return (
            self.text,
            self.row_links,
            self.hitl_convs,
            self.first_pr_link,
            self.state_dir,
            self.repo_local_dirs,
        )

    def to_layout_dict(self) -> dict[str, Any]:
        """Return the `layout_out` dictionary populated by `render()`."""
        return {
            "header_lines": self.header_lines,
            "active_row_lines": self.active_row_lines,
            "middle_row_lines": self.middle_row_lines,
            "active_rows": self.active_rows,
            "middle_rows": self.middle_rows,
            "sticky_footer_lines": self.sticky_footer_lines,
            "row_records": self.row_records,
            "row_views": self.row_views,
            "hitl_count": len(self.hitl_convs),
            "waiting_count": len(self.waiting_convs),
            "waiting_convs": list(self.waiting_convs),
            "all_pr_links": self.all_pr_links,
            "has_active_work": self.has_active_work,
            "orchestration_paused": self.orchestration_paused,
            "orchestration_phase": self.orchestration_phase,
            "active_locks": dict(self.active_locks),
            "all_locks": dict(self.all_locks),
            "items_snapshot": list(self.items_snapshot),
            "repo_local_dirs": dict(self.repo_local_dirs),
            "color_on": self.color_on,
            "use_osc8": self.use_osc8,
            "bar_w": self.bar_w,
        }


@dataclass(slots=True)
class WatchState:
    """Mutable interactive state for `run_watch_loop()`."""

    state_dir: Path
    status_msg: str = ""
    status_until: float = 0.0
    awaiting_hitl_digit: bool = False
    filter_input_active: bool = False
    live_filter: str = ""
    overlay_mode: str = ""
    overlay_scroll: int = 0
    cached_opt_analysis: dict[str, Any] | None = None
    force_clear_screen: bool = False
    cursor_idx: int = 0
    scroll_offset: int = 0
    max_scroll_offset: int = 0
    compact_pending: bool = False
    iterations: int = 0
    terminal_cleaned: bool = False
    data_dirty: bool = True
    last_auto_sync_mono: float = 0.0
    local_dirs: dict[str, Path] = field(default_factory=dict)
    row_links: dict[int, tuple[str, str]] = field(default_factory=dict)
    hitl_convs: dict[int, tuple[str, str, str]] = field(default_factory=dict)
    first_pr: tuple[str, str] | None = None
    all_pr_links: list[tuple[str, str]] = field(default_factory=list)
    has_active_work: bool = False
    active_locks_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    layout_data: dict[str, Any] = field(default_factory=dict)
    body_lines: list[str] = field(default_factory=list)
    last_good_layout: dict[str, Any] = field(default_factory=dict)
    last_good_body_lines: list[str] = field(default_factory=list)
    last_state_fp: tuple[tuple[str, int, int, int], ...] = ()
    deadline: float = 0.0
