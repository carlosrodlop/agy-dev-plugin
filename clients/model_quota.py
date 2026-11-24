"""Deterministic Model Routing, Preventive Low-Quota Eco & 429 Cooldown Fallback (`model_quota.py`).

Pure Python 3 stdlib module (Santa Lockdown compliant, Unix portable, Zero-PII) providing:
  1. Configurable unlimited-first, preventive low-quota Eco (`<= 25%`), and cross-pool
     fallback chains via `~/.env` (`AGY_DEV_UNLIMITED_MODEL_IDS`, `AGY_DEV_LOW_QUOTA_THRESHOLD_PCT`,
     `AGY_DEV_MODEL_FALLBACK_ORDER`, `AGY_DEV_DEV_ECO_FALLBACK_ORDER`,
     `AGY_DEV_REVIEWER_MODEL_FALLBACK_ORDER`).
  2. Atomic `0600` + `fcntl.flock` persistence in `<state_dir>/model_quota_cooldowns.json`
     storing both reactive `429 RESOURCE_EXHAUSTED` cooldowns (auto-pruned at reset) and
     TTL-bounded live `swarm(action="models", all=true)` quota snapshots.
  3. Transcript tail `429 RESOURCE_EXHAUSTED` / reset-timer extraction (`detect_quota_exhaustion_in_lines`).
  4. Role-aware `select_next_optimal_model()` supporting `flash`-first developer Round 1,
     automatic Round 2+ `pro` escalation, preventive low-quota Eco cross-pool routing, and
     all-models-exhausted wake suppression.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import contextlib
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import time
from typing import Any

MODEL_QUOTA_COOLDOWNS_FILENAME = "model_quota_cooldowns.json"
MODEL_QUOTA_LOCK_FILENAME = "model_quota_cooldowns.lock"
MODEL_QUOTA_SCHEMA_VERSION = 1

DEFAULT_DEV_MODEL = "flash"
DEFAULT_REVIEWER_MODEL = "pro"
DEFAULT_ORCHESTRATOR_MODEL = "gemini-flash-next"
DEFAULT_ROUTING_STRATEGY = "balanced_cross_family"
VALID_ROUTING_STRATEGIES: frozenset[str] = frozenset({"balanced_cross_family", "sequential"})
VALID_RISK_TIERS: frozenset[str] = frozenset({"micro", "standard", "critical"})
DEFAULT_LOW_QUOTA_THRESHOLD_PCT = 25
DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS = 900
DEFAULT_COOLDOWN_FALLBACK_SECONDS = 1800

DEFAULT_MODEL_FALLBACK_ORDER: tuple[str, ...] = (
    "gemini-flash-next",
    "auto",
    "hydrogen-l-tf-medium",
    "fable-5.2-medium",
    "opus-5.5-medium",
    "gemini-3.5-flash-lite",
)
DEFAULT_DEV_ECO_FALLBACK_ORDER: tuple[str, ...] = (
    "gemini-flash-next",
    "gemini-3.5-flash-lite",
    "auto",
    "fable-5.2-low",
    "hydrogen-l-tf-medium",
)
DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER: tuple[str, ...] = (
    "pro",
    "auto",
    "opus-5.5-high",
    "fable-5.2-high",
    "hydrogen-l-tf-high",
    "gemini-flash-next",
)

TIER_DEV_CANDIDATES: dict[str, tuple[str, ...]] = {
    "micro": ("flash_lite", "gemini-3.5-flash-lite", "gemini-flash-next", "flash", "fable-5.2-low"),
    "standard": ("flash", "gemini-flash-next", "gemini-3.5-flash-lite", "auto", "fable-5.2-medium"),
    "critical": ("auto", "hydrogen-l-tf-high", "opus-5.5-high", "fable-5.2-high", "pro"),
}
TIER_REVIEW_CANDIDATES: dict[str, tuple[str, ...]] = {
    "micro": ("fable-5.2-low", "opus-5.5-low", "gemini-4-carbon-low", "gemini-flash-next", "auto", "pro"),
    "standard": ("fable-5.2-medium", "opus-5.5-medium", "hydrogen-l-tf-medium", "auto", "gemini-4-carbon-medium", "pro"),
    "critical": ("opus-5.5-high", "fable-5.2-high", "hydrogen-l-tf-high", "pro", "auto"),
}

_CRITICAL_PATH_MARKERS: tuple[str, ...] = (
    "hooks.json",
    "dev_common.py",
    "git_client.py",
    "pr_remediator.py",
    "lock_store.py",
    "review_gate.py",
    "requirements.json",
    "install.sh",
    ".github/workflows/",
    "oauth",
    "auth",
    "security",
    "secret",
    "permission",
    "guardrail",
)
_CRITICAL_TEXT_RE = re.compile(
    r"\b(security|oauth|auth|hook|guardrail|concurrency|deadlock|migration|architecture\s+refactor)\b",
    re.IGNORECASE,
)
_MICRO_SAFE_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".rst", ".html", ".css", ".svg", ".json", ".toml", ".yml", ".yaml", ".example"}
)

SUBAGENT_MODEL_TIERS: frozenset[str] = frozenset({"pro", "flash", "flash_lite", "inherit"})

_QUOTA_EXHAUSTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bRESOURCE_EXHAUSTED\b", re.IGNORECASE),
    re.compile(r"\b(?:http\s+|status\s*|code\s*[:=]?\s*)429\b", re.IGNORECASE),
    re.compile(r"\bindividual\s+quota\s+reached\b", re.IGNORECASE),
    re.compile(r"\b(?:model\s+)?quota\s+(?:exceeded|exhausted|reached)\b", re.IGNORECASE),
    re.compile(r"\brate\s+limit\s+(?:exceeded|reached)\b", re.IGNORECASE),
)
_RESETS_IN_RE = re.compile(
    r"resets?\s+in\s+(?:(?P<days>\d+)\s*d(?:ays?)?\s*)?"
    r"(?:(?P<hours>\d+)\s*h(?:ours?|rs?)?\s*)?"
    r"(?:(?P<minutes>\d+)\s*m(?:in(?:utes?|s)?)?\s*)?"
    r"(?:(?P<seconds>\d+)\s*s(?:ec(?:onds?|s)?)?)?",
    re.IGNORECASE,
)
_ISO_UTC_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\b")
_MODEL_ID_HINT_RE = re.compile(
    r"\b("
    r"gemini-4-[a-z0-9-]+|gemini-flash-next|gemini-3\.5-flash-lite|gemini-pro-coordinator|"
    r"hydrogen-l-tf-[a-z0-9-]+|opus-5\.5-[a-z0-9-]+|fable-5\.[12]-[a-z0-9-]+"
    r")\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

_PRO_POOL_PREFIXES: tuple[str, ...] = ("gemini-4-carbon", "gemini-4-neon", "gemini-4-")
_FLASH_POOL_ALIASES: tuple[str, ...] = ("gemini-flash-next", "flash")
_FLASH_LITE_POOL_ALIASES: tuple[str, ...] = ("gemini-3.5-flash-lite", "flash_lite")


def _utc_iso(ts: float | None = None) -> str:
    dt = (
        datetime.fromtimestamp(float(ts), tz=timezone.utc)
        if ts is not None
        else datetime.now(timezone.utc)
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_epoch(raw: Any) -> float:
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        return val if val > 0.0 else 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    with contextlib.suppress(ValueError):
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    return 0.0


def parse_model_list(raw: Any) -> list[str]:
    """Parse comma- or whitespace-separated model IDs preserving order and removing duplicates."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = [str(item).strip() for item in raw]
    else:
        tokens = [tok.strip() for tok in re.split(r"[,\s]+", str(raw).strip())]
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if not tok:
            continue
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            out.append(tok)
    return out


