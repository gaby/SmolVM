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
import re
from pathlib import Path

import pytest

from smolvm import __version__
from smolvm.cli.main import main
from smolvm.cli.prune import find_stale_caches
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
        assert codex["size_bytes"] == 5

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

        assert payload["data"]["total_size_bytes"] == 17

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
        assert "smolvmimagepull" in flattened

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


class TestImageRm:
    def test_rm_exact_name(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _make_cache_dirs(tmp_path)

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
        assert payload["data"]["freed_bytes"] == 5
        assert not (tmp_path / "codex-v0.0.1-amd64-firecracker").exists()
        # The other codex version is untouched by an exact-name removal.
        assert (tmp_path / f"codex-v{__version__}-arm64-qemu").exists()

    def test_rm_by_preset_removes_all_versions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "codex", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        removed = {e["name"] for e in payload["data"]["removed"]}
        assert removed == {
            "codex-v0.0.1-amd64-firecracker",
            f"codex-v{__version__}-arm64-qemu",
        }
        assert payload["data"]["freed_bytes"] == 12
        # Unrelated entries survive preset-wide removal.
        assert (tmp_path / "s3").exists()
        assert (tmp_path / "_guest-agent").exists()
        assert (tmp_path / "base-kernel-v0.0.1-amd64").exists()
        assert (tmp_path / "claude-code-v0.0.1-arm64-qemu-alpine").exists()

    def test_rm_claude_alias(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "claude", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert [e["name"] for e in payload["data"]["removed"]] == [
            "claude-code-v0.0.1-arm64-qemu-alpine"
        ]

    def test_rm_dry_run_removes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        _make_cache_dirs(tmp_path)

        ret = main(["image", "rm", "codex", "--dry-run", "--image-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["dry_run"] is True
        assert payload["data"]["would_free_bytes"] == 12
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

    @pytest.mark.parametrize("name", ["../evil", "a/b", "..", "."])
    def test_rm_rejects_traversal_names(
        self, name: str, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        (tmp_path / "evil").mkdir()

        ret = main(["image", "rm", name, "--image-dir", str(tmp_path / "sub"), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert (tmp_path / "evil").exists()

    def test_rm_refuses_symlink_escaping_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        root = tmp_path / "images"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data").write_text("keep me")
        (root / "escape").symlink_to(outside)

        ret = main(["image", "rm", "escape", "--image-dir", str(root), "--json"])

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert (outside / "data").exists()


class TestPruneAlias:
    def test_top_level_prune_keeps_command_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        ret = main(["prune", "--cache-dir", str(tmp_path), "--json"])

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "prune"

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
