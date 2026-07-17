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

"""Tests for smolvm image list/rm and the SMOLVM_IMAGE_DIR resolution."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from smolvm import __version__
from smolvm.cli.image import _IMAGE_DIR_NAME_RE, _KERNEL_DIR_NAME_RE
from smolvm.cli.main import main
from smolvm.cli.prune import _format_bytes, _total_size, find_stale_caches
from smolvm.images.manager import IMAGE_DIR_ENV, ImageManager, resolve_image_dir


def _make_cache_dirs(root: Path) -> None:
    """Populate an image dir with representative cache entries."""
    entries = {
        "codex-v0.0.1-amd64-firecracker": 5,
        "claude-code-v0.0.1-arm64-qemu-alpine": 3,
        f"codex-v{__version__}-arm64-qemu": 7,
        "base-kernel-v0.0.1-amd64": 2,
    }
    for name, size in entries.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "blob").write_bytes(b"x" * size)
    (root / "s3").mkdir()
    (root / "_guest-agent").mkdir()
    (root / "stray-file.txt").write_text("not a dir")


class TestResolveImageDir:
    def test_default_is_home_smolvm_images(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir() == Path.home() / ".smolvm" / "images"

    def test_env_var_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "custom"))
        assert resolve_image_dir() == tmp_path / "custom"

    def test_explicit_arg_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "from-env"))
        assert resolve_image_dir(tmp_path / "explicit") == tmp_path / "explicit"
        assert resolve_image_dir(str(tmp_path / "explicit")) == tmp_path / "explicit"

    def test_blank_env_var_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, "  ")
        assert resolve_image_dir() == Path.home() / ".smolvm" / "images"

    def test_blank_explicit_arg_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--image-dir "$UNSET"` must never target the current directory."""
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir("") == Path.home() / ".smolvm" / "images"
        assert resolve_image_dir("   ") == Path.home() / ".smolvm" / "images"
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "env"))
        assert resolve_image_dir("") == tmp_path / "env"

    def test_unknown_user_tilde_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """expanduser failures fall back to the path as written (regression:
        a '~typo/...' value used to escape as a raw RuntimeError)."""
        monkeypatch.delenv(IMAGE_DIR_ENV, raising=False)
        assert resolve_image_dir("~nosuchuser-xyz/dir") == Path("~nosuchuser-xyz/dir")
        monkeypatch.setenv(IMAGE_DIR_ENV, "~nosuchuser-xyz/dir")
        assert resolve_image_dir() == Path("~nosuchuser-xyz/dir")

    def test_does_not_create_the_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "never-created"
        monkeypatch.setenv(IMAGE_DIR_ENV, str(target))
        resolve_image_dir()
        assert not target.exists()

    def test_image_manager_honors_env_set_after_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution happens at construction time, not module-import time."""
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "late-env"))
        assert ImageManager().cache_dir == tmp_path / "late-env"

    def test_image_manager_explicit_cache_dir_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "from-env"))
        assert ImageManager(cache_dir=tmp_path / "explicit").cache_dir == tmp_path / "explicit"

    def test_guest_agent_cache_dir_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smolvm.images.builder import _guest_agent_binary_cache_dir

        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "img"))
        assert _guest_agent_binary_cache_dir() == tmp_path / "img" / "_guest-agent"

    def test_image_builder_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smolvm.images.builder import ImageBuilder

        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path / "img"))
        assert ImageBuilder().cache_dir == tmp_path / "img"

    def test_find_stale_caches_honors_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = tmp_path / "codex-v0.0.1-amd64-firecracker"
        stale.mkdir()
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path))
        assert find_stale_caches(current_version="9.9.9") == [stale]


class TestSizeHelpers:
    def test_format_bytes_keeps_fractions(self) -> None:
        assert _format_bytes(0) == "0.0 B"
        assert _format_bytes(1536) == "1.5 KiB"
        assert _format_bytes(2_040_109_465) == "1.9 GiB"

    def test_total_size_is_sparse_aware(self, tmp_path: Path) -> None:
        sparse = tmp_path / "rootfs.ext4"
        with sparse.open("wb") as f:
            f.truncate(64 * 1024 * 1024)  # 64 MiB apparent, ~0 allocated
            f.write(b"x")
        apparent = sparse.stat().st_size
        on_disk = _total_size(tmp_path)
        assert apparent >= 64 * 1024 * 1024
        if getattr(os.stat(sparse), "st_blocks", None) is not None:
            assert on_disk < apparent

    def test_total_size_tolerates_missing_path(self, tmp_path: Path) -> None:
        assert _total_size(tmp_path / "gone") == 0


class TestCacheNameParsing:
    def test_round_trips_every_manifest_entry(self) -> None:
        """The list/rm parser must recognize everything cache_name() emits."""
        from smolvm.images.published import MANIFEST, cache_name

        for preset, arch, vmm, os_name in MANIFEST:
            for version in ("0.0.26", "0.0.14a0", "0.0.24.post3", "1.2.3.dev1"):
                name = cache_name(preset, arch, vmm, version, os=os_name)
                match = _IMAGE_DIR_NAME_RE.match(name)
                assert match is not None, f"unparsed cache name: {name}"
                assert match["preset"] == preset
                assert match["version"] == version
                assert match["arch"] == arch
                assert match["vmm"] == vmm
                assert (match["os"] or "ubuntu") == os_name

    def test_kernel_names_round_trip(self) -> None:
        for version in ("0.0.26", "0.0.24.post3"):
            name = f"base-kernel-v{version}-amd64"
            match = _KERNEL_DIR_NAME_RE.match(name)
            assert match is not None
            assert match["version"] == version

    def test_prune_matches_dotted_suffix_versions(self, tmp_path: Path) -> None:
        """Shipped 0.0.24.postN caches must be prunable (regression)."""
        stale = tmp_path / "codex-v0.0.24.post3-amd64-firecracker"
        stale.mkdir()
        assert find_stale_caches(cache_dir=tmp_path, current_version="9.9.9") == [stale]


class TestImageList:
    def test_list_json_classifies_entries(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.list"
        assert payload["ok"] is True
        assert payload["data"]["image_dir"] == str(tmp_path)
        rows = {row["name"]: row for row in payload["data"]["images"]}
        # The stray file is skipped; the six directories are classified.
        assert len(rows) == 6

        codex = rows["codex-v0.0.1-amd64-firecracker"]
        assert codex["kind"] == "image"
        assert codex["preset"] == "codex"
        assert codex["arch"] == "amd64"
        assert codex["vmm"] == "firecracker"
        assert codex["os"] == "ubuntu"
        assert codex["current"] is False
        assert codex["size_bytes"] == _total_size(tmp_path / "codex-v0.0.1-amd64-firecracker")
        assert codex["size_bytes"] > 0

        # Non-greedy preset parse: hyphenated preset with alpine suffix.
        claude = rows["claude-code-v0.0.1-arm64-qemu-alpine"]
        assert claude["preset"] == "claude-code"
        assert claude["os"] == "alpine"
        assert claude["vmm"] == "qemu"

        current = rows[f"codex-v{__version__}-arm64-qemu"]
        assert current["current"] is True

        kernel = rows["base-kernel-v0.0.1-amd64"]
        assert kernel["kind"] == "kernel"
        assert kernel["arch"] == "amd64"
        assert kernel["preset"] is None

        assert rows["s3"]["kind"] == "other"
        assert rows["s3"]["current"] is None
        assert rows["_guest-agent"]["kind"] == "other"

        assert payload["data"]["total_size_bytes"] == sum(
            row["size_bytes"] for row in payload["data"]["images"]
        )

    def test_list_classifies_dotted_suffix_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Shipped 0.0.24.postN caches parse as images, not 'other' (regression)."""
        (tmp_path / "codex-v0.0.24.post3-amd64-firecracker").mkdir(parents=True)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        row = payload["data"]["images"][0]
        assert row["kind"] == "image"
        assert row["preset"] == "codex"
        assert row["version"] == "0.0.24.post3"
        assert row["current"] is False

    def test_list_missing_dir_is_empty(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        ret = main(["image", "list", "--image-dir", str(tmp_path / "missing"), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["images"] == []
        assert payload["data"]["total_size_bytes"] == 0

    def test_list_human_empty_names_pull(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["image", "list", "--image-dir", str(tmp_path)])

        assert ret == 0
        # Rich wraps the panel mid-word with box-drawing borders; strip
        # everything but letters/digits so the recovery command is findable.
        flattened = re.sub(r"[^a-z0-9]+", "", capsys.readouterr().out.lower())
        assert "smolvmimagepullcodex" in flattened

    def test_list_env_var_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)
        monkeypatch.setenv(IMAGE_DIR_ENV, str(tmp_path))

        ret = main(["image", "list", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["image_dir"] == str(tmp_path)
        assert len(payload["data"]["images"]) == 6

    def test_list_unreadable_dir_reports_error_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permission failures produce the JSON envelope, not a traceback."""

        def boom(self: Path) -> None:
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "iterdir", boom)

        ret = main(["image", "list", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "permission" in payload["error"]["recovery"].lower()


class TestImageRm:
    def test_rm_exact_name(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _make_cache_dirs(tmp_path)
        expected_freed = _total_size(tmp_path / "codex-v0.0.1-amd64-firecracker")

        ret = main(
            [
                "image",
                "rm",
                "codex-v0.0.1-amd64-firecracker",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.rm"
        assert [e["name"] for e in payload["data"]["removed"]] == ["codex-v0.0.1-amd64-firecracker"]
        assert payload["data"]["freed_bytes"] == expected_freed
        assert expected_freed > 0
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        # The other codex version is untouched by an exact-name removal.
        assert (tmp_path / f"codex-v{__version__}-arm64-qemu").exists()

    def test_rm_accepts_trailing_slash_and_list_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Tab-completed names and the path `image list` prints both work."""
        _make_cache_dirs(tmp_path)

        ret = main(
            [
                "image",
                "rm",
                "codex-v0.0.1-amd64-firecracker/",
                "--image-dir",
                str(tmp_path),
                "--json",
            ]
        )
        assert ret == 0
        capsys.readouterr()
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()

        full_path = str(tmp_path / "base-kernel-v0.0.1-amd64")
        ret = main(["image", "rm", full_path, "--image-dir", str(tmp_path), "--json"])
        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["base-kernel-v0.0.1-amd64"]

    def test_rm_by_preset_removes_all_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)
        (tmp_path / "codex-v0.0.24.post3-amd64-firecracker").mkdir()

        ret = main(["image", "rm", "codex", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        removed = {e["name"] for e in payload["data"]["removed"]}
        assert removed == {
            "codex-v0.0.1-amd64-firecracker",
            f"codex-v{__version__}-arm64-qemu",
            "codex-v0.0.24.post3-amd64-firecracker",
        }
        assert payload["data"]["freed_bytes"] > 0
        # Unrelated entries survive preset-wide removal.
        assert (tmp_path / "s3").exists()
        assert (tmp_path / "_guest-agent").exists()
        assert (tmp_path / "base-kernel-v0.0.1-amd64").exists()
        assert (tmp_path / "claude-code-v0.0.1-arm64-qemu-alpine").exists()

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("claude", "claude-code-v0.0.1-arm64-qemu-alpine"),
            ("claw", "openclaw-v0.0.1-amd64-firecracker"),
        ],
    )
    def test_rm_preset_aliases(
        self, alias: str, expected: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Aliases come from the presets registry, not a hardcoded map."""
        _make_cache_dirs(tmp_path)
        (tmp_path / "openclaw-v0.0.1-amd64-firecracker").mkdir()

        ret = main(["image", "rm", alias, "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == [expected]

    def test_rm_dry_run_removes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "codex", "--dry-run", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["would_free_bytes"] > 0
        assert (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()

    def test_rm_unknown_name_fails_with_recovery(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "nope", "--image-dir", str(tmp_path), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "smolvm image list" in payload["error"]["recovery"]

    @pytest.mark.parametrize("name", ["../evil", "a/b", "..", ".", "/somewhere/else/evil"])
    def test_rm_rejects_traversal_names(
        self, name: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        (tmp_path / "evil").mkdir()

        ret = main(["image", "rm", name, "--image-dir", str(tmp_path / "sub"), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert (tmp_path / "evil").exists()

    def test_rm_symlink_unlinks_without_touching_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Symlink entries are unlinked in place — never followed, never
        rmtree'd (regression: rmtree on a symlink used to crash)."""
        root = tmp_path / "images"
        root.mkdir()
        real = root / "codex-v0.0.1-amd64-firecracker"
        real.mkdir()
        (real / "rootfs.ext4").write_text("data")
        (root / "old-codex").symlink_to(real)

        ret = main(["image", "rm", "old-codex", "--image-dir", str(root), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["old-codex"]
        assert payload["data"]["freed_bytes"] == 0
        assert not (root / "old-codex").exists()
        assert (real / "rootfs.ext4").exists()

    def test_rm_symlink_outside_root_only_removes_link(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        root = tmp_path / "images"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data").write_text("keep me")
        (root / "escape").symlink_to(outside)

        ret = main(["image", "rm", "escape", "--image-dir", str(root), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == ["escape"]
        assert not (root / "escape").exists()
        assert (outside / "data").exists()


class TestPruneAlias:
    def test_top_level_prune_keeps_command_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["prune", "--cache-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "prune"

    def test_top_level_prune_accepts_image_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The documented alias takes the same --image-dir flag as the group."""
        stale = tmp_path / "codex-v0.0.1-amd64-firecracker"
        stale.mkdir()

        ret = main(["prune", "--image-dir", str(tmp_path), "--dry-run", "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "prune"
        assert payload["data"]["would_remove"]

    def test_image_prune_uses_dotted_command_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["image", "prune", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.prune"

    def test_image_prune_removes_stale_dirs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "prune", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        # Stale-versioned dirs go; current-version and unversioned stay.
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        assert not (tmp_path / "base-kernel-v0.0.1-amd64").exists()
        assert (tmp_path / f"codex-v{__version__}-arm64-qemu").exists()
        assert (tmp_path / "s3").exists()


class TestChoiceListsMatchPublishedTypes:
    def test_pull_option_choices_track_published_literals(self) -> None:
        """The hardcoded click.Choice lists must not drift from the manifest
        type literals (they are hardcoded to keep published.py off the CLI
        import path)."""
        import typing

        import click

        from smolvm.cli.main import build_cli
        from smolvm.images.published import Arch, Os, Vmm

        pull = build_cli().commands["image"].commands["pull"]  # type: ignore[attr-defined]
        choices = {
            param.name: set(param.type.choices)
            for param in pull.params
            if isinstance(param.type, click.Choice)
        }
        assert choices["arch"] == set(typing.get_args(Arch))
        assert choices["vmm"] == set(typing.get_args(Vmm))
        assert choices["os_name"] == set(typing.get_args(Os))
