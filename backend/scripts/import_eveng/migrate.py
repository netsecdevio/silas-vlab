"""Migration orchestrator for the EVE-NG importer (#184).

Consumes :class:`MigrationItem` records from :mod:`walker` and, for each one,
runs the kind-appropriate copy logic against the :mod:`copy_engine`. Records
every outcome on the :class:`ImportManifest`.

Per-kind specifics:

- ``qemu``: copies all files in the template dir; picks the boot disk per
  precedence (``cdrom.iso`` > ``virtioa.qcow2`` > ``hda.qcow2`` > first
  ``*.qcow2``); creates a stable ``cdrom.iso`` symlink in the destination dir
  pointing at whatever was chosen.
- ``dynamips``: copies the ``*.image`` file verbatim into a per-image dir.
- ``iol``: copies the ``*.bin`` plus ``iourc`` license; if ``iourc`` is missing
  from the source, the migration record is flagged ``needs-manual-review``.
- ``docker``: invokes ``docker build -t nova-ve/<image>:latest <ctx>`` and
  emits a ``image.txt`` marker file in the destination dir with the resolved
  image tag. The build command is mockable for tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yaml

from ._app_owner import AppOwner
from .adapters import NeedsManualReview, select_adapter
from .copy_engine import CopyEngineError, CopyMode, CopyOutcome, perform_copy
from .manifest import (
    ErrorEntry,
    ImportManifest,
    ImportedEntry,
    SkippedEntry,
    TemplateEntry,
)
from .permissions import fixup, fixup_tree
from .walker import (
    KIND_DOCKER,
    KIND_DYNAMIPS,
    KIND_IOL,
    KIND_QEMU,
    MigrationItem,
)



_logger = logging.getLogger("nova_ve.import_eveng")


@dataclass
class MigrateOptions:
    mode: CopyMode = CopyMode.DEFAULT
    force: bool = False


# ---- docker build wrapper (mockable for tests) ---------------------------


def _default_docker_build(context: Path, tag: str) -> None:
    """Invoke ``docker build -t <tag> <context>``. Raises CalledProcessError on failure."""
    subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        check=True,
        capture_output=True,
    )


DockerBuildFn = Callable[[Path, str], None]


# ---- core --------------------------------------------------------------


def _write_template_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def _record_imported_files(
    manifest: ImportManifest, outcomes: Iterable[CopyOutcome]
) -> None:
    for outcome in outcomes:
        if outcome.status == "imported":
            manifest.imported.append(
                ImportedEntry(
                    src=outcome.src,
                    dst=outcome.dst,
                    sha256=outcome.sha256,
                    bytes=outcome.bytes,
                    mode=outcome.mode,
                )
            )
        elif outcome.status == "skipped":
            manifest.skipped.append(
                SkippedEntry(src=outcome.src, dst=outcome.dst, reason=outcome.reason or "")
            )


def _ensure_qemu_cdrom_symlink(item: MigrationItem) -> None:
    """Create the stable ``cdrom.iso`` symlink in the destination dir.

    No-op when the boot disk is already named ``cdrom.iso`` — the file itself
    already serves the stable-handle role and a same-name symlink would be a
    self-loop (caught by US-190's e2e idempotency test).
    """
    boot_disk = item.meta.get("boot_disk")
    if not isinstance(boot_disk, str) or not boot_disk:
        return
    if boot_disk == "cdrom.iso":
        return
    link_path = item.dst_dir / "cdrom.iso"
    if link_path.is_symlink():
        if link_path.readlink().name == boot_disk:
            return
        link_path.unlink()
    elif link_path.exists():
        link_path.unlink()
    link_path.symlink_to(boot_disk)


def _migrate_files(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> bool:
    """Copy every file pair on ``item.files``. Returns True if every file landed."""
    outcomes: list[CopyOutcome] = []
    for src, dst in item.files:
        try:
            outcome = perform_copy(src, dst, mode=options.mode, force=options.force)
        except CopyEngineError as exc:
            manifest.errors.append(ErrorEntry(path=str(src), error=str(exc)))
            _logger.warning(
                "migrate.copy_failed", extra={"src": str(src), "dst": str(dst), "error": str(exc)}
            )
            return False
        outcomes.append(outcome)
    _record_imported_files(manifest, outcomes)
    return True


def migrate_qemu(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    if not _migrate_files(item, options, manifest):
        return
    _ensure_qemu_cdrom_symlink(item)


def migrate_dynamips(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    _migrate_files(item, options, manifest)


def migrate_iol(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
) -> None:
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    if not _migrate_files(item, options, manifest):
        return
    if not item.meta.get("iourc_present"):
        manifest.templates.append(
            TemplateEntry(
                name=item.image_key,
                status="needs-manual-review",
                reason="iol image found but iourc license file is missing",
            )
        )


def migrate_docker(
    item: MigrationItem,
    options: MigrateOptions,
    manifest: ImportManifest,
    docker_build: DockerBuildFn,
) -> None:
    """Run ``docker build`` and emit the ``image.txt`` marker."""
    item.dst_dir.mkdir(parents=True, exist_ok=True)
    image_tag = str(item.meta.get("image_tag", f"nova-ve-{item.image_key}:latest"))
    build_context = Path(str(item.meta.get("build_context", item.src_dir)))

    try:
        docker_build(build_context, image_tag)
    except Exception as exc:  # noqa: BLE001 — surface any docker error to manifest
        manifest.errors.append(
            ErrorEntry(path=str(build_context), error=f"docker build failed: {exc}")
        )
        return

    marker = item.dst_dir / "image.txt"
    marker.write_text(image_tag + "\n")
    manifest.imported.append(
        ImportedEntry(
            src=str(build_context),
            dst=str(marker),
            sha256="",
            bytes=marker.stat().st_size,
            mode=options.mode.value,
        )
    )


# ---------------------------------------------------------------------------
# Paired-VM directory pairing table.
#
# EVE-NG / UNetLab ships paired-VM Juniper platforms as *two* addon
# directories (e.g. ``vmxvcp-18.2R1.9`` + ``vmxvfp-18.2R1.9``, or
# ``vqfxre-10K-F-17.4R1.16`` + ``vqfxpfe-10K-F-17.4R1.16``). The walker
# yields those as two independent :class:`MigrationItem` records with no
# knowledge that they belong together.
#
# Adapters such as :class:`JuniperVMXAdapter` and :class:`JuniperVQFXAdapter`
# emit a ``kind="paired"`` template that requires both halves' image + ram
# fields in a single ``raw`` dict. The pairing shim below reconstructs that
# dict at dispatch time so the paired adapters can succeed.
#
# Each entry maps a directory-name prefix → a pairing spec:
#
#   ``partner_prefix``  the counterpart's directory-name prefix.
#   ``role``            our half's role in the paired template
#                       (matches the adapter's field naming scheme).
#   ``partner_role``    the counterpart's role.
#   ``is_primary``      when True this half is the one that emits the
#                       paired template; when False the walker item is
#                       absorbed into its primary and produces no template
#                       of its own.
#   ``ram_default``     baseline RAM (MB) for our half; used when the
#                       walker meta does not carry a value.
#   ``partner_ram_default``  baseline RAM (MB) for the counterpart.
# ---------------------------------------------------------------------------

_PAIRED_VM_MAP: dict[str, dict[str, object]] = {
    "vmxvcp": {
        "partner_prefix": "vmxvfp",
        "role": "vcp",
        "partner_role": "vfp",
        "is_primary": True,
        "ram_default": 2048,
        "partner_ram_default": 4096,
    },
    "vmxvfp": {
        "partner_prefix": "vmxvcp",
        "role": "vfp",
        "partner_role": "vcp",
        "is_primary": False,
        "ram_default": 4096,
        "partner_ram_default": 2048,
    },
    "vqfxre": {
        "partner_prefix": "vqfxpfe",
        "role": "re",
        "partner_role": "pfe",
        "is_primary": True,
        "ram_default": 1024,
        "partner_ram_default": 2048,
    },
    "vqfxpfe": {
        "partner_prefix": "vqfxre",
        "role": "re",
        "partner_role": "pfe",
        "is_primary": False,
        "ram_default": 2048,
        "partner_ram_default": 1024,
    },
}


def _find_paired_vm_spec(image_key: str) -> tuple[str, dict[str, object]] | None:
    """Return (prefix, spec) if ``image_key`` matches a known paired-VM prefix."""
    lower = image_key.lower()
    for prefix, spec in _PAIRED_VM_MAP.items():
        if lower.startswith(prefix):
            return prefix, spec
    return None


def _find_paired_counterpart(
    partner_prefix: str,
    version_suffix: str,
    all_items: list[MigrationItem],
) -> MigrationItem | None:
    """Locate the counterpart :class:`MigrationItem` for a paired-VM half.

    The counterpart is identified by (a) directory-name prefix and (b) the
    version suffix matching after the prefix. This survives the common case
    where the version is embedded in the directory name (e.g. ``-18.2R1.9``
    or ``-10K-F-17.4R1.16``).
    """
    target_key = f"{partner_prefix}{version_suffix}".lower()
    for candidate in all_items:
        if candidate.kind != KIND_QEMU:
            continue
        if candidate.image_key.lower() == target_key:
            return candidate
    return None


def _enrich_raw_for_paired_vm(
    item: MigrationItem,
    all_items: list[MigrationItem],
) -> dict[str, object] | None:
    """If ``item`` is a known paired-VM half AND we can locate its partner,
    return an enriched ``raw`` dict suitable for the paired adapter. Returns
    None when the item is not a paired half, or when the primary half is
    absent (in which case the caller should fall back to the standard raw
    synth + adapter dispatch)."""
    found = _find_paired_vm_spec(item.image_key)
    if found is None:
        return None
    our_prefix, spec = found
    version_suffix = item.image_key[len(our_prefix):]  # keeps leading '-' etc
    partner = _find_paired_counterpart(
        str(spec["partner_prefix"]), version_suffix, all_items
    )
    if partner is None:
        # Half is present without its partner — cannot emit a paired
        # template. Return None so the caller falls back to the default
        # dispatch path (which will land in needs-manual-review, giving
        # the operator visibility into the missing half).
        return None

    # Only the primary half emits the paired template; the secondary half
    # produces no template of its own so we do not double-emit.
    if not spec["is_primary"]:
        return {"__paired_absorbed__": True}

    role = str(spec["role"])
    partner_role = str(spec["partner_role"])
    return {
        "image": item.image_key,
        "name": item.image_key,
        "type": item.kind,
        f"image_{role}": item.image_key,
        f"image_{partner_role}": partner.image_key,
        f"ram_{role}": int(spec["ram_default"]),
        f"ram_{partner_role}": int(spec["partner_ram_default"]),
        "_eveng_raw": dict(item.meta),
    }


def _generate_template_for_item(
    item: MigrationItem,
    *,
    templates_dir: Path,
    manifest: ImportManifest,
    all_items: list[MigrationItem] | None = None,
) -> None:
    """Run the EVE-NG vendor-adapter pipeline on one migrated item.

    The adapter registry was previously imported but never exercised
    outside tests (the converted templates were assumed to exist out of
    band). This helper closes that loop: it synthesises a minimal raw
    dict from the migration item's filename + meta, dispatches to the
    highest-priority matching :class:`VendorAdapter`, and writes the
    resulting nova-ve template YAML under ``USER_TEMPLATES_DIR``.

    Adapters that decide the item needs operator attention raise
    :class:`NeedsManualReview`; that becomes a ``needs-manual-review``
    manifest entry rather than aborting the import.
    """
    # Paired-VM shim: EVE-NG ships some Juniper platforms as two directories
    # (vmxvcp/vmxvfp, vqfxre/vqfxpfe) that must dispatch as a single raw
    # dict. The shim looks for the counterpart directory in this import
    # batch and, when found, populates ``image_<role>`` / ``ram_<role>``
    # fields so the paired adapter's REQUIRED_FIELDS are satisfied.
    if all_items is not None:
        enriched = _enrich_raw_for_paired_vm(item, all_items)
        if enriched is not None:
            if enriched.get("__paired_absorbed__"):
                # Secondary half — its primary counterpart will emit the
                # paired template, so we drop it here to avoid duplicates.
                manifest.templates.append(
                    TemplateEntry(
                        name=item.image_key,
                        status="skipped",
                        reason="absorbed into paired-VM template",
                    )
                )
                return
            raw = enriched
        else:
            raw = {
                "image": item.image_key,
                "name": item.image_key,
                "type": item.kind,
                "_eveng_raw": dict(item.meta),
            }
            for key, value in item.meta.items():
                raw.setdefault(key, value)
    else:
        # Legacy caller (tests) with no all_items — behave exactly like
        # the pre-shim path.
        raw = {
            "image": item.image_key,
            "name": item.image_key,
            "type": item.kind,
            "_eveng_raw": dict(item.meta),
        }
        for key, value in item.meta.items():
            raw.setdefault(key, value)

    adapter = select_adapter(raw)
    if adapter is None:
        # No adapter claimed the image. Record as a skipped template so
        # the manifest still surfaces the gap without aborting.
        manifest.templates.append(
            TemplateEntry(
                name=item.image_key,
                status="skipped",
                reason="no vendor adapter matched",
            )
        )
        return

    try:
        template_payload = adapter.convert(raw, item.dst_dir)
    except NeedsManualReview as exc:
        manifest.templates.append(
            TemplateEntry(
                name=item.image_key,
                status="needs-manual-review",
                reason=str(exc),
            )
        )
        return

    template_type = str(template_payload.get("kind") or item.kind)
    dest = templates_dir / template_type / f"{item.image_key}.yml"
    # ``type`` is required by the template loader; write it alongside
    # ``kind`` to satisfy both the loader and the legacy schema.
    template_payload.setdefault("type", template_type)
    _write_template_yaml(dest, template_payload)
    manifest.templates.append(
        TemplateEntry(
            name=item.image_key,
            status="generated",
            reason=f"adapter={adapter.name}",
        )
    )


def _yaml_equivalent(path: Path, payload: dict[str, object]) -> bool:
    try:
        existing = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return False
    return existing == payload


def _convert_template_json_to_yaml(
    json_path: Path,
    *,
    manifest: ImportManifest,
) -> None:
    yaml_path = json_path.with_suffix(".yml")
    try:
        payload = json.loads(json_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        manifest.errors.append(
            ErrorEntry(
                path=str(json_path),
                error=f"template JSON conversion failed: {exc}",
            )
        )
        return

    if not isinstance(payload, dict):
        manifest.errors.append(
            ErrorEntry(
                path=str(json_path),
                error="template JSON conversion failed: top-level value is not an object",
            )
        )
        return

    if yaml_path.exists() and not _yaml_equivalent(yaml_path, payload):
        manifest.errors.append(
            ErrorEntry(
                path=str(json_path),
                error=f"template YAML already exists with different content: {yaml_path}",
            )
        )
        return

    try:
        if not yaml_path.exists():
            _write_template_yaml(yaml_path, payload)
        json_path.unlink()
    except OSError as exc:
        manifest.errors.append(
            ErrorEntry(
                path=str(json_path),
                error=f"template JSON conversion failed: {exc}",
            )
        )
        return
    manifest.templates.append(
        TemplateEntry(
            name=json_path.stem,
            status="converted",
            reason=f"{json_path} -> {yaml_path}",
        )
    )


def _convert_existing_template_json_to_yaml(
    templates_dir: Path,
    *,
    manifest: ImportManifest,
) -> None:
    """Convert imported user template JSON files under ``templates/*/*.json``."""
    if not templates_dir.exists():
        return
    for json_path in sorted(templates_dir.glob("*/*.json")):
        _convert_template_json_to_yaml(json_path, manifest=manifest)


# ---- top-level entry ----------------------------------------------------


def run_migration(
    items: list[MigrationItem],
    *,
    options: MigrateOptions,
    manifest: ImportManifest,
    owner: AppOwner | None = None,
    docker_build: DockerBuildFn | None = None,
    dest_root: Path | None = None,
    templates_dir: Path | None = None,
) -> None:
    """Migrate every item; record outcomes on ``manifest``.

    If ``owner`` is provided, every destination dir / file gets owner+perm
    fixup applied. If ``dest_root`` is provided, the entire dest tree is
    re-walked for fixup at the end (catches the pre-existing root-owned
    ``<dest>/qemu/`` parent dir bug). If ``templates_dir`` is provided,
    pre-existing imported template JSON files are converted to YAML, and every
    migrated item runs through the vendor-adapter registry to produce a nova-ve
    template YAML under that path; if it is ``None``, template generation and
    conversion are skipped entirely (legacy mode — the importer only copies
    images and lets the operator hand-author templates).
    """
    docker_build = docker_build or _default_docker_build
    if templates_dir is not None:
        _convert_existing_template_json_to_yaml(templates_dir, manifest=manifest)

    for item in items:
        if item.kind == KIND_QEMU:
            migrate_qemu(item, options, manifest)
        elif item.kind == KIND_DYNAMIPS:
            migrate_dynamips(item, options, manifest)
        elif item.kind == KIND_IOL:
            migrate_iol(item, options, manifest)
        elif item.kind == KIND_DOCKER:
            migrate_docker(item, options, manifest, docker_build)
        else:  # pragma: no cover — defensive
            manifest.errors.append(
                ErrorEntry(path=str(item.src_dir), error=f"unknown kind: {item.kind}")
            )
            continue

        if templates_dir is not None:
            _generate_template_for_item(
                item, templates_dir=templates_dir, manifest=manifest, all_items=items
            )

    if owner is not None:
        if dest_root is not None and dest_root.exists():
            fixup_tree(dest_root, owner)
        else:
            for item in items:
                if item.dst_dir.exists():
                    fixup_tree(item.dst_dir, owner)
        if templates_dir is not None and templates_dir.exists():
            fixup_tree(templates_dir, owner)
