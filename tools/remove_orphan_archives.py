#!/usr/bin/env python3
"""
remove_orphan_archives.py

Scan a Mod Organizer 2 downloads folder for archive files that do not
have a corresponding meta file (e.g. "mod.zip.meta"). Optionally delete
those orphaned archives.

There are no command-line flags: the script asks for the downloads folder
(which can be dragged into the terminal), the archive extensions to consider,
and whether to actually delete. Nothing is removed without a yes.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tui  # noqa: E402  (lives beside this script)


DEFAULT_ARCH_EXTS = [".zip", ".7z", ".rar", ".7z.001"]


def find_archives(folder: Path, extensions: Iterable[str], recursive: bool) -> List[Path]:
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    if recursive:
        candidates = folder.rglob("*")
    else:
        candidates = folder.iterdir()

    archives: List[Path] = []
    for p in candidates:
        if not p.is_file():
            continue
        name = p.name.lower()
        for ext in exts:
            if name.endswith(ext):
                archives.append(p)
                break
    return archives


def meta_for_archive(archive: Path, meta_ext: str) -> Path:
    # MO2 writes meta files by appending the meta extension after the archive name,
    # e.g. "modname.zip.meta" — so the meta file path is the archive path string + meta_ext
    if not meta_ext.startswith("."):
        meta_ext = "." + meta_ext
    return Path(str(archive) + meta_ext)


def remove_orphans(folder: Path, extensions: Iterable[str], meta_ext: str, recursive: bool, execute: bool) -> int:
    archives = find_archives(folder, extensions, recursive)
    logging.info("Found %d candidate archives in %s", len(archives), folder)

    orphans: List[Path] = []
    for a in archives:
        m = meta_for_archive(a, meta_ext)
        if not m.exists():
            orphans.append(a)

    if not orphans:
        logging.info("No orphaned archives found.")
        return 0

    for o in orphans:
        if execute:
            try:
                o.unlink()
                logging.info("Deleted: %s", o)
            except Exception as e:
                logging.error("Failed to delete %s: %s", o, e)
        else:
            logging.info("Orphan (dry-run): %s", o)

    logging.info("Total orphans: %d (%s)", len(orphans), "deleted" if execute else "dry-run")
    return len(orphans)


def main() -> int:
    tui.header("Remove orphaned MO2 archives")
    folder = tui.ask_path(
        "Downloads folder to scan", Path.cwd(),
        hint="Drag the folder into the terminal, or press Enter for the default.")

    extensions = tui.ask_csv(
        "Archive extensions to consider", DEFAULT_ARCH_EXTS)
    meta_ext = tui.ask("Meta extension appended to archive names", ".meta")
    if not meta_ext.startswith("."):
        meta_ext = "." + meta_ext
    recursive = tui.ask_yes_no("Scan subdirectories recursively?", default=False)
    execute = tui.ask_yes_no(
        "Delete the orphans (answering no performs a dry run)?", default=False)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not folder.exists() or not folder.is_dir():
        logging.error("Path does not exist or is not a directory: %s", folder)
        return 2

    remove_orphans(folder, extensions, meta_ext, recursive, execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
