#!/usr/bin/env bash
# Layer a preset on top of the shared base rootfs.
#
# Usage:  build-preset.sh <preset> <base-rootfs.ext4> <output-dir> [size-mb]
# Env:    OS=ubuntu|alpine (default: ubuntu)
#         ARCH=amd64|arm64 (default: dpkg/uname)
#
# Produces:
#   ubuntu: <output-dir>/<preset>-<arch>-rootfs.ext4
#   alpine: <output-dir>/<preset>-<arch>-alpine-rootfs.ext4
#
# The Ubuntu asset name has no OS suffix for backward compat with the
# existing release; Alpine introduces the suffix as it lands. Same naming
# is mirrored in the published manifest's URL builder.
#
# Strategy: copy the base ext4, mount it, chroot into it, run the
# preset-specific install script, unmount. The result is a self-contained
# ext4 ready for zstd compression and upload.
#
# NOTE: openclaw uses its own builder (build_openclaw_rootfs) which bakes
# in a custom init script, sidecars, and systemctl proxy. It's not layered
# through this script. This script handles: codex, claude-code, hermes, pi,
# and the bare "ubuntu" image (no preset install — just the finalized base
# rootfs, used for `create --os ubuntu` on firecracker).
#
# Runs in CI on a matching-arch runner. Requires: chroot, mount (loop).
set -euo pipefail

PRESET="${1:?Usage: build-preset.sh <preset> <base-rootfs.ext4> <output-dir> [size-mb]}"
BASE_ROOTFS="${2:?Missing base-rootfs.ext4 path}"
OUT_DIR="${3:?Missing output directory}"
SIZE_MB="${4:-4096}"
OS="${OS:-ubuntu}"
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}"

# Normalize arch naming
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

case "$ARCH" in
  amd64) GUEST_AGENT_TARGET="x86_64-unknown-linux-musl" ;;
  arm64) GUEST_AGENT_TARGET="aarch64-unknown-linux-musl" ;;
esac

case "$OS" in
  ubuntu)
    OUT_NAME="${PRESET}-${ARCH}"
    SHELL_BIN="/bin/bash"
    ;;
  alpine)
    OUT_NAME="${PRESET}-${ARCH}-alpine"
    # Alpine ships /bin/bash (we apk-install it in the base) so the install
    # snippets below — which use ``set -euo pipefail`` and other bash-isms —
    # still run cleanly without busybox-ash quirks.
    SHELL_BIN="/bin/bash"
    ;;
  *)
    echo "Unsupported OS: $OS (expected: ubuntu | alpine)" >&2
    exit 1
    ;;
esac

# Phase 1 of the Alpine rollout (#264) restricts which presets are eligible:
# pure-JS presets only. hermes pulls musllinux-incompatible Python wheels
# and openclaw pulls glibc-only @node-llama-cpp prebuilts.
if [ "$OS" = "alpine" ]; then
  case "$PRESET" in
    codex|claude-code|pi) ;;
    *)
      echo "Preset '$PRESET' is not yet supported on Alpine (Phase 1 covers codex/claude-code/pi)." >&2
      exit 1
      ;;
  esac
fi

mkdir -p "$OUT_DIR"
ROOTFS="$OUT_DIR/${OUT_NAME}-rootfs.ext4"

echo "==> Copying base rootfs for preset '$PRESET' ($ARCH, $OS)..."
cp "$BASE_ROOTFS" "$ROOTFS"

# Resize if needed (base may be smaller than target)
CURRENT_SIZE_MB=$(stat -c '%s' "$ROOTFS" 2>/dev/null || stat -f '%z' "$ROOTFS")
CURRENT_SIZE_MB=$((CURRENT_SIZE_MB / 1048576))
if [ "$SIZE_MB" -gt "$CURRENT_SIZE_MB" ]; then
  echo "==> Resizing from ${CURRENT_SIZE_MB}M to ${SIZE_MB}M..."
  truncate -s "${SIZE_MB}M" "$ROOTFS"
  resize2fs "$ROOTFS" >/dev/null 2>&1
fi

# Mount the ext4 image
MNT=$(mktemp -d)
mount -o loop "$ROOTFS" "$MNT"

