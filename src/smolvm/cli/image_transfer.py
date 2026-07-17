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

"""``smolvm image save`` / ``smolvm image load`` — move images between machines.

The archive is a plain tar whose first member is ``manifest.json``
(schema_version 1) describing every other member. Large files are stored as
zstd streams so sparse rootfs images stay small; the decompressed
``rootfs.ext4`` and its sidecar are omitted entirely when the compressed
``rootfs.ext4.zst`` is present and are recreated on load, byte-compatible
with what ``ensure_published_image`` expects.
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from smolvm import __version__
from smolvm.cli.image import (
    _IMAGE_DIR_NAME_RE,
    _classify,
    _expand_custom_entries,
    _fail,
    _non_default_dir_warnings,
    _normalize_rm_name,
    _resolve_entries,
    _valid_entry_name,
)
from smolvm.cli.output import console_stdout, emit_success
from smolvm.cli.prune import _format_bytes, _total_size
from smolvm.images.manager import resolve_image_dir

_SCHEMA_VERSION = 1
_COPY_CHUNK = 1024 * 1024

# The decompressed rootfs and its sidecar are recreated on load from the
# compressed wire file, so archives never carry them alongside it.
_DECOMPRESSED_ROOTFS = "rootfs.ext4"
_ROOTFS_ZST = "rootfs.ext4.zst"
_ROOTFS_SIDECAR = "rootfs.ext4.from-sha256"


class ImageSavePayload(TypedDict):
    name: str
    archive_path: str
    archive_size_bytes: int
    files: int
    excluded_decompressed_rootfs: bool


class ImageLoadPayload(TypedDict):
    name: str
    path: str
    size_bytes: int
    files: int
    warnings: list[str]


def _valid_relative_file_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    return all(segment not in {"", ".", ".."} for segment in path.split("/"))


def run_image_save(
    *,
    name: str,
    output: str,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.save",
) -> int:
    """Execute ``smolvm image save``."""
    import zstandard

    root = resolve_image_dir(image_dir)
    requested = _normalize_rm_name(name, root)

    if not _valid_entry_name(requested):
        return _fail(
            command_name,
            f"'{name}' is not a cached image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Run 'smolvm image list' to see cached images.",
            exit_code=2,
        )

    try:
        matches = _expand_custom_entries(_resolve_entries(requested, root), root)
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {exc}",
            json_output=json_output,
            recovery=f"Fix the folder's permissions, then retry 'smolvm image save {requested}'.",
        )

    if not matches:
        return _fail(
            command_name,
            f"No cached image named '{requested}'.",
            json_output=json_output,
            recovery="Run 'smolvm image list' to see cached images.",
        )
    if len(matches) > 1:
        options = ", ".join(sorted(m.relative_to(root).as_posix() for m in matches))
        return _fail(
            command_name,
            f"'{requested}' matches more than one cached image. Pick one of: {options}.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )

    entry = matches[0]
    archive_name = entry.relative_to(root).as_posix()
    out_path = Path(output)
    retry_command = f"smolvm image save {archive_name} -o {shlex.quote(str(out_path))}"

    zst_present = (entry / _ROOTFS_ZST).is_file()
    excluded = {_DECOMPRESSED_ROOTFS, _ROOTFS_SIDECAR} if zst_present else set()
    sidecar_value: str | None = None
    if zst_present and (entry / _ROOTFS_SIDECAR).is_file():
        try:
            sidecar_value = (entry / _ROOTFS_SIDECAR).read_text().strip()
        except OSError:
            sidecar_value = None

    files_meta: list[dict[str, Any]] = []
    try:
        tmp_parent = str(out_path.parent) if out_path.parent.is_dir() else None
        with tempfile.TemporaryDirectory(dir=tmp_parent) as tmp:
            # (source path on disk, archive member name)
            staged: list[tuple[Path, str]] = []
            for f in sorted(entry.rglob("*")):
                if f.is_symlink() or not f.is_file():
                    continue
                rel = f.relative_to(entry).as_posix()
                if rel in excluded or rel == ".build.lock":
                    continue
                if rel.endswith(".zst"):
                    files_meta.append({"path": rel, "encoding": "raw", "size": f.stat().st_size})
                    staged.append((f, f"files/{rel}"))
                else:
                    compressed = Path(tmp) / f"{len(staged)}.zst"
                    with open(f, "rb") as src, open(compressed, "wb") as dst:
                        zstandard.ZstdCompressor().copy_stream(src, dst)
                    files_meta.append({"path": rel, "encoding": "zstd", "size": f.stat().st_size})
                    staged.append((compressed, f"files/{rel}.zst"))

            manifest = {
                "schema_version": _SCHEMA_VERSION,
                "saved_by": __version__,
                "created": datetime.now(timezone.utc).isoformat(),
                "name": archive_name,
                "kind": _classify(entry)["kind"] if "/" not in archive_name else "custom",
                "rootfs_sidecar": sidecar_value,
                "files": files_meta,
            }
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            with tarfile.open(out_path, "w") as tar:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(manifest_bytes)
                tar.addfile(info, io.BytesIO(manifest_bytes))
                for source, arcname in staged:
                    tar.add(source, arcname=arcname, recursive=False)
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        return _fail(
            command_name,
            f"Could not write '{out_path}': {exc}",
            json_output=json_output,
            recovery=f"Free up disk space or fix permissions, then retry '{retry_command}'.",
        )

    payload = ImageSavePayload(
        name=archive_name,
        archive_path=str(out_path),
        archive_size_bytes=out_path.stat().st_size,
        files=len(files_meta),
        excluded_decompressed_rootfs=zst_present,
    )
    if json_output:
        return emit_success(command_name, payload)
    console = console_stdout()
    console.print(
        f"Saved [bold]{archive_name}[/bold] to {out_path} "
        f"({_format_bytes(payload['archive_size_bytes'])}). Load it on another "
        f"machine with 'smolvm image load -i {shlex.quote(str(out_path))}'."
    )
    return 0


def run_image_load(
    *,
    input_file: str,
    image_dir: str | None = None,
    force: bool = False,
    json_output: bool = False,
    command_name: str = "image.load",
) -> int:
    """Execute ``smolvm image load``."""
    import zstandard

    from smolvm.host.disk import decompress_zstd_sparse

    root = resolve_image_dir(image_dir)
    in_path = Path(input_file)
    quoted_input = shlex.quote(str(input_file))
    not_an_archive = "Check that the file was created by 'smolvm image save' and is not corrupted."

    try:
        tar = tarfile.open(in_path, "r:")  # noqa: SIM115 — closed by `with tar:` below
    except (OSError, tarfile.TarError) as exc:
        return _fail(
            command_name,
            f"Could not open '{in_path}': {exc}",
            json_output=json_output,
            code="invalid_input",
            recovery=not_an_archive,
            exit_code=2,
        )

    with tar:
        members = tar.getmembers()
        if not members or members[0].name != "manifest.json" or not members[0].isreg():
            return _fail(
                command_name,
                f"'{in_path}' is not a SmolVM image archive.",
                json_output=json_output,
                code="invalid_input",
                recovery=not_an_archive,
                exit_code=2,
            )
        manifest_file = tar.extractfile(members[0])
        try:
            manifest = json.load(manifest_file) if manifest_file else None
        except ValueError:
            manifest = None
        if not isinstance(manifest, dict):
            return _fail(
                command_name,
                f"'{in_path}' is not a SmolVM image archive.",
                json_output=json_output,
                code="invalid_input",
                recovery=not_an_archive,
                exit_code=2,
            )

        schema = manifest.get("schema_version")
        if isinstance(schema, int) and schema > _SCHEMA_VERSION:
            return _fail(
                command_name,
                "This archive was saved by a newer SmolVM.",
                json_output=json_output,
                code="invalid_input",
                recovery=f"Run 'smolvm update', then retry 'smolvm image load -i {quoted_input}'.",
                exit_code=2,
            )
        name = manifest.get("name")
        if schema != _SCHEMA_VERSION or not isinstance(name, str) or not _valid_entry_name(name):
            return _fail(
                command_name,
                f"'{in_path}' is not a SmolVM image archive.",
                json_output=json_output,
                code="invalid_input",
                recovery=not_an_archive,
                exit_code=2,
            )

        # Only members declared in the manifest, as regular files, at safe
        # relative paths, are ever written to disk — extraction below builds
        # its own destination paths and never uses tar member names as paths.
        allowed: dict[str, dict[str, Any]] = {}
        raw_files = manifest.get("files")
        for meta in raw_files if isinstance(raw_files, list) else [None]:
            if (
                not isinstance(meta, dict)
                or not isinstance(meta.get("path"), str)
                or meta.get("encoding") not in {"raw", "zstd"}
                or not _valid_relative_file_path(meta["path"])
            ):
                return _fail(
                    command_name,
                    f"'{in_path}' is not a SmolVM image archive.",
                    json_output=json_output,
                    code="invalid_input",
                    recovery=not_an_archive,
                    exit_code=2,
                )
            suffix = ".zst" if meta["encoding"] == "zstd" else ""
            allowed[f"files/{meta['path']}{suffix}"] = meta
        for member in members[1:]:
            if not member.isreg() or member.name not in allowed:
                return _fail(
                    command_name,
                    f"'{in_path}' contains unexpected entries and was not loaded.",
                    json_output=json_output,
                    code="invalid_input",
                    recovery="Recreate the archive with 'smolvm image save'.",
                    exit_code=2,
                )

        target = root / name
        if target.exists() and not force:
            return _fail(
                command_name,
                f"'{name}' already exists.",
                json_output=json_output,
                recovery=f"Run 'smolvm image load -i {quoted_input} --force' to replace it.",
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.parent / f".{target.name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        try:
            partial.mkdir()
            zst_sha: str | None = None
            for member in members[1:]:
                meta = allowed[member.name]
                destination = partial / meta["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise OSError(f"could not read '{member.name}' from the archive")
                if meta["encoding"] == "raw":
                    hasher = hashlib.sha256() if meta["path"] == _ROOTFS_ZST else None
                    with source, open(destination, "wb") as out:
                        while chunk := source.read(_COPY_CHUNK):
                            out.write(chunk)
                            if hasher is not None:
                                hasher.update(chunk)
                    if hasher is not None:
                        zst_sha = hasher.hexdigest()
                else:
                    tmp_zst = destination.with_name(destination.name + ".tmp.zst")
                    with source, open(tmp_zst, "wb") as out:
                        while chunk := source.read(_COPY_CHUNK):
                            out.write(chunk)
                    decompress_zstd_sparse(tmp_zst, destination)
                    tmp_zst.unlink()

            # Recreate what save deliberately left out, byte-compatible with
            # what ensure_published_image validates on the next start.
            zst = partial / _ROOTFS_ZST
            rootfs = partial / _DECOMPRESSED_ROOTFS
            if zst.is_file() and not rootfs.is_file():
                decompress_zstd_sparse(zst, rootfs)
                sidecar_value = manifest.get("rootfs_sidecar")
                if not isinstance(sidecar_value, str) or not sidecar_value:
                    sidecar_value = f"sparse-v1:{zst_sha}" if zst_sha else None
                if sidecar_value:
                    (partial / _ROOTFS_SIDECAR).write_text(sidecar_value)

            if target.exists():
                shutil.rmtree(target)
            partial.rename(target)
        except (OSError, tarfile.TarError, zstandard.ZstdError) as exc:
            shutil.rmtree(partial, ignore_errors=True)
            return _fail(
                command_name,
                f"Could not load the image: {exc}",
                json_output=json_output,
                recovery=f"Free up disk space, then retry 'smolvm image load -i {quoted_input}'.",
            )

    warnings = _non_default_dir_warnings(root)
    stale_match = _IMAGE_DIR_NAME_RE.match(name)
    if stale_match and stale_match["version"] != __version__:
        warnings.append(
            "This image comes from a different SmolVM version, so it shows "
            "as (stale) in 'smolvm image list'."
        )

    payload = ImageLoadPayload(
        name=name,
        path=str(target),
        size_bytes=_total_size(target),
        files=len(members) - 1,
        warnings=warnings,
    )
    if json_output:
        return emit_success(command_name, payload)
    console = console_stdout()
    console.print(
        f"Loaded [bold]{name}[/bold] ({_format_bytes(payload['size_bytes'])}). "
        "Run 'smolvm image list' to confirm."
    )
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    return 0
