"""Experiment t9-env-reviewer: format reviewer gating every required artifact.

The single research agent writes each required table under ``./work`` and calls
``submit_artifact(artifact_id, path, note)``. A reviewer built on the run's own model
then reads only the ReAct system prompt, the task prompt (which carries every
artifact's specification), the specification of the submitted artifact and a computed
format summary of the file (shape, header, delimiter, head and tail, row-key checks and
a random sample of row identifiers and columns). It judges format only and either
accepts, in which case the file is copied to its required path under ``./outputs``, or
writes feedback for the agent. The agent gets two chances to convince the reviewer
(with fixes or with a ``note``); the third submission of an artifact is accepted
without review. The reviewer never sees ground truth, grader output or reference
results (none of which exist in the sandbox). Every review is appended to
``work/artifact_review.md``.

``METHODS.md`` is not reviewed: the agent writes it directly to ``./outputs``. The
sandbox runs as root, so direct writes of tables to ``./outputs`` cannot be blocked;
the final ``submit()`` instead lists artifacts that were never accepted or were changed
after acceptance, once, and the next ``submit()`` call is accepted.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from inspect_ai.agent import AgentPrompt
from inspect_ai.model import ChatMessage, ChatMessageSystem, ChatMessageUser, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolError, tool
from inspect_ai.util import sandbox, store

from .models import RuntimeCase
from .review_memory import record_accepted, related_accepted

_HERE = Path(__file__).parent
SUMMARY_SCRIPT_PATH = _HERE / "sandbox_scripts" / "artifact_summary.py"
SANDBOX_SUMMARY_PATH = "/tmp/bixbench3_artifact_summary.py"
SANDBOX_WORKSPACE = "/workspace"
SANDBOX_WORK = "/workspace/work"
SANDBOX_OUTPUTS = "/outputs"
SANDBOX_TIMEOUT_SECONDS = 300

MAX_REJECTIONS = 2
PROMPT_CHARS = 60_000

PROMPT_KEY = "review_task_prompt"
OUTPUTS_KEY = "review_expected_outputs"
LOG_KEY = "review_log"
APPROVED_KEY = "review_approved"
FINAL_CHECKED_KEY = "review_final_checked"

ACCEPT = "VERDICT: ACCEPT"
REVISE = "VERDICT: REVISE"

REVIEW_NOTICE = f"""## Artifact Review

Every required output table goes through a format reviewer before it reaches `./outputs`.

- Write each required table under `./work`, then call \
`submit_artifact(artifact_id, path, note)` with its artifact ID from "Required Outputs" \
and the file path.
- The reviewer sees only this task description and a computed summary of the file \
(shape, header, delimiter, first and last rows, row-key checks, a random sample of row \
identifiers and columns). It checks format only: file format and delimiter, exact \
column names and order of the row key, number of rows and columns consistent with the \
specification, and identifier formats. It has no reference answers.
- If it accepts, the harness copies the file to the artifact's required path under \
`./outputs`. Otherwise it returns written feedback: fix the file and submit again, or \
submit it unchanged and explain in `note` why it is already right.
- The reviewer gets {MAX_REJECTIONS} chances to reject an artifact; your submission \
number {MAX_REJECTIONS + 1} of the same artifact is accepted without review.
- Do not write required tables directly into `./outputs`; write `./outputs/METHODS.md` \
directly (it is not reviewed). Resubmit an artifact whenever you change it.
- The reviewer also compares each table with the most similar tables of this run it has \
already accepted: the same kind of column (identifiers, symbols, contrast or direction \
labels) must use the same format across artifacts.

Reviews are logged in `./work/artifact_review.md`.
"""

REVIEWER_SYSTEM = f"""You are the format reviewer for a bioinformatics analysis agent. \
The agent has submitted one required output table. You see the agent's system prompt, \
the full task description, the specification of the submitted artifact and a computed \
summary of the file. You cannot run anything and you have no reference answers.

Judge format only, against the specification:
- file format and delimiter, encoding, blank or ragged lines;
- header: required column names present with the exact spelling, the row-key column first, \
no stray or duplicate columns;
- rows: one row per entity as specified, unique and non-empty row keys, a number of rows \
and columns consistent with the specification (for example one count column per listed \
replicate, or one row per annotated gene);
- identifiers: row keys and sampled names in the format the specification and its example \
identifiers describe (prefixes, version suffixes, symbols), and sampled names and values \
that look coherent with the column they are in;
- consistency: when summaries of artifacts you already accepted in this run are given, the same \
kind of column (gene identifiers with or without version suffix, symbols, contrast, direction \
or gene-set labels, column naming) must use the same format as in those artifacts. Flag a \
format inconsistency; never require identical values, identical genes or identical row counts.

