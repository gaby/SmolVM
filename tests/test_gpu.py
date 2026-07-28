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

"""Tests for host graphics-card discovery.

Every test builds a fake ``/sys`` tree in ``tmp_path`` rather than reading the
real machine, so the suite behaves identically on a laptop with no discrete
card and on a workstation with two.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from smolvm.host import gpu

# PCI class values as sysfs reports them (24-bit register, "0x" prefixed).
CLASS_VGA = "0x030000"
CLASS_3D = "0x030200"
CLASS_AUDIO = "0x040300"
CLASS_BRIDGE = "0x060400"
CLASS_NVME = "0x010802"


def _write_device(
    sysfs: Path,
    address: str,
    *,
    class_id: str,
    vendor: str = "0x10de",
    device: str = "0x2684",
    driver: str | None = None,
    iommu_group: int | None = None,
) -> None:
    """Create one fake PCI device under *sysfs*."""
    device_dir = sysfs / "bus" / "pci" / "devices" / address
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / "class").write_text(f"{class_id}\n")
    (device_dir / "vendor").write_text(f"{vendor}\n")
    (device_dir / "device").write_text(f"{device}\n")

    if driver is not None:
        driver_dir = sysfs / "bus" / "pci" / "drivers" / driver
        driver_dir.mkdir(parents=True, exist_ok=True)
        (device_dir / "driver").symlink_to(driver_dir, target_is_directory=True)

    if iommu_group is not None:
        group_devices = sysfs / "kernel" / "iommu_groups" / str(iommu_group) / "devices"
        group_devices.mkdir(parents=True, exist_ok=True)
        (group_devices / address).symlink_to(device_dir, target_is_directory=True)
        (device_dir / "iommu_group").symlink_to(
            sysfs / "kernel" / "iommu_groups" / str(iommu_group),
            target_is_directory=True,
        )


@pytest.fixture
def sysfs(tmp_path: Path) -> Path:
    """Return an empty fake sysfs root."""
    root = tmp_path / "sys"
    (root / "bus" / "pci" / "devices").mkdir(parents=True)
    (root / "kernel" / "iommu_groups").mkdir(parents=True)
    return root


@pytest.fixture
def vfio_accessible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every VFIO group node exists and is usable by this user.

    The node lives at a fixed ``/dev`` path that a fake sysfs tree cannot
    stand in for, so readiness tests patch the probe directly.
    """
    monkeypatch.setattr(gpu, "_vfio_group_accessible", lambda group: True)


def _nvidia_pair(sysfs: Path, *, driver: str | None, group: int = 12) -> None:
    """Create the usual discrete-card layout: display function plus audio."""
    _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver=driver, iommu_group=group)
    _write_device(
        sysfs,
        "0000:01:00.1",
        class_id=CLASS_AUDIO,
        device="0x22ba",
        driver=driver,
        iommu_group=group,
    )


class TestListHostGpus:
    def test_finds_a_ready_card(self, sysfs: Path, vfio_accessible: None) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")

        found = gpu.list_host_gpus(sysfs)

        assert len(found) == 1
        card = found[0]
        assert card.address == "0000:01:00.0"
        assert card.vendor_name == "NVIDIA"
        assert card.device_id == "2684"
        assert card.driver == "vfio-pci"
        assert card.iommu_group == 12
        assert card.ready is True
        assert card.blocker is None

    def test_audio_function_is_not_listed_as_a_card(
        self, sysfs: Path, vfio_accessible: None
    ) -> None:
        """Only the graphics function is offered; its audio sibling is not."""
        _nvidia_pair(sysfs, driver="vfio-pci")

        addresses = [card.address for card in gpu.list_host_gpus(sysfs)]

        assert addresses == ["0000:01:00.0"]

    def test_headless_compute_card_is_found(self, sysfs: Path, vfio_accessible: None) -> None:
        """Datacenter cards report the 3D class and have no display output."""
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_3D, driver="vfio-pci", iommu_group=3)

        found = gpu.list_host_gpus(sysfs)

        assert [card.address for card in found] == ["0000:01:00.0"]
        assert found[0].ready is True

    def test_non_graphics_devices_are_ignored(self, sysfs: Path) -> None:
        _write_device(sysfs, "0000:02:00.0", class_id=CLASS_NVME, iommu_group=4)

        assert gpu.list_host_gpus(sysfs) == []

    def test_missing_pci_bus_returns_empty(self, tmp_path: Path) -> None:
        """A machine with no PCI bus at all reports nothing, and does not raise."""
        assert gpu.list_host_gpus(tmp_path / "nonexistent") == []

    def test_unknown_vendor_falls_back_to_the_raw_id(
        self, sysfs: Path, vfio_accessible: None
    ) -> None:
        _write_device(
            sysfs,
            "0000:01:00.0",
            class_id=CLASS_VGA,
            vendor="0x1234",
            driver="vfio-pci",
            iommu_group=1,
        )

        assert gpu.list_host_gpus(sysfs)[0].vendor_name == "1234"


