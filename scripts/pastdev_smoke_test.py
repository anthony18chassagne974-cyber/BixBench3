"""Write one dummy, non-secret process memory to past.dev and read it back.

Run before launching the v2a experiment:
    uv run --locked python scripts/pastdev_smoke_test.py
Requires PAST_API_KEY (a past.dev project key) in the environment. Prints no secret.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from bixbench3.inspect_harness.pastdev_memory import (
    LEAKAGE_LEVEL,
    MEMORY_PROJECT,
    MEMORY_VERSION,
    READER_IDENTITY,
    MemoryItem,
    PastDevClient,
    PastDevError,
    select_safe_memories,
)


def main() -> int:
    stamp = datetime.now(UTC)
    item = MemoryItem(
        project=MEMORY_PROJECT, version=MEMORY_VERSION, run_id="smoke-test", agent_role=READER_IDENTITY,
        memory_type="command_worked", title="Smoke test (dummy content)",
        content=f"Dummy process note written by scripts/pastdev_smoke_test.py at {stamp.isoformat()}. "
                "The marker word is heliotrope.",
        source="smoke test", leakage_level=LEAKAGE_LEVEL, created_at=stamp.isoformat(),
    )
    try:
        client = PastDevClient.from_environment()
        response = client.ingest(item)
        print("write:", response.get("status"), "| source id:", item.source_id())
        ingestion_id = response.get("ingestionId")
        if ingestion_id and response.get("status") != "completed":
            print("ingestion:", client.wait_until_ingested(str(ingestion_id)))
        memories = select_safe_memories(client.recall("dummy smoke test note with the marker word heliotrope"))
    except PastDevError as error:
        print("FAILED:", error)
        return 1
    found = [memory for memory in memories if memory["source_id"] == item.source_id()]
    print(f"read: {len(memories)} policy-tagged item(s) recalled; smoke item found: {bool(found)}")
    return 0 if found else 2


if __name__ == "__main__":
    sys.exit(main())
