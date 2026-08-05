"""Configuration for real agent pipeline execution."""

from __future__ import print_function

import json
import os
from pathlib import Path


CONFIG_PATH = Path(".agent-pipeline") / "config" / "orchestrator.json"


DEFAULT_CONFIG = {
    "schema_version": 2,
    "default_safety_mode": "strict",
    "supported_safety_modes": ["strict", "continuity"],
    "stage_attempt_budget": 2,
    "temporary_full_retry_budget": 1,
    "max_gate_passes": 2,
    "hard_max_gate_passes": 3,
    "timeout_seconds": 3600,
    "roles": {
        "02": {"primary": "codex", "fallbacks": ["claude", "agy"]},
        "03": {"primary": "codex", "fallbacks": ["claude", "agy"]},
        "04": {"primary": "codex", "fallbacks": ["claude", "agy"]},
        "04_gate": {"primary": "claude", "fallbacks": ["agy"], "independent_from": "04"},
        "05": {"primary": "codex", "fallbacks": []},
        "07": {"primary": "claude", "fallbacks": ["agy"], "independent_from": "05"},
        "overseer": {"primary": "codex", "fallbacks": ["claude", "agy"]},
    },
    "enable_auto_verified": True,
    "usage_ledger": {"enabled": True},
    "cross_task_cooldowns": {"enabled": True, "default_cooldown_seconds": 900},
    "reasoning_capture": {"enabled": True},
    "agents": {
        "codex": {
            "command": "codex",
            "model": None,
            "read_args": [],
            "write_args": [],
            "overseer_args": [],
            "workspace_write": True,
            "enabled": True,
        },
        "claude": {
            "command": "claude",
            "model": None,
            "read_effort": "medium",
            "write_effort": "medium",
            "read_args": [],
            "write_args": [],
            "workspace_write": False,
            "enabled": True,
        },
        "agy": {
            "command": "agy",
            "model": None,
            "common_args": [],
            "read_args": ["--mode", "plan"],
            "write_args": ["--mode", "accept-edits"],
            "prompt_mode": "auto",
            "stdin_mode_allowed": False,
            "workspace_write": False,
            "enabled": True,
        },
    },
    "turn_budgets": {
        "02": 20,
        "03": 20,
        "04": 20,
        "04_gate": 20,
        "05": 40,
        "07": 20,
        "overseer": 10,
    },
    "allow_degraded_same_agent_review": False,
}


class ConfigError(Exception):
    pass


def load_config(path=None):
    path = path or CONFIG_PATH
    config = deep_copy(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(str(path), "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except Exception as exc:
            raise ConfigError("orchestrator config is unreadable: " + str(exc))
        merge_dict(config, loaded)
    apply_env_overrides(config)
    validate_config(config)
    return config


def apply_env_overrides(config):
    env_commands = {
        "codex": "CODEX_CMD",
        "claude": "CLAUDE_CMD",
        "agy": "AGY_CMD",
    }
    for agent, var in env_commands.items():
        if os.environ.get(var):
            config["agents"][agent]["command"] = os.environ[var]


def validate_config(config):
    if config.get("schema_version") not in (1, 2):
        raise ConfigError("unsupported orchestrator config schema_version")
    if config.get("default_safety_mode") not in config.get("supported_safety_modes", []):
        raise ConfigError("default_safety_mode is not supported")
    for key in ("max_gate_passes", "hard_max_gate_passes", "stage_attempt_budget", "temporary_full_retry_budget"):
        if not isinstance(config.get(key), int) or int(config.get(key)) < 1:
            raise ConfigError(key + " must be a positive integer")
    if int(config["max_gate_passes"]) > int(config["hard_max_gate_passes"]):
        raise ConfigError("max_gate_passes cannot exceed hard_max_gate_passes")
    for stage in ("02", "03", "04", "04_gate", "05", "07", "overseer"):
        if stage not in config.get("roles", {}):
            raise ConfigError("missing role config for " + stage)
    for agent in ("codex", "claude", "agy"):
        detail = config.get("agents", {}).get(agent)
        if not detail:
            raise ConfigError("missing agent config for " + agent)
        if detail.get("enabled") and not detail.get("command"):
            raise ConfigError("missing command for " + agent)
    usage_ledger = config.get("usage_ledger", {})
    if not isinstance(usage_ledger, dict) or not isinstance(usage_ledger.get("enabled"), bool):
        raise ConfigError("usage_ledger.enabled must be a boolean")
    cooldowns = config.get("cross_task_cooldowns", {})
    if not isinstance(cooldowns, dict) or not isinstance(cooldowns.get("enabled"), bool):
        raise ConfigError("cross_task_cooldowns.enabled must be a boolean")
    if not isinstance(cooldowns.get("default_cooldown_seconds"), int) or int(cooldowns.get("default_cooldown_seconds")) < 1:
        raise ConfigError("cross_task_cooldowns.default_cooldown_seconds must be a positive integer")
    reasoning_capture = config.get("reasoning_capture", {})
    if not isinstance(reasoning_capture, dict) or not isinstance(reasoning_capture.get("enabled"), bool):
        raise ConfigError("reasoning_capture.enabled must be a boolean")
    return True


def configured_candidates(config, role_key):
    role = config["roles"][role_key]
    candidates = [role["primary"]] + list(role.get("fallbacks", []))
    result = []
    seen = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def agent_config(config, agent):
    return config.get("agents", {}).get(agent, {})


def deep_copy(value):
    return json.loads(json.dumps(value))


def merge_dict(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_dict(target[key], value)
        else:
            target[key] = value