def parse_quota_percent(raw: Any) -> tuple[float | None, bool]:
    """Parse a quota value (`'93%'`, `43`, `'unlimited'`) into `(pct_or_None, is_unlimited)`."""
    if raw is None or raw == "":
        return (None, False)
    if isinstance(raw, bool):
        return (None, False)
    if isinstance(raw, (int, float)):
        return (max(0.0, min(100.0, float(raw))), False)
    text = str(raw).strip()
    if not text:
        return (None, False)
    low = text.lower()
    if low in ("unlimited", "inf", "infinity", "unrestricted", "none_limit"):
        return (100.0, True)
    m = _PERCENT_RE.search(text)
    if m:
        with contextlib.suppress(ValueError):
            return (max(0.0, min(100.0, float(m.group(1)))), False)
    with contextlib.suppress(ValueError):
        return (max(0.0, min(100.0, float(text))), False)
    return (None, False)


def _read_config_value(
    key: str,
    *,
    env: Mapping[str, str] | None = None,
    dotenv_vars: Mapping[str, str] | None = None,
) -> str:
    eff_env = os.environ if env is None else env
    val = str(eff_env.get(key) or "").strip()
    if val:
        return val
    if dotenv_vars:
        d_val = str(dotenv_vars.get(key) or "").strip()
        if d_val:
            return d_val
    return ""


def resolve_low_quota_threshold_pct(
    threshold_pct: int | float | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
    dotenv_vars: Mapping[str, str] | None = None,
) -> int:
    """Resolve preventive low-quota Eco threshold percentage (default `25`, clamped `0..95`)."""
    if threshold_pct is not None:
        with contextlib.suppress(TypeError, ValueError):
            return max(0, min(95, int(round(float(threshold_pct)))))
    d_vars = dotenv_vars
    if d_vars is None and env is None:
        import dev_common  # noqa: PLC0415

        d_vars = dev_common.load_dotenv_dict(home_dir=home_dir)
    raw = _read_config_value("AGY_DEV_LOW_QUOTA_THRESHOLD_PCT", env=env, dotenv_vars=d_vars)
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            return max(0, min(95, int(round(float(raw)))))
    return DEFAULT_LOW_QUOTA_THRESHOLD_PCT


def get_model_family(model_id: str) -> str:
    """Classify `model_id` into its independent quota pool / family."""
    low = str(model_id or "").strip().lower()
    if not low:
        return "other"
    if low.startswith("opus-") or "claude-opus" in low:
        return "claude_opus"
    if low.startswith("fable-") or "claude" in low:
        return "claude_fable"
    if low.startswith("hydrogen-"):
        return "gemini_hydrogen"
    if low in ("auto", "gemini-pro-coordinator") or "pro-next" in low:
        return "gemini_pro_next"
    if low in ("flash_lite", "gemini-3.5-flash-lite") or "flash-lite" in low:
        return "gemini_flash_lite"
    if low in ("flash", "gemini-flash-next") or "flash" in low:
        return "gemini_flash"
    if low == "pro" or any(low.startswith(pfx) for pfx in _PRO_POOL_PREFIXES):
        return "gemini_4"
    return "other"


def get_model_broad_family(model_id: str) -> str:
    """Return broad vendor family (`claude` | `gemini` | `other`) for Cross-Family Maker-Checker routing."""
    fam = get_model_family(model_id)
    if fam.startswith("claude"):
        return "claude"
    if fam.startswith("gemini"):
        return "gemini"
    return "other"


def _extract_file_paths_and_loc(
    files: Sequence[str | Mapping[str, Any]] | None,
    lines_changed: int | None = None,
) -> tuple[list[str], int | None]:
    paths: list[str] = []
    computed_loc = 0
    saw_loc = False
    for entry in files or ():
        if isinstance(entry, Mapping):
            raw_p = str(entry.get("filename") or entry.get("path") or entry.get("file") or "").strip()
            if raw_p:
                paths.append(raw_p.replace("\\", "/"))
            add = entry.get("additions")
            rem = entry.get("deletions")
            if isinstance(add, int) or isinstance(rem, int):
                saw_loc = True
                computed_loc += max(0, int(add or 0)) + max(0, int(rem or 0))
        else:
            raw_p = str(entry or "").strip()
            if raw_p:
                paths.append(raw_p.replace("\\", "/"))
    eff_loc = lines_changed if lines_changed is not None else (computed_loc if saw_loc else None)
    return (paths, eff_loc)


