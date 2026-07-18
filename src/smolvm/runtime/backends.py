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

"""Runtime backend selection helpers for SmolVM."""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

from smolvm.exceptions import SmolVMError
from smolvm.utils import which  # noqa: F401 — imported for test patching

logger = logging.getLogger(__name__)

BACKEND_FIRECRACKER = "firecracker"
BACKEND_QEMU = "qemu"
BACKEND_LIBKRUN = "libkrun"
BACKEND_AUTO = "auto"

SUPPORTED_BACKENDS = {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN}

# Where ``smolvm`` installs a private Firecracker binary when it isn't on PATH.
# Kept in sync with ``smolvm.host.manager.HostManager.BIN_DIR``; duplicated here
# so backend selection stays free of the heavier host-manager import.
_LOCAL_BIN_DIR = Path.home() / ".smolvm" / "bin"


def _qemu_system_candidates() -> tuple[str, ...]:
    """Return host-arch-first ``qemu-system-*`` binary names to look for."""
    arch = platform.machine().lower()
    if arch in {"arm64", "aarch64"}:
        return ("qemu-system-aarch64", "qemu-system-x86_64")
    if arch in {"x86_64", "amd64"}:
        return ("qemu-system-x86_64", "qemu-system-aarch64")
    return ("qemu-system-aarch64", "qemu-system-x86_64")


def firecracker_available() -> bool:
    """Return whether a usable Firecracker binary is installed.

    Looks on ``PATH`` first, then the private ``~/.smolvm/bin`` install dir.
    """
    if which("firecracker") is not None:
        return True
    local = _LOCAL_BIN_DIR / "firecracker"
    return local.exists() and os.access(local, os.X_OK)


def qemu_available() -> bool:
    """Return whether QEMU (a system emulator plus ``qemu-img``) is installed."""
    if which("qemu-img") is None:
        return False
    return any(which(candidate) is not None for candidate in _qemu_system_candidates())


def libkrun_available() -> bool:
    """Return whether the libkrun shared library can be loaded."""
    try:
        from smolvm.runtime._libkrun_ffi import is_available
    except Exception:  # pragma: no cover - defensive import guard
        return False
    return is_available()


# Auto-selection preference order per host, most-preferred first. Each entry
# pairs a backend with the name of its probe (looked up on this module at call
# time so tests can patch the probes). macOS has no KVM, so Firecracker is not
# a candidate there.
_AUTO_PREFERENCE: dict[str, tuple[tuple[str, str], ...]] = {
    "darwin": (
        (BACKEND_QEMU, "qemu_available"),
        (BACKEND_LIBKRUN, "libkrun_available"),
    ),
    "_default": (
        (BACKEND_FIRECRACKER, "firecracker_available"),
        (BACKEND_QEMU, "qemu_available"),
        (BACKEND_LIBKRUN, "libkrun_available"),
    ),
}


def _auto_backend() -> str:
    """Pick the best installed backend for this host.

    Prefers Firecracker on Linux and QEMU on macOS, but falls through to the
    next installed backend when the preferred one isn't present — so ``auto``
    never resolves to a backend the host can't run. When nothing is detected,
    returns the platform default and lets the preflight surface the fix.
    """
    system = platform.system().lower()
    preference = _AUTO_PREFERENCE.get(system, _AUTO_PREFERENCE["_default"])
    for backend, probe_name in preference:
        if globals()[probe_name]():
            logger.debug("auto backend selection chose %s", backend)
            return backend
    fallback = preference[0][0]
    logger.debug("auto backend selection found no installed backend; defaulting to %s", fallback)
    return fallback


def resolve_backend(requested: str | None = None) -> str:
    """Resolve the effective backend name.

    Resolution order:
    1) Explicit ``requested`` argument.
    2) ``SMOLVM_BACKEND`` environment variable.
    3) ``auto``: the best backend actually installed on this host
       (Firecracker preferred on Linux, QEMU on macOS), falling back to the
       next installed backend when the preferred one is missing.

    Args:
        requested: Optional backend string.

    Returns:
        Effective backend name.

    Raises:
        ValueError: If backend is unknown.
    """
    raw = (requested or os.environ.get("SMOLVM_BACKEND") or BACKEND_AUTO).strip().lower()

    if raw == BACKEND_AUTO:
        return _auto_backend()

    if raw in SUPPORTED_BACKENDS:
        return raw

    supported = ", ".join(sorted((*SUPPORTED_BACKENDS, BACKEND_AUTO)))
    raise ValueError(f"Unsupported backend '{raw}'. Supported values: {supported}")


def _firecracker_missing_message() -> str:
    """Plain-English recovery for a missing Firecracker binary."""
    return (
        "Firecracker isn't installed on this machine. Install it and make sure "
        "the 'firecracker' binary is on your PATH (or in ~/.smolvm/bin), or "
        "create the sandbox with a different backend, e.g. "
        "'smolvm sandbox create --backend qemu'."
    )


def _qemu_missing_message() -> str:
    """Plain-English recovery for missing QEMU tooling."""
    system = platform.system()
    if system == "Darwin":
        install = "Install it with 'brew install qemu'"
    elif system == "Linux":
        install = (
            "Install it with 'sudo apt-get install -y qemu-system qemu-utils' on "
            "Debian/Ubuntu, 'sudo dnf install -y qemu-system-x86 qemu-img' on "
            "Fedora/RHEL, or your distro's QEMU package"
        )
    else:
        install = "Install QEMU for this operating system"
    return (
        "QEMU isn't installed on this machine. "
        f"{install}, then run 'smolvm doctor --backend qemu' to confirm."
    )


def _libkrun_missing_message() -> str:
    """Plain-English recovery for a missing libkrun library."""
    return (
        "libkrun isn't installed on this machine. Install it with "
        "'brew install libkrun/krun/libkrun' on macOS or 'sudo dnf install "
        "libkrun' on Fedora, then run 'smolvm doctor --backend libkrun' to confirm."
    )


def ensure_backend_available(backend: str) -> None:
    """Verify the tooling a resolved backend needs is installed.

    Call this before any slow work (such as downloading a base image) so a
    missing hypervisor fails fast with a clear, actionable message instead of
    surfacing deep inside VM start after a download.

    Args:
        backend: A resolved backend name (not ``auto``).

    Raises:
        SmolVMError: If the backend's required tooling isn't present.
    """
    if backend == BACKEND_FIRECRACKER and not firecracker_available():
        raise SmolVMError(_firecracker_missing_message())
    if backend == BACKEND_QEMU and not qemu_available():
        raise SmolVMError(_qemu_missing_message())
    if backend == BACKEND_LIBKRUN and not libkrun_available():
        raise SmolVMError(_libkrun_missing_message())
