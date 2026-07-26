"""Tests for the Juniper vJunos-switch adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng.adapters import (
    JuniperVJunosSwitchAdapter,
    iter_adapters,
    select_adapter,
)
from scripts.import_eveng.adapters.base import NeedsManualReview
from scripts.import_eveng.template_schema import is_paired, validate_template


# ---- match() ---------------------------------------------------------


def test_vjunos_switch_matches_eveng_addon_directory_names() -> None:
    a = JuniperVJunosSwitchAdapter()
    # Both hyphenated and non-hyphenated ("vjunos-switch" / "vjunosswitch")
    # forms show up in the wild — cover both.
    assert a.match({"image": "vjunosswitch-23.1R1.8"}) is True
    assert a.match({"image": "vjunosswitch-23.2R1.14"}) is True
    assert a.match({"image": "vjunos-switch-24.1R1"}) is True
    assert a.match({"image": "VJunosSwitch-23.1R1.8"}) is True  # case-insensitive


def test_vjunos_switch_matches_packaged_qcow2_filename() -> None:
    a = JuniperVJunosSwitchAdapter()
    assert a.match({"image": "vjunosswitch-23.2R1.14.qcow2"}) is True
    assert a.match({"image": "vjunos-switch-23.1R1.8.qcow2"}) is True


def test_vjunos_switch_rejects_other_juniper_images() -> None:
    """Must not steal images that belong to sibling Juniper adapters."""
    a = JuniperVJunosSwitchAdapter()
    assert a.match({"image": "media-vsrx-vmdisk-15.1X49.qcow2"}) is False
    assert a.match({"image": "vsrxng-20.2R1.10"}) is False
    assert a.match({"image": "vmx-bundle-22.4R1.qcow2"}) is False
    assert a.match({"image": "vqfx-10000-re-bsd.qcow2"}) is False


def test_vjunos_switch_rejects_unrelated_images() -> None:
    a = JuniperVJunosSwitchAdapter()
    assert a.match({"image": "vyos-1.4.qcow2"}) is False
    assert a.match({"image": "linux-ubuntu-server-22.04"}) is False


# ---- convert() -------------------------------------------------------


def test_vjunos_switch_convert_emits_single_node_template() -> None:
    a = JuniperVJunosSwitchAdapter()
    raw = {"image": "vjunosswitch-23.1R1.8"}
    out = a.convert(raw, Path("."))
    assert out["kind"] == "qemu"
    assert out["vendor"] == "juniper"
    assert out["image"] == "vjunosswitch-23.1R1.8"
    assert is_paired(out) is False
    # Lab-mode defaults — safe on constrained hosts, overridable via raw.
    assert out["cpu"] == 2
    assert out["ram"] == 4096
    assert out["ethernet"] == 10
    assert out["console"] == "serial"
    assert out["extras"]["qemu_nic"] == "virtio-net-pci"
    validate_template(out)


def test_vjunos_switch_convert_honours_operator_overrides() -> None:
    a = JuniperVJunosSwitchAdapter()
    raw = {
        "image": "vjunosswitch-23.2R1.14",
        "name": "spine-01",
        "cpu": 4,
        "ram": 8192,
        "ethernet": 24,
        "qemu_nic": "e1000",
        "qemu_options": "-cpu host",
    }
    out = a.convert(raw, Path("."))
    assert out["name"] == "spine-01"
    assert out["cpu"] == 4
    assert out["ram"] == 8192
    assert out["ethernet"] == 24
    assert out["extras"]["qemu_nic"] == "e1000"
    assert out["extras"]["qemu_options"] == "-cpu host"


def test_vjunos_switch_validate_raises_when_image_missing() -> None:
    a = JuniperVJunosSwitchAdapter()
    with pytest.raises(NeedsManualReview):
        a.convert({}, Path("."))


# ---- registry integration -------------------------------------------


def test_vjunos_switch_registered_before_generic_linux() -> None:
    names = [a.name for a in iter_adapters()]
    assert "juniper_vjunos_switch" in names
    assert names[-1] == "generic_linux"
    assert names.index("juniper_vjunos_switch") < names.index("generic_linux")


def test_select_adapter_routes_vjunos_switch_to_new_adapter() -> None:
    matched = select_adapter({"image": "vjunosswitch-23.1R1.8"})
    assert matched is not None and matched.name == "juniper_vjunos_switch"

    matched = select_adapter({"image": "vjunos-switch-23.2R1.14"})
    assert matched is not None and matched.name == "juniper_vjunos_switch"
