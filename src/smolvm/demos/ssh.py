#!/usr/bin/env python3

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

"""
SmolVM SSH Demo — Automated Image Building + SSH Access

Builds an Alpine Linux image with SSH using Docker, boots a VM,
and connects via SSH to run commands.

Usage:
    sudo .venv/bin/python demo_auto_ssh.py
"""

import logging
import os
import select
import signal
import subprocess
import sys
import time



from smolvm import HostManager, ImageBuilder, SmolVM, SSHClient, VMConfig
from smolvm.build import SSH_BOOT_ARGS

logging.basicConfig(level=logging.INFO, format="%(message)s")


from smolvm.utils import ensure_ssh_key



stop_requested = False


def _request_stop(signum: int, frame: object) -> None:
    """Signal handler that requests graceful shutdown."""
    del signum, frame
    global stop_requested
    stop_requested = True


def _wait_for_stop() -> None:
    """Wait for SIGINT/SIGTERM or terminal input (Enter/q) to stop."""
    global stop_requested
    while not stop_requested:
        if sys.stdin.isatty():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            except (OSError, ValueError):
                time.sleep(1.0)
                continue

            if ready:
                line = sys.stdin.readline()
                if line == "" or line.strip().lower() in {"", "q", "quit", "exit"}:
                    stop_requested = True
                    return
        else:
            time.sleep(1.0)