class TestReadiness:
    def test_card_still_used_by_the_host_is_blocked(self, sysfs: Path) -> None:
        _nvidia_pair(sysfs, driver="nvidia")

        card = gpu.list_host_gpus(sysfs)[0]

        assert card.ready is False
        assert "still in use by this machine" in card.blocker
        assert "smolvm gpu list" in card.blocker

    def test_blocker_names_the_grouped_device_still_in_use(self, sysfs: Path) -> None:
        """The audio sibling holding the group back is named explicitly."""
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver="vfio-pci", iommu_group=12)
        _write_device(
            sysfs,
            "0000:01:00.1",
            class_id=CLASS_AUDIO,
            driver="snd_hda_intel",
            iommu_group=12,
        )

        card = gpu.list_host_gpus(sysfs)[0]

        assert card.ready is False
        assert "0000:01:00.1" in card.blocker

    def test_hardware_isolation_off_is_reported_once(self, sysfs: Path) -> None:
        """With no isolation groups at all, the message is about the machine."""
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver="vfio-pci")

        card = gpu.list_host_gpus(sysfs)[0]

        assert card.ready is False
        assert "isn't set up to share graphics cards" in card.blocker

    def test_inaccessible_vfio_node_is_blocked(
        self, sysfs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")
        monkeypatch.setattr(gpu, "_vfio_group_accessible", lambda group: False)

        card = gpu.list_host_gpus(sysfs)[0]

        assert card.ready is False
        assert "permission" in card.blocker


class TestIommuEnabled:
    def test_true_when_groups_exist(self, sysfs: Path) -> None:
        (sysfs / "kernel" / "iommu_groups" / "0").mkdir(parents=True)

        assert gpu.iommu_enabled(sysfs) is True

    def test_false_when_empty(self, sysfs: Path) -> None:
        assert gpu.iommu_enabled(sysfs) is False

    def test_false_when_absent(self, tmp_path: Path) -> None:
        assert gpu.iommu_enabled(tmp_path / "nonexistent") is False


class TestHandOverSet:
    def test_includes_the_audio_sibling(self, sysfs: Path, vfio_accessible: None) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")

        card = gpu.find_gpu("0000:01:00.0", sysfs)

        assert card.group_members == ("0000:01:00.0", "0000:01:00.1")

    def test_graphics_function_leads(self, sysfs: Path, vfio_accessible: None) -> None:
        """QEMU puts the first entry at function 0, so ordering is load-bearing."""
        _write_device(
            sysfs,
            "0000:01:00.1",
            class_id=CLASS_AUDIO,
            driver="vfio-pci",
            iommu_group=12,
        )
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver="vfio-pci", iommu_group=12)

        card = gpu.find_gpu("0000:01:00.0", sysfs)

        assert card.group_members[0] == "0000:01:00.0"

    def test_bridges_are_excluded(self, sysfs: Path, vfio_accessible: None) -> None:
        """A root port shares the group but is never handed to the guest."""
        _nvidia_pair(sysfs, driver="vfio-pci")
        _write_device(
            sysfs, "0000:00:01.0", class_id=CLASS_BRIDGE, driver="pcieport", iommu_group=12
        )

        card = gpu.find_gpu("0000:01:00.0", sysfs)

        assert "0000:00:01.0" not in card.group_members
        assert card.ready is True, "a bridge in the group must not block the card"

    def test_card_with_no_group_still_reports_itself(self, sysfs: Path) -> None:
        """Never empty — callers would otherwise have to special-case it."""
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver="vfio-pci")

        card = gpu.find_gpu("0000:01:00.0", sysfs)

        assert card.group_members == ("0000:01:00.0",)


