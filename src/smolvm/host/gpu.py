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
  ``0000:01:00.0`` really means asking for ``0000:01:00.1`` too — that set is
  :attr:`GpuDevice.functions`. Anything *else* the machine isolates with the
  card merely has to be free (:attr:`GpuDevice.group_members`); handing it to
  the sandbox would give away hardware the user never asked about.
- **Everything is testable.** Each entry point takes ``sysfs_root`` and
  ``vfio_dev_root`` so tests can build a fake ``/sys`` tree and a fake
  ``/dev/vfio`` in a temporary directory, instead of depending on whatever
  hardware the test machine happens to have.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# A PCI address ("bus/device/function"), e.g. "0000:01:00.0". Lowercase hex;
# callers are normalized through _normalize_address before matching.
PCI_ADDRESS_PATTERN = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

DEFAULT_SYSFS_ROOT = Path("/sys")

# Where the kernel exposes one openable file per isolation group. Separate
# from sysfs because it lives under /dev, and injectable for the same reason
# sysfs_root is: readiness must be testable without real hardware.
DEFAULT_VFIO_DEV_ROOT = Path("/dev/vfio")

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
        functions: Every address handed to a sandbox for this card, the card
            itself first. These are the card's own parts — its graphics chip
            and the sound output built into the same hardware. Always
            contains at least the card itself.
        group_members: Every address the machine isolates alongside this card.
            A superset of *functions*: the extra entries have to be free
            before the card can be lent, but they are not handed over.
        blocker: Why a sandbox cannot use this card right now, in plain
            English. ``None`` when it can.
    """

    address: str
    vendor_id: str
    device_id: str
    vendor_name: str
    driver: str | None
    iommu_group: int | None
    functions: tuple[str, ...]
    group_members: tuple[str, ...]
    blocker: str | None

    @property
    def ready(self) -> bool:
        """Return whether a sandbox can use this card right now.

        Derived rather than stored: a card is ready exactly when nothing is
        blocking it, and keeping the two as separate fields would let them
        disagree.
        """
        return self.blocker is None

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
    """Return a sysfs ``0x1234`` id attribute as four lowercase hex digits.

    Maker and model ids are 16-bit. A card in a low-power state or removed
    mid-scan reads back as ``0xffffffff``, and formatting that verbatim would
    store an eight-digit id in the sandbox's settings — after which the
    start-time identity check compares it against the real id and refuses to
    start a sandbox whose hardware never changed.
    """
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        return None
    if not 0 <= value <= 0xFFFF:
        return None
    return f"{value:04x}"


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


def _read_link_name(link: Path) -> str | None:
    """Return the final path component a sysfs symlink points at.

    sysfs answers "which driver owns this?" and "which group is it in?" with
    symlinks whose target's last component is the answer.
    """
    try:
        if not link.is_symlink() and not link.exists():
            return None
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return None


def _read_driver(device_dir: Path) -> str | None:
    """Return the name of the driver bound to a device, or ``None``."""
    return _read_link_name(device_dir / "driver")


def _read_iommu_group(device_dir: Path) -> int | None:
    """Return the hardware isolation group number for a device."""
    name = _read_link_name(device_dir / "iommu_group")
    if name is None:
        return None
    try:
        return int(name)
    except ValueError:
        return None


def _iommu_group_members(sysfs_root: Path, group: int | None) -> tuple[str, ...]:
    """Return the PCI addresses in *group* that matter, sorted.

    Two kinds of entry are dropped, because neither has to be freed and
    neither can be handed to a sandbox:

    - **Non-PCI devices.** Linux lists platform devices in isolation groups
      on some machines (``soc:pcie@1000`` on arm64); they are not addressable
      as ``domain:bus:device.function``.
    - **Bridges.** The port a card hangs off shares its group by definition,
      and the hardware does not count it against the group being free.
    """
    if group is None:
        return ()
    group_dir = sysfs_root / "kernel" / "iommu_groups" / str(group) / "devices"
    try:
        names = sorted(entry.name for entry in group_dir.iterdir())
    except OSError:
        return ()
    return tuple(
        name
        for name in names
        if PCI_ADDRESS_PATTERN.match(name)
        and _read_class(sysfs_root / "bus" / "pci" / "devices" / name) not in _BRIDGE_CLASSES
    )


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


def _vfio_group_state(group: int, vfio_dev_root: Path = DEFAULT_VFIO_DEV_ROOT) -> str:
    """Return how usable the VFIO device for *group* is.

    Three answers, not two: ``"ready"``, ``"missing"`` when the operating
    system never exposed a device for the group, and ``"denied"`` when it did
    but this user cannot open it. They need different recoveries — joining a
    group cannot conjure a device node that isn't there.

    Mirrors the ``/dev/kvm`` probe in :mod:`smolvm.runtime.backends`: the
    file existing is not enough, the current user has to be able to open it.
    """
    node = vfio_dev_root / str(group)
    if not node.exists():
        return "missing"
    if not os.access(node, os.R_OK | os.W_OK):
        return "denied"
    return "ready"


def _blocker(
    *,
    sysfs_root: Path,
    vfio_dev_root: Path,
    address: str,
    iommu_on: bool,
    iommu_group: int | None,
    members: tuple[str, ...],
) -> str | None:
    """Return why a sandbox cannot use this card, or ``None`` if it can.

    The text is user-facing. It names the single most useful next step rather
    than every possible one — the full setup lives in ``smolvm gpu list``
    output and the GPU guide.
    """
    if not iommu_on:
        return (
            "This machine isn't set up to share graphics cards with sandboxes. "
            "Run 'smolvm gpu list' for the one-time setup steps."
        )
    if iommu_group is None:
        return (
            f"The graphics card at '{address}' isn't isolated from the rest of this "
            "machine, so a sandbox can't use it safely. Run 'smolvm gpu list' for the "
            "one-time setup steps."
        )

    # Every member of the group has to be free, not just the card itself.
    # A group whose listing was unreadable falls back to the card alone, so
    # this loop always checks at least the card.
    for member in members or (address,):
        member_driver = _read_driver(sysfs_root / "bus" / "pci" / "devices" / member)
        if member_driver == VFIO_DRIVER:
            continue
        # "Nothing is using it" and "something else is using it" are different
        # problems with different fixes, and saying "still in use" about a
        # card the table just listed as used by nothing reads as a bug.
        if member_driver is None:
            subject = (
                f"The graphics card at '{address}' isn't set up for sandboxes yet"
                if member == address
                else (
                    f"The graphics card at '{address}' is grouped with '{member}', "
                    "which isn't set up for sandboxes yet"
                )
            )
            return f"{subject}. Run 'smolvm gpu list' for the one-time setup steps."
        if member == address:
            return (
                f"The graphics card at '{address}' is still in use by this machine. "
                "Run 'smolvm gpu list' for the one-time steps to free it."
            )
        # Naming the exact device that is holding the group back is the
        # difference between a five-minute fix and a lost afternoon.
        return (
            f"The graphics card at '{address}' is grouped with '{member}', which is "
            "still in use by this machine. Run 'smolvm gpu list' to see what to free."
        )

    state = _vfio_group_state(iommu_group, vfio_dev_root)
    if state == "missing":
        return (
            f"This machine hasn't finished handing the graphics card at '{address}' over. "
            "Restart it, then run 'smolvm gpu list' again."
        )
    if state == "denied":
        return (
            f"You don't have permission to use the graphics card at '{address}'. "
            "Add yourself to the 'vfio' group, then start a new login session."
        )

    return None


def _build_device(
    sysfs_root: Path, address: str, *, iommu_on: bool, vfio_dev_root: Path
) -> GpuDevice | None:
    """Return a :class:`GpuDevice` for *address*, or ``None`` if not a GPU."""
    device_dir = sysfs_root / "bus" / "pci" / "devices" / address
    class_id = _read_class(device_dir)
    if class_id is None or class_id not in _GPU_CLASSES:
        return None

    vendor_id = _read_hex_id(device_dir / "vendor") or "0000"
    device_id = _read_hex_id(device_dir / "device") or "0000"
    iommu_group = _read_iommu_group(device_dir)
    members = _iommu_group_members(sysfs_root, iommu_group)
    functions = _card_functions(sysfs_root, address, iommu_group)

    return GpuDevice(
        address=address,
        vendor_id=vendor_id,
        device_id=device_id,
        vendor_name=_VENDOR_NAMES.get(vendor_id, vendor_id),
        driver=_read_driver(device_dir),
        iommu_group=iommu_group,
        functions=functions,
        group_members=members or functions,
        blocker=_blocker(
            sysfs_root=sysfs_root,
            vfio_dev_root=vfio_dev_root,
            address=address,
            iommu_on=iommu_on,
            iommu_group=iommu_group,
            members=members,
        ),
    )


def _same_hardware(address: str) -> str:
    """Return the part of a PCI address shared by one chip's functions.

    ``0000:01:00.0`` and ``0000:01:00.1`` are two functions of the same
    physical chip; ``0000:02:00.0`` is a different one.
    """
    return address.rsplit(".", 1)[0]


def _card_functions(sysfs_root: Path, address: str, group: int | None) -> tuple[str, ...]:
    """Return the addresses handed to a sandbox for this card, card first.

    Only the card's *own* functions — a graphics chip and the sound output
    built into it. Other devices the machine happens to isolate alongside it
    are deliberately excluded: a sandbox given one of those would get direct
    memory access to hardware the user never mentioned, and on a consumer
    board that can be a disk controller. The hardware only requires that such
    devices be *free*, not that they be handed over, which is what
    :func:`_blocker` checks.
    """
    prefix = _same_hardware(address)
    listed = _iommu_group_members(sysfs_root, group)
    if not listed:
        # No readable group: fall back to the sibling functions the bus lists.
        try:
            listed = tuple(
                sorted(
                    entry.name
                    for entry in (sysfs_root / "bus" / "pci" / "devices").iterdir()
                    if PCI_ADDRESS_PATTERN.match(entry.name)
                    and _read_class(entry) not in _BRIDGE_CLASSES
                )
            )
        except OSError:
            listed = ()

    functions = [
        member for member in listed if _same_hardware(member) == prefix and member != address
    ]
    # The card the user named leads: the emulator puts the first entry at the
    # guest function a driver expects the graphics part to occupy.
    return (address, *functions)


def list_host_gpus(
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    vfio_dev_root: Path = DEFAULT_VFIO_DEV_ROOT,
) -> list[GpuDevice]:
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

    # Whether the machine isolates hardware at all is the same answer for
    # every card, so read it once rather than per device.
    iommu_on = iommu_enabled(sysfs_root)

    found: list[GpuDevice] = []
    for name in entries:
        if not PCI_ADDRESS_PATTERN.match(name):
            continue
        device = _build_device(sysfs_root, name, iommu_on=iommu_on, vfio_dev_root=vfio_dev_root)
        if device is not None:
            found.append(device)
    return found


def find_gpu(
    address: str,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    vfio_dev_root: Path = DEFAULT_VFIO_DEV_ROOT,
) -> GpuDevice:
    """Return the graphics card at *address*.

    Goes straight to the device's own directory rather than scanning the
    whole bus — the address already names the path.

    Raises:
        ValueError: When no graphics card sits at that address. The message
            is user-facing and points at ``smolvm gpu list``.
    """
    wanted = _normalize_address(address)
    device = (
        _build_device(
            sysfs_root,
            wanted,
            iommu_on=iommu_enabled(sysfs_root),
            vfio_dev_root=vfio_dev_root,
        )
        if PCI_ADDRESS_PATTERN.match(wanted)
        else None
    )
    if device is None:
        raise ValueError(
            f"No graphics card found at '{address}' on this machine. "
            "Run 'smolvm gpu list' to see the cards SmolVM can use."
        )
    return device


def resolve_gpu_selection(
    selection: str,
    sysfs_root: Path = DEFAULT_SYSFS_ROOT,
    vfio_dev_root: Path = DEFAULT_VFIO_DEV_ROOT,
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
        ready = [device for device in list_host_gpus(sysfs_root, vfio_dev_root) if device.ready]
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

    device = find_gpu(selection, sysfs_root, vfio_dev_root)
    if not device.ready:
        raise ValueError(device.blocker or f"The graphics card at '{device.address}' isn't ready.")
    return device


def _exempt_from_memlock_cap() -> bool:
    """Return whether this process may keep memory resident without a cap.

    An administrator account is exempt: the kernel skips the accounting
    entirely for it, so the cap below says nothing about whether a sandbox
    will start.
    """
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def memlock_headroom_ok(memory_mib: int) -> bool:
    """Return whether this user may reserve enough memory for *memory_mib*.

    A sandbox using a graphics card has to keep all of its memory resident,
    and the operating system caps how much any one user can pin. When the cap
    is below the sandbox size the launch fails late and with an unhelpful
    message, so callers check this first.

    An administrator account is exempt from the cap — the kernel skips the
    accounting entirely for a privileged process — so answer yes for one
    rather than sending it off to raise a limit that does not apply.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - not available on Windows hosts
        return True
    if _exempt_from_memlock_cap():
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
    "GpuDevice",
    "find_gpu",
    "list_host_gpus",
    "memlock_headroom_ok",
    "resolve_gpu_selection",
]
