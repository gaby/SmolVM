# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SmolVM image-cache management — pull, list, and remove cached images.

Backs the ``smolvm image`` command group. Images normally download lazily
the first time a sandbox starts; ``image pull`` fetches them ahead of time
(offline prep, CI warm-up), ``image list`` shows what is cached, and
``image rm`` frees disk space for a single image. All commands operate on
the directory returned by :func:`smolvm.images.manager.resolve_image_dir`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, TypedDict, cast

from smolvm import __version__
from smolvm.cli.output import (
    console_stdout,
    emit_error,
    emit_success,
    render_empty,
    render_error,
)
from smolvm.cli.prune import _format_bytes, _total_size
from smolvm.images.manager import resolve_image_dir

# Cache directory names produced by ``published.cache_name()``:
# ``{preset}-v{cli_version}-{arch}-{vmm}`` with a ``-alpine`` suffix for
# Alpine rootfs builds (Ubuntu carries no suffix for backward compat).
# The preset group is non-greedy so hyphenated presets (``claude-code``)
# parse correctly against the ``-v<semver>-`` anchor.
_IMAGE_DIR_NAME_RE = re.compile(
    r"^(?P<preset>[a-z0-9-]+?)-v(?P<version>\d+\.\d+\.\d+[a-z0-9]*)"
    r"-(?P<arch>amd64|arm64)-(?P<vmm>firecracker|qemu|libkrun)(?:-(?P<os>alpine))?$"
)

# Base kernel dirs from ``published.ensure_base_kernel()``.
_KERNEL_DIR_NAME_RE = re.compile(
    r"^base-kernel-v(?P<version>\d+\.\d+\.\d+[a-z0-9]*)-(?P<arch>amd64|arm64)$"
)

# Public CLI names that differ from the manifest preset name.
_PRESET_ALIASES = {"claude": "claude-code"}


class ImageRow(TypedDict):
    """One cached entry in the image directory."""

    name: str
    kind: str  # "image" | "kernel" | "other"
    preset: str | None
    version: str | None
    arch: str | None
    vmm: str | None
    os: str | None
    current: bool | None  # matches this CLI version; None when unversioned
    size_bytes: int
    path: str


class ImageListPayload(TypedDict):
    image_dir: str
    images: list[ImageRow]
    total_size_bytes: int


class ImagePullPayload(TypedDict):
    preset: str
    arch: str
    vmm: str
    os: str
    version: str
    name: str
    kernel_path: str
    rootfs_path: str
    size_bytes: int
    already_cached: bool


class RemovedEntry(TypedDict):
    name: str
    path: str
    size_bytes: int


class ImageRmPayload(TypedDict):
    removed: list[RemovedEntry]
    freed_bytes: int


def _fail(
    command_name: str,
    message: str,
    *,
    json_output: bool,
    code: str = "runtime_error",
    recovery: str | None = None,
    exit_code: int = 1,
) -> int:
    """Report a failure in JSON or Rich form and return the exit code."""
    if json_output:
        return emit_error(command_name, code, message, recovery=recovery, exit_code=exit_code)
    render_error(message, hint=recovery)
    return exit_code


def _classify(path: Path) -> ImageRow:
    """Build an :class:`ImageRow` for one child directory of the image dir."""
    name = path.name
    size = _total_size(path)
    image_match = _IMAGE_DIR_NAME_RE.match(name)
    if image_match:
        return ImageRow(
            name=name,
            kind="image",
            preset=image_match["preset"],
            version=image_match["version"],
            arch=image_match["arch"],
            vmm=image_match["vmm"],
            os=image_match["os"] or "ubuntu",
            current=image_match["version"] == __version__,
            size_bytes=size,
            path=str(path),
        )
    kernel_match = _KERNEL_DIR_NAME_RE.match(name)
    if kernel_match:
        return ImageRow(
            name=name,
            kind="kernel",
            preset=None,
            version=kernel_match["version"],
            arch=kernel_match["arch"],
            vmm=None,
            os=None,
            current=kernel_match["version"] == __version__,
            size_bytes=size,
            path=str(path),
        )
    return ImageRow(
        name=name,
        kind="other",
        preset=None,
        version=None,
        arch=None,
        vmm=None,
        os=None,
        current=None,
        size_bytes=size,
        path=str(path),
    )


