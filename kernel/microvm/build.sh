#!/usr/bin/env bash
# Build the SmolVM QEMU/libkrun-tuned Linux kernel from upstream source.
#
# Inputs (alongside this script in kernel/microvm/):
#   linux.version    Pinned tarball version (e.g. "6.12.10")
#   linux.sha256     SHA-256 line for `sha256sum -c`
#   config.fragment  Our deltas merged onto x86_64_defconfig (x86) or
#                    defconfig (arm64)
#
# Output: vmlinux-<arch>.elf and vmlinux-<arch>.image in $OUT_DIR (default: $PWD),
#         plus vmlinux-<arch>.config for debugging.
#
# Usage:
#   bash build.sh                                # builds for host arch
#   SMOLVM_ARCH_OVERRIDE=arm64 bash build.sh     # cross-build (needs cross toolchain)
#   OUT_DIR=/tmp/k bash build.sh                 # custom output dir
#   MAKE=gmake bash build.sh                     # use a specific GNU Make
#
# In CI, the workflow runs this on a native runner per arch, so the arch
# defaults to the host. Local devs on Apple Silicon get arm64; on Intel
# Macs/Linux x86 boxes, amd64.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LINUX_VERSION="$(cat "$SCRIPT_DIR/linux.version" | tr -d '[:space:]')"
LINUX_SHA256_LINE="$(cat "$SCRIPT_DIR/linux.sha256")"
COMMON_FRAGMENT="$SCRIPT_DIR/config.fragment"
# Per-arch fragment is filled in once SMOLVM_ARCH is resolved, below.

find_make() {
    if [ -n "${MAKE:-}" ]; then
        command -v "$MAKE"
        return
    fi

    if command -v gmake >/dev/null 2>&1; then
        command -v gmake
        return
    fi

    command -v make
}

version_at_least_4() {
    case "$1" in
        ''|*[!0-9.]*)
            return 1
            ;;
    esac

    major="${1%%.*}"
    [ "$major" -ge 4 ]
}

job_count() {
    if command -v nproc >/dev/null 2>&1; then
        nproc
        return
    fi

    if command -v sysctl >/dev/null 2>&1; then
        sysctl -n hw.ncpu
        return
    fi

    echo 1
}

MAKE_BIN="$(find_make || true)"
if [ -z "$MAKE_BIN" ]; then
    echo "GNU Make 4.0 or newer is required. On macOS, install it with 'brew install make', then run 'MAKE=gmake bash build.sh'." >&2
    exit 2
fi

MAKE_VERSION="$("$MAKE_BIN" --version 2>/dev/null | sed -n '1s/.*Make //p' || true)"
MAKE_VERSION="${MAKE_VERSION%% *}"
if ! version_at_least_4 "$MAKE_VERSION"; then
    echo "GNU Make 4.0 or newer is required; '$MAKE_BIN' is version ${MAKE_VERSION:-unknown}. On macOS, install it with 'brew install make', then run 'MAKE=gmake bash build.sh'." >&2
    exit 2
fi

# Host arch → SmolVM arch label. Same mapping the manifest uses.
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
    x86_64|amd64)    SMOLVM_ARCH=amd64 ;;
    aarch64|arm64)   SMOLVM_ARCH=arm64 ;;
    *) echo "unsupported host arch: $HOST_ARCH" >&2; exit 2 ;;
esac
SMOLVM_ARCH="${SMOLVM_ARCH_OVERRIDE:-$SMOLVM_ARCH}"

# SmolVM arch label → kernel ARCH= variable.
#
# We ship TWO artifacts per arch — same source build, different output formats:
#   - vmlinux-<arch>.elf    The uncompressed ELF (kernel-source root).
#                           Firecracker REQUIRES this; bzImage/Image fail
#                           with `Kernel Loader: Invalid Elf magic number`.
#   - vmlinux-<arch>.image  The boot wrapper (bzImage on x86, Image on
#                           arm64). QEMU's `-machine virt -kernel` empirically
#                           refuses to boot a Linux ELF on aarch64 (silent
#                           hang, no console output), but boots Image fine.
#                           QEMU on x86 accepts either; we standardise on
#                           the boot wrapper for consistency.
# The `make $KMAKE_TARGET` step builds both as a side effect — the wrapper
# depends on the ELF — so producing two artifacts costs nothing extra.
# KARCH is the ARCH= value passed to make; KSRCARCH is the directory under
# arch/ that actually holds the sources. They differ on x86: ARCH=x86_64 but
# the tree is arch/x86. Only the header staging below needs KSRCARCH.
case "$SMOLVM_ARCH" in
    amd64)  KARCH=x86_64; KSRCARCH=x86;   KMAKE_TARGET=bzImage; DEFCONFIG=x86_64_defconfig ;;
    arm64)  KARCH=arm64;  KSRCARCH=arm64; KMAKE_TARGET=Image;   DEFCONFIG=defconfig ;;
    *) echo "internal error: unhandled SMOLVM_ARCH $SMOLVM_ARCH" >&2; exit 2 ;;
