"""Print the lead queue as a table."""

import io
import json
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

path = Path(__file__).resolve().parent.parent / "data" / "leads" / "queue.jsonl"
if not path.exists():
    print("(no leads queue yet)")
    sys.exit(0)

leads = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
print(f"{len(leads)} lead(s) in queue\n")
for i, l in enumerate(leads, start=1):
    handle = l.get("handle", "?")
    platform = l.get("platform", "?")
    url = l.get("profile_url") or ""
    pain = (l.get("pain_in_their_words") or "")[:140]
    angle = (l.get("pitch_angle") or "")[:180]
    print(f"{i}. @{handle}  [{platform}]")
    if url:
        print(f"   {url}")
    print(f"   pain:  {pain}")
    print(f"   angle: {angle}")
    print()