Do not judge scientific results, expected values, gene lists or exact row counts the \
specification does not determine; never guess them. If the agent's note answers an \
earlier objection convincingly, accept.

Answer with exactly "{ACCEPT}" or "{REVISE}" on the first line. With {REVISE}, follow with \
at most 8 bullet points the agent can act on, most important first.
"""


def append_review_notice(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + REVIEW_NOTICE


def react_system_prompt() -> str:
    defaults = AgentPrompt()
    return f"{defaults.assistant_prompt}\n{(defaults.submit_prompt or '').format(submit='submit')}".strip()


def artifact_spec(prompt: str, artifact_id: str) -> str:
    match = re.search(
        rf"^### `{re.escape(artifact_id)}`\s*$(.*?)(?=^#{{2,3}} |\Z)",
        prompt,
        flags=re.MULTILINE | re.DOTALL,
    )
    return f"### `{artifact_id}`{match.group(1).rstrip()}" if match else ""


def reviewer_messages(
    *,
    task_prompt: str,
    artifact: dict[str, Any],
    summary: str,
    note: str,
    history: list[dict[str, Any]],
    related: str = "",
) -> list[ChatMessage]:
    if len(task_prompt) > PROMPT_CHARS:
        task_prompt = task_prompt[:PROMPT_CHARS] + "\n<task description truncated>"
    spec = artifact_spec(task_prompt, artifact["artifact_id"]) or (
        f"Path: `{artifact['path']}`; format: `{artifact.get('format')}`; "
        f"row key: `{artifact.get('row_key')}`"
    )
    earlier = "\n\n".join(
        f"Submission {entry['attempt']}: {entry['verdict']}\n{entry['feedback']}"
        for entry in history
        if entry.get("feedback")
    )
    parts = [
        f"# Agent system prompt\n\n{react_system_prompt()}",
        f"# Task description\n\n{task_prompt}",
        f"# Submitted artifact specification\n\n{spec}",
        f"# Computed file summary\n\n{summary}",
    ]
    if related:
        parts.append(
            "# Artifacts of this run you already accepted (most similar first; for format consistency only)"
            f"\n\n{related}"
        )
    if earlier:
        parts.append(f"# Your earlier feedback on this artifact\n\n{earlier}")
    parts.append(f"# Agent note\n\n{note.strip() or '(none)'}")
    return [ChatMessageSystem(content=REVIEWER_SYSTEM), ChatMessageUser(content="\n\n".join(parts))]


def parse_verdict(text: str) -> bool:
    first = text.strip().splitlines()[0].strip().strip("*`").upper() if text.strip() else ""
    return first.startswith(ACCEPT)


def review_seed(artifact_id: str, attempt: int) -> int:
    return zlib.crc32(f"{artifact_id}:{attempt}".encode())


def resolve_sandbox_path(path: str) -> str:
    candidate = PurePosixPath(path.strip())
    if not candidate.is_absolute():
        candidate = PurePosixPath(SANDBOX_WORKSPACE) / candidate
    return str(candidate)


def sandbox_output_path(artifact: dict[str, Any]) -> str:
    path = PurePosixPath(artifact["path"])
    if path.parts and path.parts[0] == "outputs":
        path = PurePosixPath(*path.parts[1:])
    return str(PurePosixPath(SANDBOX_OUTPUTS) / path)


def log_entry(artifact_id: str, attempt: int, verdict: str, path: str, text: str) -> str:
    return (
        f"## {artifact_id} (submission {attempt}/{MAX_REJECTIONS + 1}): {verdict}\n\n"
        f"File: `{path}`\n\n{text.strip()}\n\n"
    )


class ArtifactReviewer:
    """Per-artifact review gate with injectable I/O (sandbox, model, log file)."""

    def __init__(
        self,
        *,
        state: Any,
        summarize: Callable[[str, dict[str, Any], int], Awaitable[str]],
        ask_model: Callable[[list[ChatMessage]], Awaitable[str]],
        copy: Callable[[str, str], Awaitable[str]],
        file_hash: Callable[[str], Awaitable[str | None]],
        append_log: Callable[[str], Awaitable[None]],
        related: Callable[[dict[str, Any], str], Awaitable[tuple[str, list[str]]]] | None = None,
        record: Callable[[dict[str, Any], str, str, int], Awaitable[None]] | None = None,
    ) -> None:
        self.state = state
        self.related = related  # run memory: most similar accepted artifacts (past.dev)
        self.record = record  # run memory: store an accepted artifact's summary
        self.summarize = summarize
        self.ask_model = ask_model
        self.copy = copy
        self.file_hash = file_hash
        self.append_log = append_log

    def _artifacts(self) -> dict[str, dict[str, Any]]:
        return {item["artifact_id"]: item for item in self.state.get(OUTPUTS_KEY, [])}

    async def submit(self, artifact_id: str, path: str, note: str = "") -> str:
        artifacts = self._artifacts()
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise ToolError(f"Unknown artifact_id '{artifact_id}'. Valid IDs: {', '.join(artifacts)}")
        source = resolve_sandbox_path(path)
        if await self.file_hash(source) is None:
            raise ToolError(f"File not found in the sandbox: {source}")
        log = list(self.state.get(LOG_KEY, []))
        history = [entry for entry in log if entry["artifact_id"] == artifact_id]
        attempt = len(history) + 1
        feedback = ""
        summary: str | None = None
        related_ids: list[str] = []
        if attempt > MAX_REJECTIONS:
            verdict, accepted = "accepted without review", True
        else:
            try:
                summary = await self.summarize(source, artifact, review_seed(artifact_id, attempt))
                related_text = ""
                if self.related is not None:
                    try:
                        related_text, related_ids = await self.related(artifact, summary)
                    except Exception:  # memory must never block a review
                        related_text, related_ids = "", []
                answer = (await self.ask_model(reviewer_messages(
                    task_prompt=self.state.get(PROMPT_KEY, ""),
                    artifact=artifact,
                    summary=summary,
                    note=note,
                    history=history,
                    related=related_text,
                ))).strip()
                accepted = parse_verdict(answer)
                verdict = "accepted" if accepted else "rejected"
                feedback = answer
            except Exception as error:  # the reviewer must never cost the run an artifact
                verdict, accepted = f"accepted, reviewer unavailable ({type(error).__name__})", True
        destination = sandbox_output_path(artifact)
        digest = await self.copy(source, destination) if accepted else None
        if accepted and self.record is not None:
            try:  # run memory: later reviews compare against this accepted table
                if summary is None:
                    summary = await self.summarize(source, artifact, review_seed(artifact_id, attempt))
                await self.record(artifact, summary, verdict, attempt)
            except Exception:
                pass
        self.state[LOG_KEY] = [*log, {
            "artifact_id": artifact_id, "attempt": attempt, "verdict": verdict,
            "path": source, "feedback": feedback, "note": note, "related": related_ids,
        }]
        if digest is not None:
            self.state[APPROVED_KEY] = {**self.state.get(APPROVED_KEY, {}), artifact_id: digest}
        try:
            await self.append_log(log_entry(
                artifact_id, attempt, verdict, source,
                "\n\n".join(text for text in (f"Agent note: {note.strip()}" if note.strip() else "", feedback) if text),
            ))
        except Exception:
            pass
        if accepted:
            return f"Artifact '{artifact_id}' {verdict} (submission {attempt}) and copied to {destination}."
        return (
            f"Artifact '{artifact_id}' not accepted (submission {attempt}); it was not copied "
            f"to ./outputs. Reviewer feedback:\n\n{feedback}\n\nFix the file and resubmit, or "
            "resubmit it unchanged and explain in `note` why it is right. "
            f"Submission {MAX_REJECTIONS + 1} of this artifact is accepted without review."
        )

    async def final_issues(self) -> list[str]:
        approved = self.state.get(APPROVED_KEY, {})
        issues = []
        for artifact_id, artifact in self._artifacts().items():
            current = await self.file_hash(sandbox_output_path(artifact))
            if artifact_id not in approved:
                state = "present in ./outputs but never submitted" if current else "missing"
                issues.append(f"- {artifact_id}: {state}")
            elif current != approved[artifact_id]:
                issues.append(f"- {artifact_id}: changed in ./outputs after it was accepted")
        return issues


async def _summarize(path: str, artifact: dict[str, Any], seed: int) -> str:
    environment = sandbox()
    await environment.write_file(SANDBOX_SUMMARY_PATH, SUMMARY_SCRIPT_PATH.read_text(encoding="utf-8"))
    command = ["python3", SANDBOX_SUMMARY_PATH, "--path", path, "--seed", str(seed)]
    if artifact.get("format"):
        command += ["--format", str(artifact["format"])]
    if artifact.get("row_key"):
        command += ["--row-key", str(artifact["row_key"])]
    result = await environment.exec(command, timeout=SANDBOX_TIMEOUT_SECONDS)
    if not result.success:
        raise RuntimeError((result.stderr or result.stdout)[-500:])
    return result.stdout


async def _ask_model(messages: list[ChatMessage]) -> str:
    output = await get_model().generate(messages)
    return output.completion


async def _copy(source: str, destination: str) -> str:
    result = await sandbox().exec(
        ["sh", "-c", 'mkdir -p "$(dirname "$2")" && cp -- "$1" "$2" && sha256sum -- "$2"', "sh", source, destination],
        timeout=SANDBOX_TIMEOUT_SECONDS,
    )
    if not result.success:
        raise ToolError(f"Could not copy {source} to {destination}: {result.stderr[-500:]}")
    return result.stdout.split()[0]


async def _file_hash(path: str) -> str | None:
    result = await sandbox().exec(["sha256sum", "--", path], timeout=SANDBOX_TIMEOUT_SECONDS)
    return result.stdout.split()[0] if result.success and result.stdout.strip() else None


def _log_path() -> str:
    return SANDBOX_WORK + "/artifact_review.md"


async def _append_log(entry: str) -> None:
    environment = sandbox()
    try:
        existing = await environment.read_file(_log_path())
    except FileNotFoundError:
        existing = "# Artifact reviews\n\nFormat only; the reviewer never sees reference answers.\n\n"
    await environment.write_file(_log_path(), existing + entry)


class _StoreState:
    """Mapping view over the sample store, so the review log lands in the eval log."""

    def get(self, key: str, default: Any = None) -> Any:
        return store().get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        store().set(key, value)


def _reviewer() -> ArtifactReviewer:
    return ArtifactReviewer(
        state=_StoreState(), summarize=_summarize, ask_model=_ask_model,
        copy=_copy, file_hash=_file_hash, append_log=_append_log,
        related=related_accepted, record=record_accepted,  # run memory (past.dev)
    )


@tool
def submit_artifact() -> Tool:
    async def execute(artifact_id: str, path: str, note: str = "") -> str:
        """Submit one required output table to the format reviewer.

        The reviewer reads the task description and a computed summary of the file
        (shape, header, delimiter, first and last rows, row-key checks, random sample
        of identifiers and columns) and checks format only. If it accepts, the file is
        copied to the artifact's required path under ./outputs; otherwise you get
        feedback. The third submission of the same artifact is accepted without review.

        Args:
            artifact_id: The artifact ID from "Required Outputs", e.g. "tagseq_featurecounts_matrix".
            path: Path of the file to submit, e.g. "work/tagseq_featurecounts_matrix.tsv".
            note: Optional explanation for the reviewer, e.g. why a previous objection does not apply.

        Returns:
            The reviewer's decision and feedback (also appended to ./work/artifact_review.md).
        """
        return await _reviewer().submit(artifact_id, path, note)

    return execute


@tool(name="submit")
def review_submit() -> Tool:
    async def execute(answer: str) -> str:
        """Submit an answer for evaluation.

        Args:
          answer (str): Submitted answer
        """
        if store().get(FINAL_CHECKED_KEY, False):
            return answer
        store().set(FINAL_CHECKED_KEY, True)
        issues = await _reviewer().final_issues()
        if not issues:
            return answer
        raise ToolError(
            "Not submitted yet. These required artifacts did not go through submit_artifact "
            "in their current form:\n" + "\n".join(issues) + "\n\nSubmit them with "
            "submit_artifact (artifacts missing from ./outputs score zero), then call "
            "submit() again; the next call is accepted."
        )

    return execute


@solver
def review_setup() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        case = state.metadata_as(RuntimeCase)
        state.store.set(PROMPT_KEY, state.user_prompt.text)
        state.store.set(OUTPUTS_KEY, [output.model_dump() for output in case.expected_outputs])
        state.user_prompt.text = append_review_notice(state.user_prompt.text)
        return state

    return solve
