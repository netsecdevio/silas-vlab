"""Tests for the paired-VM shim in migrate._generate_template_for_item."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng.manifest import ImportManifest
from scripts.import_eveng.migrate import (
    _enrich_raw_for_paired_vm,
    _find_paired_counterpart,
    _find_paired_vm_spec,
    _generate_template_for_item,
)
from scripts.import_eveng.walker import KIND_QEMU, MigrationItem


def _mk_item(image_key: str, src: str = "/src", dst: str = "/dst") -> MigrationItem:
    return MigrationItem(
        kind=KIND_QEMU,
        image_key=image_key,
        src_dir=Path(src),
        dst_dir=Path(dst),
        meta={"boot_disk": "hda.qcow2"},
    )


# ---- table + helper coverage ---------------------------------------


def test_find_paired_vm_spec_returns_none_for_unrelated_prefix() -> None:
    assert _find_paired_vm_spec("vyos-1.4.29") is None
    assert _find_paired_vm_spec("linux-ubuntu-server-22.04") is None
    # vsrxng is single-VM, not paired.
    assert _find_paired_vm_spec("vsrxng-20.2R1.10") is None


def test_find_paired_vm_spec_recognises_vmx_and_vqfx_halves() -> None:
    for key in ("vmxvcp-18.2R1.9", "vmxvfp-18.2R1.9"):
        got = _find_paired_vm_spec(key)
        assert got is not None
        prefix, _spec = got
        assert prefix in {"vmxvcp", "vmxvfp"}
    for key in ("vqfxre-10K-F-17.4R1.16", "vqfxpfe-10K-F-17.4R1.16"):
        got = _find_paired_vm_spec(key)
        assert got is not None


def test_find_paired_counterpart_matches_by_version_suffix() -> None:
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    got = _find_paired_counterpart("vmxvfp", "-18.2R1.9", items)
    assert got is not None
    assert got.image_key == "vmxvfp-18.2R1.9"


def test_find_paired_counterpart_returns_none_when_versions_differ() -> None:
    # vmxvfp exists but for a DIFFERENT release — must not be paired.
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-16.2R2.8")]
    got = _find_paired_counterpart("vmxvfp", "-18.2R1.9", items)
    assert got is None


# ---- enrichment behaviour ------------------------------------------


def test_enrich_primary_returns_paired_raw_dict_with_both_halves() -> None:
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    primary = items[0]
    raw = _enrich_raw_for_paired_vm(primary, items)
    assert raw is not None
    assert raw.get("image_vcp") == "vmxvcp-18.2R1.9"
    assert raw.get("image_vfp") == "vmxvfp-18.2R1.9"
    assert isinstance(raw.get("ram_vcp"), int) and raw["ram_vcp"] > 0
    assert isinstance(raw.get("ram_vfp"), int) and raw["ram_vfp"] > 0


def test_enrich_secondary_returns_absorbed_sentinel() -> None:
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    secondary = items[1]
    raw = _enrich_raw_for_paired_vm(secondary, items)
    assert raw is not None
    assert raw.get("__paired_absorbed__") is True


def test_enrich_unpaired_half_returns_none() -> None:
    # Only the VCP present — no VFP means we can't emit a paired template.
    items = [_mk_item("vmxvcp-18.2R1.9")]
    raw = _enrich_raw_for_paired_vm(items[0], items)
    assert raw is None


def test_enrich_ignores_non_paired_items() -> None:
    items = [_mk_item("vyos-1.4.29"), _mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    assert _enrich_raw_for_paired_vm(items[0], items) is None  # vyos is not paired


# ---- integration with _generate_template_for_item ------------------


def test_generate_template_emits_paired_template_only_on_primary(tmp_path: Path) -> None:
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    manifest = ImportManifest()
    for it in items:
        _generate_template_for_item(
            it, templates_dir=tmp_path, manifest=manifest, all_items=items
        )
    statuses = {t.name: t.status for t in manifest.templates}
    assert statuses.get("vmxvcp-18.2R1.9") == "generated"
    assert statuses.get("vmxvfp-18.2R1.9") == "skipped"


def test_generate_template_paired_template_uses_juniper_vmx_adapter(tmp_path: Path) -> None:
    items = [_mk_item("vmxvcp-18.2R1.9"), _mk_item("vmxvfp-18.2R1.9")]
    manifest = ImportManifest()
    _generate_template_for_item(
        items[0], templates_dir=tmp_path, manifest=manifest, all_items=items
    )
    entries = [t for t in manifest.templates if t.name == "vmxvcp-18.2R1.9"]
    assert entries and "juniper_vmx" in entries[0].reason


def test_generate_template_vqfx_pair_end_to_end(tmp_path: Path) -> None:
    items = [
        _mk_item("vqfxre-10K-F-17.4R1.16"),
        _mk_item("vqfxpfe-10K-F-17.4R1.16"),
    ]
    manifest = ImportManifest()
    for it in items:
        _generate_template_for_item(
            it, templates_dir=tmp_path, manifest=manifest, all_items=items
        )
    statuses = {t.name: t.status for t in manifest.templates}
    assert statuses.get("vqfxre-10K-F-17.4R1.16") == "generated"
    assert statuses.get("vqfxpfe-10K-F-17.4R1.16") == "skipped"


def test_generate_template_lone_primary_falls_back_to_needs_manual_review(
    tmp_path: Path,
) -> None:
    """Half without its partner still produces a manifest entry — must not
    silently disappear from operator visibility."""
    items = [_mk_item("vmxvcp-18.2R1.9")]  # no VFP
    manifest = ImportManifest()
    _generate_template_for_item(
        items[0], templates_dir=tmp_path, manifest=manifest, all_items=items
    )
    assert manifest.templates
    # Adapter fails validate() because paired-shape fields are absent.
    assert manifest.templates[0].status == "needs-manual-review"


def test_generate_template_legacy_call_still_works(tmp_path: Path) -> None:
    """Callers that pre-date the shim (no all_items kwarg) must still work."""
    item = _mk_item("vyos-1.4.29")
    manifest = ImportManifest()
    _generate_template_for_item(item, templates_dir=tmp_path, manifest=manifest)
    assert manifest.templates
    assert manifest.templates[0].status == "generated"
    assert "vyos" in manifest.templates[0].reason
