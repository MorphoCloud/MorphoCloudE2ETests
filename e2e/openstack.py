"""Read-only OpenStack state via the `openstack` CLI.

The harness never *mutates* OpenStack (all mutations go through the real GitHub
workflows). These helpers only read state for the reliable-readiness check (DESIGN.md
§3 layers 1–2) and the cleanup assertions (instance/volume/FIP gone).

Selects the cloud via `--os-cloud $E2E_OS_CLOUD` (a read-only clouds.yaml entry). If
OpenStack is not configured, `OpenStackClient.available()` is False and callers skip the
direct-state assertions.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from . import config


def instance_name(issue: int) -> str:
    prefix = f"{config.INSTANCE_NAME_PREFIX}_" if config.INSTANCE_NAME_PREFIX else ""
    return f"{prefix}instance-{issue}"


def volume_name(issue: int) -> str:
    return f"My-Data-{issue}"


class OpenStackClient:
    def __init__(self, cloud: str | None = config.OS_CLOUD):
        self.cloud = cloud

    def available(self) -> bool:
        return config.openstack_configured()

    def _run(self, *args: str) -> Any:
        cmd = ["openstack", "--os-cloud", self.cloud, *args, "-f", "json"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            # `server show` on a missing name exits non-zero; treat as "not found".
            return None
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            return None

    # -- servers -----------------------------------------------------------------------

    def server_show(self, name: str) -> dict[str, Any] | None:
        return self._run("server", "show", name)

    def server_status(self, name: str) -> str | None:
        srv = self.server_show(name)
        return srv.get("status") if srv else None

    def server_exists(self, name: str) -> bool:
        return self.server_show(name) is not None

    def exo_setup_status(self, name: str) -> str | None:
        """The authoritative readiness marker: properties.exoSetup.status."""
        srv = self.server_show(name)
        if not srv:
            return None
        props = srv.get("properties") or {}
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except json.JSONDecodeError:
                props = {}
        exo = props.get("exoSetup")
        if not exo:
            return None
        if isinstance(exo, str):
            try:
                exo = json.loads(exo)
            except json.JSONDecodeError:
                return None
        return exo.get("status")

    def server_floating_ip(self, name: str) -> str | None:
        srv = self.server_show(name)
        if not srv:
            return None
        addrs = srv.get("addresses") or {}
        # addresses may be a dict {net: [ips...]} or a formatted string depending on OSC.
        if isinstance(addrs, dict):
            for ips in addrs.values():
                seq = ips if isinstance(ips, list) else str(ips).split(",")
                for ip in seq:
                    ip = str(ip).strip()
                    if ip and not ip.startswith("10.") and not ip.startswith("192.168."):
                        return ip
        return None

    # -- volumes -----------------------------------------------------------------------

    def volume_show(self, name: str) -> dict[str, Any] | None:
        return self._run("volume", "show", name)

    def volume_status(self, name: str) -> str | None:
        vol = self.volume_show(name)
        return vol.get("status") if vol else None

    def volume_in_use(self, name: str) -> bool:
        return self.volume_status(name) == "in-use"

    def volume_exists(self, name: str) -> bool:
        return self.volume_show(name) is not None

    # -- convenience for an issue ------------------------------------------------------

    def instance_gone(self, issue: int) -> bool:
        return not self.server_exists(instance_name(issue))

    def volume_gone(self, issue: int) -> bool:
        return not self.volume_exists(volume_name(issue))
