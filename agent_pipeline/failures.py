"""Failure names and exit codes used by the mock orchestrator."""

EXIT_SUCCESS = 0
EXIT_VALIDATION = 1
EXIT_BAD_INPUT = 2
EXIT_BLOCKED = 3
EXIT_LOCKED = 4
EXIT_INTERRUPTED = 130

VALID_STATES = {
    "ready",
    "running",
    "awaiting_retry_approval",
    "awaiting_human_test",
    "awaiting_final_decision",
    "blocked",
    "failed",
    "complete",
}

BANNED_COMMAND_WORDS = {
    "codex",
    "claude",
    "agy",
    "gradle",
    "./gradlew",
    "java",
    "minecraft",
    "curl",
    "wget",
    "ssh",
    "bash",
    "sh",
    "python",
    "python3",
}

FAILURE_CLASS_MALFORMED_ARTIFACT = "malformed_artifact"
FAILURE_CLASS_EMPTY_OUTPUT = "empty_output"
FAILURE_CLASS_SOURCE_FAILURE = "source_failure"
FAILURE_CLASS_USAGE_LIMIT = "usage_limit"
FAILURE_CLASS_RATE_LIMIT = "rate_limit"
FAILURE_CLASS_MAX_TURNS = "max_turns"
FAILURE_CLASS_UNKNOWN_FAILURE = "unknown_failure"
FAILURE_CLASS_TIMEOUT = "timeout"
FAILURE_CLASS_PERMISSION_ERROR = "permission_error"
FAILURE_CLASS_SANDBOX_ENVIRONMENT = "sandbox_environment"
FAILURE_CLASS_STAGE5_AMBIGUITY = "stage5_ambiguity"
FAILURE_CLASS_GATE_REJECTED = "gate_rejected"
FAILURE_CLASS_GATE_PASS_LIMIT_EXHAUSTED = "gate_pass_limit_exhausted"
FAILURE_CLASS_PROCESS_INTERRUPTED = "process_interrupted"
FAILURE_CLASS_MALFORMED_OVERSEER = "malformed_overseer"
