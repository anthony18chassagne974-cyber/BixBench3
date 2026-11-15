"""Experiment t9-env-reviewer-pastdev: the reviewer compares with tables it already accepted."""

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
    OUTPUTS_KEY,
    PROMPT_KEY,
    REVIEW_NOTICE,
    REVIEWER_SYSTEM,
    ArtifactReviewer,
    reviewer_messages,
)

REPO = Path(__file__).resolve().parents[2]
PROMPT = "# Analysis Task\n\n## Required Outputs\n\n### `c10`\n\n- Path: `outputs/c10.csv`\n\n### `f7`\n\n- Path: `outputs/f7.csv`\n"
OUTPUTS = [
    {"artifact_id": "c10", "path": "outputs/c10.csv", "format": "csv", "row_key": "gene_id"},
    {"artifact_id": "f7", "path": "outputs/f7.csv", "format": "csv", "row_key": "gene_id"},
    {"artifact_id": "go_up", "path": "outputs/go_up.csv", "format": "csv", "row_key": "go_id"},
]


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
        ids = self.order if self.order is not None else [card["id"] for card in reversed(self.ingested)]
        return [{"sources": [{"sourceId": card_id}]} for card_id in ids]


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    fake_store = _Store()
    client = _FakePastDev()
    monkeypatch.setattr(memory, "store", lambda: fake_store)
    monkeypatch.setattr(memory, "_client", lambda: client)
    monkeypatch.delenv("BIXBENCH3_BROKER_LOG_DIR", raising=False)
    fake_store.set(memory.STORE_KEY, memory.new_state(run="review-run", pastdev=True))
    return SimpleNamespace(store=fake_store, client=client)


def _memory(env) -> dict:
    return env.store.get(memory.STORE_KEY)


def _reviewer(answers: list[str], *, with_memory: bool = True):
    state: dict = {PROMPT_KEY: PROMPT, OUTPUTS_KEY: OUTPUTS}
    files = {f"/workspace/work/{name}.csv": f"hash-{name}" for name in ("c10", "f7", "go_up")}
    asked: list = []
    queue = list(answers)

    async def summarize(path: str, artifact: dict, seed: int) -> str:
        return f"Shape: 10 rows\nHeader: gene_id,gene_symbol\nfirst row: ENSG00000100000 from {path}"

    async def ask_model(messages) -> str:
        asked.append(messages)
        return queue.pop(0)

    async def copy(source: str, destination: str) -> str:
        files[destination] = files[source]
        return files[source]

    async def file_hash(path: str) -> str | None:
        return files.get(path)

    async def append_log(entry: str) -> None:
        return None

    reviewer = ArtifactReviewer(
        state=state, summarize=summarize, ask_model=ask_model, copy=copy, file_hash=file_hash, append_log=append_log,
        related=review_memory.related_accepted if with_memory else None,
        record=review_memory.record_accepted if with_memory else None,
    )
    return reviewer, state, asked


def _flush() -> None:
    asyncio.run(memory.flush_ingestion())


def test_first_review_has_no_related_section_and_acceptance_is_recorded(env) -> None:
    reviewer, state, asked = _reviewer(["VERDICT: ACCEPT"])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    assert "already accepted" not in asked[0][1].text
    cards = [card for card in _memory(env)["cards"].values() if card["kind"] == "artifact"]
    assert [card["key"] for card in cards] == ["c10"]
    assert cards[0]["text"].startswith("artifact c10 (accepted, submission 1); path outputs/c10.csv")
    assert "ENSG00000100000" in cards[0]["text"] and state["review_log"][0]["related"] == []


def test_later_review_sees_the_most_similar_accepted_tables_never_itself(env) -> None:
    reviewer, state, asked = _reviewer(["VERDICT: ACCEPT", "VERDICT: ACCEPT", "VERDICT: REVISE\n- add version suffix"])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    asyncio.run(reviewer.submit("go_up", "work/go_up.csv"))
    _flush()
    ids = {card["key"]: card["id"] for card in _memory(env)["cards"].values()}
    env.client.order = [ids["c10"], ids["go_up"]]  # the librarian ranks c10 first for f7
    asyncio.run(reviewer.submit("f7", "work/f7.csv"))
    user = asked[2][1].text
    assert "# Artifacts of this run you already accepted" in user
    assert user.index("## c10") < user.index("## go_up")
    assert state["review_log"][-1]["related"] == ["c10", "go_up"]
    assert state["review_log"][-1]["verdict"] == "rejected"
    assert "f7" not in {card["key"] for card in _memory(env)["cards"].values()}  # rejected tables are not stored


