"""Cisco IOSv L3 adapter (#187).

Matches ``vios-adventerprise*.qcow2``. Emits a qemu node with e1000 NICs,
telnet console, and a default ethernet count of 8.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from .base import VendorAdapter

_IMAGE_RE = re.compile(
    # Match:
    #   - vios-adventerprise*.qcow2 (canonical filename form)
    #   - vios-<ver> or vios-adventerprisek9-*-<ver> (walker addon-dir form)
    # But NOT vios_l2 / viosl2 (those belong to the L2 adapter, which
    # runs at the same priority; keep the underscore negative-lookahead).
    r"vios-adventerprise|^vios(?!_?l2)[-_]",
    re.IGNORECASE,
)


class CiscoIOSvL3Adapter(VendorAdapter):
    name: ClassVar[str] = "cisco_iosv_l3"
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
            "name": str(raw.get("name") or "Cisco IOSv L3"),
            "vendor": "cisco",
            "kind": "qemu",
            "image": image,
            "cpu": int(raw.get("cpu", 1)),
            "ram": int(raw.get("ram", 512)),
            "ethernet": int(raw.get("ethernet", 8)),
            "console": str(raw.get("console_type", "telnet")),
            "extras": {
                "qemu_nic": str(raw.get("qemu_nic", "e1000")),
                "qemu_options": str(raw.get("qemu_options", "")),
                "_eveng_raw": raw.get("_eveng_raw") or dict(raw),
            },
        }
