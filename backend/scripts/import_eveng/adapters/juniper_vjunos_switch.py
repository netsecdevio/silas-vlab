"""Juniper vJunos-switch adapter.

vJunos-switch is Juniper's newer single-VM lab switch, the successor to
the paired-VM vQFX. Ships as a single ``virtioa.qcow2`` inside an
``/opt/unetlab/addons/qemu/vjunosswitch-<ver>/`` directory (e.g.
``vjunosswitch-23.1R1.8``, ``vjunosswitch-23.2R1.14``).

Runtime shape is the same as vSRX: single VM, virtio-net NICs, serial
console. Default sizing is generous (Juniper's own docs recommend 8 GB
RAM + 4 vCPU for production stress; lab-mode boots run cleanly on
4 GB + 2 vCPU which is what we default to here so operators on
constrained lab hosts still get a working template).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

# Match the EVE-NG / UNetLab addon-directory name (``vjunosswitch-<ver>``)
# that the walker produces, as well as a plausible packaged qcow2 filename
# (``vjunosswitch-*.qcow2``) so filename-driven callers also route here.
_IMAGE_RE = re.compile(
    r"^vjunos[-_]?switch[-_]|vjunos[-_]?switch.*\.qcow2$",
    re.IGNORECASE,
)


class JuniperVJunosSwitchAdapter(VendorAdapter):
    name: ClassVar[str] = "juniper_vjunos_switch"
    # Same priority tier as the other single-VM Juniper adapters.
    priority: ClassVar[int] = 80
    REQUIRED_FIELDS: ClassVar[set[str]] = {"image"}

    def match(self, raw: dict[str, Any]) -> bool:
        image = str(raw.get("image", ""))
        return bool(_IMAGE_RE.search(image))

    def convert(self, raw: dict[str, Any], image_dir: Path) -> dict[str, Any]:
        self.validate(raw)
        image = str(raw["image"])
        return {
            "schema": 1,
            "id": str(raw.get("name") or image),
            "name": str(raw.get("name") or "Juniper vJunos-switch"),
            "vendor": "juniper",
            "kind": "qemu",
            "image": image,
            # Lab-mode default; operators can override upward via the
            # template picker or a hand-edit if they need production sizing.
            "cpu": int(raw.get("cpu", 2)),
            "ram": int(raw.get("ram", 4096)),
            "ethernet": int(raw.get("ethernet", 10)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
