"""Juniper vSRX adapter (#188).

Single VM, virtio-net NICs, serial console.

Matches:

- The canonical Juniper-published qcow2 bundle name
  (``media-vsrx-vmdisk*.qcow2``) so callers that dispatch on a filename
  keep working.
- EVE-NG / UNetLab addon-directory names of the form
  ``vsrx-<ver>`` and ``vsrxng-<ver>`` (the "next-gen" line, e.g.
  ``vsrxng-20.2R1.10``). This is the form the walker actually produces
  when enumerating ``/opt/unetlab/addons/qemu/<vendor-ver>/`` —
  image there is the directory name, not a qcow2 filename.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(
    r"^vsrx(?:ng)?[-_]|media-vsrx-vmdisk.*\.qcow2$",
    re.IGNORECASE,
)


class JuniperVSRXAdapter(VendorAdapter):
    name: ClassVar[str] = "juniper_vsrx"
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
            "name": str(raw.get("name") or "Juniper vSRX"),
            "vendor": "juniper",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 2)),
            "ram": int(raw.get("ram", 4096)),
            "ethernet": int(raw.get("ethernet", 4)),
            "console": str(raw.get("console_type", "serial")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "virtio-net-pci")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