esac
case "$SMOLVM_ARCH" in
    amd64)  IMAGE_REL=arch/x86/boot/bzImage ;;
    arm64)  IMAGE_REL=arch/arm64/boot/Image ;;
esac
ELF_REL=vmlinux  # ELF, root of the kernel source tree

ARCH_FRAGMENT="$SCRIPT_DIR/config.$SMOLVM_ARCH.fragment"
if [ ! -f "$ARCH_FRAGMENT" ]; then
    echo "internal error: missing $ARCH_FRAGMENT" >&2
    exit 2
fi

# Kernel variant. "default" is the small, module-free microvm kernel every
# sandbox boots. "gpu" adds loadable-module support so a sandbox given a real
# graphics card can install that card's vendor driver — impossible otherwise,
# because those drivers live outside the kernel tree. See config.gpu.fragment
# for why this is a separate artifact rather than a change to the default.
SMOLVM_KERNEL_VARIANT="${SMOLVM_KERNEL_VARIANT:-default}"
VARIANT_FRAGMENT=""
ARTIFACT_SUFFIX=""
case "$SMOLVM_KERNEL_VARIANT" in
    default)
        ;;
    gpu)
        VARIANT_FRAGMENT="$SCRIPT_DIR/config.$SMOLVM_KERNEL_VARIANT.fragment"
        ;;
    *)
        echo "unsupported SMOLVM_KERNEL_VARIANT: $SMOLVM_KERNEL_VARIANT (want 'default' or 'gpu')" >&2
        exit 2
        ;;
esac
# Everything below is variant-agnostic: adding a variant means adding one
# case arm above plus its fragment, not touching the build or packaging.
if [ -n "$VARIANT_FRAGMENT" ]; then
    ARTIFACT_SUFFIX="-$SMOLVM_KERNEL_VARIANT"
    if [ ! -f "$VARIANT_FRAGMENT" ]; then
        echo "internal error: missing $VARIANT_FRAGMENT" >&2
        exit 2
    fi
fi

OUT_DIR="${OUT_DIR:-$PWD}"
WORK_DIR="${WORK_DIR:-$(mktemp -d)}"
TARBALL="$WORK_DIR/linux-$LINUX_VERSION.tar.xz"
SRC_DIR="$WORK_DIR/linux-$LINUX_VERSION"
ELF_ARTIFACT="$OUT_DIR/vmlinux-$SMOLVM_ARCH$ARTIFACT_SUFFIX.elf"
IMAGE_ARTIFACT="$OUT_DIR/vmlinux-$SMOLVM_ARCH$ARTIFACT_SUFFIX.image"
MODULES_ARTIFACT="$OUT_DIR/modules-$SMOLVM_ARCH$ARTIFACT_SUFFIX.tar.zst"
JOBS="$(job_count)"

echo "==> Linux $LINUX_VERSION → $SMOLVM_ARCH (kernel ARCH=$KARCH, variant=$SMOLVM_KERNEL_VARIANT)"
echo "    work dir:    $WORK_DIR"
echo "    elf out:     $ELF_ARTIFACT"
echo "    image out:   $IMAGE_ARTIFACT"
echo "    make:        $MAKE_BIN ($MAKE_VERSION)"

# 1. Download the tarball and verify against our pinned SHA.
if [ ! -f "$TARBALL" ]; then
    echo "==> Downloading linux-$LINUX_VERSION.tar.xz"
    curl --fail --location --silent --show-error \
        --output "$TARBALL" \
        "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$LINUX_VERSION.tar.xz"
fi

echo "==> Verifying tarball SHA-256"
(cd "$WORK_DIR" && echo "$LINUX_SHA256_LINE" | sha256sum -c -)

