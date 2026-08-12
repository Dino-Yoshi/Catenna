"""Configuration for real agent pipeline execution."""

from __future__ import print_function

import json
import os
import re
from pathlib import Path

from .cost_policy import ALLOWED_STAGES

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2 compatibility fallback.
    from collections import Mapping


CONFIG_PATH = Path(".agent-pipeline") / "config" / "orchestrator.json"
DRIVEN_PROJECT_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


DEFAULT_CONFIG = {
    "schema_version": 2,
    "default_safety_mode": "strict",
    "supported_safety_modes": ["strict", "continuity"],
    "stage_attempt_budget": 2,
    "max_gate_passes": 2,
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
    "pricing": {
        "codex": {},
    },
    "cost_control": {
        "enabled": False,
        "quality_aware": False,
        "min_samples": 5,
        "max_retry_rate": 0.2,
        "max_rejection_rate": 0.2,
        "eligible_stages": ["02", "03", "04", "04_gate", "07"],
        "downgrade_candidates": {
            "claude": {"model": "claude-haiku-4-5", "effort": "low"},
            "codex": None,
            "agy": None,
        },
    },
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
    "verification": {
        "driven_project_commands": [],
        "skip_self_check": False,
        "build_implies_compile": False,
    },
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
    for key in ("max_gate_passes", "stage_attempt_budget"):
        if not isinstance(config.get(key), int) or int(config.get(key)) < 1:
            raise ConfigError(key + " must be a positive integer")
    for stage in ("02", "03", "04", "04_gate", "05", "07", "overseer"):
        if stage not in config.get("roles", {}):
            raise ConfigError("missing role config for " + stage)
    validate_role_agent_references(config)
    for agent in ("codex", "claude", "agy"):
        detail = config.get("agents", {}).get(agent)
        if not detail:
            raise ConfigError("missing agent config for " + agent)
        if detail.get("enabled") and not detail.get("command"):
            raise ConfigError("missing command for " + agent)
    usage_ledger = config.get("usage_ledger", {})
    if not isinstance(usage_ledger, dict) or not isinstance(usage_ledger.get("enabled"), bool):
        raise ConfigError("usage_ledger.enabled must be a boolean")
    validate_pricing_config(config)
    validate_cost_control_config(config)
    cooldowns = config.get("cross_task_cooldowns", {})
    if not isinstance(cooldowns, dict) or not isinstance(cooldowns.get("enabled"), bool):
        raise ConfigError("cross_task_cooldowns.enabled must be a boolean")
    if not isinstance(cooldowns.get("default_cooldown_seconds"), int) or int(cooldowns.get("default_cooldown_seconds")) < 1:
        raise ConfigError("cross_task_cooldowns.default_cooldown_seconds must be a positive integer")
    reasoning_capture = config.get("reasoning_capture", {})
    if not isinstance(reasoning_capture, dict) or not isinstance(reasoning_capture.get("enabled"), bool):
        raise ConfigError("reasoning_capture.enabled must be a boolean")
    if not isinstance(config.get("enable_auto_verified"), bool):
        raise ConfigError("enable_auto_verified must be a boolean")
    if not isinstance(config.get("allow_degraded_same_agent_review"), bool):
        raise ConfigError("allow_degraded_same_agent_review must be a boolean")
    validate_verification_config(config)
    timeout_seconds = config.get("timeout_seconds")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ConfigError("timeout_seconds must be a positive integer")
    turn_budgets = config.get("turn_budgets", {})
    if not isinstance(turn_budgets, Mapping):
        raise ConfigError("turn_budgets must be a mapping")
    for stage, budget in turn_budgets.items():
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ConfigError("turn_budgets.%s must be a positive integer" % stage)
    return True


def validate_pricing_config(config):
    pricing = config.get("pricing", {})
    if not isinstance(pricing, Mapping):
        raise ConfigError("pricing must be a mapping")
    codex = pricing.get("codex", {})
    if not isinstance(codex, Mapping):
        raise ConfigError("pricing.codex must be a mapping")
    required_rates = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
    for model, rates in codex.items():
        if not isinstance(model, str) or not model:
            raise ConfigError("pricing.codex model keys must be non-empty strings")
        if not isinstance(rates, Mapping):
            raise ConfigError("pricing.codex.%s must be a mapping" % model)
        for rate_name in required_rates:
            if rate_name not in rates:
                raise ConfigError("pricing.codex.%s.%s is required" % (model, rate_name))
            value = rates.get(rate_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError("pricing.codex.%s.%s must be a non-negative number" % (model, rate_name))
            if value < 0:
                raise ConfigError("pricing.codex.%s.%s must be a non-negative number" % (model, rate_name))


def validate_verification_config(config):
    verification = config.get("verification", {})
    if not isinstance(verification, Mapping):
        raise ConfigError("verification must be a mapping")
    for field in ("skip_self_check", "build_implies_compile"):
        if field in verification and not isinstance(verification.get(field), bool):
            raise ConfigError("verification.%s must be a boolean" % field)
    commands = verification.get("driven_project_commands", [])
    if not isinstance(commands, list):
        raise ConfigError("verification.driven_project_commands must be a list")
    names = set()
    for index, command in enumerate(commands):
        prefix = "verification.driven_project_commands[%d]" % index
        if not isinstance(command, Mapping):
            raise ConfigError(prefix + " must be a mapping")
        name = command.get("name")
        if not isinstance(name, str) or not name or not DRIVEN_PROJECT_COMMAND_NAME_RE.match(name):
            raise ConfigError(prefix + ".name must be a non-empty string matching ^[A-Za-z0-9_.-]+$")
        if name in names:
            raise ConfigError("verification.driven_project_commands name is duplicated: " + name)
        names.add(name)
        argv = command.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
            raise ConfigError(prefix + ".argv must be a non-empty list of strings")
        if "timeout_seconds" in command:
            timeout = command.get("timeout_seconds")
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
                raise ConfigError(prefix + ".timeout_seconds must be a positive integer")


def validate_cost_control_config(config):
    cost_control = config.get("cost_control", {})
    if not isinstance(cost_control, Mapping):
        raise ConfigError("cost_control must be a mapping")
    if not isinstance(cost_control.get("enabled"), bool):
        raise ConfigError("cost_control.enabled must be a boolean")
    if not isinstance(cost_control.get("quality_aware"), bool):
        raise ConfigError("cost_control.quality_aware must be a boolean")
    min_samples = cost_control.get("min_samples")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ConfigError("cost_control.min_samples must be a positive integer")
    max_retry_rate = cost_control.get("max_retry_rate")
    if isinstance(max_retry_rate, bool) or not isinstance(max_retry_rate, (int, float)) or max_retry_rate < 0 or max_retry_rate > 1:
        raise ConfigError("cost_control.max_retry_rate must be a number in [0, 1]")
    max_rejection_rate = cost_control.get("max_rejection_rate")
    if isinstance(max_rejection_rate, bool) or not isinstance(max_rejection_rate, (int, float)) or max_rejection_rate < 0 or max_rejection_rate > 1:
        raise ConfigError("cost_control.max_rejection_rate must be a number in [0, 1]")
    eligible_stages = cost_control.get("eligible_stages")
    if not isinstance(eligible_stages, list) or not all(isinstance(stage, str) for stage in eligible_stages):
        raise ConfigError("cost_control.eligible_stages must be a list of strings")
    roles = config.get("roles", {})
    for stage in eligible_stages:
        if stage not in roles:
            raise ConfigError("cost_control eligible stage %s is not configured in roles" % stage)
        if stage not in ALLOWED_STAGES:
            raise ConfigError("cost_control eligible stage %s is not supported" % stage)
    candidates = cost_control.get("downgrade_candidates")
    if not isinstance(candidates, Mapping):
        raise ConfigError("cost_control.downgrade_candidates must be a mapping")
    for agent, candidate in candidates.items():
        if candidate is None:
            continue
        if not isinstance(candidate, Mapping):
            raise ConfigError("cost_control.downgrade_candidates.%s must be a mapping or null" % agent)
        for field in ("model", "effort"):
            if field in candidate and not isinstance(candidate.get(field), str):
                raise ConfigError("cost_control.downgrade_candidates.%s.%s must be a string" % (agent, field))


def validate_role_agent_references(config):
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        raise ConfigError("agents must be a mapping")
    for role_name, role in config.get("roles", {}).items():
        if not isinstance(role, dict):
            raise ConfigError("role %s must be a mapping" % role_name)
        for field in ("model_override", "effort_override"):
            if field in role and not isinstance(role.get(field), str):
                raise ConfigError("role %s %s must be a string" % (role_name, field))
        primary = role.get("primary")
        if primary not in agents:
            raise ConfigError("role %s primary references unknown agent %r" % (role_name, primary))
        fallbacks = role.get("fallbacks", [])
        if not isinstance(fallbacks, list):
            raise ConfigError("role %s fallbacks must be a list" % role_name)
        for fallback in fallbacks:
            if fallback not in agents:
                raise ConfigError("role %s fallback references unknown agent %r" % (role_name, fallback))


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