def main() -> int:
    global stop_requested
    stop_requested = False
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    print("\n🔥 SmolVM — Automated SSH Demo\n")
    print("=" * 70)

    
    try:
        # ── Step 1: Build image ──────────────────────────────────────────────
        builder = ImageBuilder()
        # Use shared key location (or custom /tmp for demo if desired? keeping simple)
        private_key, public_key = ensure_ssh_key()

        print("\n🐳 Checking Docker...")
        if not builder.check_docker():
            print("   ❌ Docker not found. Install: sudo apt install docker.io")
            return 1
        print("   ✓ Docker available")

        print("\n🔨 Building Alpine key-only SSH image (cached after first run)...\n")
        try:
            kernel, rootfs = builder.build_alpine_ssh_key(
                ssh_public_key=public_key,
                name="alpine-ssh-key-demo",
            )
        except Exception as e:
            print(f"\n   ❌ Build failed: {e}")
            return 1

        print(f"\n   ✓ Kernel:  {kernel}")
        print(f"   ✓ Rootfs:  {rootfs}")
        print(f"   ✓ SSH key: {private_key}")

        # ── Step 2: Host preflight (auto-install Firecracker if needed) ─────
        sdk = SmolVM()
        print("\n📋 Checking host prerequisites...")
        errors = sdk.check_prerequisites()

        if errors and any("firecracker" in e.lower() for e in errors):
            print("   ⚙️  Firecracker not found, auto-installing...")
            try:
                fc_path = HostManager().install_firecracker()
                print(f"   ✓ Installed Firecracker: {fc_path}")
                errors = sdk.check_prerequisites()
            except Exception as e:
                print(f"   ❌ Firecracker install failed: {e}")
                return 1

        if errors:
            print(f"   ❌ Missing prerequisites: {', '.join(errors)}")
            return 1
        print("   ✓ Prerequisites OK")

        # ── Step 3: Create & start VM ────────────────────────────────────────
        vm_id = "ssh-demo"

        # Clean up any leftover VM from a previous run
        try:
            sdk.delete(vm_id)
        except Exception:
            pass

        config = VMConfig(
            vm_id=vm_id,
            vcpu_count=1,
            mem_size_mib=512,
            kernel_path=kernel,
            rootfs_path=rootfs,
            boot_args=SSH_BOOT_ARGS,
        )

        print(f"\n📝 Creating VM '{vm_id}'...")
        vm_info = sdk.create(config)
        guest_ip = vm_info.network.guest_ip

        print(f"   ✓ VM created — IP: {guest_ip}")
        if vm_info.network and vm_info.network.ssh_host_port:
            print(f"   ✓ Host SSH port: {vm_info.network.ssh_host_port}")

        print("\n🚀 Starting VM...")
        try:
            sdk.start(vm_id)
        except Exception as e:
            print(f"   ❌ Start failed: {e}")
            sdk.delete(vm_id)
            return 1
        print("   ✓ VM started")

        # ── Step 4: Wait for SSH ─────────────────────────────────────────────
        print("\n⏳ Waiting for SSH (takes ~5-10 seconds)...")
        ssh_candidates: list[tuple[str, int, str]] = []
        if vm_info.network and vm_info.network.ssh_host_port:
            ssh_candidates.append(("127.0.0.1", vm_info.network.ssh_host_port, "host port-forward"))
        ssh_candidates.append((guest_ip, 22, "guest IP"))

        # Try multiple endpoints
        ssh: SSHClient | None = None
        selected_host = ""
        selected_port = 0
        wait_errors: list[tuple[str, str, int, str]] = []
        per_target_timeout = 30.0 if len(ssh_candidates) == 1 else 15.0

        for host, port, label in ssh_candidates:
            print(f"   → Trying {label}: {host}:{port}")
            candidate = SSHClient(
                host=host,
                user="root",
                port=port,
                key_path=str(private_key),
            )
            try:
                candidate.wait_for_ssh(timeout=per_target_timeout, interval=2.0)
                ssh = candidate
                selected_host = host
                selected_port = port
                print(f"   ✓ SSH is ready via {label}")
                break
            except Exception as e:
                wait_errors.append((label, host, port, str(e)))

        if ssh is None:
            print("\n   ⚠️  SSH timed out on all endpoints.")
            for label, host, port, error in wait_errors:
                print(f"      - {label} ({host}:{port}): {error}")

            print("\n   🔍 Automated Diagnostics:")
            log_file = sdk.data_dir / f"{vm_id}.log"
            if log_file.exists():
                 print(f"      📄 Last 20 lines of {log_file}:")
                 print("-" * 40)
                 try:
                     subprocess.run(["tail", "-n", "20", str(log_file)])
                 except Exception:
                     print("         (failed to read log)")
                 print("-" * 40)
            
            # Get correct TAP name
            tap_name = "unknown"
            if vm_info.network and vm_info.network.tap_device:
                tap_name = vm_info.network.tap_device

            print(f"\n      🌐 Network Status (TAP: {tap_name}):")
            
            # 1. Check if the specific TAP exists
            try:
                 subprocess.run(["ip", "addr", "show", tap_name])
            except Exception:
                 print(f"         (failed to show {tap_name})")

            # 2. Check for conflicting routes/TAPs
            print("\n      🛣️  Routes & Conflicts:")
            try:
                 subprocess.run(["ip", "route", "show", "172.16.0.0/24"])
                 print("         Full interface list (tap*):")
                 subprocess.run("ip link show | grep tap", shell=True)
            except Exception:
                 pass

            # 3. Connectivity check
            print(f"\n      📶 Connectivity Check ({guest_ip}):")
            try:
                 subprocess.run(["ping", "-c", "2", "-W", "1", guest_ip])
            except Exception:
                 pass
                 
            # 4. Firewall rules
            print("\n      🛡️  Firewall Rules (Forwarding):")
            try:
                 subprocess.run("sudo -n iptables -L FORWARD -v -n | head -n 10", shell=True)
            except Exception:
                 pass

            print(f"\n   Debug: sudo tail -f {log_file}")
            print("   Try manually:")
            for host, port, _ in ssh_candidates:
                print(f"      ssh -i {private_key} -p {port} root@{host}")
            print("\n   Press Ctrl+C (or Enter) to clean up...")
            _wait_for_stop()

        # ── Step 5: Run commands via SSH ─────────────────────────────────────
        print("\n" + "=" * 70)
        print("🔧 Running commands via SSH")
        print("=" * 70)

        commands = [
            ("uname -a",                    "Kernel"),
            ("cat /etc/os-release | head -2", "OS"),
            ("whoami",                       "User"),
            ("free -m | head -2",           "Memory"),
            ("df -h / | tail -1",           "Disk"),
            ("ip addr show eth0 | grep inet", "Network"),
            ("ps aux | grep sshd | grep -v grep", "SSH daemon"),
        ]

        for cmd, label in commands:
            result = ssh.run(cmd, timeout=5)
            if result.ok:
                lines = result.stdout.strip()
                print(f"\n  {label}:")
                for line in lines.split("\n"):
                    print(f"    {line}")
            else:
                print(f"\n  {label}: (failed) {result.stderr.strip()}")

        # ── Done ─────────────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("✅ SSH is fully working!")
        print("=" * 70)
        print("\n  💡 Connect from another terminal:")
        print("     # Works immediately (key is created by root in this demo)")
        print(f"     sudo ssh -i {private_key} root@{guest_ip}")
        if vm_info.network and vm_info.network.ssh_host_port:
            print(f"     sudo ssh -i {private_key} -p {vm_info.network.ssh_host_port} root@127.0.0.1")

        if os.environ.get("SUDO_USER"):
            sudo_user = os.environ["SUDO_USER"]
            print("\n     # Optional: allow your normal user to use the same key")
            print(
                f"     sudo chown {sudo_user}:{sudo_user} {private_key} {public_key}"
            )
            print(f"     ssh -i {private_key} root@{guest_ip}")
            if vm_info.network and vm_info.network.ssh_host_port:
                print(f"     ssh -i {private_key} -p {vm_info.network.ssh_host_port} root@127.0.0.1")

        if selected_host != guest_ip or selected_port != 22:
            print(f"     # Endpoint used by this run: ssh -i {private_key} -p {selected_port} root@{selected_host}")
        
        # ── Step 6: Keep running until Ctrl+C ────────────────────────────────
        print("\n  ⏸️  VM is running. Press Ctrl+C (or Enter) to stop and clean up...\n")
        _wait_for_stop()
    except Exception as e:
        print(f"\n❌ Unexpected Error: {e}")
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)

        if stop_requested:
            print("\n\n🛑 Received stop signal. Exiting (please wait for cleanup)...")

        # ── Cleanup ────────────────────────────────────────────────────────
        print("\n   Cleaning up resources...")

        if 'vm_id' in locals():
            try:
                if 'sdk' in locals():
                    class _CleanupTimeout(Exception):
                        pass

                    def _cleanup_timeout_handler(signum, frame):
                        raise _CleanupTimeout()

                    old_alarm_handler = signal.getsignal(signal.SIGALRM)
                    print(f"   Deleting VM {vm_id}...")
                    signal.signal(signal.SIGALRM, _cleanup_timeout_handler)
                    signal.alarm(20)
                    try:
                        sdk.delete(vm_id)
                        print("✓ VM deleted and resources cleaned up.\n")
                    except _CleanupTimeout:
                        print("   ⏰ Cleanup timed out after 20s; resources may remain.")
                    finally:
                        signal.alarm(0)
                        signal.signal(signal.SIGALRM, old_alarm_handler)
            except KeyboardInterrupt:
                print("   Cleanup interrupted by user; resources may remain.")
            except Exception as e:
                print(f"   (Cleanup warning: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
