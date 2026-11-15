"""Experiments t9-env-reviewer-pastdev and t9-env-reviewer-dual-pastdev: reviewer memory.

Dual version: a local reviewer judges each table alone, as in t9-env-reviewer; a consistency
reviewer keeps a rolling memory of the run (conventions it noted, most similar accepted tables,
most relevant earlier reviews) and both must accept. See ``memory_reviewer_context``.

Single version: the format reviewer remembers what it already accepted.

The t9-env-reviewer reviewer judges one table at a time and cannot see a format
inconsistency between two tables (for example gene identifiers with a version suffix in
one DE table and without in another). Here, every accepted table's computed format
summary is stored in a past.dev memory scoped to this run. When a new table is submitted,
past.dev ranks the accepted tables against its specification and summary, and the
reviewer receives the most similar ones (at most three, excluding earlier versions of the
same artifact) so it can check that the same kind of column uses the same format.

Only computed format summaries of this run's own accepted tables are stored: no ground
truth, grader output or reference result ever enters the memory, and nothing crosses
runs (one past.dev audience per run). Only the host-side harness calls past.dev; the key
stays out of the sandbox. If past.dev is unavailable, a local keyword ranking is used and
recorded in broker_logs/memory_stats.json.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver

from .run_memory import (
    STORE_KEY,
    PastDevClient,
    PastDevError,
    _append_log,
    _client,
    _now,
    _save,
    _state,
    new_state,
    ranked_cards,
    remember,
    run_identifier,
)

CONDITION = "t9-env-reviewer-dual-pastdev"
REVIEWS_MAX = 3  # earlier consistency reviews shown to the memory reviewer
REVIEW_CARD_CHARS = 900
NOTES_MAX = 3  # conventions stored from one review
NOTE_CHARS = 300
CONVENTIONS_SHOWN_CHARS = 3000
RELATED_MAX = 3  # accepted artifacts shown to the reviewer
RELATED_CARD_CHARS = 2500  # of each accepted artifact's summary
SUMMARY_STORED_CHARS = 1900  # of the summary stored in a card (cards hold up to 2,000 characters)


def _artifact_card_text(artifact: dict[str, Any], summary: str, verdict: str, attempt: int) -> str:
    header = (
        f"artifact {artifact['artifact_id']} ({verdict}, submission {attempt}); "
        f"path {artifact.get('path')}; format {artifact.get('format')}; row key {artifact.get('row_key')}"
    )
    return header + "\n" + summary.strip()[:SUMMARY_STORED_CHARS]


async def record_accepted(artifact: dict[str, Any], summary: str, verdict: str, attempt: int) -> None:
    """Store an accepted table's format summary; a later acceptance replaces the earlier card."""
    await remember(
        "artifact", key=artifact["artifact_id"], author="reviewer",
        title=f"accepted artifact {artifact['artifact_id']}",
        text=_artifact_card_text(artifact, summary, verdict, attempt),
        paths=[str(artifact.get("path", ""))],
    )


async def related_accepted(artifact: dict[str, Any], summary: str) -> tuple[str, list[str]]:
    """The accepted tables past.dev ranks as most similar to this submission (never itself)."""
    query = f"Artifact {artifact['artifact_id']}, {artifact.get('format')}, row key {artifact.get('row_key')}\n{summary[:3000]}"
    ranked, source = await ranked_cards(query)
    chosen = [card for card in ranked if card["kind"] == "artifact" and card["key"] != artifact["artifact_id"]][:RELATED_MAX]
    value = _state()
    value["stats"]["packs"].append({
        "role": f"review:{artifact['artifact_id']}", "ranking": source, "conventions": 0,
        "cards": len(chosen), "chars": sum(min(len(card["text"]), RELATED_CARD_CHARS) for card in chosen), "ts": _now(),
    })
    _save(value)
    _append_log("memory_packs.jsonl", {"role": f"review:{artifact['artifact_id']}", "ranking": source,
                                       "card_ids": [card["id"] for card in chosen], "ts": _now()})
    text = "\n\n".join(f"## {card['key']}\n\n{card['text'][:RELATED_CARD_CHARS]}" for card in chosen)
    return text, [card["key"] for card in chosen]


