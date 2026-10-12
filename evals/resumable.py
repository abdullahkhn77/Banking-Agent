"""Append-only, resumable trial log: one JSON row per finished trial, flushed as soon as it finishes.

On restart with ``resume=True`` the completed rows are kept and their keys are skipped, provided the run
configuration (fingerprint) is identical; a torn last line from an interrupted write is dropped.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Any


class ResumeMismatchError(RuntimeError):
    """The stored run configuration differs from the requested one."""


class TrialLog:
    def __init__(
        self, out_dir: Path, fingerprint: dict[str, Any], key: Callable[[dict[str, Any]], Hashable], resume: bool
    ) -> None:
        self.path = out_dir / "trials.jsonl"
        self.config_path = out_dir / "run_config.json"
        self.key = key
        self.lock = threading.Lock()
        out_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = json.loads(json.dumps(fingerprint, default=str))
        self.rows: list[dict[str, Any]] = []
        if resume and self.config_path.exists():
            stored = json.loads(self.config_path.read_text(encoding="utf-8"))
            if stored != fingerprint:
                changed = sorted(k for k in set(stored) | set(fingerprint) if stored.get(k) != fingerprint.get(k))
                raise ResumeMismatchError(f"cannot resume {out_dir}: run configuration differs in {changed}")
            self.rows = self._load()
        else:
            self.path.write_text("", encoding="utf-8")
        self.resumed_rows = len(self.rows)
        self.config_path.write_text(json.dumps(fingerprint, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows, lines = [], self.path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if i != len(lines) - 1:
                    raise
        with self.path.open("w", encoding="utf-8") as handle:  # rewrite without a torn trailing line
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")
        return rows

    def done(self) -> set[Hashable]:
        return {self.key(row) for row in self.rows}

    def append(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, default=str) + "\n"
        with self.lock:
            self.rows.append(row)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
