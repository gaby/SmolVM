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
SmolVM API Demo - Programmatic VM Management
Shows the full lifecycle API without needing SSH
"""

import sys
import time
from pathlib import Path



from smolvm import VM, VMConfig, ImageManager


def demo_basic_lifecycle():
    """Demo 1: Basic lifecycle with context manager"""
    print("\n" + "=" * 70)
    print("📘 Demo 1: Basic VM Lifecycle (Context Manager)")
    print("=" * 70)
    
    images = ImageManager()
    local_image = images.ensure_image("hello")
    
    config = VMConfig(
        vm_id="api-demo-1",
        vcpu_count=1,
        mem_size_mib=256,
        kernel_path=local_image.kernel_path,
        rootfs_path=local_image.rootfs_path,
    )
    
    print("\n📝 Creating VM with context manager...")
    with VM(config) as vm:
        print(f"   ✓ VM created: {vm.vm_id}")
        print(f"   ✓ IP: {vm.get_ip()}")
        
        print("\n🚀 Starting VM...")
        vm.start()
        print(f"   ✓ VM started")
        
        print("\n⏸️  Running for 3 seconds...")
        time.sleep(3)
        
        print("\n⏹️  Stopping VM...")
        vm.stop()
        print(f"   ✓ VM stopped")
    
    print("\n✅ Context manager auto-cleaned up resources!")


def demo_manual_management():
    """Demo 2: Manual VM management with SmolVM class"""
    print("\n" + "=" * 70)
    print("📘 Demo 2: Manual VM Management")
    print("=" * 70)
    
    from smolvm import SmolVM
    
    sdk = SmolVM()
    images = ImageManager()
    local_image = images.ensure_image("hello")
    
    config = VMConfig(
        vm_id="api-demo-2",
        vcpu_count=1,
        mem_size_mib=256,
        kernel_path=local_image.kernel_path,
        rootfs_path=local_image.rootfs_path,
    )
    
    try:
        print("\n📝 sdk.create(config)...")
        vm_info = sdk.create(config)
        print(f"   ✓ Created: {vm_info.vm_id}")
        print(f"   ✓ Status: {vm_info.status.value}")
        print(f"   ✓ IP: {vm_info.network.guest_ip}")
        print(f"   ✓ TAP: {vm_info.network.tap_device}")
        
        print("\n🚀 sdk.start(vm_id)...")
        vm_info = sdk.start("api-demo-2")
        print(f"   ✓ Started with PID: {vm_info.pid}")
        
        print("\n📊 sdk.get(vm_id)...")
        vm_info = sdk.get("api-demo-2")
        print(f"   ✓ Status: {vm_info.status.value}")
        print(f"   ✓ PID: {vm_info.pid}")
        
        print("\n📋 sdk.list_vms()...")
        vms = sdk.list_vms()
        print(f"   ✓ Total VMs: {len(vms)}")
        for v in vms:
            ip = v.network.guest_ip if v.network else "no-ip"
            print(f"     • {v.vm_id}: {v.status.value} @ {ip}")
        
        print("\n⏸️  Running for 3 seconds...")
        time.sleep(3)
        
    finally:
        print("\n🧹 sdk.stop() and sdk.delete()...")
        try:
            sdk.stop("api-demo-2")
            print("   ✓ Stopped")
        except Exception as e:
            print(f"   ⚠️  Stop: {e}")
        
        try:
            sdk.delete("api-demo-2")
            print("   ✓ Deleted")
        except Exception as e:
            print(f"   ⚠️  Delete: {e}")


def demo_multiple_vms():
    """Demo 3: Managing multiple VMs"""
    print("\n" + "=" * 70)
    print("📘 Demo 3: Multiple VMs")
    print("=" * 70)
    
    from smolvm import SmolVM
    
    sdk = SmolVM()
    images = ImageManager()
    local_image = images.ensure_image("hello")
    
    print("\n🚀 Creating 3 VMs...")
    vm_ids = []
    
    try:
        for i in range(1, 4):
            config = VMConfig(
                vm_id=f"multi-vm-{i}",
                vcpu_count=1,
                mem_size_mib=256,
                kernel_path=local_image.kernel_path,
                rootfs_path=local_image.rootfs_path,
            )
            
            vm = sdk.create(config)
            sdk.start(f"multi-vm-{i}")
            vm_ids.append(f"multi-vm-{i}")
            print(f"   ✓ VM {i}: {vm.network.guest_ip} (TAP: {vm.network.tap_device})")
        
        print("\n📊 All running VMs:")
        vms = sdk.list_vms()
        for v in vms:
            print(f"   • {v.vm_id}: {v.network.guest_ip if v.network else 'no-ip'}")
        
        print("\n⏸️  All VMs running for 3 seconds...")
        time.sleep(3)
        
    finally:
        print("\n🧹 Cleaning up all VMs...")
        for vm_id in vm_ids:
            try:
                sdk.stop(vm_id)
                sdk.delete(vm_id)
                print(f"   ✓ Cleaned: {vm_id}")
            except Exception as e:
                print(f"   ⚠️  {vm_id}: {e}")


def demo_reconnect():
    """Demo 4: Reconnecting to existing VM"""
    print("\n" + "=" * 70)
    print("📘 Demo 4: Reconnect to Existing VM")
    print("=" * 70)
    
    from smolvm import SmolVM
    
    sdk = SmolVM()
    images = ImageManager()
    local_image = images.ensure_image("hello")
    
    config = VMConfig(
        vm_id="reconnect-demo",
        vcpu_count=1,
        mem_size_mib=256,
        kernel_path=local_image.kernel_path,
        rootfs_path=local_image.rootfs_path,
    )
    
    try:
        print("\n📝 Process 1: Creating VM...")
        vm = sdk.create(config)
        sdk.start("reconnect-demo")
        print(f"   ✓ Created and started: {vm.network.guest_ip}")
        
        print("\n🔄 Process 2: Reconnecting to existing VM...")
        vm_reconnected = VM.from_id("reconnect-demo")
        print(f"   ✓ Reconnected to: {vm_reconnected.vm_id}")
        print(f"   ✓ IP: {vm_reconnected.get_ip()}")
        
        print("\n⏸️  VM running for 3 seconds...")
        time.sleep(3)
        
    finally:
        print("\n🧹 Cleanup...")
        try:
            sdk.stop("reconnect-demo")
            sdk.delete("reconnect-demo")
            print("   ✓ Cleaned up")
        except Exception as e:
            print(f"   ⚠️  {e}")


def main():
    print("\n🔥 SmolVM API Demonstrations")
    print("\nShowing 4 common usage patterns:\n")
    
    try:
        demo_basic_lifecycle()
        print("\n⏸️  Moving to next demo in 2 seconds...")
        time.sleep(2)
        
        demo_manual_management()
        print("\n⏸️  Moving to next demo in 2 seconds...")
        time.sleep(2)
        
        demo_multiple_vms()
        print("\n⏸️  Moving to next demo in 2 seconds...")
        time.sleep(2)
        
        demo_reconnect()
        
    except KeyboardInterrupt:
        print("\n\n⏹️  Interrupted by user")
        return 1
    
    print("\n" + "=" * 70)
    print("✅ All demos complete!")
    print("=" * 70)
    print("\n💡 Key takeaways:")
    print("  • Use VM() context manager for auto-cleanup")
    print("  • Use SmolVM() for fine-grained control")
    print("  • VMs are isolated with unique IPs and TAP devices")
    print("  • Use VM.from_id() to reconnect from other processes")
    print("  • All network setup is automatic (TAP + NAT)")
    print("\n📚 Next: Build custom rootfs with SSH for vm.run() support")
    return 0


if __name__ == "__main__":
    sys.exit(main())
