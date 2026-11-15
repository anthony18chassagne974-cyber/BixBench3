"""Experiment v2a: shared process memory through past.dev.

Before the analyst starts, the harness recalls *process* memories written by earlier
runs (setup failures and fixes, package and version notes, commands that worked or
failed, accepted or rejected fallbacks, task pitfalls) and writes them to
``work/process_memory.md``. During the run the agent keeps process notes in
``work/process_notes.md``. When the agent stops, the harness stores those notes and a
short run summary back to past.dev.

Only the harness talks to past.dev, from the VM host. The API key is read from the
``PAST_API_KEY`` environment variable, is never printed, and never enters the sandbox.
Every item passes a leakage filter first: secrets, grader or reference material and
result values are never stored. See ``docs/memory_policy.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox

API_KEY_ENV = "PAST_API_KEY"
BASE_URL_ENV = "PAST_API_BASE_URL"
DEFAULT_BASE_URL = "https://api.past.dev/api/v1"
MEMORY_PROJECT = "bixbench3"
MEMORY_VERSION = "v2a"
READER_IDENTITY = "bixbench3-analyst"
LEAKAGE_LEVEL = "process_only"

SANDBOX_MEMORY_PATH = "/workspace/work/process_memory.md"
SANDBOX_NOTES_PATH = "/workspace/work/process_notes.md"
STORE_KEY = "pastdev_memory"

HTTP_TIMEOUT_SECONDS = 30
INGEST_WAIT_SECONDS = 90
RECALL_LIMIT = 12
RECALL_MAX_TOKENS = 3000
MAX_ITEM_CHARS = 4000
MAX_ITEMS_WRITTEN = 20
MAX_MEMORY_FILE_CHARS = 12000

RECALL_QUERIES = (
    "setup failures and their fixes: package installation errors, missing tools, environment problems",
    "package and version notes: which tool versions were installed and how",
    "commands that worked and commands that failed during the analysis",
    "fallbacks that were accepted or rejected, and process pitfalls to avoid",
)

MEMORY_TYPES = {
    "setup failures and fixes": "setup_failure_fix",
    "package and version notes": "package_version_note",
    "commands that worked": "command_worked",
    "commands that failed": "command_failed",
    "fallbacks accepted or rejected": "fallback_decision",
    "pitfalls": "process_pitfall",
    "validated helper scripts": "validated_helper_script",
    "validator findings": "validator_finding",
}

_SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|past_[sm]k_[A-Za-z0-9_-]{6,}|hf_[A-Za-z0-9]{8,}|AIza[A-Za-z0-9_-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|token|password|secret)\s*[=:]\s*\S{8,}",
    re.IGNORECASE,
)
_FORBIDDEN = re.compile(
    r"ground[ _-]?truth|reference output|grader|grade\.json|judge\.json|expected[ _]row|expected[ _]count|"
    r"bixbench3-ground-truth|answer key",
    re.IGNORECASE,
)
# Lines that state result values rather than process: p-values, fold changes, significance counts.
_RESULT_VALUE = re.compile(
    r"\b(p[ _-]?value|padj|fdr|log2 ?fold ?change|nes)\b[^\n]{0,40}?[=:<>]\s*-?\d|"
    r"\b\d+\s+(significant|enriched|differentially expressed|degs?|gene sets?|pathways?|rows?)\b",
    re.IGNORECASE,
)

MEMORY_NOTICE = """## Shared Process Memory

Earlier runs of this benchmark left process notes: setup failures and their fixes, package \
and version notes, commands that worked or failed, and fallbacks that were accepted or \
rejected. They are in `./work/process_memory.md`. Read that file before you begin. Treat the \
notes as hints about the environment, not as results: verify them before relying on them.

