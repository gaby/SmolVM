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

# Tests configuration

from collections.abc import Callable

import pytest

from smolvm.host.gpu import GpuDevice


@pytest.fixture
def gpu_device() -> Callable[..., GpuDevice]:
    """Return a factory for host graphics cards.

    Shared because four test modules need one and they only ever vary
    whether the card is free. Pass ``blocker=`` to get a card that is not:
    ``ready`` is derived from it, and the driver follows unless overridden.
    """

    def _make(
        address: str = "0000:01:00.0",
        *,
        blocker: str | None = None,
        driver: str | None = None,
        functions: tuple[str, ...] = ("0000:01:00.0", "0000:01:00.1"),
        group_members: tuple[str, ...] | None = None,
        vendor_id: str = "10de",
        device_id: str = "2684",
    ) -> GpuDevice:
        return GpuDevice(
            address=address,
            vendor_id=vendor_id,
            device_id=device_id,
            vendor_name="NVIDIA",
            driver=driver if driver is not None else ("nvidia" if blocker else "vfio-pci"),
            iommu_group=12,
            functions=functions,
            # Defaults to the card's own parts; pass this to model a machine
            # that isolates something unrelated alongside the card.
            group_members=group_members if group_members is not None else functions,
            blocker=blocker,
        )

    return _make
