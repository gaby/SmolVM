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
import shlex
import shutil
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, TypedDict, cast

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

from smolvm import __version__
from smolvm.cli.output import (
    console_stdout,
    emit_error,
    emit_success,
    render_empty,
    render_error,
)
from smolvm.cli.prune import _VERSION_PATTERN, _format_bytes, _total_size
from smolvm.images.manager import resolve_image_dir

# Cache directory names produced by ``published.cache_name()``:
# ``{preset}-v{cli_version}-{arch}-{vmm}`` with a ``-alpine`` suffix for
# Alpine rootfs builds (Ubuntu carries no suffix for backward compat).
# The preset group is non-greedy so hyphenated presets (``claude-code``)
# parse correctly against the ``-v<version>-`` anchor; the version
# fragment is shared with prune so list/rm/prune agree on what counts as
# a versioned cache directory (a test round-trips it against cache_name()).
_IMAGE_DIR_NAME_RE = re.compile(
    rf"^(?P<preset>[a-z0-9-]+?)-v(?P<version>{_VERSION_PATTERN})"
    r"-(?P<arch>amd64|arm64)-(?P<vmm>firecracker|qemu|libkrun)(?:-(?P<os>alpine))?$"
)

# Base kernel dirs from ``published.ensure_base_kernel()``.
_KERNEL_DIR_NAME_RE = re.compile(
    rf"^base-kernel-v(?P<version>{_VERSION_PATTERN})-(?P<arch>amd64|arm64)$"
)


def _canonical_preset(name: str) -> str:
    """Map a public CLI alias (e.g. ``claude``) to its manifest preset name.

    Alias data lives on the preset definitions themselves so a new alias
    automatically works here without touching this module.
    """
    from smolvm.presets import list_presets

    for preset in list_presets():
        if name == preset.name or name in preset.aliases:
            return preset.name
    return name


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
    warnings: list[str]


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


def _download_progress() -> Progress:
    """The download progress renderer shared with the sandbox boot flow."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console_stdout(),
        transient=True,
    )


def _pull_retry_command(
    preset: str,
    arch: str | None,
    vmm: str | None,
    os_name: str | None,
    image_dir: str | None,
) -> str:
    """The user's pull invocation, reconstructed for recovery messages."""
    parts = ["smolvm", "image", "pull", preset]
    if arch:
        parts += ["--arch", arch]
    if vmm:
        parts += ["--vmm", vmm]
    if os_name:
        parts += ["--os", os_name]
    if image_dir:
        parts += ["--image-dir", shlex.quote(image_dir)]
    return " ".join(parts)


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
    from smolvm.exceptions import ImageError
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

    canonical = _canonical_preset(preset)
    retry_command = _pull_retry_command(preset, arch, vmm, os_name, image_dir)

    try:
        resolved_arch = cast("Arch", arch) if arch else _host_arch_for_published()
        resolved_vmm = cast("Vmm", vmm) if vmm else _vmm_for_host()
    except RuntimeError:
        return _fail(
            command_name,
            "SmolVM doesn't publish prebuilt images for this machine. Pass the "
            f"platform explicitly, e.g. 'smolvm image pull {preset} --arch amd64 "
            "--vmm firecracker'.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )
    resolved_os: Os = cast("Os", os_name) if os_name else "ubuntu"

    if not is_preset_published(canonical, resolved_arch, resolved_vmm, resolved_os):
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

    published_preset = cast("Preset", canonical)
    root = resolve_image_dir(image_dir)
    name = cache_name(published_preset, resolved_arch, resolved_vmm, os=resolved_os)
    target_dir = root / name

    # "Already cached" must mean the pull was a no-op. Downloads are
    # observable through on_download, but rootfs decompression is not, so
    # also watch the cache directory itself: any file (re)created in it —
    # by download or decompression — bumps its mtime.
    pre_mtime = target_dir.stat().st_mtime_ns if target_dir.is_dir() else None
    downloaded = False

    progress_cm: AbstractContextManager[Progress | None] = (
        nullcontext(None) if json_output else _download_progress()
    )
    try:
        with progress_cm as progress:
            download_tasks: dict[str, Any] = {}
            if progress is not None:
                # Visible immediately: SHA verification of cached files and
                # rootfs decompression run without download callbacks and
                # can take a while on multi-GB images.
                progress.add_task("Preparing image...", total=None)

            def on_download(label: str, chunk: int, total: int | None) -> None:
                nonlocal downloaded
                downloaded = True
                if progress is None:
                    return
                if label not in download_tasks:
                    download_tasks[label] = progress.add_task(f"Downloading {label}", total=total)
                progress.update(download_tasks[label], advance=chunk)

            local = ensure_published_image(
                published_preset,
                resolved_arch,
                resolved_vmm,
                resolved_os,
                cache_dir=root,
                on_download=on_download,
            )
    except ImageError as exc:
        return _fail(
            command_name,
            f"Could not download the image: {exc}",
            json_output=json_output,
            recovery=f"Check your network connection and retry '{retry_command}'.",
        )
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not save the image: {exc}",
            json_output=json_output,
            recovery=f"Free up disk space or fix permissions on '{root}', "
            f"then retry '{retry_command}'.",
        )
    except Exception as exc:
        return _fail(
            command_name,
            f"Could not get the image: {exc}",
            json_output=json_output,
            recovery=f"Retry '{retry_command}'.",
        )

    post_mtime = target_dir.stat().st_mtime_ns if target_dir.is_dir() else None
    already_cached = not downloaded and pre_mtime is not None and post_mtime == pre_mtime

    warnings: list[str] = []
    default_root = resolve_image_dir(None)
    if root != default_root:
        warnings.append(
            f"Sandboxes look for images in '{default_root}'. Set "
            f"SMOLVM_IMAGE_DIR={shlex.quote(str(root))} so they use this download."
        )

    size_bytes = _total_size(target_dir)
    payload = ImagePullPayload(
        preset=canonical,
        arch=resolved_arch,
        vmm=resolved_vmm,
        os=resolved_os,
        version=__version__,
        name=name,
        kernel_path=str(local.kernel_path),
        rootfs_path=str(local.rootfs_path),
        size_bytes=size_bytes,
        already_cached=already_cached,
        warnings=warnings,
    )
    if json_output:
        return emit_success(command_name, payload)

    console = console_stdout()
    size = _format_bytes(size_bytes)
    if already_cached:
        console.print(f"Already cached: [bold]{name}[/bold] ({size})")
    else:
        console.print(f"Pulled [bold]{name}[/bold] ({size})")
    console.print(f"  kernel: {local.kernel_path}")
    console.print(f"  rootfs: {local.rootfs_path}")
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    return 0


