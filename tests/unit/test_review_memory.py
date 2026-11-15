"""Experiment t9-env-reviewer-dual-pastdev: a local reviewer and a consistency reviewer with a rolling past.dev memory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import ChatMessageUser, ModelName
from inspect_ai.solver import TaskState

from bixbench3.inspect_harness import review_memory
from bixbench3.inspect_harness import run_memory as memory
from bixbench3.inspect_harness.artifact_review import (
    MEMORY_REVIEWER_SYSTEM,
    OUTPUTS_KEY,
    PROMPT_KEY,
    REVIEW_NOTICE,
    REVIEWER_SYSTEM,
    ArtifactReviewer,
)
from bixbench3.inspect_harness.review_memory import parse_notes

REPO = Path(__file__).resolve().parents[2]
PROMPT = "# Analysis Task\n\n## Required Outputs\n\n### `c10`\n\n- Path: `outputs/c10.csv`\n\n### `f7`\n\n- Path: `outputs/f7.csv`\n"
OUTPUTS = [
    {"artifact_id": "c10", "path": "outputs/c10.csv", "format": "csv", "row_key": "gene_id"},
    {"artifact_id": "f7", "path": "outputs/f7.csv", "format": "csv", "row_key": "gene_id"},
    {"artifact_id": "go_up", "path": "outputs/go_up.csv", "format": "csv", "row_key": "go_id"},
]
ACCEPT = "VERDICT: ACCEPT"
ACCEPT_NOTED = "VERDICT: ACCEPT\nNOTES:\n- DE tables use unversioned ENSG gene ids\n- gene_symbol comes from the GTF gene_name"


class _Store:
    def __init__(self) -> None:
        self.values: dict = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        self.values[key] = value


class _FakePastDev:
    def __init__(self) -> None:
        self.ingested: list = []
        self.order: list[str] | None = None
        self.audiences: list = []
        self.fail_recall = False
        self.store = None

    def put_audience(self, slug, identities):
        self.audiences.append((slug, identities))
        return {}

    def ingest(self, card, audience):
        self.ingested.append(card)
        return {"ingestionId": f"i{len(self.ingested)}", "status": "queued"}

    def ingestion_status(self, ingestion_id):
        return "completed"

    def recall(self, query, *, identity, max_tokens=0, limit=0):
        if self.fail_recall:
            raise memory.PastDevError("past.dev POST /recall failed: HTTP 503")
        # default ranking: every card of the run, newest first (background ingestion may not have run
        # in these single-call event loops, so read the run's mirror rather than self.ingested)
        mirror = list((self.store.get(memory.STORE_KEY) or {}).get("cards", {}).values()) if self.store else []
        ids = self.order if self.order is not None else [card["id"] for card in reversed(mirror)]
        return [{"sources": [{"sourceId": card_id}]} for card_id in ids]


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    fake_store = _Store()
    client = _FakePastDev()
    client.store = fake_store
    monkeypatch.setattr(memory, "store", lambda: fake_store)
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.delenv("BIXBENCH3_BROKER_LOG_DIR", raising=False)
    fake_store.set(memory.STORE_KEY, memory.new_state(run="review-run", pastdev=True))
    return SimpleNamespace(store=fake_store, client=client)


def _memory(env) -> dict:
    return env.store.get(memory.STORE_KEY)


def _reviewer(local: list, remembered: list):
    """Two answer queues; an Exception in a queue makes that reviewer fail."""
    state: dict = {PROMPT_KEY: PROMPT, OUTPUTS_KEY: OUTPUTS}
    files = {f"/workspace/work/{name}.csv": f"hash-{name}" for name in ("c10", "f7", "go_up")}
    calls: dict = {"local": [], "memory": []}

    async def summarize(path: str, artifact: dict, seed: int) -> str:
        return f"Shape: 10 rows\nHeader: gene_id,gene_symbol\nfirst row: ENSG00000100000 from {path}"

    async def ask_model(messages) -> str:
        which = "memory" if messages[0].text == MEMORY_REVIEWER_SYSTEM else "local"
        calls[which].append(messages)
        answer = (remembered if which == "memory" else local).pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def copy(source: str, destination: str) -> str:
        files[destination] = files[source]
        return files[source]

    async def file_hash(path: str) -> str | None:
        return files.get(path)

    async def append_log(entry: str) -> None:
        return None

    reviewer = ArtifactReviewer(
        state=state, summarize=summarize, ask_model=ask_model, copy=copy, file_hash=file_hash, append_log=append_log,
        related=review_memory.memory_reviewer_context, record=review_memory.record_accepted,
        note_review=review_memory.note_review,
    )
    return reviewer, state, calls


def _flush() -> None:
    asyncio.run(memory.flush_ingestion())


def _kinds(env) -> list[str]:
    return sorted(card["kind"] for card in _memory(env)["cards"].values())


def test_local_reviewer_keeps_the_original_instructions_and_memory_reviewer_extends_them() -> None:
    assert "consistency" not in REVIEWER_SYSTEM
    assert MEMORY_REVIEWER_SYSTEM.startswith(REVIEWER_SYSTEM)
    assert "rolling" in MEMORY_REVIEWER_SYSTEM and "NOTES:" in MEMORY_REVIEWER_SYSTEM
    assert "never\n  require identical values" in MEMORY_REVIEWER_SYSTEM
    assert "both must accept" in " ".join(REVIEW_NOTICE.split())


def test_both_reviewers_review_every_table_and_both_must_accept(env) -> None:
    reviewer, _, calls = _reviewer([ACCEPT, ACCEPT], ["VERDICT: REVISE\n- ids keep a version suffix here", ACCEPT])
    first = asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    assert "not accepted (submission 1)" in first
    assert "Local reviewer (this table only):\nVERDICT: ACCEPT" in first
    assert "Consistency reviewer (run memory):\nVERDICT: REVISE" in first
    second = asyncio.run(reviewer.submit("c10", "work/c10.csv", note="ids fixed"))
    assert "accepted (submission 2)" in second
    assert len(calls["local"]) == len(calls["memory"]) == 2
    assert "rolling memory" not in calls["local"][0][1].text  # the local reviewer never sees the memory


def test_rolling_memory_carries_notes_tables_and_reviews_to_the_next_table(env) -> None:
    reviewer, state, calls = _reviewer([ACCEPT, ACCEPT, ACCEPT], [ACCEPT_NOTED, "VERDICT: REVISE\n- x\nNOTES: none", ACCEPT])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    assert _kinds(env) == ["artifact", "convention", "convention", "review"]
    asyncio.run(reviewer.submit("go_up", "work/go_up.csv"))
    _flush()
    asyncio.run(reviewer.submit("f7", "work/f7.csv"))
    memory_view = calls["memory"][2][1].text
    assert "# Your rolling memory of this run" in memory_view
    assert "DE tables use unversioned ENSG gene ids (noted on c10)" in memory_view  # conventions always shown
    assert "### c10" in memory_view  # the accepted c10 table
    assert "go_up submission 1: rejected" in memory_view  # an earlier review of another table
    assert "### f7" not in memory_view and "f7 submission" not in memory_view  # never its own
    assert state["review_log"][-1]["related"]


def test_conventions_are_shown_even_when_the_ranking_returns_nothing(env) -> None:
    reviewer, _, calls = _reviewer([ACCEPT, ACCEPT], [ACCEPT_NOTED, ACCEPT])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    env.client.order = []
    asyncio.run(reviewer.submit("f7", "work/f7.csv"))
    view = calls["memory"][1][1].text
    assert "gene_symbol comes from the GTF gene_name" in view and "### c10" not in view


def test_one_reviewer_failing_leaves_the_decision_to_the_other(env) -> None:
    reviewer, state, _ = _reviewer([RuntimeError("timeout")], ["VERDICT: REVISE\n- inconsistent ids"])
    result = asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    assert "not accepted" in result and "inconsistent ids" in result
    assert state["review_log"][-1]["verdict"] == "rejected (only the consistency reviewer answered)"
    reviewer2, _, _ = _reviewer([RuntimeError("a")], [RuntimeError("b")])
    assert "reviewer unavailable" in asyncio.run(reviewer2.submit("c10", "work/c10.csv"))


def test_memory_failures_never_block_a_review(env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*arguments):
        raise RuntimeError("memory exploded")

    reviewer, _, calls = _reviewer([ACCEPT], [ACCEPT])
    reviewer.related, reviewer.record, reviewer.note_review = broken, broken, broken
    assert "accepted (submission 1)" in asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    assert "# Your rolling memory" not in calls["memory"][0][1].text


def test_pastdev_outage_falls_back_to_local_ranking(env) -> None:
    reviewer, _, calls = _reviewer([ACCEPT, ACCEPT], [ACCEPT, ACCEPT])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    env.client.fail_recall = True
    asyncio.run(reviewer.submit("f7", "work/f7.csv"))
    assert "### c10" in calls["memory"][1][1].text and _memory(env)["stats"]["ranking_fallbacks"] == 1


def test_notes_parsing() -> None:
    assert parse_notes(ACCEPT_NOTED) == ["DE tables use unversioned ENSG gene ids", "gene_symbol comes from the GTF gene_name"]
    assert parse_notes("VERDICT: ACCEPT\nNOTES: none") == [] and parse_notes("VERDICT: ACCEPT") == []
    assert len(parse_notes("NOTES:\n" + "".join(f"- n{i}\n" for i in range(9)))) == review_memory.NOTES_MAX


def test_setup_creates_a_run_scoped_audience_without_touching_the_prompt() -> None:
    client = _FakePastDev()
    state = TaskState(model=ModelName("mockllm/model"), sample_id="t9", epoch=1, input="p",
                      messages=[ChatMessageUser(content="# Analysis Task\n")])
    state = asyncio.run(review_memory.review_memory_setup(lambda: client)(state, None))
    value = state.store.get(memory.STORE_KEY)
    assert value["pastdev"] is True and value["condition"] == "t9-env-reviewer-dual-pastdev"
    assert client.audiences == [(value["audience"], [value["reader"]])]
    assert state.user_prompt.text == "# Analysis Task\n"


def test_wiring_redaction_and_sherlock_secret() -> None:
    harness = REPO / "src/bixbench3/inspect_harness"
    assert "setup=[prepare_case_workspace(), review_setup(), review_memory_setup()]," in (harness / "tasks.py").read_text()
    review = (harness / "artifact_review.py").read_text()
    assert "related=memory_reviewer_context, record=record_accepted, note_review=note_review" in review
    assert 'past_[sm]k_[A-Za-z0-9_-]+' in (REPO / "src/bixbench3/gcp_runner.py").read_text()
    assert "/^PAST_API_KEY=/p" in (REPO / "sherlock/run_job.sbatch").read_text()
