"""Cross-task usage ledger and cross-task agent cooldown store.

Both stores live under a controller-owned root (``.agent-pipeline/usage/``)
and are written by every real ``pipeline-run`` process, potentially
concurrently across different tasks. This module owns their on-disk
format and concurrency handling; it deliberately knows nothing about
``controller``/``TASKS_ROOT`` to avoid a circular import (``real_runner.py``,
which calls into this module, sits below ``controller.py``).

Every public function here is best-effort: a ledger/cooldown-store failure
(disk full, permission error, corrupt file) must never fail a pipeline run,
so writers swallow exceptions and return False, and readers degrade to an
empty result rather than raising.
"""

from __future__ import print_function

import calendar
import fcntl
import json
import os
import time
from pathlib import Path


SCHEMA_VERSION = 1


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Layer 1: usage ledger
# ---------------------------------------------------------------------------


def build_entry(task, run_id, stage_key, agent, result, usage):
    return {
        "schema_version": SCHEMA_VERSION,
        "task": task,
        "run_id": run_id,
        "stage": stage_key,
        "agent": agent,
        "recorded_at": _now_iso(),
        "duration_seconds": result.get("duration_seconds"),
        "exit_code": result.get("exit_code"),
        "failure_class": result.get("failure_class"),
        "model": result.get("model"),
        "pass_number": result.get("pass_number"),
        "attempt_number": result.get("attempt_number"),
        "retry_reason": result.get("retry_reason"),
        "usage": usage,
    }