cleanup() {
  umount "$MNT/dev/pts" 2>/dev/null || true
  umount "$MNT/dev" 2>/dev/null || true
  umount "$MNT/sys" 2>/dev/null || true
  umount "$MNT/proc" 2>/dev/null || true
  umount "$MNT" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

# Bind-mount /proc, /sys, /dev for chroot
mount --bind /proc "$MNT/proc"
mount --bind /sys "$MNT/sys"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true

# DNS resolution inside chroot. Save the original so we can restore it
# before unmount — otherwise the CI runner's resolv.conf bakes into the
# published rootfs and ends up on every guest VM.
RESOLV_BACKUP=""
if [ -e "$MNT/etc/resolv.conf" ]; then
  RESOLV_BACKUP=$(mktemp)
  cp -a "$MNT/etc/resolv.conf" "$RESOLV_BACKUP"
fi
cp /etc/resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || true

echo "==> Installing preset '$PRESET' ($OS)..."

case "$PRESET" in
  codex)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @openai/codex
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  claude-code)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @anthropic-ai/claude-code
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  hermes)
    # Ubuntu-only — gated above, but keep the install body here so a
    # future Alpine-compatible spike can flip the gate without rewriting.
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      if [ ! -d /opt/hermes-agent ]; then
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent
      fi
      cd /opt/hermes-agent
      uv venv
      uv pip install -e ".[all]" || uv pip install -e .
      ln -sf /opt/hermes-agent/.venv/bin/hermes /usr/local/bin/hermes
      # uv keeps a wheel cache (~/.cache/uv) — gigabytes for "[all]" extras.
      uv cache clean >/dev/null 2>&1 || true
      # .git is dead weight for a non-developing install.
      rm -rf /opt/hermes-agent/.git
      rm -rf /root/.cache /tmp/*
    '
    ;;

  pi)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @mariozechner/pi-coding-agent
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  ubuntu)
    # Bare Ubuntu image: nothing to install on top of the base rootfs. The
    # shared finalize below (install /init + bake the guest agent) is all it
    # needs. Gives firecracker a raw-ext4 Ubuntu so `create --os ubuntu`
    # works download-only (no qcow2, which firecracker can't read).
    echo "==> Bare ubuntu image — no preset install."
    ;;

  *)
    echo "Unknown preset: $PRESET"
    exit 1
    ;;
esac

# Bake the SmolVM PID 1 init script. CLI boot args (init=/init +
# smolvm.authorized_key_b64=<base64>) are read by this script to install
# the launching user's pubkey into /root/.ssh/authorized_keys at boot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$SCRIPT_DIR/preset-init.sh" "$MNT/init"

# Bake the SmolVM Rust guest agent (vsock control plane) into every published
# image. preset-init.sh launches it before sshd, so the host can drive the
# guest over vsock. Keep the guest path in sync with
# src/smolvm/images/builder.py.
GUEST_AGENT_BINARY="${SMOLVM_GUEST_AGENT_BINARY:-$SCRIPT_DIR/../../target/$GUEST_AGENT_TARGET/release/smolvm-guest-agent}"
if [ ! -x "$GUEST_AGENT_BINARY" ]; then
  echo "Rust guest agent binary not found: $GUEST_AGENT_BINARY" >&2
  echo "Build it first: cargo build --release --target $GUEST_AGENT_TARGET -p smolvm-guest-agent" >&2
  exit 1
fi
install -D -m 0755 "$GUEST_AGENT_BINARY" "$MNT/usr/local/bin/smolvm-guest-agent"

# Bake the loadable-module tree for images meant to run with a graphics
# card. SMOLVM_MODULES_ARCHIVE points at the `modules-<arch>-gpu.tar.zst`
# produced by kernel/microvm/build.sh with SMOLVM_KERNEL_VARIANT=gpu.
#
# Without this the guest has no /lib/modules at all, so `modprobe` fails
# even for drivers that exist — which is exactly why the default images
# cannot load a vendor GPU driver. Left unset for ordinary images, so
# their contents are unchanged.
if [ -n "${SMOLVM_MODULES_ARCHIVE:-}" ]; then
  if [ ! -f "$SMOLVM_MODULES_ARCHIVE" ]; then
    echo "Module archive not found: $SMOLVM_MODULES_ARCHIVE" >&2
    exit 1
  fi
  echo "==> Installing kernel modules from $SMOLVM_MODULES_ARCHIVE"
  mkdir -p "$MNT/lib/modules"

  # Read the version out of the archive rather than off the mount. Listing
  # the directory after extraction would also see any lib/modules the base
  # image already carries, and picking the wrong one indexes a tree the
  # sandbox never boots — modprobe then fails with a missing modules.dep.
  KVER="$(tar --zstd -tf "$SMOLVM_MODULES_ARCHIVE" \
    | sed -n 's#^lib/modules/\([^/][^/]*\)/.*#\1#p' | sort -u)"
  if [ -z "$KVER" ]; then
    echo "Module archive contained no kernel version directory" >&2
    exit 1
  fi
  if [ "$(printf '%s\n' "$KVER" | wc -l)" -ne 1 ]; then
    echo "Module archive holds more than one kernel version: $KVER" >&2
    exit 1
  fi

  tar -C "$MNT" --zstd -xf "$SMOLVM_MODULES_ARCHIVE"

  # depmod runs here, not in the guest: it needs the module tree only, and
  # doing it now means the sandbox can modprobe on first boot. It comes from
  # the 'kmod' package, which the base image installs — say so plainly if a
  # base without it ever reaches this branch.
  if [ ! -x "$MNT/sbin/depmod" ] && [ ! -x "$MNT/usr/sbin/depmod" ] \
     && [ ! -x "$MNT/bin/depmod" ] && [ ! -x "$MNT/usr/bin/depmod" ]; then
    echo "This image has no depmod; add the 'kmod' package to its base rootfs." >&2
    exit 1
  fi
  chroot "$MNT" depmod -a "$KVER"

  # nouveau is the in-tree NVIDIA driver. It claims the card first and the
  # proprietary driver then refuses to bind, which is the single most
  # common reason an otherwise-correct setup ends with no working GPU.
  mkdir -p "$MNT/etc/modprobe.d"
  cat > "$MNT/etc/modprobe.d/smolvm-gpu.conf" <<'MODPROBE_EOF'
# Keep the in-tree NVIDIA driver out of the way so the vendor driver can
# bind to the card. Installed by scripts/ci/build-preset.sh.
blacklist nouveau
options nouveau modeset=0
MODPROBE_EOF
fi

# Restore (or remove) /etc/resolv.conf so the runner's DNS doesn't leak
# into the published rootfs. The init script writes 8.8.8.8 / 8.8.4.4 at
# boot, so removing it on the empty case is safe.
if [ -n "$RESOLV_BACKUP" ]; then
  cp -a "$RESOLV_BACKUP" "$MNT/etc/resolv.conf"
  rm -f "$RESOLV_BACKUP"
else
  rm -f "$MNT/etc/resolv.conf"
fi

echo "==> Preset rootfs: $ROOTFS ($(du -sh "$ROOTFS" | cut -f1))"
