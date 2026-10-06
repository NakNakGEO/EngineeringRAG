from __future__ import annotations

import re
from pathlib import Path

from eios_domain.events import EventType

REPO = Path(__file__).resolve().parents[2]


def test_web_event_types_mirror_the_backend_enum() -> None:
    text = (REPO / "web" / "src" / "eventTypes.ts").read_text()
    in_ui = re.findall(r'^\s+"([A-Z_]+)",$', text, flags=re.M)
    assert sorted(in_ui) == sorted(e.value for e in EventType), (
        "web/src/eventTypes.ts is out of date with eios_domain.events.EventType"
    )