def classify_change_risk_tier(
    files: Sequence[str | Mapping[str, Any]] | None = None,
    *,
    lines_changed: int | None = None,
    title: str = "",
    proposed_fix: str = "",
) -> dict[str, Any]:
    """0-LLM deterministic 3-tier change risk classifier (`micro` | `standard` | `critical`)."""
    paths, eff_loc = _extract_file_paths_and_loc(files, lines_changed=lines_changed)
    file_count = len(paths)
    critical_files: list[str] = []
    for p in paths:
        p_low = p.lower()
        if any(marker in p_low for marker in _CRITICAL_PATH_MARKERS):
            critical_files.append(p)

    text_blob = f"{title} {proposed_fix}".strip()
    has_critical_text = bool(text_blob and _CRITICAL_TEXT_RE.search(text_blob))

    if critical_files or file_count > 8 or (eff_loc is not None and eff_loc > 400) or has_critical_text:
        reasons: list[str] = []
        if critical_files:
            reasons.append(f"critical_paths({len(critical_files)})")
        if file_count > 8:
            reasons.append(f"files({file_count})>8")
        if eff_loc is not None and eff_loc > 400:
            reasons.append(f"loc({eff_loc})>400")
        if has_critical_text:
            reasons.append("critical_keywords")
        return {
            "risk_tier": "critical",
            "reason": ",".join(reasons) or "critical_scope",
            "file_count": file_count,
            "lines_changed": eff_loc,
            "critical_files": critical_files,
            "dev_effort": "high",
            "review_effort": "high",
        }

    all_micro_safe = bool(paths) and all(
        (Path(p).suffix.lower() in _MICRO_SAFE_EXTENSIONS or Path(p).name.lower().startswith(".env"))
        for p in paths
    )
    if all_micro_safe and file_count <= 3 and (eff_loc is None or eff_loc <= 80):
        return {
            "risk_tier": "micro",
            "reason": f"docs_or_ui_micro(files={file_count},loc={eff_loc if eff_loc is not None else 'n/a'})",
            "file_count": file_count,
            "lines_changed": eff_loc,
            "critical_files": [],
            "dev_effort": "flash_lite",
            "review_effort": "low",
        }

    return {
        "risk_tier": "standard",
        "reason": f"standard_change(files={file_count},loc={eff_loc if eff_loc is not None else 'n/a'})",
        "file_count": file_count,
        "lines_changed": eff_loc,
        "critical_files": [],
        "dev_effort": "flash",
        "review_effort": "medium",
    }


