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

"""Tests for SmolVM kernel config fragments."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_FRAGMENT = REPO_ROOT / "kernel" / "microvm" / "config.fragment"


def _enabled_symbols(fragment: Path) -> set[str]:
    """Return symbols explicitly enabled in a kernel config fragment."""
    symbols: set[str] = set()
    for raw_line in fragment.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("CONFIG_") and line.endswith("=y"):
            symbols.add(line.removesuffix("=y"))
    return symbols


def test_microvm_kernel_enables_podman_netavark_networking() -> None:
    """Podman/Netavark needs nftables support for default bridge networking."""
    symbols = _enabled_symbols(COMMON_FRAGMENT)

    required = {
        "CONFIG_NETFILTER_ADVANCED",
        "CONFIG_NETFILTER_XTABLES",
        "CONFIG_NF_TABLES",
        "CONFIG_NF_TABLES_INET",
        "CONFIG_NFT_CT",
        "CONFIG_NFT_NAT",
        "CONFIG_NFT_MASQ",
        "CONFIG_NFT_COMPAT",
        "CONFIG_NETFILTER_XT_MATCH_COMMENT",
    }
    missing = required - symbols
    assert required <= symbols, f"missing symbols: {missing}"


GPU_FRAGMENT = REPO_ROOT / "kernel" / "microvm" / "config.gpu.fragment"


def _unset_symbols(fragment: Path) -> set[str]:
    """Return symbols a fragment explicitly turns off.

    ``# CONFIG_X is not set`` is a real Kconfig directive that merely looks
    like a comment, and both merge_config.sh and build.sh only honor it on a
    line of its own. Matching that exact shape here means a stray trailing
    comment fails these tests rather than silently voiding the directive.
    """
    symbols: set[str] = set()
    for raw_line in fragment.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            symbols.add(line.removeprefix("# ").removesuffix(" is not set"))
    return symbols


def _module_symbols(fragment: Path) -> set[str]:
    """Return symbols a fragment builds as loadable modules."""
    symbols: set[str] = set()
    for raw_line in fragment.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("CONFIG_") and line.endswith("=m"):
            symbols.add(line.removesuffix("=m"))
    return symbols


def test_default_kernel_still_has_no_modules() -> None:
    """The regression guard for keeping the GPU kernel a separate variant.

    Every sandbox boots the default kernel. If module support ever leaks into
    the common fragment, it drags in hundreds of drivers the project
    deliberately removed — so this asserts the split is still real.
    """
    assert "CONFIG_MODULES" in _unset_symbols(COMMON_FRAGMENT)
    assert "CONFIG_MODULES" not in _enabled_symbols(COMMON_FRAGMENT)


def test_gpu_variant_enables_modules() -> None:
    """A vendor graphics driver is built outside the tree and must load late."""
    symbols = _enabled_symbols(GPU_FRAGMENT)

    assert "CONFIG_MODULES" in symbols
    assert "CONFIG_MODULE_UNLOAD" in symbols


def test_gpu_variant_allows_unsigned_modules() -> None:
    """A driver compiled inside the sandbox is unsigned by definition."""
    assert "CONFIG_MODULE_SIG_FORCE" in _unset_symbols(GPU_FRAGMENT)


def test_gpu_variant_provides_the_rendering_core() -> None:
    """Both the NVIDIA and AMD drivers bind to the DRM layer."""
    assert "CONFIG_DRM" in _module_symbols(GPU_FRAGMENT)


def test_gpu_variant_trims_hardware_a_sandbox_never_has() -> None:
    """Turning modules on revives the defconfig's =m symbols; drop the big ones."""
    unset = _unset_symbols(GPU_FRAGMENT)

    for symbol in ("CONFIG_SOUND", "CONFIG_WLAN", "CONFIG_INFINIBAND", "CONFIG_BT"):
        assert symbol in unset, f"{symbol} should be trimmed from the GPU variant"


def test_gpu_variant_does_not_add_host_side_symbols() -> None:
    """VFIO belongs to the machine handing the card over, not to the sandbox."""
    text = GPU_FRAGMENT.read_text()
    enabled = _enabled_symbols(GPU_FRAGMENT) | _module_symbols(GPU_FRAGMENT)

    assert not any(symbol.startswith("CONFIG_VFIO") for symbol in enabled), text


def test_gpu_variant_leaves_boot_critical_drivers_built_in() -> None:
    """The variant must not move a boot-path driver into a module.

    There is no initrd, so anything needed to mount the root disk has to be
    inside the kernel. The variant may only add modules, never convert one.
    """
    modular = _module_symbols(GPU_FRAGMENT)
    boot_critical = _enabled_symbols(COMMON_FRAGMENT)

    assert not (modular & boot_critical), f"moved to modules: {modular & boot_critical}"