def run_image_list(
    *,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.list",
) -> int:
    """Execute ``smolvm image list``."""
    root = resolve_image_dir(image_dir)
    try:
        rows = _cached_rows(root)
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {exc}",
            json_output=json_output,
            recovery="Fix the folder's permissions and run 'smolvm image list' again.",
        )
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
            f"No cached images in {root}. Run 'smolvm image pull codex' to download one.",
        )
        return 0

    from rich.table import Table

    table = Table(title="Cached Images")
    # Fold long names instead of truncating: the full name is the handle
    # `smolvm image rm <name>` needs.
    table.add_column("Name", overflow="fold")
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


def _normalize_rm_name(name: str, root: Path) -> str:
    """Normalize what the user typed to a bare cache-entry name.

    Accepts trailing path separators (shell tab completion) and the
    absolute path that ``image list`` prints, as long as it points at a
    direct child of the image directory.
    """
    requested = name.rstrip("/\\")
    candidate = Path(requested)
    if candidate.is_absolute() and candidate.parent in {root, root.resolve()}:
        return candidate.name
    return requested


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
    requested = _normalize_rm_name(name, root)

    if not requested or "/" in requested or "\\" in requested or requested in {".", ".."}:
        return _fail(
            command_name,
            f"'{name}' is not a cached image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Run 'smolvm image list' to see cached images.",
            exit_code=2,
        )

    permission_recovery = f"Fix the folder's permissions, then retry 'smolvm image rm {requested}'."
    matches: list[Path] = []
    try:
        exact = root / requested
        if exact.is_dir() or exact.is_symlink():
            matches = [exact]
        elif root.is_dir():
            # Fall back to preset-wide removal: every cached image whose
            # parsed preset matches, across versions, arches, vmms, and oses.
            canonical = _canonical_preset(requested)
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                match = _IMAGE_DIR_NAME_RE.match(child.name)
                if match and match["preset"] == canonical:
                    matches.append(child)
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {exc}",
            json_output=json_output,
            recovery=permission_recovery,
        )

    if not matches:
        return _fail(
            command_name,
            f"No cached image named '{requested}'.",
            json_output=json_output,
            recovery="Run 'smolvm image list' to see cached images.",
        )

    resolved_root = root.resolve()
    entries: list[RemovedEntry] = []
    for target in matches:
        if target.is_symlink():
            # A link is removed with unlink() — what it points at is never
            # touched — so removing it frees no space.
            entries.append(RemovedEntry(name=target.name, path=str(target), size_bytes=0))
            continue
        # Names are validated above and matches are direct children, so a
        # real directory escaping the root should be impossible; keep a
        # hard stop in front of the recursive delete anyway.
        if target.resolve().parent != resolved_root:
            return _fail(
                command_name,
                f"'{target.name}' could not be removed safely.",
                json_output=json_output,
                recovery=f"Delete it yourself if needed: '{target}'.",
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

    for target in matches:
        try:
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError as exc:
            return _fail(
                command_name,
                f"Could not remove '{target.name}': {exc}",
                json_output=json_output,
                recovery=permission_recovery,
            )

    payload = ImageRmPayload(removed=entries, freed_bytes=freed)
    if json_output:
        return emit_success(command_name, payload)
    console = console_stdout()
    console.print(f"Removed {len(entries)} image(s), freed {_format_bytes(freed)}.")
    return 0