def _cached_rows(root: Path) -> list[ImageRow]:
    """Classify every child directory of *root* (empty when it's missing)."""
    if not root.is_dir():
        return []
    return [_classify(child) for child in sorted(root.iterdir()) if child.is_dir()]


def run_image_pull(
    *,
    preset: str,
    arch: str | None = None,
    vmm: str | None = None,
    os_name: str | None = None,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.pull",
) -> int:
    """Execute ``smolvm image pull``."""
    from smolvm.cli.main import _host_arch_for_published, _vmm_for_host
    from smolvm.images.published import (
        MANIFEST,
        Arch,
        Os,
        Preset,
        Vmm,
        cache_name,
        ensure_published_image,
        is_preset_published,
    )

    canonical = _PRESET_ALIASES.get(preset, preset)

    try:
        resolved_arch = arch or _host_arch_for_published()
        resolved_vmm = vmm or _vmm_for_host()
    except RuntimeError as exc:
        return _fail(
            command_name,
            f"{exc} Pass the platform explicitly, e.g. "
            f"'smolvm image pull {preset} --arch amd64 --vmm firecracker'.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )
    resolved_os = os_name or "ubuntu"

    if not is_preset_published(
        canonical, cast(Arch, resolved_arch), cast(Vmm, resolved_vmm), cast(Os, resolved_os)
    ):
        known_presets = sorted({key[0] for key in MANIFEST})
        if canonical not in known_presets:
            return _fail(
                command_name,
                f"'{preset}' is not a published image. Choose one of: {', '.join(known_presets)}.",
                json_output=json_output,
                code="invalid_input",
                exit_code=2,
            )
        return _fail(
            command_name,
            f"No published image for '{preset}' on "
            f"{resolved_arch}/{resolved_vmm} with os '{resolved_os}'. Run "
            f"'smolvm image pull {preset}' to use the defaults for this machine.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )

    root = resolve_image_dir(image_dir)
    name = cache_name(
        cast(Preset, canonical),
        cast(Arch, resolved_arch),
        cast(Vmm, resolved_vmm),
        os=cast(Os, resolved_os),
    )
    downloaded_labels: set[str] = set()

    try:
        if json_output:

            def on_download(label: str, chunk: int, total: int | None) -> None:
                downloaded_labels.add(label)

            local = ensure_published_image(
                cast(Preset, canonical),
                cast(Arch, resolved_arch),
                cast(Vmm, resolved_vmm),
                cast(Os, resolved_os),
                cache_dir=root,
                on_download=on_download,
            )
        else:
            from rich.progress import (
                BarColumn,
                DownloadColumn,
                Progress,
                SpinnerColumn,
                TextColumn,
                TimeElapsedColumn,
                TransferSpeedColumn,
            )

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=console_stdout(),
                transient=True,
            ) as progress:
                download_tasks: dict[str, Any] = {}

                def on_progress(label: str, chunk: int, total: int | None) -> None:
                    downloaded_labels.add(label)
                    if label not in download_tasks:
                        download_tasks[label] = progress.add_task(
                            f"Downloading {label}", total=total
                        )
                    progress.update(download_tasks[label], advance=chunk)

                local = ensure_published_image(
                    cast("Preset", canonical),
                    cast("Arch", resolved_arch),
                    cast("Vmm", resolved_vmm),
                    cast("Os", resolved_os),
                    cache_dir=root,
                    on_download=on_progress,
                )
    except Exception as exc:
        return _fail(
            command_name,
            f"Could not download the image: {exc}",
            json_output=json_output,
            recovery=f"Check your network connection and retry 'smolvm image pull {preset}'.",
        )

    payload = ImagePullPayload(
        preset=canonical,
        arch=resolved_arch,
        vmm=resolved_vmm,
        os=resolved_os,
        version=__version__,
        name=name,
        kernel_path=str(local.kernel_path),
        rootfs_path=str(local.rootfs_path),
        size_bytes=_total_size(root / name),
        already_cached=not downloaded_labels,
    )
    if json_output:
        return emit_success(command_name, payload)

    console = console_stdout()
    size = _format_bytes(payload["size_bytes"])
    if payload["already_cached"]:
        console.print(f"Already cached: [bold]{name}[/bold] ({size})")
    else:
        console.print(f"Pulled [bold]{name}[/bold] ({size})")
    console.print(f"  kernel: {payload['kernel_path']}")
    console.print(f"  rootfs: {payload['rootfs_path']}")
    return 0


def run_image_list(
    *,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.list",
) -> int:
    """Execute ``smolvm image list``."""
    root = resolve_image_dir(image_dir)
    rows = _cached_rows(root)
    payload = ImageListPayload(
        image_dir=str(root),
        images=rows,
        total_size_bytes=sum(row["size_bytes"] for row in rows),
    )
    if json_output:
        return emit_success(command_name, payload)

    if not rows:
        render_empty(
            "Images",
            f"No cached images in {root}. Run 'smolvm image pull <preset>' to download one.",
        )
        return 0

    from rich.table import Table

    table = Table(title="Cached Images")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Platform")
    table.add_column("Version")
    table.add_column("Size", justify="right")
    for row in rows:
        platform_parts = [part for part in (row["arch"], row["vmm"], row["os"]) if part]
        version = row["version"] or "-"
        if row["current"] is False:
            version = f"[yellow]{version} (stale)[/yellow]"
        table.add_row(
            row["name"],
            row["kind"],
            "/".join(platform_parts) or "-",
            version,
            _format_bytes(row["size_bytes"]),
        )
    console = console_stdout()
    console.print(table)
    console.print(
        f"Total: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, "
        f"{_format_bytes(payload['total_size_bytes'])} in {root}"
    )
    return 0


def run_image_rm(
    *,
    name: str,
    image_dir: str | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    command_name: str = "image.rm",
) -> int:
    """Execute ``smolvm image rm``."""
    root = resolve_image_dir(image_dir)

    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return _fail(
            command_name,
            f"'{name}' is not a cached image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Run 'smolvm image list' to see cached images.",
            exit_code=2,
        )

    targets: list[Path] = []
    exact = root / name
    if exact.is_dir():
        targets = [exact]
    elif root.is_dir():
        # Fall back to preset-wide removal: every cached image whose parsed
        # preset matches, across versions, arches, vmms, and oses.
        canonical = _PRESET_ALIASES.get(name, name)
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            match = _IMAGE_DIR_NAME_RE.match(child.name)
            if match and match["preset"] == canonical:
                targets.append(child)

    if not targets:
        return _fail(
            command_name,
            f"No cached image named '{name}'.",
            json_output=json_output,
            recovery="Run 'smolvm image list' to see cached images.",
        )

    resolved_root = root.resolve()
    entries: list[RemovedEntry] = []
    for target in targets:
        # Deletion is confined to direct children of the image directory;
        # a symlinked entry pointing elsewhere is refused rather than
        # followed.
        if target.resolve().parent != resolved_root:
            return _fail(
                command_name,
                f"'{target.name}' points outside the image directory and was not removed.",
                json_output=json_output,
                recovery=f"Remove it manually: {target}",
            )
        entries.append(
            RemovedEntry(name=target.name, path=str(target), size_bytes=_total_size(target))
        )

    freed = sum(entry["size_bytes"] for entry in entries)

    if dry_run:
        if json_output:
            return emit_success(
                command_name,
                {"dry_run": True, "would_remove": entries, "would_free_bytes": freed},
            )
        console = console_stdout()
        console.print(
            f"[bold]Would remove {len(entries)} image(s) ({_format_bytes(freed)}):[/bold]"
        )
        for entry in entries:
            console.print(f"  {entry['name']}  ({_format_bytes(entry['size_bytes'])})")
        return 0

    for target in targets:
        shutil.rmtree(target)

    payload = ImageRmPayload(removed=entries, freed_bytes=freed)
    if json_output:
        return emit_success(command_name, payload)
    console = console_stdout()
    console.print(f"Removed {len(entries)} image(s), freed {_format_bytes(freed)}.")
    return 0
