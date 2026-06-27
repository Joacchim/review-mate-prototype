"""Append-only event log — one JSONL file per session, the durable source of truth.

`append` assigns the monotonic `seq`, writes the line, and **fsyncs before returning** so an
acked event is on disk (AC-7). `replay` rebuilds the event stream and tolerates a truncated
trailing line left by a crash mid-append — that event was never acked, so dropping it is correct.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from review_mate.session import events as ev


class EventLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next = self._scan_last_seq() + 1
        self._f = open(self.path, "a", encoding="utf-8")

    def _scan_last_seq(self) -> int:
        last = 0
        for event in self.replay():
            last = event.seq
        return last

    def append(self, event: "ev.Event") -> int:
        event.seq = self._next
        self._next += 1
        self._f.write(event.model_dump_json() + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())
        return event.seq

    def replay(self) -> Iterator["ev.Event"]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield ev.parse_event(line)
                except Exception:
                    # only a truncated trailing line should fail to parse — stop here
                    break

    def close(self) -> None:
        self._f.close()
