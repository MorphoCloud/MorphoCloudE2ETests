"""Milestone 2 — individual instance lifecycle (C=1).

Phase C (non-destructive) keeps the instance ALIVE: create → reliable readiness →
shelve → unshelve → email → status reconcile → uptime → create-idempotency.
Phase D (destructive) is the teardown: /delete_instance → /delete_volume.

Gated behind `provision` (E2E_PROVISION=1) so it cannot run before Milestone 0 has
chosen a working E2E_FLAVOR. See DESIGN.md §6.2 / §6.4.
"""

from __future__ import annotations

import pytest

from e2e import config, forms, openstack
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = [pytest.mark.individual, pytest.mark.provision]


def test_individual_lifecycle(bot, admin, os_client, inbox, ensure_test_labels, issues):
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)

    # --- request opened -------------------------------------------------------------
    num = issues.open("individual", "individual lifecycle", forms.individual_body(flavor))
    lc.ensure_flavor_label(admin, num, flavor)
    assert sum(1 for lbl in admin.labels(num) if lbl.startswith("flavor:")) == 1

    # --- /create + reliable readiness -----------------------------------------------
    since_create = lc.create(bot, admin, num)
    lc.assert_ready(admin, os_client, num)

    # --- credential email (real inbox) ----------------------------------------------
    msg = inbox.wait_for(
        lambda m: str(num) in (m.subject + m.text) or openstack.instance_name(num) in m.text,
        since=since_create,
    )
    assert msg.connection_url(), "credential email has no connection URL"
    assert msg.passphrase(), "credential email has no passphrase"

    # --- /shelve --------------------------------------------------------------------
    name = openstack.instance_name(num)
    fip_before = os_client.server_floating_ip(name) if os_client.available() else None
    lc.command(bot, admin, num, "/shelve", "control-instance.yml")
    lc.assert_status_label(admin, num, "shelved")
    if os_client.available():
        poll(lambda: os_client.server_status(name) == "SHELVED_OFFLOADED",
             timeout=config.TIMEOUT_COMMAND, desc="SHELVED_OFFLOADED")

    # --- /unshelve (same IP reused) -------------------------------------------------
    lc.command(bot, admin, num, "/unshelve", "control-instance.yml")
    lc.assert_status_label(admin, num, "active")
    if os_client.available() and fip_before:
        assert os_client.server_floating_ip(name) == fip_before, "unshelve did not reuse the FIP"

    # --- /email (second credential mail) --------------------------------------------
    since_email = utcnow()
    lc.command(bot, admin, num, "/email", "send-email.yml")
    inbox.wait_for(lambda m: openstack.instance_name(num) in m.text or str(num) in m.subject,
                   since=since_email)

    # --- scheduled reconcilers ------------------------------------------------------
    since = utcnow()
    admin.dispatch_workflow("update-request-status-label.yml")
    run = admin.wait_for_run("update-request-status-label.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"

    since = utcnow()
    admin.dispatch_workflow("collect-instance-uptime.yml")
    run = admin.wait_for_run("collect-instance-uptime.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"

    # --- create idempotency (what workshop-backfill relies on) ----------------------
    since = utcnow()
    bot.comment(num, "/create")
    bot.wait_for_comment(num, "already created", since=since)
    if os_client.available():
        assert os_client.server_exists(name), "idempotent re-create must not delete the instance"

    # --- Phase D teardown: /delete_instance then /delete_volume ---------------------
    vol = openstack.volume_name(num)
    lc.command(bot, admin, num, "/delete_instance", "control-instance.yml")
    if os_client.available():
        poll(lambda: os_client.instance_gone(num), timeout=config.TIMEOUT_COMMAND,
             desc="instance deleted")
        assert os_client.volume_exists(vol), "/delete_instance must keep the volume"

    lc.command(bot, admin, num, "/delete_volume", "delete-volume.yml")
    if os_client.available():
        poll(lambda: os_client.volume_gone(num), timeout=config.TIMEOUT_COMMAND,
             desc="volume deleted")
