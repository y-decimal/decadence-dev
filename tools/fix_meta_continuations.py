#!/usr/bin/env python3

"""Fold multi-line INI values in MO2 download .meta files back onto one line.

Wabbajack's compiler reads every download .meta file with IniParser 2.5.3,
which rejects the tab-indented continuation lines that Python's configparser
writes whenever a value contains real newlines.  The result is a hard failure:

    Failed compilation: Unknown file format. Couldn't parse the line:
    '<br />Auto refit.'. while parsing line number 9 ...

This tool rewrites those continuations into the literal two-character escape
\\n, which is the convention MO2 itself uses for multi-line values (see the
nexusDescription values in mods/*/meta.ini).  Everything else is left alone.

A timestamped .bak is written next to each changed file the first time it is
modified, so the operation is reversible.

There are no command-line flags: the tool asks for the downloads folder and
whether to write, then reports what it did.

Examples:
    python fix_meta_continuations.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tui  # noqa: E402  (lives beside this script)

SECTION_CHARS = ("[", ";", "#")

# The working tree of this repo IS the MO2 instance, so downloads/ sits beside
# this script's repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOADS = REPO_ROOT / "downloads"


@dataclass
class FixResult:
    path: Path
    folded_lines: int
    text: str
    encoding: str


def _read_text(path: Path) -> Tuple[str, str]:
    """Return (text, encoding). utf-8-sig keeps/exposes any BOM."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def _is_key_line(content: str) -> bool:
    stripped = content.strip()
    if not stripped or stripped.startswith(SECTION_CHARS):
        return False
    return "=" in content


def _is_continuation(content: str) -> bool:
    """An indented line continues the value of the preceding key line.

    Any Unicode whitespace counts: configparser indents with \\t, but hand
    edited files often use spaces, and copying descriptions around can leave
    non-breaking spaces behind.
    """
    return content[:1].isspace()


def fix_text(text: str) -> Tuple[str, int]:
    """Fold indented continuation lines into the preceding key=value line."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    anchor = None  # index in `out` of the key line we may extend
    folded = 0

    for raw in lines:
        content = raw.rstrip("\r\n")
        ending = raw[len(content):] or newline

        if content.strip() == "":
            # A whitespace-only line is still part of the value: configparser
            # serialises a "\n" inside a value as "\n\t", so a blank line in a
            # description arrives here as a lone tab. Keep the anchor so the
            # lines after it are still folded. Only a truly empty line ends
            # the value.
            if content == "":
                anchor = None
            out.append(raw)
            continue

        if anchor is not None and _is_continuation(content):
            out[anchor] = out[anchor].rstrip("\r\n") + "\\n" + content.strip() + ending
            folded += 1
            continue

        out.append(raw)
        anchor = len(out) - 1 if _is_key_line(content) else None

    result = "".join(out)
    if result and newline == "\n" and "\r\n" in result:
        result = result.replace("\r\n", "\n")
    return result, folded


def scan(downloads_dir: Path, apply: bool, verbose: bool) -> Tuple[int, int, List[FixResult]]:
    scanned = 0
    changed: List[FixResult] = []
    total_folded = 0

    for meta in sorted(downloads_dir.glob("*.meta")):
        if not meta.is_file():
            continue
        scanned += 1
        text, encoding = _read_text(meta)
        fixed, folded = fix_text(text)
        if folded == 0:
            continue

        total_folded += folded
        changed.append(FixResult(meta, folded, fixed, encoding))

        if apply:
            backup = meta.with_name(meta.name + ".bak-multiline")
            if not backup.exists():
                backup.write_bytes(meta.read_bytes())
            meta.write_bytes(fixed.encode(encoding))

        if verbose:
            print(f"{'FIXED' if apply else 'WOULD FIX'}: {meta.name} "
                  f"({folded} continuation line{'s' if folded != 1 else ''})")

    return scanned, total_folded, changed


def main() -> int:
    tui.header("Fold multi-line .meta values onto one line")
    downloads_dir = tui.ask_path(
        "Downloads directory to scan", DEFAULT_DOWNLOADS,
        hint="Scanned for *.meta directly inside it, not recursively.")
    if not downloads_dir.is_dir():
        print(f"error: not a directory: {downloads_dir}", file=sys.stderr)
        return 2

    apply = tui.ask_yes_no(
        "Write the fixed files (a .bak-multiline copy is kept)?", default=False)
    verbose = tui.ask_yes_no("List every file that needs repair?", default=True)

    scanned, folded, changed = scan(downloads_dir, apply, verbose=verbose)

    mode = "applied" if apply else "dry run"
    print(f"\n[{mode}] scanned {scanned} .meta file(s); "
          f"{len(changed)} file(s) needed repair; folded {folded} continuation line(s).")
    if changed and not apply:
        print("Run it again and answer yes to write the files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
