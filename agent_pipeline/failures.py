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