class TestFindGpu:
    def test_accepts_the_short_lspci_form(self, sysfs: Path, vfio_accessible: None) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")

        assert gpu.find_gpu("01:00.0", sysfs).address == "0000:01:00.0"

    def test_is_case_insensitive(self, sysfs: Path, vfio_accessible: None) -> None:
        _write_device(sysfs, "0000:0a:00.0", class_id=CLASS_VGA, driver="vfio-pci", iommu_group=2)

        assert gpu.find_gpu("0000:0A:00.0", sysfs).address == "0000:0a:00.0"

    def test_unknown_address_points_at_gpu_list(self, sysfs: Path) -> None:
        with pytest.raises(ValueError) as excinfo:
            gpu.find_gpu("0000:09:00.0", sysfs)

        assert "No graphics card found at '0000:09:00.0'" in str(excinfo.value)
        assert "smolvm gpu list" in str(excinfo.value)


class TestResolveGpuSelection:
    def test_auto_picks_the_only_ready_card(self, sysfs: Path, vfio_accessible: None) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")

        assert gpu.resolve_gpu_selection("auto", sysfs).address == "0000:01:00.0"

    def test_auto_ignores_cards_that_are_not_ready(
        self, sysfs: Path, vfio_accessible: None
    ) -> None:
        _nvidia_pair(sysfs, driver="vfio-pci")
        _write_device(sysfs, "0000:02:00.0", class_id=CLASS_VGA, driver="nvidia", iommu_group=13)

        assert gpu.resolve_gpu_selection("auto", sysfs).address == "0000:01:00.0"

    def test_auto_with_no_ready_card_explains_why(self, sysfs: Path) -> None:
        _nvidia_pair(sysfs, driver="nvidia")

        with pytest.raises(ValueError) as excinfo:
            gpu.resolve_gpu_selection("auto", sysfs)

        assert "No graphics card on this machine is free" in str(excinfo.value)

    def test_auto_with_several_ready_cards_names_them(
        self, sysfs: Path, vfio_accessible: None
    ) -> None:
        _write_device(sysfs, "0000:01:00.0", class_id=CLASS_VGA, driver="vfio-pci", iommu_group=12)
        _write_device(sysfs, "0000:02:00.0", class_id=CLASS_VGA, driver="vfio-pci", iommu_group=13)

        with pytest.raises(ValueError) as excinfo:
            gpu.resolve_gpu_selection("auto", sysfs)

        message = str(excinfo.value)
        assert "more than one graphics card free" in message
        assert "0000:01:00.0" in message and "0000:02:00.0" in message

    def test_named_card_that_is_not_ready_raises_its_blocker(self, sysfs: Path) -> None:
        _nvidia_pair(sysfs, driver="nvidia")

        with pytest.raises(ValueError) as excinfo:
            gpu.resolve_gpu_selection("0000:01:00.0", sysfs)

        assert "still in use by this machine" in str(excinfo.value)


class TestMemlockHeadroom:
    def test_unlimited_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import resource

        monkeypatch.setattr(
            resource,
            "getrlimit",
            lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        )

        assert gpu.memlock_headroom_ok(8192) is True

    def test_small_limit_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import resource

        eight_mib = 8 * 1024 * 1024
        monkeypatch.setattr(resource, "getrlimit", lambda _which: (eight_mib, eight_mib))

        assert gpu.memlock_headroom_ok(4096) is False

    def test_generous_limit_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import resource

        limit = 32 * 1024 * 1024 * 1024
        monkeypatch.setattr(resource, "getrlimit", lambda _which: (limit, limit))

        assert gpu.memlock_headroom_ok(4096) is True


