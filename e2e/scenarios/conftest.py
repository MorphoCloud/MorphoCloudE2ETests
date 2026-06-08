"""Autouse fixtures for the live integration scenarios (DESIGN.md §4a run ordering).

Scoped to e2e/scenarios so offline unit tests under tests/ don't trigger a vendorize or
require an admin token:
  * stage0     — mandatory vendorize MWF -> Test-Instances (gating), runs first.
  * leak_guard — Phase B: abort if a previous run leaked too many [E2E] issues.
"""

from __future__ import annotations

import warnings

import pytest

from e2e import config, vendorize
from e2e.sweeper import Sweeper


@pytest.fixture(scope="session", autouse=True)
def stage0():
    if config.SKIP_VENDORIZE:
        warnings.warn(
            "E2E_SKIP_VENDORIZE=1 — NOT syncing Test-Instances to MWF. "
            "For harness-plumbing development ONLY; real runs test stale code without it.",
            stacklevel=2,
        )
        yield None
        return
    result = vendorize.run_stage0()
    print(f"\n[stage0] vendorized MWF@{result.mwf_sha[:9]} "
          f"(changed={result.changed}, pushed={result.pushed})")
    yield result


@pytest.fixture(scope="session", autouse=True)
def leak_guard(stage0, admin):
    Sweeper(admin).leak_guard()
    yield
