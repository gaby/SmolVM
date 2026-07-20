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

"""One-shot installation of shell tab-completion for the smolvm CLI."""

from __future__ import annotations

import os
from pathlib import Path

from smolvm.cli.output import console_stdout, render_error


def _rc_path(shell: str) -> Path:
    """Return the shell startup file completion should be sourced from."""
    if shell == "zsh":
        zdotdir = os.environ.get("ZDOTDIR")
        base = Path(zdotdir).expanduser() if zdotdir else Path.home()
        return base / ".zshrc"
    return Path.home() / ".bashrc"


def _fish_completions_dir() -> Path:
    """Return fish's user completions directory (autoloaded by fish)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return config_home / "fish" / "completions"


def run_completion_install(shell: str, script: str) -> int:
    """Install the completion ``script`` for ``shell`` so new shells load it.

    fish autoloads per-command files from its completions directory, so a
    single file write is enough. bash and zsh have no autoload directory,
    so the script is written under ``~/.smolvm/completions`` and one
    ``source`` line is added to the shell's startup file (idempotently:
    re-running refreshes the script without duplicating the line).
    """
    console = console_stdout()
    try:
        if shell == "fish":
            target = _fish_completions_dir() / "smolvm.fish"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(script)
            console.print(
                f"Tab completion for fish is set up at '{target}'. Open a new shell to use it."
            )
            return 0

        script_path = Path.home() / ".smolvm" / "completions" / f"smolvm.{shell}"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script)

        rc_path = _rc_path(shell)
        rc_content = rc_path.read_text() if rc_path.exists() else ""
        if str(script_path) in rc_content:
            console.print(
                f"Tab completion for {shell} is already set up in '{rc_path}'. "
                "Open a new shell if it isn't active."
            )
            return 0

        source_line = f"source '{script_path}'  # smolvm tab completion"
        with rc_path.open("a") as handle:
            if rc_content and not rc_content.endswith("\n"):
                handle.write("\n")
            handle.write(f"{source_line}\n")
        console.print(
            f"Tab completion for {shell} is set up: added one line to '{rc_path}'. "
            "Open a new shell to use it."
        )
        return 0
    except OSError as exc:
        render_error(
            f"Could not set up tab completion: {exc}. "
            f"Run 'smolvm completion {shell}' and add the printed script "
            "to your shell yourself."
        )
        return 1
