"""Baseline capture/assert (DESIGN.md §8).

Workflow comment/label text drifts, so scenarios assert against a captured baseline
rather than hardcoded prose. A scenario calls `baseline.check(key, observed)`:

  * `--capture-baseline`  → records `observed` into `e2e/baseline/expected.json`
    (the re-capture diff IS the change-review).
  * normal run, key present in the baseline → asserts `observed == expected[key]`.
  * normal run, key absent (no baseline yet) → **inert** (records for the next capture).

The inert-when-absent rule means `baseline.check(...)` calls are safe to sprinkle into
scenarios before a baseline exists — they only start enforcing once one is captured.
Prefer structural values (sorted label lists, run conclusions, counts) over exact prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _normalize(value: Any) -> Any:
    """Make values order-stable + JSON-roundtrippable so comparisons are deterministic."""
    if isinstance(value, (set, tuple)):
        value = list(value)
    if isinstance(value, list):
        try:
            return sorted(_normalize(v) for v in value)
        except TypeError:
            return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


class BaselineMismatch(AssertionError):
    pass


class Baseline:
    def __init__(self, path: Path, *, capture: bool):
        self.path = Path(path)
        self.capture = capture
        self.expected: dict[str, Any] = {} if capture else self._load()
        self.collected: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def check(self, key: str, observed: Any) -> Any:
        observed = _normalize(observed)
        self.collected[key] = observed
        if self.capture:
            return observed
        if key in self.expected and observed != self.expected[key]:
            raise BaselineMismatch(
                f"baseline mismatch for {key!r}:\n  expected: {self.expected[key]!r}\n"
                f"  observed: {observed!r}\n(re-run with --capture-baseline if this change is intended)"
            )
        return observed

    def save(self) -> None:
        data = dict(self.expected)
        data.update(self.collected)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
