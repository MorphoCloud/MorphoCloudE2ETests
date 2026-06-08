"""Nox sessions for the MorphoCloud E2E harness.

Sessions:
  e2e        — full suite (Stage 0 vendorize runs first via the autouse fixture)
  e2e-cheap  — Milestone 1 only: cheap validation, no provisioning (C=0)
  e2e-sweep  — run the leak-guard sweeper and exit (force-clean any [E2E] leftovers)

The harness drives MorphoCloud/Test-Instances. Configuration comes from the
environment (see e2e/config.py and e2e/docs/setup_bot.md); a local `.env` is loaded
automatically if present.
"""

from __future__ import annotations

import nox

nox.options.reuse_existing_virtualenvs = True
nox.options.sessions = ["e2e_cheap"]

PYTHON = "3.12"


def _install(session: nox.Session) -> None:
    session.install("-e", ".")


@nox.session(python=PYTHON, name="e2e")
def e2e(session: nox.Session) -> None:
    """Run the full suite. Pass pytest args after `--`, e.g. `-m 'cheap or individual'`."""
    _install(session)
    session.run("pytest", *session.posargs, env={"PYTHONUNBUFFERED": "1"})


@nox.session(python=PYTHON, name="e2e-cheap")
def e2e_cheap(session: nox.Session) -> None:
    """Milestone 1: cheap validation suite (no provisioning)."""
    _install(session)
    session.run("pytest", "-m", "cheap", *session.posargs, env={"PYTHONUNBUFFERED": "1"})


@nox.session(python=PYTHON, name="e2e-sweep")
def e2e_sweep(session: nox.Session) -> None:
    """Run the sweeper: force-clean any [E2E] leftovers, then exit."""
    _install(session)
    session.run("python", "-m", "e2e.sweeper", *session.posargs)


@nox.session(python=PYTHON, name="units")
def units(session: nox.Session) -> None:
    """Offline unit tests — no network, no secrets, no Test-Instances."""
    _install(session)
    session.run("pytest", "tests/", *session.posargs)