# 2. Extract.
if [ ! -d "$SRC_DIR" ]; then
    echo "==> Extracting"
    tar -C "$WORK_DIR" -xf "$TARBALL"
fi

cd "$SRC_DIR"

# 3. Apply baseline defconfig + our fragments (common + per-arch).
echo "==> Generating .config (baseline=$DEFCONFIG + common + $SMOLVM_ARCH fragments)"
"$MAKE_BIN" ARCH="$KARCH" "$DEFCONFIG" >/dev/null

# merge_config.sh accepts multiple fragments; -m mode merges, preserving any
# settings not mentioned. Common first, per-arch second.
# Variant fragment (if any) goes last so it can override the common one —
# the gpu variant deliberately flips CONFIG_MODULES back on.
scripts/kconfig/merge_config.sh -m -O . .config \
    "$COMMON_FRAGMENT" "$ARCH_FRAGMENT" ${VARIANT_FRAGMENT:+"$VARIANT_FRAGMENT"} >/dev/null

# olddefconfig fills in any new symbols introduced by Linux that weren't in
# the merged base — picks each one's default. Without this, a Linux bump can
# leave half-configured symbols that fail the build cryptically.
"$MAKE_BIN" ARCH="$KARCH" olddefconfig >/dev/null

# 4. Sanity check: every directive in our fragments must hold in .config —
# both `CONFIG_X=y` and `# CONFIG_X is not set`. Catches the case where
# olddefconfig silently flips one of our deltas (e.g. a missing dependency
# downgrades =y, or a new Kconfig dep forces modules on) — actionable signal
# that the fragment needs adjustment, not a silent failure to debug at boot.
echo "==> Verifying fragments were honored"
# `fail` is intentionally NOT local in verify_fragment — it accumulates
# across both calls below so we report ALL violations in one pass.
fail=0

# Symbols the variant fragment deliberately overrides. The gpu variant flips
# CONFIG_MODULES back on, which directly contradicts the common fragment's
# `# CONFIG_MODULES is not set` — so verifying the common fragment against
# the merged .config would report a violation that is in fact the point.
# Skip those symbols when checking the earlier fragments; the variant
# fragment is still verified on its own terms below.
overridden_symbols=""
if [ -n "$VARIANT_FRAGMENT" ]; then
    overridden_symbols=" $(sed -n \
        -e 's/^[[:space:]]*#[[:space:]]*\(CONFIG_[A-Za-z0-9_]*\)[[:space:]]*is not set[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*\(CONFIG_[A-Za-z0-9_]*\)=.*/\1/p' \
        "$VARIANT_FRAGMENT" | tr '\n' ' ')"
fi

is_overridden() {
    case "$overridden_symbols" in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

verify_fragment() {
    local fragment="$1"
    local allow_override_skip="${2:-no}"
    while IFS= read -r raw; do
        # ltrim + rtrim WITHOUT stripping '#' — we need to detect "is not set"
        # lines, which look like comments but are real Kconfig directives.
        local trimmed="${raw#"${raw%%[![:space:]]*}"}"
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
        case "$trimmed" in
            "# CONFIG_"*" is not set")
                local rest="${trimmed#"# "}"
                local symbol="${rest%% *}"
                if [ "$allow_override_skip" = yes ] && is_overridden "$symbol"; then
                    continue
                fi
                if grep -qE "^${symbol}=(y|m)$" .config; then
                    echo "  MISSING: $symbol — wanted unset, .config has: $(grep -E "^${symbol}=" .config) (from $(basename "$fragment"))"
                    fail=1
                fi
                continue
                ;;
        esac
        # Plain comments (not "is not set" directives) and inline comments are
        # discarded only after the "is not set" check above.
        local line="${raw%%#*}"
        line="${line%"${line##*[![:space:]]}"}"
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            "") continue ;;
            "CONFIG_"*=y|"CONFIG_"*=m)
                local symbol="${line%%=*}"
                local want="${line#*=}"
                if [ "$allow_override_skip" = yes ] && is_overridden "$symbol"; then
                    continue
                fi
                grep -qE "^${symbol}=${want}$" .config && continue
                echo "  MISSING: $symbol — wanted =$want, .config has: $(grep -E "^# ?${symbol}[ =]" .config || echo '<absent>') (from $(basename "$fragment"))"
                fail=1
                ;;
        esac
    done < "$fragment"
}
verify_fragment "$COMMON_FRAGMENT" yes
verify_fragment "$ARCH_FRAGMENT" yes
if [ -n "$VARIANT_FRAGMENT" ]; then
    verify_fragment "$VARIANT_FRAGMENT"
