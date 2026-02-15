# GEMINI.md - SmolVM Context

SmolVM is a secure, lightweight runtime for AI agents and tools, built on top of [Firecracker microVMs](https://firecracker-microvm.github.io/). It provides a high-level Python SDK and CLI to manage microVM lifecycles, networking, and command execution.

## 🚀 Project Overview

- **Purpose:** Securely isolate untrusted code (like AI agent tools) using microVMs.
- **Technology Stack:**
    - **Language:** Python >= 3.10
    - **Orchestrator:** Firecracker
    - **Build System:** Hatchling
    - **Dependencies:** Pydantic (data validation), Requests (API/Unix socket interaction), SSH (for command execution).
- **Architecture:**
    - **`smolvm.facade.VM`**: The user-facing class for easy VM interaction.
    - **`smolvm.vm.SmolVM`**: The core SDK orchestrator managing state, networking, and Firecracker processes.
    - **`smolvm.api`**: Low-level Firecracker API client.
    - **`smolvm.network`**: Manages TAP devices, NAT rules, and port forwarding.
    - **`smolvm.storage`**: SQLite-backed state management.
    - **`smolvm.host`**: Environment validation and Firecracker binary management.

## 🛠️ Building and Running

### Prerequisites
- **OS:** Linux (KVM support required).
- **System Setup:** Run the provided setup script to install dependencies and configure permissions.
  ```bash
  sudo ./scripts/system-setup.sh --configure-runtime
  ```

### Installation
```bash
pip install smolvm
```

### CLI Usage
The `smolvm` command provides demos and utility functions:
- `smolvm demo list`: List available demos.
- `smolvm demo simple`: Run a basic VM lifecycle demo.
- `smolvm cleanup --all`: Clean up all active and stale VMs.

### Quickstart Example
```python
from smolvm import VM

with VM() as vm:
    print(f"VM IP: {vm.get_ip()}")
    result = vm.run("echo 'Hello from SmolVM'")
    print(result.stdout.strip())
```

## 🧪 Development

### Key Commands
- **Testing:** `pytest` (runs the suite in `tests/`)
- **Linting & Formatting:** `ruff check .` or `ruff format .`
- **Type Checking:** `mypy src`
- **Build:** `hatch build`

### Design Conventions
- **Strict Typing:** All new code should be type-annotated and pass `mypy --strict`.
- **Async-ready but Sync-first:** The current SDK is synchronous for simplicity but designed with clear separation of concerns to allow future async support.
- **State Persistence:** All VM state is stored in `~/.local/state/smolvm/smolvm.db` by default.
- **Networking:** Each VM gets a dedicated TAP device and a private IP in the `172.16.0.0/16` range (default).

## 📂 Project Structure
- `src/smolvm/`: Main source code.
    - `facade.py`: High-level `VM` class.
    - `vm.py`: Core `SmolVM` orchestrator.
    - `build.py`: Image building logic (rootfs/kernel).
    - `network.py`: Linux networking setup (iptables/iproute2).
- `scripts/`: System-level setup and configuration.
- `tests/`: Comprehensive test suite covering all modules.
- `examples/`: Reference implementations and advanced usage.
