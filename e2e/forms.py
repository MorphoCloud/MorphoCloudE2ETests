"""Issue-form body builders.

We open issues via the API, not the web template, so we must render the body in the
same `### Heading` / value shape that GitHub produces for a submitted issue form —
otherwise `zentered/issue-forms-body-parser` (used by `extract-issue-fields` /
`extract-workshop-fields`) can't parse the fields. The flavor value mirrors the dropdown
option text; `extract-issue-fields` takes `split(" - ")[0]`, so "m3.tiny - ..." → m3.tiny.

Note: the API also won't apply the template's default labels, so callers add
`INDIVIDUAL_REQUEST_LABELS` / `WORKSHOP_REQUEST_LABELS` explicitly (see config).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config

_TAG = "Automated MorphoCloud E2E test — safe to delete."


def _section(heading: str, value: str) -> str:
    return f"### {heading}\n\n{value}\n\n"


def individual_body(flavor: str = config.E2E_FLAVOR) -> str:
    return _section(
        "Cloud Computing Instance Flavor", f"{flavor} - E2E test flavor"
    ).rstrip() + "\n"


def workshop_body(
    *,
    flavor: str = config.E2E_FLAVOR,
    duration_days: int = 1,
    number_of_instances: int = 2,
    start_date: str | None = None,
    start_time: str | None = None,
    timezone_label: str = "UTC",
    description: str | None = None,
) -> str:
    """Build a workshop-request body.

    Defaults to a start ~3 h in the future (UTC): far enough ahead that request-time
    validation accepts it (a *past* start is rejected with needs-fix), yet `start − 12h`
    is already in the past so `/create` is immediately allowed. Override start_date with
    a far-future date to exercise the 12-hour create-window rejection, or with a bad
    value to exercise the malformed-date rejection.
    """
    if start_date is None and start_time is None:
        dt = datetime.now(timezone.utc) + timedelta(hours=3)
        start_date = dt.strftime("%Y-%m-%d")
        start_time = dt.strftime("%H:%M")
    elif start_time is None:
        start_time = "09:00"
    if description is None:
        description = (
            f"**Workshop name:** {_TAG}\n\n"
            "**Target audience:** automated test\n\n"
            "**Workshop content / goals:** E2E\n\n"
            "**Software / data to be used:** none"
        )
    return (
        _section("Cloud Computing Instance Flavor", f"{flavor} - E2E test flavor")
        + _section("Duration (days)", str(duration_days))
        + _section("Number of Instances", str(number_of_instances))
        + _section("Workshop start date", start_date)
        + _section("Workshop start time", start_time)
        + _section("Timezone", timezone_label)
        + _section("Description", description)
    ).rstrip() + "\n"


def far_future_date(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