fi
[ "$fail" -eq 0 ] || { echo "==> Fragment verification failed"; exit 1; }

# Local-iteration knob: when truthy, stop after fragment verification — the
# kernel compile is the long pole and not what changes when iterating on
# the fragments. CI runs the full build; this is just for fast feedback
# loops on a dev machine (or a Linux container). Truthy = 1/true/yes
# (case-insensitive); empty/0/false/no = full build.
case "$(printf '%s' "${SMOLVM_VERIFY_ONLY:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes)
        echo "==> SMOLVM_VERIFY_ONLY set; skipping kernel compile."
        exit 0
        ;;
esac

# 5. Build the kernel image.
# Invoking the boot-wrapper target (bzImage/Image) drives a full link, so the
# ELF `vmlinux` is produced as a prerequisite. We then ship both: the wrapper
# for QEMU, the ELF for Firecracker.
echo "==> Building kernel ($JOBS jobs)"
"$MAKE_BIN" ARCH="$KARCH" -j"$JOBS" "$KMAKE_TARGET"

# 5b. Build and package the module tree for variants that enable modules.
# The default variant has none (CONFIG_MODULES is off), so this is skipped
# and its artifact set is unchanged.
if grep -q '^CONFIG_MODULES=y$' .config; then
    MOD_STAGE="$WORK_DIR/modules-install"
    rm -rf "$MOD_STAGE"
    mkdir -p "$MOD_STAGE"

    # modules_install only *warns* when depmod is absent, and the resulting
    # tree silently lacks modules.dep — so `modprobe` in the guest cannot
    # resolve dependencies and every driver install fails for a reason that
    # points nowhere near here. Fail up front instead.
    if ! command -v depmod >/dev/null 2>&1 && [ ! -x /sbin/depmod ]; then
        echo "depmod is required to build the gpu variant (install the 'kmod' package)." >&2
        exit 2
    fi

    echo "==> Building modules ($JOBS jobs)"
    "$MAKE_BIN" ARCH="$KARCH" -j"$JOBS" modules

    # INSTALL_MOD_STRIP cuts debug info; the tree is shipped to sandboxes and
    # nobody debugs a vendor driver from inside one. depmod runs here so the
    # guest does not need to (and could not — it has no kernel source).
    echo "==> Installing modules"
    "$MAKE_BIN" ARCH="$KARCH" INSTALL_MOD_PATH="$MOD_STAGE" \
        INSTALL_MOD_STRIP=1 modules_install >/dev/null

    # modules_install leaves build/source symlinks pointing into this
    # throwaway work directory; they would dangle inside a sandbox.
    find "$MOD_STAGE/lib/modules" -maxdepth 2 \( -name build -o -name source \) \
        -type l -delete

    # Stage the headers an out-of-tree driver needs to compile. Without
    # these, a sandbox can load modules but cannot build one — and NVIDIA's
    # driver is only ever built on the target machine, so the whole variant
    # would be pointless. This is the same content a distro ships as
    # "kernel-devel" / "linux-headers-$(uname -r)", trimmed to what
    # scripts/package/builddeb installs.
    KVER="$("$MAKE_BIN" ARCH="$KARCH" -s kernelrelease)"
    BUILD_DIR="$MOD_STAGE/lib/modules/$KVER/build"
    echo "==> Staging kernel headers for $KVER"
    mkdir -p "$BUILD_DIR"

    # Top-level files the kbuild machinery reads on every out-of-tree build.
    for f in Makefile Module.symvers .config System.map; do
        [ -e "$f" ] && cp -a "$f" "$BUILD_DIR/"
    done

    # Per-directory kbuild files, copied through tar so the tree layout is
    # preserved — kbuild resolves relative include paths and a flat copy
    # would break them. tar (not cpio) because the build already requires
    # it and cpio is not in CI's package list.
    #
    # Errors are NOT swallowed: a silent failure here ships a module tree
    # whose build/ directory has no Makefiles, and the first sign of it is a
    # vendor driver failing to compile inside somebody's sandbox.
    find . -path ./.git -prune -o \
        \( -name 'Makefile*' -o -name 'Kconfig*' -o -name 'Kbuild*' \) -print \
        | tar -cf - --files-from=- \
        | tar -C "$BUILD_DIR" -xf -

    # Note KSRCARCH, not KARCH: the kernel's ARCH= value for 64-bit x86 is
    # "x86_64" but its source tree is arch/x86. Using KARCH here silently
    # copies nothing on x86, and the missing asm/ headers only surface much
    # later as a failed driver build inside a sandbox.
    for d in include "arch/$KSRCARCH/include" scripts "arch/$KSRCARCH/tools"; do
        if [ -d "$d" ]; then
            mkdir -p "$BUILD_DIR/$(dirname "$d")"
            cp -a "$d" "$BUILD_DIR/$(dirname "$d")/"
        fi
    done
    # objtool is invoked during the link of any module on x86.
    if [ -d tools/objtool ]; then
        mkdir -p "$BUILD_DIR/tools"
        cp -a tools/objtool "$BUILD_DIR/tools/"
    fi
    # The guest resolves `source` the same way distros do.
    ln -sfn build "$MOD_STAGE/lib/modules/$KVER/source"

    # Belt and braces: modules.dep is what modprobe actually reads, so a tree
    # without it looks fine on disk and fails in the sandbox.
    if [ ! -f "$MOD_STAGE/lib/modules/$KVER/modules.dep" ]; then
        echo "==> ERROR: module tree has no modules.dep; modprobe would fail in the guest" >&2
        exit 1
    fi

    # modules.dep comes from modules_install, so it says nothing about the
    # header staging above. Check that separately, or an empty build/ ships.
    if [ ! -f "$BUILD_DIR/Makefile" ] || [ ! -f "$BUILD_DIR/scripts/Makefile" ]; then
        echo "==> ERROR: staged headers are incomplete; an out-of-tree driver could not build" >&2
        exit 1
    fi

    mkdir -p "$OUT_DIR"
    tar -C "$MOD_STAGE" -cf - lib/modules | zstd -q -19 -T0 -o "$MODULES_ARTIFACT" -f
    echo "==> Modules: $MODULES_ARTIFACT ($(wc -c <"$MODULES_ARTIFACT" | tr -d ' ') bytes)"