As you work, keep your own process notes in `./work/process_notes.md`, as short bullet points \
under these headings: `## Setup failures and fixes`, `## Package and version notes`, \
`## Commands that worked`, `## Commands that failed`, `## Fallbacks accepted or rejected`, \
`## Pitfalls`. Record only how things were done. Do not write result values (counts of \
significant genes, p-values, enriched pathways), secrets, or anything about grading there.
"""


@dataclass(frozen=True)
class MemoryItem:
    """One process memory, following the team schema."""

    project: str
    version: str
    run_id: str
    agent_role: str
    memory_type: str
    title: str
    content: str
    source: str
    leakage_level: str
    created_at: str

    def source_id(self) -> str:
        digest = hashlib.sha256(f"{self.project}\0{self.memory_type}\0{self.title}\0{self.content}".encode())
        return f"{self.project}-{self.memory_type}-{digest.hexdigest()[:24]}"

    def metadata(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if key != "content"}


class PastDevError(RuntimeError):
    pass


class PastDevClient:
    """Minimal past.dev Memory API client (ingest, ingestion status, recall)."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL,
                 opener: Callable[..., Any] = urlopen) -> None:
        if not api_key:
            raise PastDevError(f"{API_KEY_ENV} is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._opener = opener

    @classmethod
    def from_environment(cls) -> PastDevClient:
        return cls(os.environ.get(API_KEY_ENV, ""), base_url=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = response.read()
        except HTTPError as error:
            raise PastDevError(f"past.dev {method} {path} failed with HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError) as error:
            raise PastDevError(f"past.dev {method} {path} unreachable: {type(error).__name__}") from None
        return json.loads(payload) if payload else {}

    def ingest(self, item: MemoryItem) -> dict[str, Any]:
        return self._request("POST", "/ingest", {
            "id": item.source_id(),
            "content": f"{item.title}\n\n{item.content}",
            "label": item.title,
            "timestamp": item.created_at,
            "metadata": item.metadata(),
            "identity": item.agent_role,
            "idempotencyKey": item.source_id(),
        })

    def wait_until_ingested(self, ingestion_id: str, *, timeout_seconds: float = INGEST_WAIT_SECONDS,
                            sleep: Callable[[float], None] = time.sleep) -> str:
        deadline = time.monotonic() + timeout_seconds
        status = "queued"
        while time.monotonic() < deadline:
            status = str(self._request("GET", f"/ingest/{ingestion_id}").get("status", "unknown"))
            if status in ("completed", "failed"):
                return status
            sleep(3)
        return status

    def recall(self, query: str, *, identity: str = READER_IDENTITY, limit: int = RECALL_LIMIT,
               max_tokens: int = RECALL_MAX_TOKENS) -> list[dict[str, Any]]:
        response = self._request("POST", "/recall", {
            "query": query, "identity": identity, "limit": limit, "maxTokens": max_tokens, "sort": "relevance",
        })
        results = response.get("results", [])
        return results if isinstance(results, list) else []


def leakage_reason(text: str) -> str | None:
    """Why this text must not be stored, or None if it is safe process content."""
    if _SECRET.search(text):
        return "looks like a secret"
    if _FORBIDDEN.search(text):
        return "mentions grading or reference material"
    return None


def scrub_result_values(text: str) -> tuple[str, int]:
    """Drop lines that state result values; keep process statements."""
    kept, dropped = [], 0
    for line in text.splitlines():
        if _RESULT_VALUE.search(line):
            dropped += 1
        else:
            kept.append(line)
    return "\n".join(kept).strip(), dropped


def parse_process_notes(notes: str) -> list[tuple[str, str, str]]:
    """Split the agent's notes file into (memory_type, title, content) by known headings."""
    sections: list[tuple[str, str, str]] = []
    heading: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if heading is None:
            return
        memory_type = MEMORY_TYPES.get(heading.lower())
        content = "\n".join(lines).strip()
        if memory_type and content:
            sections.append((memory_type, heading, content))

    for line in notes.splitlines():
        if line.startswith("## "):
            flush()
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    flush()
    return sections


def build_items(notes: str, *, run_id: str, task_id: str, summary: str, now: datetime | None = None) -> tuple[list[MemoryItem], list[dict[str, str]]]:
    """Turn the notes and the run summary into safe memory items; report what was rejected."""
    created = (now or datetime.now(UTC)).isoformat()
    candidates = [(memory_type, f"{title} (task {task_id})", content, "agent process notes (work/process_notes.md)")
                  for memory_type, title, content in parse_process_notes(notes)]
    candidates.append(("run_summary", f"Run summary (task {task_id})", summary, "harness run summary"))
    items: list[MemoryItem] = []
    rejected: list[dict[str, str]] = []
    for memory_type, title, content, source in candidates[:MAX_ITEMS_WRITTEN]:
        reason = leakage_reason(title + "\n" + content)
        if reason:
            rejected.append({"title": title, "reason": reason})
            continue
        content, dropped = scrub_result_values(content)
        if dropped:
            rejected.append({"title": title, "reason": f"{dropped} result-value line(s) removed"})
        if not content:
            continue
        items.append(MemoryItem(
            project=MEMORY_PROJECT, version=MEMORY_VERSION, run_id=run_id, agent_role=READER_IDENTITY,
            memory_type=memory_type, title=title, content=content[:MAX_ITEM_CHARS], source=source,
            leakage_level=LEAKAGE_LEVEL, created_at=created,
        ))
    return items, rejected


def select_safe_memories(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only recalled sources written under this policy, de-duplicated by source id."""
    selected: dict[str, dict[str, str]] = {}
    for result in results:
        for source in result.get("sources") or []:
            metadata = source.get("metadata") or {}
            if metadata.get("project") != MEMORY_PROJECT or metadata.get("leakage_level") != LEAKAGE_LEVEL:
                continue
            text = "\n".join(str(excerpt) for excerpt in source.get("excerpts") or []) or str(result.get("content") or "")
            if not text.strip() or leakage_reason(text):
                continue
            source_id = str(source.get("sourceId"))
            selected.setdefault(source_id, {
                "source_id": source_id,
                "title": str(metadata.get("title") or source.get("label") or "untitled"),
                "memory_type": str(metadata.get("memory_type") or "unknown"),
                "version": str(metadata.get("version") or "unknown"),
                "run_id": str(metadata.get("run_id") or "unknown"),
                "content": text.strip(),
            })
    return list(selected.values())


def render_memory_file(memories: list[dict[str, str]], *, status: str) -> str:
    lines = [
        "# Shared process memory",
        "",
        "Process notes recalled from earlier runs. They describe how things were done in the",
        "environment, not what the results are. Verify a note before relying on it.",
        "",
    ]
    if status != "ok":
        lines.append(f"Memory could not be read for this run ({status}). Proceed without it.")
    elif not memories:
        lines.append("No process memory is available yet: this is the first run to use it.")
    for memory in memories:
        lines += [f"## {memory['title']}", "",
                  f"_Type: {memory['memory_type']}; from version {memory['version']}, run {memory['run_id']}_", "",
                  memory["content"], ""]
    return "\n".join(lines)[:MAX_MEMORY_FILE_CHARS] + "\n"


def append_memory_notice(prompt: str) -> str:
    return prompt.rstrip() + "\n\n" + MEMORY_NOTICE


def run_identifier(state: TaskState) -> str:
    return os.environ.get("BIXBENCH3_RUN_ID") or f"{state.sample_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def summarize_run(state: TaskState) -> str:
    """Process-only facts about the run: tools used and which required files exist. No values."""
    calls: dict[str, int] = {}
    for message in state.messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls[call.function] = calls.get(call.function, 0) + 1
    tools = ", ".join(f"{name} x{count}" for name, count in sorted(calls.items())) or "none"
    return f"Agent turns: {len(state.messages)} messages. Tool calls: {tools}."


@solver
def read_process_memory(client_factory: Callable[[], PastDevClient] = PastDevClient.from_environment) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        record: dict[str, Any] = {"read_status": "ok", "read": []}
        memories: list[dict[str, str]] = []
        try:
            client = client_factory()
            results: list[dict[str, Any]] = []
            for query in RECALL_QUERIES:
                results += client.recall(f"{query} (BixBench3 task {state.sample_id})")
            memories = select_safe_memories(results)
            record["read"] = [{k: m[k] for k in ("source_id", "title", "memory_type", "version", "run_id")}
                              for m in memories]
        except Exception as error:  # memory must never cost the run
            record["read_status"] = f"unavailable: {type(error).__name__}: {error}"[:300]
        status = "ok" if record["read_status"] == "ok" else "unavailable"
        await sandbox().write_file(SANDBOX_MEMORY_PATH, render_memory_file(memories, status=status))
        state.store.set(STORE_KEY, record)
        state.user_prompt.text = append_memory_notice(state.user_prompt.text)
        return state

    return solve


async def write_process_memory(state: TaskState, client_factory: Callable[[], PastDevClient]) -> None:
    record: dict[str, Any] = dict(state.store.get(STORE_KEY, {}))
    try:
        try:
            notes = str(await sandbox().read_file(SANDBOX_NOTES_PATH))
        except Exception:
            notes = ""
        items, rejected = build_items(
            notes, run_id=run_identifier(state), task_id=str(state.sample_id), summary=summarize_run(state),
        )
        client = client_factory()
        written = []
        for item in items:
            response = client.ingest(item)
            written.append({"source_id": item.source_id(), "title": item.title, "memory_type": item.memory_type,
                            "status": str(response.get("status", "unknown"))})
        record.update({"write_status": "ok", "written": written, "rejected": rejected,
                       "notes_file_found": bool(notes.strip())})
    except Exception as error:  # memory must never cost the run
        record["write_status"] = f"failed: {type(error).__name__}: {error}"[:300]
    state.store.set(STORE_KEY, record)


def with_memory_writeback(inner: Solver,
                          client_factory: Callable[[], PastDevClient] = PastDevClient.from_environment) -> Solver:
    """Run the unchanged analyst, then store its process notes, even if it stops on a limit."""

    @solver(name="analyst_with_memory_writeback")
    def wrapped() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            try:
                state = await inner(state, generate)
            finally:
                await write_process_memory(state, client_factory)
            return state

        return solve

    return wrapped()
