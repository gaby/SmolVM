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

"""Discovery of host graphics cards that a sandbox can borrow.

SmolVM can hand a whole graphics card from the host machine to one sandbox.
Linux does that through VFIO: the card is detached from whatever driver the
host normally uses for it and attached to ``vfio-pci`` instead, after which a
virtual machine can drive the hardware directly.

**SmolVM never performs that detach itself.** Rebinding the wrong card takes
the user's display away mid-session, so this module only *reads* the current
state and reports what is left to do. Everything here is a plain read of
``/sys``; there is no subprocess, no privileged call, and no side effect.

Two details drive the shape of the API:

- **Cards come in groups.** The hardware isolates devices in "IOMMU groups",
  and VFIO hands over a whole group at a time. A discrete graphics card is
  almost always grouped with its own HDMI audio function, so asking for
  ``0000:01:00.0`` really means asking for ``0000:01:00.1`` too. See
  :func:`assignable_functions`.
- **Everything is testable.** Each entry point takes ``sysfs_root`` so tests
  can build a fake ``/sys`` tree in a temporary directory instead of depending
  on whatever hardware the test machine happens to have.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A PCI address ("bus/device/function"), e.g. "0000:01:00.0". Lowercase hex;
# callers are normalized through _normalize_address before matching.
PCI_ADDRESS_PATTERN = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

DEFAULT_SYSFS_ROOT = Path("/sys")
_VFIO_DEV_ROOT = Path("/dev/vfio")

# The driver name a card must be attached to before a sandbox can use it.
VFIO_DRIVER = "vfio-pci"

# Slack added on top of guest memory when checking the pinned-memory cap. The
# emulator maps more than just guest RAM, and a check that lands exactly on
# the boundary would pass here and still fail at launch.
_MEMLOCK_OVERHEAD_MIB = 512

# PCI class codes, upper 16 bits of the 24-bit class register. A card is a
# graphics card if it reports one of these.
_GPU_CLASSES = frozenset(
    {
        0x0300,  # VGA compatible controller — ordinary desktop graphics card
        0x0302,  # 3D controller — headless compute cards (datacenter NVIDIA)
        0x0380,  # Display controller, other
    }
)

# Bridges show up as members of an IOMMU group but are not handed to a guest;
# VFIO ignores them when it checks whether a group is ready.
_BRIDGE_CLASSES = frozenset(
    {
        0x0600,  # Host bridge
        0x0604,  # PCI-to-PCI bridge (the root port above a graphics card)
    }
)

_VENDOR_NAMES = {
    "10de": "NVIDIA",
    "1002": "AMD",
    "1022": "AMD",
    "8086": "Intel",
}


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """One graphics card found on the host machine.

    Attributes:
        address: PCI address of the card's main function, e.g. ``0000:01:00.0``.
        vendor_id: Four hex digits identifying the maker, e.g. ``10de``.
        device_id: Four hex digits identifying the model, e.g. ``2684``.
        vendor_name: Human name for *vendor_id* when SmolVM recognizes it,
            otherwise the raw id.
        driver: Name of the driver currently using the card, or ``None`` when
            nothing has claimed it. ``vfio-pci`` means it is free for sandboxes.
        iommu_group: The hardware isolation group this card belongs to, or
            ``None`` when the machine has hardware isolation switched off.
        group_members: Every address in that group that must be handed over
            together, main function first. Bridges are excluded.
        ready: Whether a sandbox can use this card right now.
        blocker: When *ready* is False, a plain-English reason. ``None``
            otherwise.
    """

    address: str
    vendor_id: str
    device_id: str
    vendor_name: str
    driver: str | None
    iommu_group: int | None
    group_members: tuple[str, ...]
    ready: bool
    blocker: str | None

    @property
    def description(self) -> str:
        """Return a short human label such as ``NVIDIA 2684``."""
        return f"{self.vendor_name} {self.device_id}"


def _normalize_address(address: str) -> str:
    """Return *address* lowercased and widened to the ``0000:00:00.0`` form.

    Users copy addresses out of ``lspci``, which prints the common
    ``01:00.0`` short form with the leading domain omitted. Accept both.
    """
    value = address.strip().lower()
    if re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", value):
        value = f"0000:{value}"
    return value


def _read_text(path: Path) -> str | None:
    """Return the stripped contents of *path*, or ``None`` when unreadable.

    sysfs attributes disappear when a device is removed mid-scan, and some
    are unreadable for an unprivileged user. Neither should crash a listing.
    """
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_hex_id(path: Path) -> str | None:
    """Return a sysfs ``0x1234`` id attribute as four lowercase hex digits."""
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return f"{int(raw, 16):04x}"
    except ValueError:
        return None


def _read_class(device_dir: Path) -> int | None:
    """Return the upper 16 bits of a device's PCI class register."""
    raw = _read_text(device_dir / "class")
    if raw is None:
        return None
    try:
        # The register is 24 bits (class, subclass, programming interface);
        # the interface byte distinguishes things we don't care about here.
        return int(raw, 16) >> 8
    except ValueError:
        return None


