"""Experiment v2a: shared process memory through past.dev (no network in these tests)."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from inspect_ai.model import ChatMessageUser, ModelName
from inspect_ai.solver import TaskState

from bixbench3.inspect_harness import pastdev_memory as module
from bixbench3.inspect_harness.pastdev_memory import (
    MEMORY_NOTICE,
    PastDevClient,
    PastDevError,
    build_items,
    leakage_reason,
    parse_process_notes,
    render_memory_file,
    scrub_result_values,
    select_safe_memories,
)

SECRET = "past_sk_" + "x" * 24
NOTES = """# my notes

## Setup failures and fixes
- BiocManager::install("QuasR") failed on R 4.2; installing from the 3.10 source tarball worked.

## Package and version notes
- DESeq2 1.28.1 needs R 4.0.

## Commands that failed
- pip install piano: no such package.
- found 2862 significant genes at padj < 0.1

## Unknown heading
- ignored
"""


class _Recorder:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.requests: list = []

    def __call__(self, request, timeout):
        del timeout
        self.requests.append(request)
        path = request.full_url.split("/api/v1", 1)[1]
        for prefix, payload in self.responses.items():
            if path.startswith(prefix):
                return _Response(json.dumps(payload).encode())
        raise HTTPError(request.full_url, 404, "not found", None, io.BytesIO(b""))


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *arguments) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _item(**overrides):
    items, _ = build_items(NOTES, run_id="run-1", task_id="t11", summary="Tool calls: python x3.",
                           now=datetime(2026, 11, 15, tzinfo=UTC))
    return items[0] if not overrides else items


def test_client_sends_documented_ingest_and_recall_requests_without_leaking_the_key() -> None:
    recorder = _Recorder({"/ingest": {"ingestionId": "abc", "status": "queued"}, "/recall": {"results": [{"id": 1}]}})
    client = PastDevClient(SECRET, opener=recorder)
    item = _item()
    assert client.ingest(item)["ingestionId"] == "abc"
    request = recorder.requests[0]
    assert request.full_url == "https://api.past.dev/api/v1/ingest" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {SECRET}"
    body = json.loads(request.data)
    assert set(body) == {"id", "content", "label", "timestamp", "metadata", "identity", "idempotencyKey"}
    assert set(body["metadata"]) == {
        "project", "version", "run_id", "agent_role", "memory_type", "title", "source", "leakage_level", "created_at",
    }
    assert body["metadata"]["leakage_level"] == "process_only" and SECRET not in request.data.decode()
    assert client.recall("setup failures") == [{"id": 1}]
    recall = json.loads(recorder.requests[1].data)
    assert recall["identity"] == "bixbench3-analyst" and "limit" in recall and "maxTokens" in recall


def test_client_errors_never_contain_the_key() -> None:
    with pytest.raises(PastDevError, match="PAST_API_KEY is not set"):
        PastDevClient("")
    client = PastDevClient(SECRET, opener=_Recorder({}))
    with pytest.raises(PastDevError) as failure:
        client.recall("anything")
    assert "HTTP 404" in str(failure.value) and SECRET not in str(failure.value)


def test_ingestion_wait_polls_until_completed() -> None:
    states = iter(["processing", "completed"])

    def opener(request, timeout):
        del request, timeout
        return _Response(json.dumps({"status": next(states)}).encode())

    assert PastDevClient(SECRET, opener=opener).wait_until_ingested("abc", sleep=lambda seconds: None) == "completed"


def test_notes_are_split_by_known_headings_only() -> None:
    sections = parse_process_notes(NOTES)
    assert [memory_type for memory_type, _, _ in sections] == [
        "setup_failure_fix", "package_version_note", "command_failed",
    ]


def test_items_follow_the_schema_and_drop_result_values() -> None:
    items, rejected = build_items(NOTES, run_id="run-1", task_id="t11", summary="Tool calls: python x3.")
    assert [item.memory_type for item in items] == [
        "setup_failure_fix", "package_version_note", "command_failed", "run_summary",
    ]
    failed = items[2]
    assert "pip install piano" in failed.content and "2862" not in failed.content
    assert rejected == [{"title": "Commands that failed (task t11)", "reason": "1 result-value line(s) removed"}]
    assert all(item.project == "bixbench3" and item.leakage_level == "process_only" and item.run_id == "run-1"
               for item in items)
    assert len({item.source_id() for item in items}) == len(items)
    assert items[0].source_id() == build_items(NOTES, run_id="other", task_id="t11", summary="x")[0][0].source_id()


@pytest.mark.parametrize("text", [
    f"export KEY={SECRET}", "token: sk-proj-abcdefghijklmnop", "password = hunter2hunter2",
    "the grader expects 2862 rows", "copied from grade.json", "matches the ground truth table",
    "reference output had 615 rows", "see bixbench3-ground-truth bucket",
])
def test_secrets_and_grading_material_are_rejected(text: str) -> None:
    assert leakage_reason(text) is not None
    notes = f"## Pitfalls\n- {text}\n"
    items, rejected = build_items(notes, run_id="r", task_id="t", summary="Tool calls: none.")
    assert [item.memory_type for item in items] == ["run_summary"]
    assert rejected and rejected[0]["title"].startswith("Pitfalls")


def test_process_statements_are_kept() -> None:
    text = "- Rscript -e 'BiocManager::install(version=\"3.11\")' worked after apt-get install libxml2-dev\n- bowtie 1.3.1 built in 40 s"
    assert leakage_reason(text) is None
    assert scrub_result_values(text) == (text, 0)


def test_only_policy_tagged_memories_reach_the_agent() -> None:
    def source(source_id: str, metadata: dict, excerpt: str) -> dict:
        return {"sources": [{"sourceId": source_id, "label": "L", "metadata": metadata, "excerpts": [excerpt]}]}

    good = {"project": "bixbench3", "leakage_level": "process_only", "title": "Fix", "memory_type": "setup_failure_fix",
            "version": "v2a", "run_id": "run-1"}
    results = [
        source("a", good, "install libxml2-dev first"),
        source("a", good, "install libxml2-dev first"),
        source("b", {**good, "leakage_level": "unknown"}, "something"),
        source("c", {**good, "project": "other"}, "something"),
        source("d", good, "the grader wants 615 rows"),
        source("e", {}, "untagged"),
    ]
    memories = select_safe_memories(results)
    assert [memory["source_id"] for memory in memories] == ["a"]
    rendered = render_memory_file(memories, status="ok")
    assert "## Fix" in rendered and "install libxml2-dev first" in rendered and "run run-1" in rendered
    assert "first run to use it" in render_memory_file([], status="ok")
    assert "could not be read" in render_memory_file([], status="unavailable")


class _Sandbox:
    def __init__(self, notes: str | None) -> None:
        self.files: dict[str, str] = {}
        self.notes = notes

    async def write_file(self, file: str, contents: str) -> None:
        self.files[file] = contents

    async def read_file(self, file: str) -> str:
        if self.notes is None:
            raise FileNotFoundError(file)
        return self.notes


class _FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.ingested: list = []
        self.queries: list[str] = []

    def recall(self, query: str) -> list:
        if self.fail:
            raise PastDevError("past.dev POST /recall failed with HTTP 500")
        self.queries.append(query)
        return [{"sources": [{"sourceId": "a", "excerpts": ["use micromamba for hisat2"], "metadata": {
            "project": "bixbench3", "leakage_level": "process_only", "title": "Tip", "memory_type": "command_worked",
            "version": "v2a", "run_id": "run-0"}}]}]

    def ingest(self, item):
        if self.fail:
            raise PastDevError("past.dev POST /ingest failed with HTTP 500")
        self.ingested.append(item)
        return {"status": "queued", "ingestionId": "x"}


def _state() -> TaskState:
    return TaskState(model=ModelName("mockllm/model"), sample_id="t11", epoch=1, input="p",
                     messages=[ChatMessageUser(content="# Analysis Task\n")])


@pytest.mark.parametrize("fail", [False, True])
def test_read_and_writeback_record_what_happened_and_never_fail_the_run(
    monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    box = _Sandbox(NOTES)
    client = _FakeClient(fail=fail)
    monkeypatch.setattr(module, "sandbox", lambda: box)
    state = asyncio.run(module.read_process_memory(lambda: client)(_state(), None))
    assert state.user_prompt.text.endswith(MEMORY_NOTICE)
    memory_file = box.files[module.SANDBOX_MEMORY_PATH]
    assert ("use micromamba for hisat2" in memory_file) is not fail
    assert ("could not be read" in memory_file) is fail

    async def analyst(inner_state: TaskState, generate: object) -> TaskState:
        del generate
        return inner_state

    state = asyncio.run(module.with_memory_writeback(analyst, lambda: client)(state, None))
    record = state.store.get(module.STORE_KEY)
    if fail:
        assert record["read_status"].startswith("unavailable") and record["write_status"].startswith("failed")
    else:
        assert len(client.queries) == len(module.RECALL_QUERIES) and all("task t11" in q for q in client.queries)
        assert [entry["source_id"] for entry in record["read"]] == ["a"]
        assert [entry["memory_type"] for entry in record["written"]] == [
            "setup_failure_fix", "package_version_note", "command_failed", "run_summary",
        ]
        assert record["write_status"] == "ok" and record["notes_file_found"] is True
    assert SECRET not in json.dumps(record)


def test_writeback_runs_even_when_the_analyst_stops_on_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(module, "sandbox", lambda: _Sandbox(None))

    async def analyst(state: TaskState, generate: object) -> TaskState:
        raise TimeoutError("time limit")

    state = _state()
    with pytest.raises(TimeoutError):
        asyncio.run(module.with_memory_writeback(analyst, lambda: client)(state, None))
    assert [item.memory_type for item in client.ingested] == ["run_summary"]
    assert state.store.get(module.STORE_KEY)["notes_file_found"] is False


def test_runner_passes_only_explicitly_named_extra_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from bixbench3.config import resolve_model
    from bixbench3.gcp_runner import extra_environment_keys, required_environment_keys

    model = resolve_model("openai/gpt-5.4-mini", "max")
    monkeypatch.delenv("BIXBENCH3_EXTRA_ENV_KEYS", raising=False)
    assert extra_environment_keys() == () and required_environment_keys(model) == ("OPENAI_API_KEY",)
    monkeypatch.setenv("BIXBENCH3_EXTRA_ENV_KEYS", "PAST_API_KEY, ")
    assert required_environment_keys(model) == ("OPENAI_API_KEY", "PAST_API_KEY")


def test_task_wiring_and_policy_document() -> None:
    root = Path(__file__).resolve().parents[2]
    tasks = (root / "src/bixbench3/inspect_harness/tasks.py").read_text()
    assert "setup=[prepare_case_workspace(), read_process_memory()]," in tasks
    assert "solver=with_memory_writeback(bixbench3_agent())," in tasks
    policy = (root / "docs/memory_policy.md").read_text(encoding="utf-8")
    for field in ("project", "version", "run_id", "agent_role", "memory_type", "title", "content", "source",
                  "leakage_level", "created_at"):
        assert f"`{field}`" in policy
    assert "never stored" in policy.lower() and SimpleNamespace  # policy lists the exclusions
