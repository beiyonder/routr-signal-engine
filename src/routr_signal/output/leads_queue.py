"""Lead queue, v3.

Pre-v3 this was an append-only JSONL file at data/leads/queue.jsonl. v3 moves
the queue into the SQLite `signals` table: leads ARE just signals with
`action_label = 'queued'`.

This module keeps `append(leads)` as a stable entry point so the existing
pipeline glue in main.py keeps working unchanged. Under the hood it marks
the corresponding signal rows as queued so the dashboard / future operations
endpoints can surface them.
"""

from __future__ import annotations

from ..lib import signal_store
from ..lib.logging import info
from ..lib.types import Lead


def append(leads: list[Lead]) -> int:
    """Mark the corresponding signal rows as `action_label='queued'`.

    Returns the number of rows updated.
    """

    if not leads:
        return 0

    updated = 0
    for lead in leads:
        signal_id = lead.source_id  # we set this when building the Lead in lead_extractor
        if not signal_id:
            continue
        signal_store.update_action_label(
            signal_id,
            label="queued",
            notes=f"queued from {lead.platform}:{lead.handle} -- {lead.pitch_angle[:160]}",
        )
        updated += 1

    info(f"leads_queue: marked {updated} signal(s) as queued")
    return updated