def append_entry(ledger_path, entry):
    """Append one JSON line to the shared ledger. Safe across concurrent
    processes via an exclusive flock held only around the single write.
    Never raises; returns False on any failure."""
    try:
        ledger_path = Path(ledger_path)
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, sort_keys=True) + "\n"
        fd = os.open(str(ledger_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def read_entries(ledger_path):
    """Read and parse the ledger, skipping unparseable lines. Returns []
    if the file doesn't exist or can't be read at all."""
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []
    entries = []
    try:
        with open(str(ledger_path), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except Exception:
        return entries
    return entries


_USAGE_TOTAL_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def estimate_cost_usd(usage_data, model, codex_price_table):
    if not isinstance(usage_data, dict) or not model:
        return None
    if not isinstance(codex_price_table, dict) or model not in codex_price_table:
        return None
    rates = codex_price_table.get(model)
    if not isinstance(rates, dict):
        return None
    total = 0.0
    for field in _USAGE_TOTAL_FIELDS:
        raw_value = usage_data.get(field, 0)
        if raw_value is None:
            raw_value = 0
        if isinstance(raw_value, bool):
            return None
        try:
            tokens = float(raw_value)
        except (TypeError, ValueError):
            return None
        try:
            rate = float(rates[field])
        except (KeyError, TypeError, ValueError):
            return None
        total += (tokens * rate) / 1000000.0
    return total


def _new_bucket():
    bucket = {
        "count": 0,
        "failures": 0,
        "duration_seconds": 0.0,
        "total_cost_usd": 0.0,
        "total_cost_usd_estimated": 0.0,
        "tokens_known": False,
        "cost_known": False,
        "cost_estimated_known": False,
        "cache_hit_ratio": None,
    }
    for field in _USAGE_TOTAL_FIELDS:
        bucket[field] = 0
    return bucket


def _accumulate(bucket, entry):
    bucket["count"] += 1
    if entry.get("failure_class"):
        bucket["failures"] += 1
    try:
        bucket["duration_seconds"] += float(entry.get("duration_seconds") or 0.0)
    except (TypeError, ValueError):
        pass
    usage = entry.get("usage") or {}
    if not isinstance(usage, dict):
        return
    for field in _USAGE_TOTAL_FIELDS:
        value = usage.get(field)
        if isinstance(value, (int, float)):
            bucket[field] += value
            bucket["tokens_known"] = True
    cost = usage.get("total_cost_usd")
    if not isinstance(cost, bool) and isinstance(cost, (int, float)):
        bucket["total_cost_usd"] += cost
        bucket["cost_known"] = True
    estimated_cost = usage.get("total_cost_usd_estimated")
    if not isinstance(estimated_cost, bool) and isinstance(estimated_cost, (int, float)):
        bucket["total_cost_usd_estimated"] += estimated_cost
        bucket["cost_estimated_known"] = True


def summarize(entries, group_by="agent"):
    """Aggregate ledger entries into per-group and overall counts/duration/
    tokens/cost buckets. ``group_by`` is any entry key (``"agent"``,
    ``"task"``, ``"stage"``, ...); missing keys land under ``"unknown"``."""
    groups = {}
    overall = _new_bucket()
    for entry in entries:
        key = entry.get(group_by) or "unknown"
        bucket = groups.setdefault(key, _new_bucket())
        _accumulate(bucket, entry)
        _accumulate(overall, entry)
    for bucket in list(groups.values()) + [overall]:
        _finalize_bucket(bucket)
    return {"groups": groups, "overall": overall}


def _finalize_bucket(bucket):
    denominator = bucket["input_tokens"] + bucket["cache_read_tokens"]
    if bucket["tokens_known"] and denominator:
        bucket["cache_hit_ratio"] = float(bucket["cache_read_tokens"]) / float(denominator)
    else:
        bucket["cache_hit_ratio"] = None
    return bucket


# ---------------------------------------------------------------------------
# Layer 2: cross-task agent cooldowns
# ---------------------------------------------------------------------------


def _cooldown_lock_path(cooldowns_path):
    return Path(str(cooldowns_path) + ".lock")


def _compute_expires_at(reset_at, default_cooldown_seconds):
    if reset_at:
        try:
            return calendar.timegm(time.strptime(str(reset_at), "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            pass
    return time.time() + float(default_cooldown_seconds)


def load_cooldowns(cooldowns_path):
    """Return {agent: detail} for agents currently on an unexpired
    cooldown. Never raises; degrades to {} on any read/parse failure."""
    cooldowns_path = Path(cooldowns_path)
    if not cooldowns_path.exists():
        return {}
    try:
        with open(str(cooldowns_path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    now_ts = time.time()
    active = {}
    for agent, detail in data.items():
        if not isinstance(detail, dict):
            continue
        try:
            expires_at = float(detail.get("expires_at"))
        except (TypeError, ValueError):
            continue
        if expires_at > now_ts:
            active[agent] = detail
    return active


def record_cooldown(cooldowns_path, agent, reason, reset_at, source_task, source_run_id, default_cooldown_seconds):
    """Record (or extend) a cross-task cooldown for ``agent``. Merge is
    extend-only: the resulting expiry is the max of any existing unexpired
    window and the one computed from this call, so a later short report
    can never shorten a longer window recorded moments earlier. Never
    raises; returns False on any failure."""
    try:
        cooldowns_path = Path(cooldowns_path)
        cooldowns_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = _cooldown_lock_path(cooldowns_path)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                data = {}
                if cooldowns_path.exists():
                    try:
                        with open(str(cooldowns_path), "r", encoding="utf-8") as handle:
                            loaded = json.load(handle)
                        if isinstance(loaded, dict):
                            data = loaded
                    except Exception:
                        data = {}
                computed_expires_at = _compute_expires_at(reset_at, default_cooldown_seconds)
                existing = data.get(agent) if isinstance(data.get(agent), dict) else {}
                try:
                    existing_expires_at = float(existing.get("expires_at"))
                except (TypeError, ValueError):
                    existing_expires_at = None
                expires_at = max(existing_expires_at, computed_expires_at) if existing_expires_at is not None else computed_expires_at
                data[agent] = {
                    "reason": reason,
                    "reset_at": reset_at,
                    "recorded_at": _now_iso(),
                    "expires_at": expires_at,
                    "source_task": source_task,
                    "source_run_id": source_run_id,
                }
                tmp = cooldowns_path.parent / (cooldowns_path.name + ".tmp.%d" % os.getpid())
                with open(str(tmp), "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(str(tmp), str(cooldowns_path))
                return True
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Layer 3: quality outcomes
# ---------------------------------------------------------------------------


def build_outcome_entry(task, run_id, stage, agent, model, pass_number, accepted, classification):
    return {
        "schema_version": SCHEMA_VERSION,
        "task": task,
        "run_id": run_id,
        "stage": stage,
        "agent": agent,
        "model": model,
        "pass_number": pass_number,
        "accepted": accepted,
        "classification": classification,
        "recorded_at": _now_iso(),
    }
