#!/usr/bin/env python3

"""Interactive terminal helpers shared by the tools in this directory.

Every tool in tools/ is driven by prompts instead of command-line flags, and
this module is the collection of widgets they ask their questions with: a
numbered menu chooser, yes/no prompts, path entry that copes with the quoting
Windows produces when a folder is dragged into the terminal, and the little
bits of framing (headers, pauses) that make a prompted session readable.

Nothing here knows about MO2, Wabbajack or Nexus; it is all generic console
plumbing so each tool can stay focused on its own logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Sequence


def _input(prompt: str) -> str:
    """`input` that treats Ctrl+C as "leave" and EOF as an empty answer."""
    try:
        return input(prompt)
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)


def _clean_path(text: str) -> str:
    """Strip the wrapping a drag-and-drop or terminal paste can leave behind.

    Dragging a folder into a Windows terminal typically pastes either a bare
    path or a PowerShell invocation of it. Both arrive wrapped in a paste of
    shell syntax, so peel off a leading `& ` (PowerShell call operator) and
    matching outer quotes, repeatedly, until a plain path is left.
    """
    stripped = text.strip()
    while stripped:
        if stripped[0] == "&":
            stripped = stripped[1:].lstrip()
            continue
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            stripped = stripped[1:-1].strip()
            continue
        break
    return stripped


def ask(prompt: str, default: str = "") -> str:
    """Ask for a free-text answer, falling back to `default` when empty."""
    suffix = f" [{default}]" if default else ""
    answer = _input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_path(prompt: str, default: Optional[Path] = None, hint: str = "") -> Path:
    """Ask for a directory or file, tolerating drag-and-drop quoting.

    An empty answer picks `default`, so the common case is one keypress.
    The question repeats until something usable is entered.
    """
    if hint:
        print(f"  ({hint})")
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = _input(f"{prompt}{suffix}: ").strip()
        cleaned = _clean_path(raw)
        if cleaned:
            return Path(cleaned).expanduser()
        if default is not None:
            return default
        print("  please enter a path")


def ask_csv(prompt: str, default: Sequence[str]) -> List[str]:
    """Ask for a comma- or space-separated list, with a one-keypress default."""
    shown = ", ".join(default)
    raw = _input(f"{prompt} [{shown}]: ").strip()
    if not raw:
        return list(default)
    items = [part for part in raw.replace(",", " ").replace(";", " ").split() if part]
    return items or list(default)


def ask_yes_no(question: str, default: bool = False) -> bool:
    """A yes/no prompt that accepts y/n/Enter, repeating on anything else."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        answer = _input(f"{question}{suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("  please answer y or n.")


def ask_optional_float(question: str) -> Optional[float]:
    """Ask for a number where "none" (Enter) is a valid answer."""
    while True:
        raw = _input(f"{question} [none]: ").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            print("  please enter a number, or press Enter for none.")


def choose(question: str, options: Sequence[str], default_index: int = 0) -> int:
    """Show a numbered menu and return the chosen index (0-based).

    An empty answer picks the default, marked with a `*` in the listing.
    """
    print(question)
    for i, option in enumerate(options, start=1):
        marker = " *" if i - 1 == default_index else ""
        print(f"  {i}){marker} {option}")

    while True:
        raw = _input(f"  1-{len(options)} [default: {default_index + 1}]: ").strip()
        if not raw:
            return default_index
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  please enter a number from 1 to {len(options)}.")


def header(title: str) -> None:
    """Print a titled separator, so each tool announces itself clearly."""
    bar = "=" * (len(title) + 6)
    print()
    print(bar)
    print(f"== {title} ==")
    print(bar)


def pause(message: str = "Press Enter to continue...") -> None:
    """Wait for the user; useful at the end of a run so results stay visible."""
    _input(f"\n{message}")