class TestSandboxGuards:
    """Guards that stop a graphics-card sandbox from reaching a broken state."""

    def _vm_info(self, tmp_path: Path, *, memory: int = 512):
        from smolvm.types import GpuPassthrough, VMConfig, VMInfo, VMState

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        card = GpuPassthrough(
            address="0000:01:00.0",
            functions=("0000:01:00.0", "0000:01:00.1"),
            vendor_id="10de",
            device_id="2684",
        )
        return VMInfo(
            vm_id="gputest",
            status=VMState.CREATED,
            config=VMConfig(
                vm_id="gputest",
                kernel_path=kernel,
                rootfs_path=rootfs,
                backend="qemu",
                memory=memory,
                gpus=[card],
            ),
        )

    def _manager(self):
        from smolvm.vm import SmolVMManager

        return SmolVMManager.__new__(SmolVMManager)

    def test_start_is_blocked_when_the_card_is_gone(self, tmp_path: Path) -> None:
        """Hardware moves; a saved sandbox may start on a machine without it."""
        from smolvm.exceptions import SmolVMError

        with (
            patch("smolvm.host.gpu.list_host_gpus", return_value=[]),
            pytest.raises(SmolVMError, match="No graphics card found at '0000:01:00.0'"),
        ):
            self._manager()._check_gpus(self._vm_info(tmp_path).config, "gputest")

    def test_start_is_blocked_when_the_card_was_reclaimed(self, tmp_path: Path, gpu_device) -> None:
        from smolvm.exceptions import SmolVMError

        reclaimed = gpu_device(
            blocker="The graphics card at '0000:01:00.0' is still in use by this machine."
        )
        with (
            patch("smolvm.host.gpu.find_gpu", return_value=reclaimed),
            pytest.raises(SmolVMError, match="still in use by this machine"),
        ):
            self._manager()._check_gpus(self._vm_info(tmp_path).config, "gputest")

    def test_start_is_blocked_when_a_different_card_took_the_slot(
        self, tmp_path: Path, gpu_device
    ) -> None:
        """Addresses are positional; a swapped card must not be used silently."""
        from smolvm.exceptions import SmolVMError

        other = gpu_device(device_id="1234")
        with (
            patch("smolvm.host.gpu.find_gpu", return_value=other),
            pytest.raises(SmolVMError, match="A different graphics card is now at"),
        ):
            self._manager()._check_gpus(self._vm_info(tmp_path).config, "gputest")

    def test_start_is_blocked_by_a_low_memory_reservation_cap(
        self, tmp_path: Path, gpu_device
    ) -> None:
        from smolvm.exceptions import SmolVMError

        with (
            patch("smolvm.host.gpu.find_gpu", return_value=gpu_device()),
            patch("smolvm.host.gpu.memlock_headroom_ok", return_value=False),
            pytest.raises(SmolVMError, match="ulimit -l unlimited"),
        ):
            self._manager()._check_gpus(self._vm_info(tmp_path).config, "gputest")

    def test_a_sandbox_without_a_card_skips_every_check(self, tmp_path: Path) -> None:
        """The check must not touch the host for the overwhelmingly common case."""
        from smolvm.types import VMConfig, VMInfo, VMState

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()
        plain = VMInfo(
            vm_id="plain",
            status=VMState.CREATED,
            config=VMConfig(vm_id="plain", kernel_path=kernel, rootfs_path=rootfs),
        )

        with patch("smolvm.host.gpu.find_gpu", side_effect=AssertionError("probed host")):
            self._manager()._check_gpus(plain.config, "plain")

    def test_memory_saving_snapshots_are_rejected(self, tmp_path: Path) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.types import SnapshotType

        manager = self._manager()
        vm_info = self._vm_info(tmp_path)

        for snapshot_type in (SnapshotType.FULL, SnapshotType.DIFF):
            with pytest.raises(SmolVMError, match="can only save its disk"):
                manager._ensure_snapshot_supported(vm_info, snapshot_type)

    def test_disk_snapshots_are_allowed(self, tmp_path: Path) -> None:
        """Disk-only snapshots never touch device state, so a card is fine."""
        from smolvm.exceptions import SmolVMError
        from smolvm.types import SnapshotType

        manager = self._manager()
        vm_info = self._vm_info(tmp_path)

        try:
            manager._ensure_snapshot_supported(vm_info, SnapshotType.DISK)
        except SmolVMError as exc:
            assert "graphics card" not in str(exc), "a disk snapshot must not be blocked by the GPU"

    def test_the_rejection_names_the_command_that_works(self, tmp_path: Path) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.types import SnapshotType

        with pytest.raises(SmolVMError) as excinfo:
            self._manager()._ensure_snapshot_supported(self._vm_info(tmp_path), SnapshotType.FULL)

        assert "smolvm sandbox snapshot create gputest --snapshot-type disk" in str(excinfo.value)