fi

# 6. Stage the artifacts + record the resolved config (debugging aid).
mkdir -p "$OUT_DIR"
cp "$IMAGE_REL" "$IMAGE_ARTIFACT"
cp "$ELF_REL" "$ELF_ARTIFACT"
cp .config "$OUT_DIR/vmlinux-$SMOLVM_ARCH$ARTIFACT_SUFFIX.config"

# Strip the ELF: with the default kernel build it's ~290 MB on arm64
# (debug_info + symbols). Firecracker doesn't need any of that. Keep
# `--strip-debug` (not `--strip-all`) — preserves the symbol table for
# tooling like crash dumps. Cuts the artifact by ~3x.
strip --strip-debug "$ELF_ARTIFACT"

# Sanity checks:
#   ELF magic (7f 45 4c 46) on the .elf — Firecracker requires this.
elf_magic=$(head -c 4 "$ELF_ARTIFACT" | od -An -tx1 | tr -d ' ')
if [ "$elf_magic" != "7f454c46" ]; then
    echo "==> ERROR: $ELF_ARTIFACT is not an ELF binary (got magic $elf_magic)" >&2
    exit 1
fi
#   Image must NOT be ELF — QEMU on aarch64 virt empirically refuses to boot
#   a Linux ELF kernel and just hangs silently. Catch it early.
img_magic=$(head -c 4 "$IMAGE_ARTIFACT" | od -An -tx1 | tr -d ' ')
if [ "$img_magic" = "7f454c46" ]; then
    echo "==> ERROR: $IMAGE_ARTIFACT is unexpectedly ELF (QEMU/virt won't boot)" >&2
    exit 1
fi

echo "==> Done."
echo "    $ELF_ARTIFACT    ($(wc -c <"$ELF_ARTIFACT" | tr -d ' ') bytes, ELF for Firecracker)"
echo "    $IMAGE_ARTIFACT  ($(wc -c <"$IMAGE_ARTIFACT" | tr -d ' ') bytes, ${KMAKE_TARGET} for QEMU)"
echo "    $OUT_DIR/vmlinux-$SMOLVM_ARCH$ARTIFACT_SUFFIX.config"
if [ -f "$MODULES_ARTIFACT" ]; then
    echo "    $MODULES_ARTIFACT  ($(wc -c <"$MODULES_ARTIFACT" | tr -d ' ') bytes, module tree)"
fi
