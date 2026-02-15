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

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence

from smolvm import HostManager, SmolVM


def run_setup(
    *,
    check_only: bool = False,
    install_firecracker: bool = True,
    install_docker: bool = True,
) -> int:
    """Run prerequisite checks and optional dependency installation."""
    print("Checking SmolVM prerequisites...")
    sdk = SmolVM()
    try:
        errors = sdk.check_prerequisites()

        # Auto-install Firecracker if missing
        needs_firecracker = errors and any("firecracker" in e.lower() for e in errors)
        if needs_firecracker and install_firecracker and not check_only:
            print("Firecracker unavailable. Installing...")
            try:
                HostManager().install_firecracker()
                print("Installed Firecracker.")
            except Exception as e:
                print(f"Failed to install Firecracker: {e}")
                return 1

        # Re-check after optional install
        errors = sdk.check_prerequisites()
        if errors:
            print("Missing prerequisites:")
            for e in errors:
                print(f" - {e}")
            return 1

        # Check for Docker
        if shutil.which("docker") is None:
            if check_only or not install_docker:
                print("Docker unavailable.")
            else:
                print("Docker unavailable. Installing...")
                try:
                    # Use official convenience script
                    subprocess.check_call("curl -fsSL https://get.docker.com | sh", shell=True)
                    print("Installed Docker.")
                    print(
                        "NOTE: You may need to add your user to the 'docker' group: "
                        "sudo usermod -aG docker $USER"
                    )
                except subprocess.CalledProcessError as e:
                    print(f"Failed to install Docker: {e}")
                    print("Please install Docker manually: https://docs.docker.com/engine/install/")
                    # Don't fail: some demos (simple/api) don't require Docker.

        print("SmolVM is ready to run!")
        return 0
    finally:
        sdk.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SmolVM setup and prerequisite checks")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only validate prerequisites; do not install missing dependencies.",
    )
    parser.add_argument(
        "--no-install-firecracker",
        action="store_true",
        help="Skip auto-installing Firecracker when missing.",
    )
    parser.add_argument(
        "--no-install-docker",
        action="store_true",
        help="Skip auto-installing Docker when missing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_setup(
        check_only=args.check_only,
        install_firecracker=not args.no_install_firecracker,
        install_docker=not args.no_install_docker,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
