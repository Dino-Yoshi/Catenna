"""Prompt rendering for real pipeline stages."""

from __future__ import print_function

import os

from .artifacts import CONTRACTS
from .state import orchestrator_dir


COMPAT_PROMPTS = {
    "02": ".prompt_stage2_spec.txt",
    "03": ".prompt_stage3_audit.txt",
    "04": ".prompt_stage4_final_brief.txt",
    "04_gate": ".prompt_stage4_audit.txt",
    "05": ".prompt_stage5_implement.txt",
    "07": ".prompt_stage7_review.txt",
    "overseer": ".prompt_implementation_handoff.txt",
}


def render_prompt(task_dir, task, stage_key, pass_number=1, config=None):
    prompt = prompt_text(task_dir, task, stage_key, config=config)
    prompt_dir = orchestrator_dir(task_dir) / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / ("%s-pass-%s.txt" % (stage_key, pass_number))
    path.write_text(prompt, encoding="utf-8")
    compat = task_dir / COMPAT_PROMPTS[stage_key]
    compat.write_text(prompt, encoding="utf-8")
    return path


def prompt_text(task_dir, task, stage_key, config=None):
    paths = stage_paths(task_dir)
    if stage_key == "02":
        return lines(
            "Stage 2 of the multi-agent pipeline.",
            "",
            "Read:",
            "- " + paths["00"],
            "- " + paths["01"],
            "- AGENTS.md and docs/REPO_CONTEXT.md if present.",
            "- The current repository only where needed to ground file/class/API claims.",
            "",
            "Repository-analysis budget:",
            "- Read AGENTS.md and docs/REPO_CONTEXT.md first.",
            "- Treat those files as orientation context, not as the task contract.",
            "- Read the current task Stage 0 and Stage 1 artifacts.",
            "- Inspect only source files directly relevant to unresolved requirements.",
            "- Prefer targeted rg searches over broad directory traversal.",
            "- Stop investigating once the specification can identify affected systems, risks, implementation boundaries, acceptance criteria, and verification steps.",
            "",
            "Task:",
            "Convert the Stage 1 requirements/design packet into a structured technical specification.",
            "",
            "Requirements:",
            "- Resolve ambiguity where the repository provides a reliable answer.",
            "- Explicitly list remaining unknowns.",
            "- Separate must-haves from nice-to-haves.",
            "- Define affected systems/files/classes if inferable from the repository.",
            "- Define compatibility and implementation constraints.",
            "- Define acceptance criteria and verification steps.",
            "- Keep the task narrow.",
            "- Do not edit source code or task artifacts.",
            "",
            "Return only the completed Markdown document. The controller records the artifact atomically.",
            "",
            "Output format:",
            CONTRACTS["02"].heading,
            "",
            *section_lines("02")
        )
    if stage_key == "03":
        return lines(
            "Stage 3 of the multi-agent pipeline.",
            "",
            "Read:",
            "- " + paths["00"],
            "- " + paths["01"],
            "- " + paths["02"],
            "- AGENTS.md and repository context/instructions if present.",
            "- The current repository.",
            "",
            "Repository-analysis budget:",
            "- Read repository instructions first.",
            "- Prefer targeted rg searches over broad directory traversal.",
            "- Inspect only files needed to verify the specification's claims, risks, implementation boundaries, and verification steps.",
            "- Stop investigating once the audit can be grounded in concrete repository evidence.",
            "",
            "Task:",
            "Audit the technical specification against the request and repository.",
            "",
            "Reviewer stance:",
            "- Be adversarial and evidence-driven.",
            "- Do not rewrite the specification.",
            "- Do not edit source code or task artifacts.",
            "",
            "Return only the completed Markdown audit. The controller records the artifact atomically.",
            "",
            "Output format:",
            CONTRACTS["03"].heading,
            "",
            *section_lines("03"),
            "",
            'Wrap every blocking_issues, nonblocking_issues, and required_revision_targets list item in double quotes, including items that look safe unquoted.',
            "End with this exact YAML gate shape:",
            "```yaml",
            "ready_for_implementation: false",
            "blocking_issues: []",
            "nonblocking_issues: []",
            "required_revision_targets: []",
            "```",
            "",
            "Use ready_for_implementation: true only if the specification is concrete, internally consistent, repository-compatible, and safe to convert into a final implementation brief."
        )
    if stage_key == "04":
        return lines(
            "Stage 4 of the multi-agent pipeline.",
            "",
            "Read:",
            "- " + paths["00"],
            "- " + paths["01"],
            "- " + paths["02"],
            "- " + paths["03"],
            "- " + paths["04"] + ", if it already exists from an earlier bounded gate pass.",
            "- " + paths["04_gate"] + ", if it exists; treat it as required revision feedback.",
            "- AGENTS.md and repository context/instructions if present.",
            "- The current repository where needed to make the brief concrete.",
            "",
            "Repository-analysis budget:",
            "- Read repository instructions first.",
            "- Prefer targeted rg searches over broad directory traversal.",
            "- Inspect only files needed to resolve audit feedback and make affected systems, constraints, edge cases, and verification commands concrete.",
            "- Stop investigating once the final brief is actionable and scoped to this task.",
            "",
            "Task:",
            "Produce or revise the final implementation brief for the current task only.",
            "",
            "Critical scope rules:",
            "- The implementation slice is defined only by the current task artifacts.",
            "- Do not import scope, assumptions, files, or acceptance criteria from another task.",
            "- Resolve audit blockers when the available evidence supports a safe resolution.",
            "- If a blocker cannot be resolved, narrow to diagnostic/stop-and-report behavior.",
            "- Address every required revision in the latest final-brief audit when one exists.",
            "- Do not edit source code or task artifacts.",
            "",
            "Return only the completed Markdown brief. The legacy filename 04_final_codex_brief.md is retained for repository compatibility.",
            "",
            "Output format:",
            CONTRACTS["04"].heading,
            "",
            *section_lines("04"),
            "",
            "Also include these additional sections when relevant to the task",
            "(not structurally required, but expected for most briefs):",
            "## Compatibility constraints",
            "## Step-by-step implementation plan",
            "## Verification commands",
            "## Manual test checklist",
            "## Out-of-scope work",
            "## Stop conditions"
        )
    if stage_key == "04_gate":
        return lines(
            "Audit the Stage 4 final implementation brief before implementation.",
            "",
            "Read:",
            "- " + paths["00"],
            "- " + paths["01"],
            "- " + paths["02"],
            "- " + paths["03"],
            "- " + paths["04"],
            "- AGENTS.md and repository context/instructions if present.",
            "- The current repository.",
            "",
            "Repository-analysis budget:",
            "- Read repository instructions first.",
            "- Prefer targeted rg searches over broad directory traversal.",
            "- Inspect only files needed to verify the final brief's compatibility, scope, and actionability.",
            "- Stop investigating once the gate decision is supported by concrete repository evidence.",
            "",
            "Task:",
            "Determine only whether 04_final_codex_brief.md is complete, correct, scoped, repository-compatible, and actionable for an implementation agent.",
            "",
            "Reviewer stance:",
            "- Be an adversarial implementation-gate reviewer.",
            "- Repository source inspection is allowed for technical compatibility and API/context evidence.",
            "- Existing implementation files may be used only as repository/API evidence, not as a reason to reject an otherwise valid brief as already implemented.",
            "- Do not reject the brief solely because of the invoking controller lock, current run PID, transient .orchestrator/state.json, stale task-local status.json, previous Stage 5 or later artifacts, implementation files already present, or provider-run metadata from the active controller run.",
            "- Deterministic runtime safety checks are owned by the controller, not this semantic audit.",
            "- Do not implement anything.",
            "- Do not rewrite the brief.",
            "- Do not edit source code or task artifacts.",
            "",
                       "Return only the completed Markdown audit. The controller records the artifact atomically.",
                       "",
                       "Strict output requirements:",
                       "- Do not include acknowledgements, analysis, transition text, or commentary before the required heading.",
                       "- The first non-whitespace character of the response must be '#'.",
                       "- The first line must be exactly:",
                       CONTRACTS["04_gate"].heading,
                       "- Begin the response immediately with that heading.",
                       "- Do not include any text after the final YAML gate.",
                       "",
                       "Output format:",
                       CONTRACTS["04_gate"].heading,
            "",
            *section_lines("04_gate"),
            "",
            'Wrap every blocking_issues, nonblocking_issues, and required_revision_targets list item in double quotes, including items that look safe unquoted.',
            "End with this exact YAML gate shape:",
            "```yaml",
            "ready_for_implementation: true",
            "blocking_issues: []",
            "nonblocking_issues: []",
            "required_revision_targets: []",
            "```",
            "",
            "Use ready_for_implementation: true only when the brief is aligned, concrete, internally consistent, repository-compatible, and safe to implement."
        )
    if stage_key == "05":
        driven_project_commands = ((config or {}).get("verification", {}) or {}).get("driven_project_commands") or []
        if driven_project_commands:
            verification_line = "- Run the configured verification commands: " + ", ".join(
                "%s (%s)" % (command.get("name"), " ".join(command.get("argv", [])))
                for command in driven_project_commands
            ) + "."
        else:
            verification_line = "- Run the verification commands available in the brief and repository."
        return lines(
            "Stage 5 of the multi-agent pipeline.",
            "",
            "Read:",
            "- " + paths["04"],
            "- " + paths["04_gate"],
            "- AGENTS.md and repository context/instructions if present.",
            "",
            "Task:",
            "Implement the accepted final brief only.",
            "",
            "Hard rules:",
            "- Do not expand scope beyond " + paths["04"] + ".",
            "- Do not implement nice-to-haves unless explicitly required.",
            "- Do not perform unrelated refactors.",
            "- Preserve existing behavior unless the brief explicitly changes it.",
            "- Prefer minimal, maintainable changes compatible with the repository.",
            verification_line,
            "- Stop and report rather than guessing when a stop condition is reached.",
            "",
            "Modify source files as required by the brief.",
            "Do not directly edit pipeline task artifacts.",
            "Return only the completed implementation report as the final response; the controller writes it to the legacy path " + paths["05"] + ".",
            "",
            "Implementation report format:",
            CONTRACTS["05"].heading,
            "",
            *section_lines("05")
        )
    if stage_key == "07":
        return lines(
            "Stage 7 of the multi-agent pipeline.",
            "",
            "Read:",
            "- " + paths["04"],
            "- " + paths["04_gate"],
            "- " + paths["05"],
            "- " + paths["06"],
            "- " + os.path.join(str(task_dir), "05_implementation_manifest.json") + ", for the list of files Stage 5 changed.",
            "- The current repository's git diff for those changed files (use your own tools, e.g. `git diff`).",
            "- AGENTS.md and repository context/instructions if present.",
            "",
            "Task:",
            "Review the resulting implementation against the accepted brief and repository.",
            "",
            "Reviewer stance:",
            "- Be a strict independent reviewer, not the implementer.",
            "- Do not edit source code or task artifacts.",
            "",
            "Review focus:",
            "- Correctness, maintainability, regressions, performance, and compatibility.",
            "- Compliance with the final brief and final gate.",
            "- Accuracy of the implementation report.",
            "- Unresolved issues exposed by the manual test notes.",
            "- Missing tests or verification.",
            "",
            "Return only the completed Markdown review. The controller records the artifact atomically.",
            "",
            "Output format:",
            CONTRACTS["07"].heading,
            "",
            *section_lines("07"),
            "",
            "End with exactly one verdict value on its own line: accept, reject, or needs_followup."
        )
    if stage_key == "overseer":
        return lines(
            "Implementation handoff overseer.",
            "",
            "Read:",
            "- " + paths["04"],
            "- " + paths["04_gate"],
            "- " + paths["05"],
            "- " + os.path.join(str(task_dir), "05_implementation_manifest.json"),
            "- " + os.path.join(str(task_dir), "05_verification_report.md") + ", if present, for actual build/test evidence.",
            "",
            "Task:",
            "Produce a concise implementation handoff for human testing. Do not control stage execution, request repairs, or route to Stage 7/8.",
            "",
            "Return only JSON with this shape:",
            "{",
            '  "route": "manual_test",',
            '  "summary": ["string"],',
            '  "verified": ["string"],',
            '  "needs_human_testing": ["string"],',
            '  "known_limitations": ["string"],',
            '  "next_action": "string"',
            "}",
            "",
            "Allowed routes are manual_test, blocked, and administrator_action."
        )
    raise ValueError("unsupported stage prompt: " + stage_key)


def stage_paths(task_dir):
    result = {}
    for stage_key, contract in CONTRACTS.items():
        result[stage_key] = str(task_dir / contract.filename)
    return result


def section_lines(stage_key):
    return ["## " + section for section in CONTRACTS[stage_key].sections]


def lines(*items):
    return "\n".join(items).rstrip() + "\n"
