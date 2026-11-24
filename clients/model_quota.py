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
DEFAULT_LOW_QUOTA_THRESHOLD_PCT = 25
DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS = 900
DEFAULT_COOLDOWN_FALLBACK_SECONDS = 1800

DEFAULT_MODEL_FALLBACK_ORDER: tuple[str, ...] = (
    "auto",
    "hydrogen-l-tf-high",
    "opus-5.5-high",
    "fable-5.2-high",
    "gemini-flash-next",
    "gemini-3.5-flash-lite",
)
DEFAULT_DEV_ECO_FALLBACK_ORDER: tuple[str, ...] = (
    "gemini-flash-next",
    "gemini-3.5-flash-lite",
    "auto",
    "hydrogen-l-tf-high",
    "fable-5.2-high",
)
DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER: tuple[str, ...] = (
    "pro",
    "auto",
    "hydrogen-l-tf-high",
    "opus-5.5-high",
    "fable-5.2-high",
    "gemini-flash-next",
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


def resolve_model_fallback_config(
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Load declarative model fallback & low-quota Eco settings from `env` and `~/.env`."""
    dotenv_vars: dict[str, str] = {}
    if env is None:
        import dev_common  # noqa: PLC0415

        dotenv_vars = dev_common.load_dotenv_dict(home_dir=home_dir)

    unlimited = parse_model_list(
        _read_config_value("AGY_DEV_UNLIMITED_MODEL_IDS", env=env, dotenv_vars=dotenv_vars)
    )
    fallback_order = parse_model_list(
        _read_config_value("AGY_DEV_MODEL_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    ) or list(DEFAULT_MODEL_FALLBACK_ORDER)
    dev_eco_order = parse_model_list(
        _read_config_value("AGY_DEV_DEV_ECO_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    ) or list(DEFAULT_DEV_ECO_FALLBACK_ORDER)
    reviewer_order = parse_model_list(
        _read_config_value("AGY_DEV_REVIEWER_MODEL_FALLBACK_ORDER", env=env, dotenv_vars=dotenv_vars)
    ) or list(DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER)

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
) -> tuple[str, list[str]]:
    role_l = str(role or "orchestrator").strip().lower()
    pref = str(preferred_model or "").strip()

    if role_l == "developer":
        if not pref:
            pref = DEFAULT_REVIEWER_MODEL if int(review_iteration) >= 2 else DEFAULT_DEV_MODEL
        if int(review_iteration) >= 2 or pref.lower() == "pro":
            base_chain = [pref, *config["reviewer_fallback_order"], *config["dev_eco_fallback_order"]]
        else:
            base_chain = [pref, *config["dev_eco_fallback_order"], *config["model_fallback_order"]]
    elif role_l == "reviewer":
        if not pref:
            pref = DEFAULT_REVIEWER_MODEL
        base_chain = [pref, *config["reviewer_fallback_order"], *config["model_fallback_order"]]
    else:
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


def select_next_optimal_model(
    *,
    role: str = "orchestrator",
    preferred_model: str = "",
    available_models: Sequence[str] | None = None,
    live_quotas: Mapping[str, Any] | None = None,
    extra_cooldowns: Mapping[str, dict[str, Any]] | None = None,
    state_dir: Path | str | None = None,
    review_iteration: int = 1,
    low_quota_threshold_pct: int | None = None,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Deterministically pick the best available model honoring Unlimited-First, Low-Quota Eco (`<=25%`), and 429 cooldowns."""
    cur_ts = float(now_ts if now_ts is not None else time.time())
    cfg = resolve_model_fallback_config(env=env, home_dir=home_dir)
    threshold = (
        max(0, min(95, int(low_quota_threshold_pct)))
        if low_quota_threshold_pct is not None
        else int(cfg["low_quota_threshold_pct"])
    )

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
        role, preferred_model, cfg, review_iteration=review_iteration
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
            "available": _is_available(m_id),
            "in_cooldown": in_cd,
            "quota_pct": pct,
            "is_unlimited": is_unl or (m_id.lower() in {u.lower() for u in unlimited_ids}),
            "exhausted": exhausted,
            "low_quota": low_quota,
            "resets_at": resets_str,
        }

    # Priority 1: Unlimited models (configured via AGY_DEV_UNLIMITED_MODEL_IDS or reported by host)
    for u_id in unlimited_ids:
        ev = _evaluate(u_id)
        if ev["available"] and not ev["exhausted"]:
            return {
                "role": str(role or "orchestrator").lower(),
                "preferred_model": pref_model,
                "selected_model": u_id,
                "subagent_model": map_model_to_subagent_tier(u_id),
                "requires_swarm_dispatch": u_id.lower() not in SUBAGENT_MODEL_TIERS,
                "mode": "unlimited",
                "reason": "unlimited_model_preferred",
                "low_quota_triggered": False,
                "low_quota_threshold_pct": threshold,
                "selected_quota_pct": ev["quota_pct"],
                "all_exhausted": False,
                "next_reset_at": "",
                "fallback_models": [c for c in candidates if c.lower() != u_id.lower()],
                "skipped_models": skipped,
            }

    # Priority 2: Healthy candidates strictly above low-quota threshold (or unknown quota & not in cooldown)
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

        # Healthy candidate found!
        if idx == 0:
            mode = "normal"
            reason = "preferred_model_healthy"
        elif preferred_was_low and not preferred_was_exhausted:
            mode = "eco_low_quota"
            reason = f"preventive_low_quota_fallback_le_{threshold}pct"
        else:
            mode = "quota_fallback"
            reason = "preferred_model_exhausted_or_unavailable"

        return {
            "role": str(role or "orchestrator").lower(),
            "preferred_model": pref_model,
            "selected_model": cand,
            "subagent_model": map_model_to_subagent_tier(cand),
            "requires_swarm_dispatch": cand.lower() not in SUBAGENT_MODEL_TIERS,
            "mode": mode,
            "reason": reason,
            "low_quota_triggered": bool(preferred_was_low),
            "low_quota_threshold_pct": threshold,
            "selected_quota_pct": ev["quota_pct"],
            "all_exhausted": False,
            "next_reset_at": earliest_reset_iso,
            "fallback_models": [c for c in candidates if c.lower() != cand.lower()],
            "skipped_models": skipped,
        }

    # Priority 3: All healthy candidates were below threshold, pick highest remaining > 0% rather than stalling
    if low_quota_survivors:
        best_low = max(low_quota_survivors, key=lambda e: float(e["quota_pct"] or 0.0))
        sel = str(best_low["model"])
        return {
            "role": str(role or "orchestrator").lower(),
            "preferred_model": pref_model,
            "selected_model": sel,
            "subagent_model": map_model_to_subagent_tier(sel),
            "requires_swarm_dispatch": sel.lower() not in SUBAGENT_MODEL_TIERS,
            "mode": "eco_low_quota",
            "reason": "best_remaining_low_quota_model",
            "low_quota_triggered": True,
            "low_quota_threshold_pct": threshold,
            "selected_quota_pct": best_low["quota_pct"],
            "all_exhausted": False,
            "next_reset_at": earliest_reset_iso,
            "fallback_models": [c for c in candidates if c.lower() != sel.lower()],
            "skipped_models": skipped,
        }

    # Priority 4: Every candidate is exhausted / in cooldown
    return {
        "role": str(role or "orchestrator").lower(),
        "preferred_model": pref_model,
        "selected_model": "",
        "subagent_model": "",
        "requires_swarm_dispatch": False,
        "mode": "all_exhausted",
        "reason": "all_fallback_models_exhausted",
        "low_quota_triggered": False,
        "low_quota_threshold_pct": threshold,
        "selected_quota_pct": 0.0,
        "all_exhausted": True,
        "next_reset_at": earliest_reset_iso,
        "fallback_models": [],
        "skipped_models": skipped,
    }


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