@solver
def review_memory_setup(client_factory: Callable[[], PastDevClient | None] = _client) -> Solver:
    """Create this run's past.dev audience. The agent's prompt is left unchanged."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        value = new_state(run=run_identifier(state), pastdev=False)
        value["condition"] = CONDITION
        client = client_factory()
        if client is None:
            value["pastdev_reason"] = "PAST_API_KEY is not set: local fallback ranking only"
        else:
            try:
                await asyncio.to_thread(client.put_audience, value["audience"], [value["reader"]])
                value["pastdev"] = True
            except PastDevError as error:
                value["pastdev_reason"] = f"audience creation failed: {error}: local fallback ranking only"
        state.store.set(STORE_KEY, value)
        _append_log("memory_events.jsonl", {"event": "setup", "condition": CONDITION, "run": value["run"],
                                            "audience": value["audience"], "pastdev": value["pastdev"],
                                            "reason": value["pastdev_reason"], "ts": _now()})
        return state

    return solve


_NOTES = re.compile(r"^\s*NOTES:\s*(.*)$", re.MULTILINE | re.DOTALL)


def parse_notes(answer: str) -> list[str]:
    """Format conventions the memory reviewer noted after its verdict ("NOTES: none" gives none)."""
    match = _NOTES.search(answer)
    if not match:
        return []
    notes = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith(("-", "*")):
            text = line.lstrip("-* ").strip()
            if text and text.lower() != "none":
                notes.append(text[:NOTE_CHARS])
    return notes[:NOTES_MAX]


async def note_review(artifact: dict[str, Any], attempt: int, accepted: bool, answer: str) -> None:
    """Rolling memory: store the consistency review and the conventions it noted."""
    verdict = "accepted" if accepted else "rejected"
    match = _NOTES.search(answer)
    body = answer[:match.start()].strip() if match else answer.strip()
    await remember(
        "review", key=f"{artifact['artifact_id']}#{attempt}", author="consistency-reviewer",
        title=f"review of {artifact['artifact_id']} (submission {attempt}): {verdict}",
        text=f"{artifact['artifact_id']} submission {attempt}: {verdict}\n{body[:REVIEW_CARD_CHARS]}",
    )
    for note in parse_notes(answer):
        await remember("convention", key=note.lower(), author="consistency-reviewer",
                       title="format convention", text=f"{note} (noted on {artifact['artifact_id']})")


async def memory_reviewer_context(artifact: dict[str, Any], summary: str) -> tuple[str, list[str]]:
    """What the consistency reviewer remembers for this table.

    All conventions it noted (never left to the ranking), then the accepted tables and earlier
    reviews past.dev ranks as most relevant to this submission (never this artifact's own).
    """
    query = f"Artifact {artifact['artifact_id']}, {artifact.get('format')}, row key {artifact.get('row_key')}\n{summary[:3000]}"
    ranked, source = await ranked_cards(query)
    value = _state()
    conventions = sorted((card for card in value["cards"].values() if card["kind"] == "convention"),
                         key=lambda card: card["ts"])
    own = artifact["artifact_id"]
    tables = [card for card in ranked if card["kind"] == "artifact" and card["key"] != own][:RELATED_MAX]
    reviews = [card for card in ranked if card["kind"] == "review" and not card["key"].startswith(own + "#")][:REVIEWS_MAX]

    sections = []
    shown, used = [], 0
    for card in conventions:
        if used + len(card["text"]) > CONVENTIONS_SHOWN_CHARS:
            break
        shown.append(f"- {card['text']}")
        used += len(card["text"])
    if shown:
        sections.append("## Format conventions you noted earlier in this run\n\n" + "\n".join(shown))
    if tables:
        sections.append("## Most similar tables you already accepted\n\n" + "\n\n".join(
            f"### {card['key']}\n\n{card['text'][:RELATED_CARD_CHARS]}" for card in tables))
    if reviews:
        sections.append("## Your most relevant earlier reviews\n\n" + "\n\n".join(
            card["text"][:REVIEW_CARD_CHARS] for card in reviews))

    value = _state()
    value["stats"]["packs"].append({
        "role": f"review:{own}", "ranking": source, "conventions": len(shown), "cards": len(tables) + len(reviews),
        "chars": sum(map(len, sections)), "ts": _now(),
    })
    _save(value)
    _append_log("memory_packs.jsonl", {"role": f"review:{own}", "ranking": source,
                                       "convention_ids": [card["id"] for card in conventions[:len(shown)]],
                                       "card_ids": [card["id"] for card in tables + reviews], "ts": _now()})
    return "\n\n".join(sections), [card["key"] for card in tables + reviews]