def resolve_model_fallback_config(
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Load declarative model fallback, balanced cross-family & low-quota Eco settings from `env` and `~/.env`."""
    dotenv_vars: dict[str, str] = {}
    if env is None:
        import dev_common  # noqa: PLC0415

        dotenv_vars = dev_common.load_dotenv_dict(home_dir=home_dir)

    unlimited = parse_model_list(
        _read_config_value("AGY_DEV_UNLIMITED_MODEL_IDS", env=env, dotenv_vars=dotenv_vars)
    )
    raw_fallback = _read_config_value("AGY_DEV_MODEL_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    fallback_order = parse_model_list(raw_fallback) or list(DEFAULT_MODEL_FALLBACK_ORDER)
    dev_eco_order = parse_model_list(
        _read_config_value("AGY_DEV_DEV_ECO_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    ) or list(DEFAULT_DEV_ECO_FALLBACK_ORDER)
    reviewer_order = parse_model_list(
        _read_config_value("AGY_DEV_REVIEWER_MODEL_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    ) or list(DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER)

    raw_strategy = _read_config_value("AGY_DEV_MODEL_ROUTING_STRATEGY", env=env, dotenv_vars=dotenv_vars).lower()
    routing_strategy = raw_strategy if raw_strategy in VALID_ROUTING_STRATEGIES else DEFAULT_ROUTING_STRATEGY

    raw_orch_model = _read_config_value("AGY_DEV_ORCHESTRATOR_MODEL", env=env, dotenv_vars=dotenv_vars)
    # Honor explicit AGY_DEV_MODEL_FALLBACK_ORDER override when AGY_DEV_ORCHESTRATOR_MODEL is not set
    if raw_orch_model:
        orchestrator_model = raw_orch_model
    elif raw_fallback:
        orchestrator_model = fallback_order[0]
    else:
        orchestrator_model = DEFAULT_ORCHESTRATOR_MODEL

    low_thresh = resolve_low_quota_threshold_pct(env=env, home_dir=home_dir, dotenv_vars=dotenv_vars)

    ttl_raw = _read_config_value("AGY_DEV_QUOTA_SNAPSHOT_TTL_SECONDS", env=env, dotenv_vars=dotenv_vars)
    snapshot_ttl = DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS
    if ttl_raw:
        with contextlib.suppress(TypeError, ValueError):
            snapshot_ttl = max(30, int(round(float(ttl_raw))))

    return {
        "unlimited_model_ids": unlimited,
        "low_quota_threshold_pct": low_thresh,
        "snapshot_ttl_seconds": snapshot_ttl,
        "routing_strategy": routing_strategy,
        "orchestrator_model": orchestrator_model,
        "model_fallback_order": fallback_order,
        "dev_eco_fallback_order": dev_eco_order,
        "reviewer_fallback_order": reviewer_order,
    }


def _resolve_state_dir_for_quota(
    state_dir: Path | str | None = None,
    *,
    home_dir: Path | None = None,
) -> Path:
    import dev_common  # noqa: PLC0415

    return dev_common.resolve_state_dir(state_dir, home_dir=home_dir)


def _prune_cooldowns_map(raw_cooldowns: Any, cur_ts: float) -> dict[str, dict[str, Any]]:
    """Return active non-expired cooldown entries keyed by lowercase model ID."""
    if not isinstance(raw_cooldowns, Mapping):
        return {}
    pruned: dict[str, dict[str, Any]] = {}
    for raw_k, entry in raw_cooldowns.items():
        if not isinstance(entry, Mapping):
            continue
        m_id = str(entry.get("model") or raw_k or "").strip()
        if not m_id:
            continue
        resets_epoch = _parse_iso_epoch(entry.get("resets_at_epoch") or entry.get("resets_at"))
        if resets_epoch > 0.0 and cur_ts >= resets_epoch:
            continue
        pruned[m_id.lower()] = {
            "model": m_id,
            "resets_at": str(entry.get("resets_at") or (_utc_iso(resets_epoch) if resets_epoch > 0.0 else "")),
            "resets_at_epoch": resets_epoch,
            "reason": str(entry.get("reason") or "429 RESOURCE_EXHAUSTED"),
            "source": str(entry.get("source") or "transcript"),
            "recorded_at": str(entry.get("recorded_at") or ""),
        }
    return pruned


def load_model_quota_state(
    state_dir: Path | str | None = None,
    *,
    now_ts: float | None = None,
    home_dir: Path | None = None,
    snapshot_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Load `model_quota_cooldowns.json` (read-only), auto-pruning expired cooldowns in memory."""
    import dev_common  # noqa: PLC0415

    cur_ts = float(now_ts if now_ts is not None else time.time())
    st_dir = _resolve_state_dir_for_quota(state_dir, home_dir=home_dir)
    path = st_dir / MODEL_QUOTA_COOLDOWNS_FILENAME
    raw = dev_common.atomic_read_json(path, default={})
    if not isinstance(raw, dict):
        raw = {}

    cooldowns = _prune_cooldowns_map(raw.get("cooldowns"), cur_ts)
    snap_raw = raw.get("quota_snapshot") if isinstance(raw.get("quota_snapshot"), dict) else {}
    snap_epoch = _parse_iso_epoch(snap_raw.get("recorded_at_epoch") or snap_raw.get("recorded_at"))
    ttl = (
        max(30, int(snapshot_ttl_seconds))
        if snapshot_ttl_seconds is not None
        else max(30, int(snap_raw.get("ttl_seconds") or DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS))
    )
    snap_age = max(0.0, cur_ts - snap_epoch) if snap_epoch > 0.0 else float("inf")
    snap_quotas = snap_raw.get("quotas") if isinstance(snap_raw.get("quotas"), dict) else {}
    snapshot_fresh = bool(snap_quotas and snap_epoch > 0.0 and snap_age <= ttl)

    return {
        "schema_version": MODEL_QUOTA_SCHEMA_VERSION,
        "state_path": str(path),
        "updated_at": str(raw.get("updated_at") or ""),
        "cooldowns": cooldowns,
        "quota_snapshot": {
            "recorded_at": str(snap_raw.get("recorded_at") or ""),
            "recorded_at_epoch": snap_epoch,
            "ttl_seconds": ttl,
            "age_seconds": None if snap_epoch <= 0.0 else int(round(snap_age)),
            "fresh": snapshot_fresh,
            "quotas": dict(snap_quotas) if snapshot_fresh else {},
        },
    }


def record_model_quota_cooldown(
    model_id: str,
    *,
    resets_at: str = "",
    resets_in_seconds: int | float | None = None,
    reason: str = "429 RESOURCE_EXHAUSTED",
    source: str = "transcript",
    state_dir: Path | str | None = None,
    now_ts: float | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist a model quota cooldown atomically (`0600` + `file_lock`), pruning expired entries."""
    import dev_common  # noqa: PLC0415

    clean_model = str(model_id or "").strip()
    if not clean_model:
        return {"status": "skipped", "reason": "empty_model_id"}

    cur_ts = float(now_ts if now_ts is not None else time.time())
    resets_epoch = _parse_iso_epoch(resets_at)
    if resets_epoch <= 0.0:
        delta = (
            max(60.0, float(resets_in_seconds))
            if resets_in_seconds is not None and float(resets_in_seconds) > 0
            else float(DEFAULT_COOLDOWN_FALLBACK_SECONDS)
        )
        resets_epoch = cur_ts + delta
    resets_iso = _utc_iso(resets_epoch)

    st_dir = _resolve_state_dir_for_quota(state_dir, home_dir=home_dir)
    path = st_dir / MODEL_QUOTA_COOLDOWNS_FILENAME
    lock_path = st_dir / MODEL_QUOTA_LOCK_FILENAME

    with dev_common.file_lock(lock_path):
        existing = dev_common.atomic_read_json(path, default={})
        if not isinstance(existing, dict):
            existing = {}
        cooldowns = _prune_cooldowns_map(existing.get("cooldowns"), cur_ts)
        entry = {
            "model": clean_model,
            "resets_at": resets_iso,
            "resets_at_epoch": resets_epoch,
            "reason": str(reason or "429 RESOURCE_EXHAUSTED"),
            "source": str(source or "transcript"),
            "recorded_at": _utc_iso(cur_ts),
        }
        cooldowns[clean_model.lower()] = entry
        # Mirror specific gemini-4-* exhaustion onto the `pro` subagent tier alias as well
        if any(clean_model.lower().startswith(pfx) for pfx in _PRO_POOL_PREFIXES):
            cooldowns["pro"] = {**entry, "model": "pro"}
        elif clean_model.lower() == "gemini-flash-next":
            cooldowns["flash"] = {**entry, "model": "flash"}
        elif clean_model.lower() == "gemini-3.5-flash-lite":
            cooldowns["flash_lite"] = {**entry, "model": "flash_lite"}

        payload = {
            "schema_version": MODEL_QUOTA_SCHEMA_VERSION,
            "updated_at": _utc_iso(cur_ts),
            "cooldowns": cooldowns,
            "quota_snapshot": existing.get("quota_snapshot")
            if isinstance(existing.get("quota_snapshot"), dict)
            else {},
        }
        dev_common.atomic_write_json(path, payload, mode=0o600)

    return {
        "status": "ok",
        "model": clean_model,
        "resets_at": resets_iso,
        "active_cooldown_count": len(cooldowns),
    }


def _normalize_swarm_quotas_payload(raw_payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Extract normalized `{model_id: {'quota_remaining': ..., 'quota_resets': ...}}` from a `swarm models` response."""
    if not isinstance(raw_payload, Mapping):
        return {}
    source_map = raw_payload.get("quotas") if isinstance(raw_payload.get("quotas"), Mapping) else raw_payload
    if not isinstance(source_map, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for m_id, info in source_map.items():
        clean_id = str(m_id or "").strip()
        if not clean_id:
            continue
        if isinstance(info, Mapping):
            normalized[clean_id] = {
                "quota_remaining": info.get("quota_remaining", info.get("remaining", "")),
                "quota_resets": str(info.get("quota_resets") or info.get("resets_at") or ""),
            }
        else:
            normalized[clean_id] = {"quota_remaining": info, "quota_resets": ""}
    return normalized


def record_swarm_quota_snapshot(
    quotas_payload: Mapping[str, Any],
    *,
    state_dir: Path | str | None = None,
    ttl_seconds: int | None = None,
    now_ts: float | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist a live `swarm(action='models', all=true)` quota snapshot (`0600`) and sync 0% cooldowns."""
    import dev_common  # noqa: PLC0415

    cur_ts = float(now_ts if now_ts is not None else time.time())
    normalized = _normalize_swarm_quotas_payload(quotas_payload)
    eff_ttl = max(30, int(ttl_seconds or DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS))
    st_dir = _resolve_state_dir_for_quota(state_dir, home_dir=home_dir)
    path = st_dir / MODEL_QUOTA_COOLDOWNS_FILENAME
    lock_path = st_dir / MODEL_QUOTA_LOCK_FILENAME

    with dev_common.file_lock(lock_path):
        existing = dev_common.atomic_read_json(path, default={})
        if not isinstance(existing, dict):
            existing = {}
        cooldowns = _prune_cooldowns_map(existing.get("cooldowns"), cur_ts)

        zero_models: list[str] = []
        pro_pcts: list[float] = []
        pro_resets: list[str] = []
        for m_id, q_info in normalized.items():
            pct, is_unl = parse_quota_percent(q_info.get("quota_remaining"))
            low_id = m_id.lower()
            resets_str = str(q_info.get("quota_resets") or "")
            if any(low_id.startswith(pfx) for pfx in _PRO_POOL_PREFIXES) and pct is not None:
                pro_pcts.append(pct)
                if resets_str:
                    pro_resets.append(resets_str)
            if not is_unl and pct is not None and pct <= 0.0:
                zero_models.append(m_id)
                r_epoch = _parse_iso_epoch(resets_str)
                if r_epoch <= cur_ts:
                    r_epoch = cur_ts + float(DEFAULT_COOLDOWN_FALLBACK_SECONDS)
                cooldowns[low_id] = {
                    "model": m_id,
                    "resets_at": _utc_iso(r_epoch),
                    "resets_at_epoch": r_epoch,
                    "reason": "swarm_quota_0pct",
                    "source": "swarm_models",
                    "recorded_at": _utc_iso(cur_ts),
                }
            elif (is_unl or (pct is not None and pct > 0.0)) and low_id in cooldowns:
                # Live swarm reports quota recovered > 0% -> clear stale cooldown
                cooldowns.pop(low_id, None)

        if pro_pcts:
            if max(pro_pcts) <= 0.0:
                r_epoch = _parse_iso_epoch(pro_resets[0] if pro_resets else "")
                if r_epoch <= cur_ts:
                    r_epoch = cur_ts + float(DEFAULT_COOLDOWN_FALLBACK_SECONDS)
                cooldowns["pro"] = {
                    "model": "pro",
                    "resets_at": _utc_iso(r_epoch),
                    "resets_at_epoch": r_epoch,
                    "reason": "swarm_pro_pool_0pct",
                    "source": "swarm_models",
                    "recorded_at": _utc_iso(cur_ts),
                }
            elif "pro" in cooldowns:
                cooldowns.pop("pro", None)

        payload = {
            "schema_version": MODEL_QUOTA_SCHEMA_VERSION,
            "updated_at": _utc_iso(cur_ts),
            "cooldowns": cooldowns,
            "quota_snapshot": {
                "recorded_at": _utc_iso(cur_ts),
                "recorded_at_epoch": cur_ts,
                "ttl_seconds": eff_ttl,
                "quotas": normalized,
            },
        }
        dev_common.atomic_write_json(path, payload, mode=0o600)

    return {
        "status": "ok",
        "models_recorded": len(normalized),
        "zero_quota_models": zero_models,
        "active_cooldown_count": len(cooldowns),
        "ttl_seconds": eff_ttl,
    }


def _lookup_live_quota_for_model(
    model_id: str,
    quotas_map: Mapping[str, dict[str, Any]],
) -> tuple[float | None, bool, str]:
    """Resolve `(quota_pct, is_unlimited, resets_at)` for `model_id` across direct keys and tier aliases."""
    if not quotas_map:
        return (None, False, "")
    low_map = {k.lower(): v for k, v in quotas_map.items()}
    low_id = str(model_id or "").strip().lower()

    if low_id in low_map:
        info = low_map[low_id]
        pct, unl = parse_quota_percent(info.get("quota_remaining"))
        return (pct, unl, str(info.get("quota_resets") or ""))

    if low_id == "pro":
        matched: list[tuple[float, bool, str]] = []
        for k_low, info in low_map.items():
            if any(k_low.startswith(pfx) for pfx in _PRO_POOL_PREFIXES):
                pct, unl = parse_quota_percent(info.get("quota_remaining"))
                if pct is not None:
                    matched.append((pct, unl, str(info.get("quota_resets") or "")))
        if matched:
            best = max(matched, key=lambda t: t[0])
            return best

    if low_id == "flash":
        for alias in _FLASH_POOL_ALIASES:
            if alias in low_map:
                info = low_map[alias]
                pct, unl = parse_quota_percent(info.get("quota_remaining"))
                return (pct, unl, str(info.get("quota_resets") or ""))

    if low_id == "flash_lite":
        for alias in _FLASH_LITE_POOL_ALIASES:
            if alias in low_map:
                info = low_map[alias]
                pct, unl = parse_quota_percent(info.get("quota_remaining"))
                return (pct, unl, str(info.get("quota_resets") or ""))

    return (None, False, "")


def _is_model_in_cooldown(
    model_id: str,
    cooldowns: Mapping[str, dict[str, Any]],
) -> tuple[bool, str]:
    low_id = str(model_id or "").strip().lower()
    if low_id in cooldowns:
        return (True, str(cooldowns[low_id].get("resets_at") or ""))
    if low_id == "pro":
        for k_low, entry in cooldowns.items():
            if any(k_low.startswith(pfx) for pfx in _PRO_POOL_PREFIXES):
                return (True, str(entry.get("resets_at") or ""))
    if low_id == "flash" and "gemini-flash-next" in cooldowns:
        return (True, str(cooldowns["gemini-flash-next"].get("resets_at") or ""))
    if low_id == "flash_lite" and "gemini-3.5-flash-lite" in cooldowns:
        return (True, str(cooldowns["gemini-3.5-flash-lite"].get("resets_at") or ""))
    return (False, "")


def map_model_to_subagent_tier(model_id: str, *, default: str = "inherit") -> str:
    """Map an arbitrary model ID onto `invoke_subagent`'s enum (`pro | flash | flash_lite | inherit`)."""
    low = str(model_id or "").strip().lower()
    if low in SUBAGENT_MODEL_TIERS:
        return low
    if low == "gemini-flash-next":
        return "flash"
    if low == "gemini-3.5-flash-lite":
        return "flash_lite"
    if any(low.startswith(pfx) for pfx in _PRO_POOL_PREFIXES):
        return "pro"
    return default


def _build_role_candidates(
    role: str,
    preferred_model: str,
    config: Mapping[str, Any],
    *,
    review_iteration: int = 1,
    risk_tier: str = "",
) -> tuple[str, list[str]]:
    role_l = str(role or "orchestrator").strip().lower()
    pref = str(preferred_model or "").strip()
    norm_tier = str(risk_tier or "").strip().lower()
    if norm_tier not in VALID_RISK_TIERS:
        norm_tier = ""

    if role_l == "developer":
        if not pref:
            if int(review_iteration) >= 2:
                pref = DEFAULT_REVIEWER_MODEL
            elif norm_tier == "micro":
                pref = "flash_lite"
            elif norm_tier == "critical":
                pref = "auto"
            else:
                pref = DEFAULT_DEV_MODEL
        if int(review_iteration) >= 2 or pref.lower() == "pro" or norm_tier == "critical":
            tier_cands = TIER_DEV_CANDIDATES["critical"] if norm_tier == "critical" else ()
            base_chain = [pref, *tier_cands, *config["reviewer_fallback_order"], *config["dev_eco_fallback_order"]]
        elif norm_tier in TIER_DEV_CANDIDATES:
            base_chain = [pref, *TIER_DEV_CANDIDATES[norm_tier], *config["dev_eco_fallback_order"], *config["model_fallback_order"]]
        else:
            base_chain = [pref, *config["dev_eco_fallback_order"], *config["model_fallback_order"]]
    elif role_l == "reviewer":
        if not pref:
            pref = TIER_REVIEW_CANDIDATES[norm_tier][0] if norm_tier in TIER_REVIEW_CANDIDATES else DEFAULT_REVIEWER_MODEL
        if norm_tier in TIER_REVIEW_CANDIDATES:
            base_chain = [pref, *TIER_REVIEW_CANDIDATES[norm_tier], *config["reviewer_fallback_order"], *config["model_fallback_order"]]
        else:
            base_chain = [pref, *config["reviewer_fallback_order"], *config["model_fallback_order"]]
    else:
        orch_default = str(config.get("orchestrator_model") or "").strip()
        if not pref and orch_default:
            pref = orch_default
        base_chain = ([pref] if pref else []) + list(config["model_fallback_order"])
        if not pref and base_chain:
            pref = base_chain[0]

    dedup: list[str] = []
    seen: set[str] = set()
    for m in base_chain:
        clean = str(m or "").strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            dedup.append(clean)
    return (pref, dedup)


def _is_tier_primary_candidate(model_id: str, *, role: str, risk_tier: str) -> bool:
    """Keep tier-appropriate primary pools ahead of fallback tiers (e.g., flash_lite for micro dev, reasoning for standard/critical review)."""
    role_l = str(role or "").strip().lower()
    fam = get_model_family(model_id)
    low = str(model_id or "").strip().lower()
    if role_l == "developer":
        if risk_tier == "micro":
            return fam == "gemini_flash_lite"
        if risk_tier == "critical":
            return fam not in ("gemini_flash", "gemini_flash_lite")
        return True
    if role_l == "reviewer":
        if risk_tier == "micro":
            return low.endswith("-low") or fam in ("gemini_flash", "gemini_flash_lite")
        return fam not in ("gemini_flash", "gemini_flash_lite")
    return True


def select_next_optimal_model(
    *,
    role: str = "orchestrator",
    preferred_model: str = "",
    available_models: Sequence[str] | None = None,
    live_quotas: Mapping[str, Any] | None = None,
    extra_cooldowns: Mapping[str, dict[str, Any]] | None = None,
    state_dir: Path | str | None = None,
    review_iteration: int = 1,
    risk_tier: str = "",
    dev_model: str = "",
    exclude_family: str = "",
    routing_strategy: str = "",
    low_quota_threshold_pct: int | None = None,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Deterministically pick the best available model honoring Unlimited-First, Highest-Quota Cross-Family Balancing, 3-Tier Risk Effort, Low-Quota Eco (`<=25%`), and 429 cooldowns."""
    cur_ts = float(now_ts if now_ts is not None else time.time())
    cfg = resolve_model_fallback_config(env=env, home_dir=home_dir)
    threshold = (
        max(0, min(95, int(low_quota_threshold_pct)))
        if low_quota_threshold_pct is not None
        else int(cfg["low_quota_threshold_pct"])
    )
    eff_strategy = str(routing_strategy or "").strip().lower()
    if eff_strategy not in VALID_ROUTING_STRATEGIES:
        eff_strategy = str(cfg.get("routing_strategy") or DEFAULT_ROUTING_STRATEGY)
    norm_tier = str(risk_tier or "").strip().lower()
    if norm_tier not in VALID_RISK_TIERS:
        norm_tier = ""

    excl_pool = str(exclude_family or "").strip().lower()
    excl_broad = ""
    if not excl_pool and str(dev_model or "").strip():
        excl_pool = get_model_family(dev_model)
        excl_broad = get_model_broad_family(dev_model)
    elif excl_pool in ("claude", "gemini"):
        excl_broad = excl_pool
    elif excl_pool:
        excl_broad = "claude" if excl_pool.startswith("claude") else ("gemini" if excl_pool.startswith("gemini") else "")

    state = load_model_quota_state(
        state_dir=state_dir,
        now_ts=cur_ts,
        home_dir=home_dir,
        snapshot_ttl_seconds=int(cfg["snapshot_ttl_seconds"]),
    )
    cooldowns: dict[str, dict[str, Any]] = dict(state["cooldowns"])
    if extra_cooldowns:
        for k, entry in _prune_cooldowns_map(extra_cooldowns, cur_ts).items():
            cooldowns[k] = entry

    merged_quotas: dict[str, dict[str, Any]] = dict(state["quota_snapshot"].get("quotas") or {})
    if live_quotas is not None:
        live_norm = _normalize_swarm_quotas_payload(live_quotas)
        merged_quotas.update(live_norm)
        # Clear stale cooldowns if live quotas explicitly report >0%
        for m_id, q_info in live_norm.items():
            pct, unl = parse_quota_percent(q_info.get("quota_remaining"))
            if unl or (pct is not None and pct > 0.0):
                cooldowns.pop(m_id.lower(), None)

    avail_set: set[str] | None = None
    if available_models is not None:
        avail_set = {str(m).strip().lower() for m in available_models if str(m).strip()}

    def _is_available(m_id: str) -> bool:
        if avail_set is None:
            return True
        low = m_id.lower()
        if low in avail_set:
            return True
        if low == "pro" and any(any(a.startswith(pfx) for pfx in _PRO_POOL_PREFIXES) for a in avail_set):
            return True
        if low == "flash" and any(a in avail_set for a in _FLASH_POOL_ALIASES):
            return True
        if low == "flash_lite" and any(a in avail_set for a in _FLASH_LITE_POOL_ALIASES):
            return True
        return False

    pref_model, candidates = _build_role_candidates(
        role, preferred_model, cfg, review_iteration=review_iteration, risk_tier=norm_tier
    )
    unlimited_ids = list(cfg["unlimited_model_ids"])
    # Also detect runtime-reported unlimited models in live quotas
    for m_id, q_info in merged_quotas.items():
        _, is_unl = parse_quota_percent(q_info.get("quota_remaining"))
        if is_unl and m_id.lower() not in {u.lower() for u in unlimited_ids}:
            unlimited_ids.append(m_id)

    skipped: list[dict[str, Any]] = []
    earliest_reset_epoch = 0.0
    earliest_reset_iso = ""

    def _track_reset(resets_str: str) -> None:
        nonlocal earliest_reset_epoch, earliest_reset_iso
        ep = _parse_iso_epoch(resets_str)
        if ep > 0.0 and (earliest_reset_epoch <= 0.0 or ep < earliest_reset_epoch):
            earliest_reset_epoch = ep
            earliest_reset_iso = resets_str

    def _evaluate(m_id: str) -> dict[str, Any]:
        in_cd, cd_reset = _is_model_in_cooldown(m_id, cooldowns)
        pct, is_unl, q_reset = _lookup_live_quota_for_model(m_id, merged_quotas)
        resets_str = cd_reset or q_reset
        exhausted = bool(in_cd or (not is_unl and pct is not None and pct <= 0.0))
        low_quota = bool(not is_unl and not exhausted and pct is not None and pct <= float(threshold))
        return {
            "model": m_id,
            "family": get_model_family(m_id),
            "broad_family": get_model_broad_family(m_id),
            "available": _is_available(m_id),
            "in_cooldown": in_cd,
            "quota_pct": pct,
            "is_unlimited": is_unl or (m_id.lower() in {u.lower() for u in unlimited_ids}),
            "exhausted": exhausted,
            "low_quota": low_quota,
            "resets_at": resets_str,
        }

    def _build_result(
        selected_id: str,
        *,
        mode: str,
        reason: str,
        quota_pct: float | None,
        low_triggered: bool,
        all_exhausted: bool = False,
        next_reset: str = "",
    ) -> dict[str, Any]:
        sel_fam = get_model_family(selected_id) if selected_id else ""
        sel_broad = get_model_broad_family(selected_id) if selected_id else ""
        cross_matched = bool(
            selected_id
            and (
                (excl_broad and sel_broad and sel_broad != excl_broad)
                or (excl_pool and sel_fam and sel_fam != excl_pool)
            )
        )
        role_l = str(role or "orchestrator").lower()
        tier_fallback_default = (
            "flash_lite"
            if (role_l == "developer" and norm_tier == "micro")
            else (
                DEFAULT_REVIEWER_MODEL
                if (
                    (role_l == "reviewer" and norm_tier != "micro")
                    or (role_l == "developer" and (int(review_iteration) >= 2 or norm_tier == "critical"))
                )
                else DEFAULT_DEV_MODEL
            )
        )
        return {
            "role": role_l,
            "risk_tier": norm_tier,
            "routing_strategy": eff_strategy,
            "preferred_model": pref_model,
            "selected_model": selected_id,
            "model_family": sel_fam,
            "broad_family": sel_broad,
            "cross_family_matched": cross_matched,
            "subagent_model": map_model_to_subagent_tier(selected_id, default=tier_fallback_default)
            if selected_id
            else "",
            "requires_swarm_dispatch": bool(selected_id and selected_id.lower() not in SUBAGENT_MODEL_TIERS),
            "mode": mode,
            "reason": reason,
            "low_quota_triggered": bool(low_triggered),
            "low_quota_threshold_pct": threshold,
            "selected_quota_pct": quota_pct,
            "all_exhausted": bool(all_exhausted),
            "next_reset_at": next_reset,
            "fallback_models": [c for c in candidates if c.lower() != selected_id.lower()] if selected_id else [],
            "skipped_models": skipped,
        }

    # Priority 1: Unlimited models (configured via AGY_DEV_UNLIMITED_MODEL_IDS or reported by host)
    for u_id in unlimited_ids:
        ev = _evaluate(u_id)
        if ev["available"] and not ev["exhausted"]:
            return _build_result(
                u_id,
                mode="unlimited",
                reason="unlimited_model_preferred",
                quota_pct=ev["quota_pct"],
                low_triggered=False,
            )

    # Priority 2: Evaluate all candidates for healthy (> threshold) vs low-quota vs exhausted
    healthy_candidates: list[tuple[int, dict[str, Any]]] = []
    low_quota_survivors: list[dict[str, Any]] = []
    preferred_was_exhausted = False
    preferred_was_low = False

    for idx, cand in enumerate(candidates):
        ev = _evaluate(cand)
        if not ev["available"]:
            skipped.append({"model": cand, "reason": "not_in_available_catalog"})
            continue
        if ev["exhausted"]:
            if idx == 0:
                preferred_was_exhausted = True
            _track_reset(ev["resets_at"])
            skipped.append(
                {
                    "model": cand,
                    "reason": "cooldown_429" if ev["in_cooldown"] else "quota_0pct",
                    "resets_at": ev["resets_at"],
                }
            )
            continue
        if ev["low_quota"]:
            if idx == 0:
                preferred_was_low = True
            low_quota_survivors.append(ev)
            skipped.append(
                {
                    "model": cand,
                    "reason": f"below_low_quota_threshold_{threshold}pct",
                    "quota_pct": ev["quota_pct"],
                }
            )
            continue
        healthy_candidates.append((idx, ev))

    if healthy_candidates:
        has_live_quota_signal = any(ev["quota_pct"] is not None for _, ev in healthy_candidates)
        use_balanced = bool(
            eff_strategy == "balanced_cross_family"
            and (has_live_quota_signal or excl_pool or excl_broad)
            and not (str(role or "").lower() == "developer" and int(review_iteration) == 1 and not norm_tier and not preferred_was_low and not preferred_was_exhausted and not excl_pool)
        )
        if use_balanced:
            role_l_sc = str(role or "orchestrator").lower()
            tier_preferred_set: set[str] = set()
            if role_l_sc == "developer" and norm_tier in TIER_DEV_CANDIDATES:
                tier_preferred_set = {m.lower() for m in TIER_DEV_CANDIDATES[norm_tier]}
            elif role_l_sc == "reviewer" and norm_tier in TIER_REVIEW_CANDIDATES:
                tier_preferred_set = {m.lower() for m in TIER_REVIEW_CANDIDATES[norm_tier]}

            def _score_healthy(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int, float, int]:
                c_idx, ev_item = item
                primary_ok = 1 if _is_tier_primary_candidate(ev_item["model"], role=role, risk_tier=norm_tier) else 0
                in_tier = 1 if (tier_preferred_set and ev_item["model"].lower() in tier_preferred_set) else 0
                diff_broad = 1 if (excl_broad and ev_item["broad_family"] and ev_item["broad_family"] != excl_broad) else 0
                diff_pool = 1 if (excl_pool and ev_item["family"] and ev_item["family"] != excl_pool) else 0
                # Known live quota scores strictly by % remaining; unknown quota gets modest baseline (-1.0) so known high-quota pools win
                q_score = float(ev_item["quota_pct"]) if ev_item["quota_pct"] is not None else -1.0
                return (primary_ok, in_tier, diff_broad, diff_pool, q_score, -c_idx)

            best_idx, best_ev = max(healthy_candidates, key=_score_healthy)
            sel_id = str(best_ev["model"])
            if preferred_was_low and not preferred_was_exhausted:
                mode = "eco_low_quota"
                reason = f"preventive_low_quota_balanced_le_{threshold}pct"
            elif preferred_was_exhausted:
                mode = "quota_fallback"
                reason = "preferred_model_exhausted_balanced_fallback"
            elif best_idx == 0 and not (excl_broad and best_ev["broad_family"] != excl_broad):
                mode = "normal"
                reason = "preferred_model_healthy"
            else:
                mode = "balanced_cross_family"
                reason = (
                    "highest_quota_cross_family_selected"
                    if (excl_broad and best_ev["broad_family"] != excl_broad)
                    else "highest_quota_pool_selected"
                )
            return _build_result(
                sel_id,
                mode=mode,
                reason=reason,
                quota_pct=best_ev["quota_pct"],
                low_triggered=bool(preferred_was_low),
                next_reset=earliest_reset_iso,
            )

        first_idx, first_ev = healthy_candidates[0]
        sel_id = str(first_ev["model"])
        if first_idx == 0:
            mode = "normal"
            reason = "preferred_model_healthy"
        elif preferred_was_low and not preferred_was_exhausted:
            mode = "eco_low_quota"
            reason = f"preventive_low_quota_fallback_le_{threshold}pct"
        else:
            mode = "quota_fallback"
            reason = "preferred_model_exhausted_or_unavailable"
        return _build_result(
            sel_id,
            mode=mode,
            reason=reason,
            quota_pct=first_ev["quota_pct"],
            low_triggered=bool(preferred_was_low),
            next_reset=earliest_reset_iso,
        )

    # Priority 3: All healthy candidates were below threshold, pick highest remaining > 0% rather than stalling
    if low_quota_survivors:
        best_low = max(low_quota_survivors, key=lambda e: float(e["quota_pct"] or 0.0))
        return _build_result(
            str(best_low["model"]),
            mode="eco_low_quota",
            reason="best_remaining_low_quota_model",
            quota_pct=best_low["quota_pct"],
            low_triggered=True,
            next_reset=earliest_reset_iso,
        )

    # Priority 4: Every candidate is exhausted / in cooldown
    return _build_result(
        "",
        mode="all_exhausted",
        reason="all_fallback_models_exhausted",
        quota_pct=0.0,
        low_triggered=False,
        all_exhausted=True,
        next_reset=earliest_reset_iso,
    )


def parse_reset_duration_seconds(text: str) -> int:
    """Extract reset duration in seconds from phrases like `Resets in 5h 49m 32s`."""
    m = _RESETS_IN_RE.search(str(text or ""))
    if not m:
        return 0
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def detect_quota_exhaustion_in_lines(
    lines: Sequence[str],
    *,
    default_model: str = "pro",
    now_ts: float | None = None,
    recovery_line_index: int = -1,
) -> dict[str, Any]:
    """Scan transcript tail lines for unrecovered `429 RESOURCE_EXHAUSTED` quota errors and reset timers."""
    cur_ts = float(now_ts if now_ts is not None else time.time())
    last_quota_idx = -1
    last_line = ""

    for idx, raw_line in enumerate(lines):
        if any(pat.search(raw_line) for pat in _QUOTA_EXHAUSTED_PATTERNS):
            last_quota_idx = idx
            last_line = raw_line

    if last_quota_idx < 0 or recovery_line_index > last_quota_idx:
        return {
            "quota_exhausted": False,
            "exhausted_model": "",
            "resets_at": "",
            "resets_in_seconds": 0,
            "snippet": "",
            "line_index": -1,
        }

    # Check both matched line and immediate neighbors for reset timestamp/model ID
    window_start = max(0, last_quota_idx - 1)
    window_end = min(len(lines), last_quota_idx + 2)
    window_blob = "\n".join(lines[window_start:window_end])

    model_match = _MODEL_ID_HINT_RE.search(window_blob)
    exhausted_model = model_match.group(1) if model_match else str(default_model or "pro")

    resets_in_secs = parse_reset_duration_seconds(window_blob)
    resets_at = ""
    iso_matches = _ISO_UTC_RE.findall(window_blob)
    # Pick first ISO timestamp strictly in the future relative to cur_ts (avoid picking log created_at)
    for iso_cand in iso_matches:
        ep = _parse_iso_epoch(iso_cand)
        if ep > cur_ts:
            resets_at = _utc_iso(ep)
            if resets_in_secs <= 0:
                resets_in_secs = max(1, int(round(ep - cur_ts)))
            break
    if not resets_at and resets_in_secs > 0:
        resets_at = (
            datetime.fromtimestamp(cur_ts, tz=timezone.utc) + timedelta(seconds=resets_in_secs)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif not resets_at:
        resets_in_secs = DEFAULT_COOLDOWN_FALLBACK_SECONDS
        resets_at = _utc_iso(cur_ts + resets_in_secs)

    snippet = " ".join(last_line.strip().split())[:220]
    return {
        "quota_exhausted": True,
        "exhausted_model": exhausted_model,
        "resets_at": resets_at,
        "resets_in_seconds": resets_in_secs,
        "snippet": snippet,
        "line_index": last_quota_idx,
    }
