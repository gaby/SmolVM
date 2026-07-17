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

"""Tests for `smolvm image save` / `smolvm image load`."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest
import zstandard

from smolvm.cli.main import main

_SPARSE_PAYLOAD = b"\x00" * (1 << 20) + b"REAL-DATA" + b"\x00" * (1 << 20)


def _make_published_entry(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "rootfs.ext4.zst").write_bytes(zstandard.compress(_SPARSE_PAYLOAD))
    sha = hashlib.sha256((d / "rootfs.ext4.zst").read_bytes()).hexdigest()
    (d / "rootfs.ext4.from-sha256").write_text(f"sparse-v1:{sha}")
    (d / "vmlinux.bin").write_bytes(b"kernel-bytes")
    with open(d / "rootfs.ext4", "wb") as f:
        f.truncate(len(_SPARSE_PAYLOAD))
        f.seek(1 << 20)
        f.write(b"REAL-DATA")
    return d


def _write_archive(
    path: Path, manifest: dict[str, Any], members: list[tarfile.TarInfo | tuple[str, bytes]]
) -> None:
    with tarfile.open(path, "w") as tar:
        blob = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
        for member in members:
            if isinstance(member, tuple):
                name, data = member
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(member)


class TestSaveLoadRoundTrip:
    def test_published_entry_round_trips(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        src = tmp_path / "src"
        entry = _make_published_entry(src, "codex-v0.0.1-amd64-firecracker")
        archive = tmp_path / "codex.tar"

        ret = main(
            ["image", "save", "codex", "--image-dir", str(src), "-o", str(archive), "--json"]
        )
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.save"
        assert payload["data"]["excluded_decompressed_rootfs"] is True

        # The multi-MB decompressed rootfs never travels.
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert names[0] == "manifest.json"
        assert "files/rootfs.ext4" not in names
        assert "files/rootfs.ext4.zst" in names
        assert archive.stat().st_size < 64 * 1024

        dest = tmp_path / "dest"
        ret = main(["image", "load", "-i", str(archive), "--image-dir", str(dest), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.load"

        loaded = dest / "codex-v0.0.1-amd64-firecracker"
        assert sorted(p.name for p in loaded.iterdir()) == [
            "rootfs.ext4",
            "rootfs.ext4.from-sha256",
            "rootfs.ext4.zst",
            "vmlinux.bin",
        ]
        # Sidecar reproduced exactly, so ensure_published_image accepts it.
        assert (loaded / "rootfs.ext4.from-sha256").read_text() == (
            entry / "rootfs.ext4.from-sha256"
        ).read_text()
        # Rootfs content intact and holes restored.
        with open(loaded / "rootfs.ext4", "rb") as f:
            f.seek(1 << 20)
            assert f.read(9) == b"REAL-DATA"
        st = os.stat(loaded / "rootfs.ext4")
        if getattr(st, "st_blocks", None) is not None:
            assert st.st_blocks * 512 < st.st_size

    def test_custom_entry_round_trips(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        src = tmp_path / "src"
        fp = src / "custom" / "myimg" / "abc123def4567890"
        fp.mkdir(parents=True)
        (fp / "metadata.json").write_text(json.dumps({"name": "myimg", "arch": "amd64"}))
        (fp / "rootfs.ext4").write_bytes(b"custom-rootfs")
        (fp / ".build.lock").write_text("")
        archive = tmp_path / "myimg.tar"

        ret = main(
            [
                "image",
                "save",
                "custom/myimg",
                "--image-dir",
                str(src),
                "-o",
                str(archive),
                "--json",
            ]
        )
        assert ret == 0
        capsys.readouterr()

        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert "files/.build.lock" not in names and "files/.build.lock.zst" not in names

        dest = tmp_path / "dest"
        ret = main(["image", "load", "-i", str(archive), "--image-dir", str(dest), "--json"])
        assert ret == 0
        capsys.readouterr()
        loaded = dest / "custom" / "myimg" / "abc123def4567890"
        assert (loaded / "rootfs.ext4").read_bytes() == b"custom-rootfs"
        assert json.loads((loaded / "metadata.json").read_text())["arch"] == "amd64"

    def test_load_refuses_overwrite_without_force(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        src = tmp_path / "src"
        _make_published_entry(src, "codex-v0.0.1-amd64-firecracker")
        archive = tmp_path / "codex.tar"
        assert main(["image", "save", "codex", "--image-dir", str(src), "-o", str(archive)]) == 0
        capsys.readouterr()

        dest = tmp_path / "dest"
        assert main(["image", "load", "-i", str(archive), "--image-dir", str(dest)]) == 0
        capsys.readouterr()

        ret = main(["image", "load", "-i", str(archive), "--image-dir", str(dest), "--json"])
        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert "--force" in payload["error"]["recovery"]

        ret = main(
            ["image", "load", "-i", str(archive), "--image-dir", str(dest), "--force", "--json"]
        )
        assert ret == 0

    def test_save_ambiguous_preset_lists_options(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        src = tmp_path / "src"
        _make_published_entry(src, "codex-v0.0.1-amd64-firecracker")
        _make_published_entry(src, "codex-v0.0.2-amd64-qemu")

        ret = main(
            [
                "image",
                "save",
                "codex",
                "--image-dir",
                str(src),
                "-o",
                str(tmp_path / "x.tar"),
                "--json",
            ]
        )
        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "codex-v0.0.1-amd64-firecracker" in payload["error"]["message"]
        assert "codex-v0.0.2-amd64-qemu" in payload["error"]["message"]


class TestLoadRejectsMaliciousArchives:
    def _assert_rejected(
        self, archive: Path, dest: Path, capsys: pytest.CaptureFixture
    ) -> dict[str, Any]:
        ret = main(["image", "load", "-i", str(archive), "--image-dir", str(dest), "--json"])
        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        # Nothing may be written outside (or inside) the destination.
        assert not dest.exists() or not any(dest.iterdir())
        return dict(payload)

    def test_traversal_path_in_manifest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        archive = tmp_path / "evil.tar"
        _write_archive(
            archive,
            {
                "schema_version": 1,
                "name": "evil",
                "rootfs_sidecar": None,
                "files": [{"path": "../../escape", "encoding": "raw", "size": 4}],
            },
            [("files/../../escape", b"boom")],
        )
        self._assert_rejected(archive, tmp_path / "dest", capsys)
        assert not (tmp_path / "escape").exists()

    def test_absolute_member_name(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        archive = tmp_path / "evil.tar"
        _write_archive(
            archive,
            {
                "schema_version": 1,
                "name": "evil",
                "rootfs_sidecar": None,
                "files": [{"path": "/etc/passwd", "encoding": "raw", "size": 4}],
            },
            [("files//etc/passwd", b"boom")],
        )
        self._assert_rejected(archive, tmp_path / "dest", capsys)

    def test_symlink_member_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        archive = tmp_path / "evil.tar"
        link = tarfile.TarInfo("files/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        _write_archive(
            archive,
            {
                "schema_version": 1,
                "name": "evil",
                "rootfs_sidecar": None,
                "files": [{"path": "link", "encoding": "raw", "size": 0}],
            },
            [link],
        )
        self._assert_rejected(archive, tmp_path / "dest", capsys)

    def test_undeclared_member_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        archive = tmp_path / "evil.tar"
        _write_archive(
            archive,
            {"schema_version": 1, "name": "ok-name", "rootfs_sidecar": None, "files": []},
            [("files/surprise", b"boom")],
        )
        self._assert_rejected(archive, tmp_path / "dest", capsys)

    def test_bad_entry_name_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        archive = tmp_path / "evil.tar"
        _write_archive(
            archive,
            {"schema_version": 1, "name": "../outside", "rootfs_sidecar": None, "files": []},
            [],
        )
        self._assert_rejected(archive, tmp_path / "dest", capsys)

    def test_newer_schema_names_update(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        archive = tmp_path / "future.tar"
        _write_archive(
            archive,
            {"schema_version": 99, "name": "x", "rootfs_sidecar": None, "files": []},
            [],
        )
        ret = main(
            ["image", "load", "-i", str(archive), "--image-dir", str(tmp_path / "dest"), "--json"]
        )
        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "smolvm update" in payload["error"]["recovery"]

    def test_not_a_tar(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        bogus = tmp_path / "bogus.tar"
        bogus.write_bytes(b"not a tar at all")

        ret = main(
            ["image", "load", "-i", str(bogus), "--image-dir", str(tmp_path / "dest"), "--json"]
        )
        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "smolvm image save" in payload["error"]["recovery"]