def _read_driver(device_dir: Path) -> str | None:
    """Return the name of the driver bound to a device, or ``None``."""
    link = device_dir / "driver"
    try:
        if not link.is_symlink() and not link.exists():
            return None
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return None


def _read_iommu_group(device_dir: Path) -> int | None:
    """Return the hardware isolation group number for a device."""
    link = device_dir / "iommu_group"
    try:
        if not link.is_symlink() and not link.exists():
            return None
        name = os.path.basename(os.path.realpath(link))
    except OSError:
        return None
    try:
        return int(name)
    except ValueError:
        return None


def _iommu_group_members(sysfs_root: Path, group: int) -> tuple[str, ...]:
    """Return every PCI address in *group*, sorted."""
    group_dir = sysfs_root / "kernel" / "iommu_groups" / str(group) / "devices"
    try:
        return tuple(sorted(entry.name for entry in group_dir.iterdir()))
    except OSError:
        return ()


def iommu_enabled(sysfs_root: Path = DEFAULT_SYSFS_ROOT) -> bool:
    """Return whether this machine can isolate hardware for sandboxes.

    The kernel only populates ``/sys/kernel/iommu_groups`` once the machine's
    hardware isolation feature is switched on in firmware and on the host's
    own boot options. An empty directory is the signal that the one-time
    setup has not been done.
    """
    groups = sysfs_root / "kernel" / "iommu_groups"
    try:
        return any(groups.iterdir())
    except OSError:
        return False


def _vfio_group_accessible(group: int) -> bool:
    """Return whether this user can drive the VFIO device for *group*.

    Mirrors the ``/dev/kvm`` probe in :mod:`smolvm.runtime.backends`: the
    file existing is not enough, the current user has to be able to open it.
    """
    node = _VFIO_DEV_ROOT / str(group)
    return node.exists() and os.access(node, os.R_OK | os.W_OK)


