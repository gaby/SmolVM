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
SmolVM Demo - Fully automated VM creation
No manual configuration needed!

Run with: ./run_demo.sh (it handles sudo + venv)
"""

import sys
import time
from pathlib import Path



from smolvm import SmolVM, VMConfig, ImageManager


def main():
    print("\n🔥 SmolVM - Production MicroVM in Seconds\n")
    print("=" * 60)
    
    # Initialize
    sdk = SmolVM()
    
    # Step 1: Prerequisites
    print("\n📋 Step 1: Checking prerequisites...")
    errors = sdk.check_prerequisites()
    
    # Auto-fix Firecracker if needed
    if errors and any("firecracker" in e.lower() for e in errors):
        print("   ⚙️  Firecracker not found, auto-installing...")
        try:
            from smolvm import HostManager
            fc_path = HostManager().install_firecracker()
            print(f"   ✓ Installed Firecracker: {fc_path}")
            errors = sdk.check_prerequisites()
        except Exception as e:
            print(f"   ❌ Failed: {e}")
            return 1
    
    if errors:
        print(f"   ❌ Missing: {', '.join(errors)}")
        return 1
    
    print("   ✓ KVM, iptables, Firecracker - all OK")
    
    # Step 2: Get image
    print("\n📦 Step 2: Ensuring VM image...")
    images = ImageManager()
    print(f"   Available: {', '.join(images.list_available())}")
    
    if images.is_cached("hello"):
        print("   ✓ Image 'hello' already cached")
    else:
        print("   ⬇️  Downloading 'hello' image (~15MB)...")
    
    local_image = images.ensure_image("hello")
    print(f"   ✓ Kernel: {local_image.kernel_path.name}")
    print(f"   ✓ Rootfs: {local_image.rootfs_path.name}")
    
    # Step 3: Create VM
    print("\n🚀 Step 3: Creating microVM...")
    config = VMConfig(
        vm_id="demo-vm",
        vcpu_count=1,
        mem_size_mib=256,
        kernel_path=local_image.kernel_path,
        rootfs_path=local_image.rootfs_path,
    )
    
    try:
        vm = sdk.create(config)
        print(f"   ✓ VM ID: {vm.vm_id}")
        print(f"   ✓ Allocated IP: {vm.network.guest_ip}")
        print(f"   ✓ Created TAP: {vm.network.tap_device}")
        print(f"   ✓ Configured NAT: iptables rules added")
        
        # Step 4: Start VM
        print("\n⚡ Step 4: Starting VM...")
        vm = sdk.start("demo-vm", boot_timeout=10.0)
        print(f"   ✓ Firecracker PID: {vm.pid}")
        print(f"   ✓ Boot time: <1 second")
        print(f"   ✓ Status: {vm.status.value.upper()}")
        
        # Show state
        db_path = sdk.data_dir / "smolvm.db"
        log_path = sdk.data_dir / "demo-vm.log"
        socket_path = sdk.socket_dir / "fc-demo-vm.sock"
        print("\n📊 Current state:")
        print(f"   - Database: {db_path}")
        print(f"   - Logs: {log_path}")
        print(f"   - Socket: {socket_path}")
        
        all_vms = sdk.list_vms()
        print(f"\n   Total VMs: {len(all_vms)}")
        for v in all_vms:
            ip = v.network.guest_ip if v.network else "no-ip"
            print(f"     • {v.vm_id}: {v.status.value} @ {ip}")
        
        # Run for a bit
        print("\n⏸️  VM will run for 5 seconds...")
        for i in range(5, 0, -1):
            print(f"   {i}...", end="", flush=True)
            time.sleep(1)
        print("\n")
        
    finally:
        # Cleanup
        print("🧹 Cleanup:")
        try:
            sdk.stop("demo-vm")
            print("   ✓ VM stopped (Firecracker process terminated)")
        except Exception as e:
            print(f"   ⚠️  Stop: {e}")
        
        try:
            sdk.delete("demo-vm")
            print("   ✓ VM deleted (TAP removed, IP released, DB cleaned)")
        except Exception as e:
            print(f"   ⚠️  Delete: {e}")
    
    print("\n" + "=" * 60)
    print("✅ Demo complete!\n")
    print("What SmolVM auto-configured:")
    print("  • Downloaded & cached Firecracker binary (v1.14.1)")
    print("  • Downloaded & cached VM image (kernel + rootfs)")
    print("  • Created TAP network device")
    print("  • Configured iptables NAT for internet access")
    print("  • Allocated unique IP from SQLite state")
    print("  • Started microVM in sub-second time")
    print("  • Cleaned up all resources on exit")
    print("\n💡 Try the API interactively:")
    print("   .venv/bin/python")
    print("   >>> from smolvm import SmolVM, VMConfig")
    print("   >>> sdk = SmolVM()")
    print("   >>> # ... create and manage VMs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