def test_resubmitted_artifact_replaces_its_card_and_is_excluded_from_its_own_review(env) -> None:
    reviewer, _, asked = _reviewer(["VERDICT: ACCEPT", "VERDICT: ACCEPT"])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    asyncio.run(reviewer.submit("c10", "work/c10.csv", note="fixed symbols"))
    _flush()
    assert "already accepted" not in asked[1][1].text
    assert [card["key"] for card in _memory(env)["cards"].values()] == ["c10"]


def test_related_section_is_capped(env) -> None:
    async def seed() -> None:
        for index in range(6):
            artifact = {"artifact_id": f"a{index}", "path": f"outputs/a{index}.csv", "format": "csv", "row_key": "gene_id"}
            await review_memory.record_accepted(artifact, "x" * 5000, "accepted", 1)
        await memory.flush_ingestion()

    asyncio.run(seed())
    text, keys = asyncio.run(review_memory.related_accepted(OUTPUTS[1], "Shape: 10 rows"))
    assert len(keys) == review_memory.RELATED_MAX
    assert len(text) <= review_memory.RELATED_MAX * (review_memory.RELATED_CARD_CHARS + 20)


def test_memory_failures_never_block_a_review(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer, _, asked = _reviewer(["VERDICT: ACCEPT", "VERDICT: ACCEPT"])
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    _flush()
    env.client.fail_recall = True  # past.dev down: local fallback still shows c10
    asyncio.run(reviewer.submit("f7", "work/f7.csv"))
    assert "## c10" in asked[1][1].text and _memory(env)["stats"]["ranking_fallbacks"] == 1

    async def broken(*arguments):
        raise RuntimeError("memory exploded")

    reviewer2, _, asked2 = _reviewer(["VERDICT: ACCEPT"])
    reviewer2.related, reviewer2.record = broken, broken
    assert "accepted" in asyncio.run(reviewer2.submit("go_up", "work/go_up.csv"))
    assert "already accepted" not in asked2[0][1].text


def test_without_memory_hooks_the_reviewer_is_unchanged() -> None:
    reviewer, _, asked = _reviewer(["VERDICT: ACCEPT"], with_memory=False)
    asyncio.run(reviewer.submit("c10", "work/c10.csv"))
    plain = reviewer_messages(task_prompt=PROMPT, artifact=OUTPUTS[0], summary="s", note="", history=[])
    assert "already accepted" not in plain[1].text and "already accepted" not in asked[0][1].text


def test_prompts_explain_the_consistency_check_and_stay_format_only() -> None:
    assert "same format across artifacts" in REVIEW_NOTICE.replace("\n", " ")
    system = " ".join(REVIEWER_SYSTEM.split())
    assert "consistency:" in system and "never require identical values, identical genes or identical row counts" in system
    assert "no reference answers" in system


def test_setup_creates_a_run_scoped_audience_without_touching_the_prompt() -> None:
    client = _FakePastDev()
    state = TaskState(model=ModelName("mockllm/model"), sample_id="t9", epoch=1, input="p",
                      messages=[ChatMessageUser(content="# Analysis Task\n")])
    state = asyncio.run(review_memory.review_memory_setup(lambda: client)(state, None))
    value = state.store.get(memory.STORE_KEY)
    assert value["pastdev"] is True and value["condition"] == "t9-env-reviewer-pastdev"
    assert client.audiences == [(value["audience"], [value["reader"]])]
    assert state.user_prompt.text == "# Analysis Task\n"


def test_wiring_redaction_and_sherlock_secret() -> None:
    harness = REPO / "src/bixbench3/inspect_harness"
    assert "setup=[prepare_case_workspace(), review_setup(), review_memory_setup()]," in (harness / "tasks.py").read_text()
    assert "related=related_accepted, record=record_accepted" in (harness / "artifact_review.py").read_text()
    assert 'past_[sm]k_[A-Za-z0-9_-]+' in (REPO / "src/bixbench3/gcp_runner.py").read_text()
    assert "/^PAST_API_KEY=/p" in (REPO / "sherlock/run_job.sbatch").read_text()
