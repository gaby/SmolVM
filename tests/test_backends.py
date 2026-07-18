from unittest.mock import patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    ensure_backend_available,
    resolve_backend,
)


def test_resolve_backend_auto_defaults_to_qemu_on_macos() -> None:
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Darwin"),
        patch("smolvm.runtime.backends.qemu_available", return_value=True),
    ):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_defaults_to_qemu_on_macos_even_with_krunvm() -> None:
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Darwin"),
        patch("smolvm.runtime.backends.which", return_value="/usr/local/bin/krunvm"),
        patch("smolvm.runtime.backends.qemu_available", return_value=True),
    ):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_accepts_libkrun_explicitly() -> None:
    assert resolve_backend(BACKEND_LIBKRUN) == BACKEND_LIBKRUN


def test_resolve_backend_auto_prefers_firecracker_on_linux() -> None:
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Linux"),
        patch("smolvm.runtime.backends.firecracker_available", return_value=True),
        patch("smolvm.runtime.backends.qemu_available", return_value=True),
    ):
        assert resolve_backend("auto") == BACKEND_FIRECRACKER


def test_resolve_backend_auto_falls_back_to_qemu_when_firecracker_missing() -> None:
    # The reported bug: /dev/kvm is present but Firecracker isn't installed, so
    # auto must not pick Firecracker when QEMU is the installed backend.
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Linux"),
        patch("smolvm.runtime.backends.firecracker_available", return_value=False),
        patch("smolvm.runtime.backends.qemu_available", return_value=True),
    ):
        assert resolve_backend("auto") == BACKEND_QEMU


def test_resolve_backend_auto_falls_back_to_libkrun_when_only_libkrun_present() -> None:
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Linux"),
        patch("smolvm.runtime.backends.firecracker_available", return_value=False),
        patch("smolvm.runtime.backends.qemu_available", return_value=False),
        patch("smolvm.runtime.backends.libkrun_available", return_value=True),
    ):
        assert resolve_backend("auto") == BACKEND_LIBKRUN


def test_resolve_backend_auto_defaults_to_firecracker_when_nothing_installed() -> None:
    # With nothing detected, auto keeps the platform default so the downstream
    # preflight can surface an actionable "install Firecracker" message.
    with (
        patch("smolvm.runtime.backends.platform.system", return_value="Linux"),
        patch("smolvm.runtime.backends.firecracker_available", return_value=False),
        patch("smolvm.runtime.backends.qemu_available", return_value=False),
        patch("smolvm.runtime.backends.libkrun_available", return_value=False),
    ):
        assert resolve_backend("auto") == BACKEND_FIRECRACKER


def test_ensure_backend_available_passes_when_qemu_installed() -> None:
    with patch("smolvm.runtime.backends.qemu_available", return_value=True):
        ensure_backend_available(BACKEND_QEMU)  # does not raise


def test_ensure_backend_available_raises_for_missing_qemu() -> None:
    with (
        patch("smolvm.runtime.backends.qemu_available", return_value=False),
        pytest.raises(SmolVMError, match="QEMU isn't installed"),
    ):
        ensure_backend_available(BACKEND_QEMU)


def test_ensure_backend_available_raises_for_missing_firecracker() -> None:
    with (
        patch("smolvm.runtime.backends.firecracker_available", return_value=False),
        pytest.raises(SmolVMError, match="Firecracker isn't installed"),
    ):
        ensure_backend_available(BACKEND_FIRECRACKER)


def test_ensure_backend_available_raises_for_missing_libkrun() -> None:
    with (
        patch("smolvm.runtime.backends.libkrun_available", return_value=False),
        pytest.raises(SmolVMError, match="libkrun isn't installed"),
    ):
        ensure_backend_available(BACKEND_LIBKRUN)


def test_qemu_available_needs_both_system_and_img() -> None:
    from smolvm.runtime import backends

    def only_qemu_img(binary: str):
        return "/usr/bin/qemu-img" if binary == "qemu-img" else None

    # qemu-img present but no system emulator -> not available.
    with patch("smolvm.runtime.backends.which", side_effect=only_qemu_img):
        assert backends.qemu_available() is False

    # Both present -> available.
    with patch("smolvm.runtime.backends.which", return_value="/usr/bin/qemu-x"):
        assert backends.qemu_available() is True