def _classify(
    *,
    sysfs_root: Path,
    address: str,
    driver: str | None,
    iommu_group: int | None,
    members: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Return ``(ready, blocker)`` for one card.

    The blocker text is user-facing. It names the single most useful next
    step rather than every possible one — the full setup lives in
    ``smolvm gpu list`` output and the GPU guide.
    """
    if not iommu_enabled(sysfs_root):
        return False, (
            "This machine isn't set up to share graphics cards with sandboxes. "
            "Run 'smolvm gpu list' for the one-time setup steps."
        )
    if iommu_group is None:
        return False, (
            f"The graphics card at '{address}' isn't isolated from the rest of this "
            "machine, so a sandbox can't use it safely."
        )

    # Every member of the group has to be free, not just the card itself.
    # This is the failure people hit most often and the one a generic message
    # is least helpful for, so name the exact device that is still in use.
    for member in members:
        member_driver = _read_driver(sysfs_root / "bus" / "pci" / "devices" / member)
        if member_driver == VFIO_DRIVER:
            continue
        if member == address:
            return False, (
                f"The graphics card at '{address}' is still in use by this machine. "
                "Run 'smolvm gpu list' for the one-time steps to free it."
            )
        return False, (
            f"The graphics card at '{address}' is grouped with '{member}', which is "
            "still in use by this machine. Run 'smolvm gpu list' to see what to free."
        )

    if driver != VFIO_DRIVER:
        return False, (
            f"The graphics card at '{address}' is still in use by this machine. "
            "Run 'smolvm gpu list' for the one-time steps to free it."
        )

    if not _vfio_group_accessible(iommu_group):
        return False, (
            f"You don't have permission to use the graphics card at '{address}'. "
            "Add yourself to the 'vfio' group, then start a new login session."
        )

    return True, None


def _build_device(sysfs_root: Path, address: str) -> GpuDevice | None:
    """Return a :class:`GpuDevice` for *address*, or ``None`` if not a GPU."""
    device_dir = sysfs_root / "bus" / "pci" / "devices" / address
    class_id = _read_class(device_dir)
    if class_id is None or class_id not in _GPU_CLASSES:
        return None

    vendor_id = _read_hex_id(device_dir / "vendor") or "0000"
    device_id = _read_hex_id(device_dir / "device") or "0000"
    driver = _read_driver(device_dir)
    iommu_group = _read_iommu_group(device_dir)

    members: tuple[str, ...] = ()
    if iommu_group is not None:
        members = _assignable_members(sysfs_root, address, iommu_group)

    ready, blocker = _classify(
        sysfs_root=sysfs_root,
        address=address,
        driver=driver,
        iommu_group=iommu_group,
        members=members,
    )

    return GpuDevice(
        address=address,
        vendor_id=vendor_id,
        device_id=device_id,
        vendor_name=_VENDOR_NAMES.get(vendor_id, vendor_id),
        driver=driver,
        iommu_group=iommu_group,
        group_members=members,
        ready=ready,
        blocker=blocker,
    )


def _assignable_members(sysfs_root: Path, address: str, group: int) -> tuple[str, ...]:
    """Return the group's hand-over set, main function first.

    Bridges are dropped: they appear in the group listing but are never
    handed to a guest, and VFIO does not require them to be freed.
    """
    assignable: list[str] = []
    for member in _iommu_group_members(sysfs_root, group):
        class_id = _read_class(sysfs_root / "bus" / "pci" / "devices" / member)
        if class_id is not None and class_id in _BRIDGE_CLASSES:
            continue
        assignable.append(member)

    # The card the user named must lead, because QEMU puts the first function
    # at slot function 0 and guests expect the display/compute function there.
    if address in assignable:
        assignable.remove(address)
        assignable.insert(0, address)
    return tuple(assignable)


def list_host_gpus(sysfs_root: Path = DEFAULT_SYSFS_ROOT) -> list[GpuDevice]:
    """Return every graphics card on this machine, ready or not.

    Cards that are not ready are still returned, carrying a ``blocker`` that
    explains what is left to do. Returns an empty list on machines with no
    PCI bus at all (including non-Linux hosts), rather than raising.
    """
    devices_dir = sysfs_root / "bus" / "pci" / "devices"
    try:
        entries = sorted(entry.name for entry in devices_dir.iterdir())
    except OSError:
        return []

    found: list[GpuDevice] = []
    for name in entries:
        if not PCI_ADDRESS_PATTERN.match(name):
            continue
        device = _build_device(sysfs_root, name)
        if device is not None:
            found.append(device)
    return found


def find_gpu(address: str, sysfs_root: Path = DEFAULT_SYSFS_ROOT) -> GpuDevice:
    """Return the graphics card at *address*.

    Raises:
        ValueError: When no graphics card sits at that address. The message
            is user-facing and points at ``smolvm gpu list``.
    """
    wanted = _normalize_address(address)
    for device in list_host_gpus(sysfs_root):
        if device.address == wanted:
            return device
    raise ValueError(
        f"No graphics card found at '{address}' on this machine. "
        "Run 'smolvm gpu list' to see the cards SmolVM can use."
    )


def resolve_gpu_selection(
    selection: str,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
) -> GpuDevice:
    """Return the card named by a ``--gpu`` value.

    Accepts a PCI address in either the long (``0000:01:00.0``) or short
    (``01:00.0``) form, or the word ``auto`` to mean "the one card that is
    ready", which is the common case on a machine with a single spare card.

    Raises:
        ValueError: When the selection names nothing, is ambiguous, or names
            a card that is not ready. Every message is user-facing.
    """
    if selection.strip().lower() == "auto":
        ready = [device for device in list_host_gpus(sysfs_root) if device.ready]
        if not ready:
            raise ValueError(
                "No graphics card on this machine is free for sandboxes. "
                "Run 'smolvm gpu list' to see what to do."
            )
        if len(ready) > 1:
            addresses = ", ".join(f"'{device.address}'" for device in ready)
            raise ValueError(
                f"This machine has more than one graphics card free ({addresses}); "
                f"name the one you want, for example '--gpu {ready[0].address}'."
            )
        return ready[0]

    device = find_gpu(selection, sysfs_root)
    if not device.ready:
        raise ValueError(device.blocker or f"The graphics card at '{device.address}' isn't ready.")
    return device


def assignable_functions(device: GpuDevice) -> tuple[str, ...]:
    """Return every PCI address handed to the guest for *device*.

    A discrete graphics card is nearly always paired with its own audio
    function on the same chip, and the hardware isolates the pair together,
    so both go to the sandbox. Falls back to the card alone when the machine
    reports no grouping.
    """
    return device.group_members or (device.address,)


def memlock_headroom_ok(memory_mib: int) -> bool:
    """Return whether this user may reserve enough memory for *memory_mib*.

    A sandbox using a graphics card has to keep all of its memory resident,
    and the operating system caps how much any one user can pin. When the cap
    is below the sandbox size the launch fails late and with an unhelpful
    message, so callers check this first.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on Windows hosts
        return True
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return True
    if soft < 0 or soft == resource.RLIM_INFINITY:
        return True
    # Leave room for the emulator's own mappings on top of guest memory.
    required = (memory_mib + _MEMLOCK_OVERHEAD_MIB) * 1024 * 1024
    return soft >= required


__all__ = [
    "DEFAULT_SYSFS_ROOT",
    "PCI_ADDRESS_PATTERN",
    "VFIO_DRIVER",
    "GpuDevice",
    "assignable_functions",
    "find_gpu",
    "iommu_enabled",
    "list_host_gpus",
    "memlock_headroom_ok",
    "resolve_gpu_selection",
]
