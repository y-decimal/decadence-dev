#!/usr/bin/env python3

"""Compile the modlist through the headless Wabbajack CLI.

Wabbajack ships a console frontend next to the GUI at `<install>\\cli\\wabbajack-cli.exe`.
It exposes a `compile` verb taking two options:

    wabbajack-cli.exe compile -i <installPath> -o <outputPath>

* `-i` accepts either an MO2 root (Wabbajack then infers the settings from it) or a
  `.compiler_settings` file.  This repo tracks the settings file, so passing it makes
  the compile reproducible from git and keeps every field - including
  `UseGamePaths` - exactly as committed.
* `-o` is only honoured when it is an existing *directory*; Wabbajack then writes the
  `.wabbajack` file into it, keeping the name recorded in the settings.  Anything
  else is silently ignored, so this wrapper creates the directory first.

Exit codes returned by the CLI: 0 success, 1 bad arguments, 2 settings inference
failed, 3 the compilation failed.  This wrapper adds 4 for a failed pre-flight check
and 130 when the run was aborted.

This wrapper is set up for one machine and one modlist, so it needs no arguments:

    python tools/compile_modlist.py

`WABBAJACK_CLI_PATH` pins the Wabbajack install and `DEFAULT_SETTINGS_DIR` supplies
the compiler settings; everything else is derived from those. Running it bare
pre-flights the list, compiles it, and streams the result to the console and a log.

The artifact lands in `artifacts/` beside the MO2 instance (`DEFAULT_OUTPUT_DIR`,
changeable at the prompt) - never inside it, where Wabbajack would index the
file it is part-way through writing. The artifact is not committed to git:
every input to a compile is versioned (compiler settings, profiles, download
.meta files), so any tag rebuilds the identical list;
`tools/publish_release.py` attaches a built artifact to a GitHub Release.

It runs pre-flight checks for the failure modes this repo actually hits (see
fix_meta_continuations.py), and translates Wabbajack's exit code into something
readable.

When a compile fails it offers to clear Wabbajack's cached indexes and try once
more. That is worth trying because Wabbajack caches archive *expansions* keyed by
content hash in `%LOCALAPPDATA%\\Wabbajack\\GlobalVFSCache*.sqlite`: one bad
expansion, usually from a download that was aborted part-way, makes every later
compile fail with "No Match in Stack" for a file that is perfectly correct. The
same file re-downloaded hashes identically, so it keeps hitting the poisoned
entry and re-installing the mod cannot clear it. Only the index caches are
removed - never `saved_settings` or the encrypted store, which is what
`wabbajack-cli.exe reset` would wipe.

The log deliberately defaults to `..\\compile-logs` rather than `tools/logs`:
the working tree of this repo IS the MO2 instance, Wabbajack opens every file
it indexes with exclusive access, and a log living inside the compile source
aborts the run with:

    System.IO.IOException: The process cannot access the file
    '...\\Decadence Dev\\tools\\logs\\compile-....log' because it is being used by
    another process.

`pick_log_dir` re-checks that rule against the settings - including through
junctions - and moves the log out of harm's way if it is ever violated.

There are no command-line flags: run it bare and answer the prompts. It asks
for the output and log directories, whether to pre-flight, dry-run, clear the
caches and offer a retry on failure, then does the rest on its own.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tui  # noqa: E402  (lives beside this script)

# The working tree of this repo IS the MO2 instance, so REPO_ROOT is both where
# the compiler settings live and the tree Wabbajack indexes while compiling.
# Anything Wabbajack must not reach while indexing (the compile log, the output
# artifact) therefore lives one level up, beside the instance rather than inside
# it - see _reachable_from for why that matters.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_DIR = REPO_ROOT / "compiler_settings"
DEFAULT_LOG_DIR = REPO_ROOT.parent / "Decadence Releases" / "compile-logs"
# The settings' OutputFile already points beside the instance; defaulting the
# output directory to its parent means a bare run writes exactly where the
# settings say.
DEFAULT_OUTPUT_DIR = REPO_ROOT.parent / "Decadence Releases"

CLI_EXE = "wabbajack-cli.exe"
APP_EXE = "Wabbajack.exe"
SETTINGS_EXT = ".compiler_settings"

# The Wabbajack install on this machine, so a bare run needs no arguments.
# `WABBAJACK_CLI_PATH` is the exact executable and is tried first; the install
# root is also treated as a search root, so when Wabbajack updates and unpacks
# itself into a new `4.2.x.y` folder the newest build is picked up on its own
# rather than the script needing an edit.
WABBAJACK_CLI_PATH = Path(r"D:\Programs\Wabbajack\4.2.3.0\wabbajack-cli.exe")
WABBAJACK_INSTALL = Path(r"D:\Programs\Wabbajack")

EXIT_MEANING = {
    0: "compiled successfully",
    1: "Wabbajack rejected the arguments, or threw an unhandled exception",
    2: "Wabbajack could not infer the compiler settings",
    3: "the compilation failed",
}

# Lines worth repeating in the summary: Wabbajack buries the reason for a failure
# in a hundred-frame stack trace, so pull out the part that actually says it.
HIGHLIGHT_PATTERNS = (
    "Unhandled exception:",
    "Failed compilation",
    "[FATAL]",
    "because it is being used by another process",
)

# Index caches Wabbajack rebuilds on demand. The globs carry Wabbajack's schema
# version in the name, so they keep matching after a Wabbajack update.
#
# Only these are ever touched. `wabbajack-cli.exe reset` is NOT used: it deletes
# the whole %LOCALAPPDATA%\Wabbajack folder, which also holds saved_settings and
# the encrypted store.
CACHE_GLOBS = ("GlobalVFSCache*.sqlite", "GlobalHashCache*.sqlite")
CACHE_DIR_NAME = "Wabbajack"

# Wrapper-only exit codes, deliberately clear of Wabbajack's own 0-3.
EXIT_USAGE = 1
EXIT_PREFLIGHT = 4
EXIT_ABORTED = 130


class PreflightError(Exception):
    """A pre-flight check failed hard enough that compiling would be a waste."""


class ToolNotFound(Exception):
    """The Wabbajack CLI could not be located on this machine."""


@dataclass
class Message:
    level: str  # "error" | "warn" | "info"
    text: str
    hint: str = ""


@dataclass
class Report:
    messages: List[Message] = field(default_factory=list)
    errors: int = 0

    def add(self, level: str, text: str, hint: str = "") -> None:
        self.messages.append(Message(level, text, hint))
        if level == "error":
            self.errors += 1

    def error(self, text: str, hint: str = "") -> None:
        self.add("error", text, hint)

    def warn(self, text: str, hint: str = "") -> None:
        self.add("warn", text, hint)

    def info(self, text: str, hint: str = "") -> None:
        self.add("info", text, hint)

    def emit(self, quiet: bool) -> None:
        for msg in self.messages:
            if msg.level == "info" and quiet:
                continue
            tag = {"error": "ERROR", "warn": "WARN ", "info": "  ok "}[msg.level]
            print(f"[{tag}] {msg.text}")
            if msg.hint:
                print(f"        {msg.hint}")


# --------------------------------------------------------------------------- #
# Locating the CLI
# --------------------------------------------------------------------------- #


def _version_key(path: Path) -> Tuple[int, ...]:
    """Sort key from the dotted version folder a Wabbajack install keeps builds in.

    Wabbajack's launcher unpacks each release into `<install>/<version>/`, so the
    newest build is whichever directory parses as the highest dotted number.
    """
    for part in reversed(path.parts):
        nums = part.split(".")
        if len(nums) >= 2 and all(n.isdigit() for n in nums):
            return tuple(int(n) for n in nums)
    return (0,)


def _candidates_in(root: Path) -> Tuple[List[Path], List[Path]]:
    """Return (cli exes, gui exes) found at or just below `root`.

    Deliberately shallow: the CLI lives either beside `Wabbajack.exe` or in a
    `cli/` subdirectory of it, and any deeper search would be unusable on a
    drive root.
    """
    clis: List[Path] = []
    guis: List[Path] = []
    if not root.is_dir():
        return clis, guis

    lookups = (
        root,
        root / "cli",
    )
    for base in lookups:
        cli = base / CLI_EXE
        if cli.is_file():
            clis.append(cli)
        gui = base / APP_EXE
        if gui.is_file():
            guis.append(gui)

    # Version folders created by the Wabbajack launcher.
    try:
        children = [c for c in root.iterdir() if c.is_dir()]
    except OSError:
        children = []
    for child in children:
        for base in (child, child / "cli"):
            cli = base / CLI_EXE
            if cli.is_file():
                clis.append(cli)
            gui = base / APP_EXE
            if gui.is_file():
                guis.append(gui)

    return clis, guis


def _default_roots(settings: Path) -> List[Path]:
    """Places worth checking without being told, cheapest first."""
    roots: List[Path] = []

    for var in ("WABBAJACK_HOME", "WABBAJACK_DIR"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value).expanduser())

    # The install this repo is developed against, and the parent of the exact
    # executable recorded above - covers Wabbajack having moved or been upgraded.
    roots.append(WABBAJACK_INSTALL)
    roots.append(WABBAJACK_CLI_PATH.parent)

    for drive in ("C:", "D:", "E:"):
        roots.append(Path(f"{drive}/Wabbajack"))

    # `Source` in the settings points at the MO2 instance; Wabbajack itself is
    # often parked next to it.
    try:
        source = Path(load_settings_json(
            settings.read_text(encoding="utf-8-sig")).get("Source", ""))
        for ancestor in list(source.parents)[:3]:
            roots.append(ancestor / "Wabbajack")
    except (OSError, ValueError, AttributeError, TypeError):
        pass

    seen = set()
    unique: List[Path] = []
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def find_cli(settings: Path, report: Report) -> Path:
    """Locate wabbajack-cli.exe, falling back to the GUI exe only if we must.

    Always returns a usable executable or raises `ToolNotFound`.
    """
    env_cli = os.environ.get("WABBAJACK_CLI")

    seeds: List[Path] = []
    if env_cli:
        seeds.append(Path(env_cli).expanduser())
    else:
        # Nothing asked for: start with the hard-wired install.
        seeds.append(WABBAJACK_CLI_PATH)

    for seed in seeds:
        if seed.is_file():
            return seed.resolve()
        if seed.is_dir():
            clis, guis = _candidates_in(seed)
            for found in sorted(clis, key=_version_key, reverse=True):
                return found.resolve()
            for found in sorted(guis, key=_version_key, reverse=True):
                report.warn("Only the GUI executable turned up, not the CLI.",
                            f"Using {found}")
                return found.resolve()

    # The hard-wired executable has moved or this is a different machine, so
    # fall back to looking around before giving up.
    if not env_cli:
        report.warn(f"{WABBAJACK_CLI_PATH} is missing; searching instead.",
                    "Edit WABBAJACK_CLI_PATH at the top of this script to pin the "
                    "new location.")

    roots: List[Path] = []
    roots.extend(_default_roots(settings))

    clis: List[Path] = []
    guis: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found_clis, found_guis = _candidates_in(root)
        clis.extend(found_clis)
        guis.extend(found_guis)

    if clis:
        return sorted(clis, key=_version_key, reverse=True)[0].resolve()

    if guis:
        chosen = sorted(guis, key=_version_key, reverse=True)[0].resolve()
        report.warn("Found Wabbajack.exe but not cli\\wabbajack-cli.exe.",
                    "Falling back to the GUI executable, which also understands the "
                    "compile verb.")
        if _wabbajack_gui_running():
            report.warn("A Wabbajack window is already open.",
                        "It is single-instance: the arguments would be handed to the "
                        "running window and this compile would never start. Close it "
                        "first, or point WABBAJACK_CLI at cli\\wabbajack-cli.exe.")
        return chosen

    places = "\n".join(f"        - {root}" for root in roots) or "        (none)"
    raise ToolNotFound(
        "Could not find wabbajack-cli.exe.  Looked in:\n"
        f"{places}\n"
        f"        Update WABBAJACK_CLI_PATH at the top of this script (currently "
        f"{WABBAJACK_CLI_PATH}), or set the WABBAJACK_CLI / WABBAJACK_HOME "
        "environment variable."
    )


def _wabbajack_gui_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {APP_EXE}", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return APP_EXE.lower() in out.lower()


# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #


def _scan_meta_continuations(directory: Path, suffix: str, recursive: bool,
                             budget: int) -> Tuple[List[Path], int]:
    """Files whose metadata still holds tab-indented continuation lines.

    Wabbajack parses MO2 metadata with IniParser, which rejects those lines
    outright, so this is a hard compile failure rather than a warning.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fix_meta_continuations import fix_text  # type: ignore[import-not-found]
    except ImportError:
        return [], budget

    files = directory.rglob(f"*{suffix}") if recursive else directory.glob(f"*{suffix}")
    bad: List[Path] = []
    for path in files:
        if budget <= 0 or not path.is_file():
            break
        budget -= 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, folded = fix_text(text)
        if folded:
            bad.append(path)
    return bad, budget


def load_settings_json(text: str) -> Any:
    """Parse the JSON dialect Wabbajack writes into `.compiler_settings`.

    Wabbajack's serialiser allows trailing commas and comments, so a settings file
    the GUI reads happily can still trip Python's parser (the committed
    Decadence Dev.compiler_settings has a trailing comma in `NoMatchInclude`).
    Strip both, taking care never to touch anything inside a string literal, then
    hand the result to `json`.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_string = False

    while i < n:
        ch = text[i]

        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue

        if ch == ",":
            # A comma whose next meaningful character closes a container is a
            # trailing comma, which Wabbajack tolerates and json does not.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue

        out.append(ch)
        i += 1

    return json.loads("".join(out))


def _source_from_settings(settings_path: Path) -> Optional[Path]:
    """The MO2 instance the settings point at, or None if the file is unreadable."""
    try:
        settings = load_settings_json(settings_path.read_text(encoding="utf-8-sig"))
        source = settings.get("Source")
        return Path(source) if source else None
    except (OSError, ValueError, AttributeError):
        return None


def _is_link_like(path: Path) -> bool:
    """True for NTFS junctions as well as symlinks."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0)) or path.is_symlink()


def _reachable_from(path: Path, source: Path) -> bool:
    """Whether Wabbajack will walk into `path` while indexing `source`.

    Consulted for both the compile log and the output directory. A plain "is it
    underneath" test misses the junctions and symlinks MO2 setups are often
    full of, so those are consulted too: Wabbajack indexes everything it can
    reach from the compile source, and opens every file it indexes with
    exclusive access:

        System.IO.IOException: The process cannot access the file
        'D:\\CP2077\\Decadence Dev\\tools\\logs\\compile-....log' because it is
        being used by another process.

    For the log, being reachable is enough to abort the compile, so it is fatal.
    """
    try:
        real_path = path.resolve()
        real_source = source.resolve()
    except OSError:
        return False

    if real_path == real_source or real_source in real_path.parents:
        return True

    # Anything the instance links to that contains `path` counts as reachable too.
    # Junctions sit at the instance root and under mods/ in practice, so those two
    # levels are enough to look at without walking a whole modlist.
    candidates: List[Path] = []
    for parent in (real_source, real_source / "mods"):
        try:
            candidates.extend(parent.iterdir())
        except OSError:
            continue

    for candidate in candidates:
        if not _is_link_like(candidate):
            continue
        try:
            target = candidate.resolve()
        except OSError:
            continue
        if real_path == target or target in real_path.parents:
            return True

    return False


def pick_log_dir(requested: Path, source: Optional[Path], report: Report) -> Path:
    """Keep the compile log out of the tree Wabbajack is hashing."""
    if source is None or not _reachable_from(requested, source):
        return requested

    fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WabbajackCompileLogs"
    report.warn(f"Log directory {requested} sits inside the compile source; "
                f"writing logs to {fallback} instead.",
                "Wabbajack opens everything it indexes with exclusive access, so a "
                "log it can reach while still open aborts the compile.")
    return fallback


def preflight(settings_path: Path, output_dir: Optional[Path],
              report: Report) -> Optional[Path]:
    """Validate the settings and the MO2 instance before handing over to Wabbajack.

    Returns the path the `.wabbajack` file is expected to land at, when the
    settings say so.
    """
    if not settings_path.is_file():
        report.error(f"Compiler settings not found: {settings_path}")
        raise PreflightError("nothing to compile")

    if settings_path.suffix != SETTINGS_EXT:
        report.error(f"{settings_path.name} does not end in {SETTINGS_EXT}.",
                     "Wabbajack only reads the file as settings when the extension "
                     "matches; otherwise it treats the path as an MO2 root.")
        raise PreflightError("wrong settings extension")

    try:
        settings = load_settings_json(settings_path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("settings file is not a JSON object")
    except (OSError, ValueError) as exc:
        report.warn(f"Could not parse {settings_path.name} ({exc}).",
                    "Continuing anyway, but the checks below are skipped - Wabbajack's "
                    "parser may still accept the file.")
        return None

    report.info(f"Settings: {settings_path}")

    name = settings.get("ModListName") or settings_path.stem
    version = settings.get("ModlistVersion") or settings.get("Version")
    report.info(f"Modlist: {name} {version or ''}".rstrip())

    source = settings.get("Source")
    if not source:
        report.error("Settings have no \"Source\" (the MO2 instance to compile).")
    else:
        source_path = Path(source)
        if not source_path.is_dir():
            report.error(f"MO2 instance not found: {source_path}")
        else:
            profile = settings.get("Profile")
            modlist_txt = source_path / "profiles" / str(profile) / "modlist.txt"
            if modlist_txt.is_file():
                enabled = sum(
                    1 for line in modlist_txt.read_text(encoding="utf-8-sig",
                                                        errors="replace").splitlines()
                    if line.startswith("+")
                )
                report.info(f"Profile \"{profile}\": {enabled} enabled mod(s)")
            else:
                report.error(f"Profile modlist.txt not found: {modlist_txt}")

    downloads = settings.get("Downloads")
    if downloads:
        downloads_path = Path(downloads)
        if not downloads_path.is_dir():
            report.error(f"Downloads folder not found: {downloads_path}")
        else:
            budget = 40000
            bad, _ = _scan_meta_continuations(downloads_path, ".meta", False, budget)
            if bad:
                report.error(
                    f"{len(bad)} download .meta file(s) still contain indented "
                    "continuation lines.",
                    "Wabbajack's INI parser rejects those and aborts the compile. "
                    "Fix them with fix_meta_continuations.py before compiling.",
                )
            else:
                report.info("Download metadata: clean")

    mods = Path(source) / "mods" if source else None
    if mods and mods.is_dir():
        bad, _ = _scan_meta_continuations(mods, "meta.ini", True, 40000)
        if bad:
            report.error(
                f"{len(bad)} mod meta.ini file(s) contain indented continuation lines.",
                "These are parsed too and will abort the compile. Fix with: "
                "python fix_meta_continuations.py "
                "(run a dry run first - it reports the files it would change).",
            )

    output_file = settings.get("OutputFile")
    resolved = Path(output_file) if output_file else None
    if resolved is None:
        report.warn("Settings have no \"OutputFile\"; the output directory decides "
                    "nothing - Wabbajack would write where it likes.")
    else:
        if output_dir is not None and output_dir != resolved.parent:
            # -o only chooses the directory: Wabbajack keeps the file name the
            # settings record, so the artifact does not get renamed by moving it.
            resolved = output_dir / resolved.name
        report.info(f"Output: {resolved}")

    return resolved


# --------------------------------------------------------------------------- #
# Running the compile
# --------------------------------------------------------------------------- #


def wabbajack_cache_dir() -> Path:
    """`%LOCALAPPDATA%\\Wabbajack`, where Wabbajack keeps its rebuildable indexes."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / CACHE_DIR_NAME


def clear_wabbajack_caches(report_cb=None) -> List[Tuple[Path, int]]:
    """Delete the rebuildable index caches, returning (path, bytes) for each.

    A stale entry here is the usual reason a file keeps reporting "No Match in
    Stack" however many times its mod is re-downloaded: Wabbajack caches archive
    *expansions* keyed by content hash, so one bad expansion of an aborted
    download poisons every later attempt with the same file.
    """
    cache_dir = wabbajack_cache_dir()
    removed: List[Tuple[Path, int]] = []
    if not cache_dir.is_dir():
        return removed

    for pattern in CACHE_GLOBS:
        for db in sorted(cache_dir.glob(pattern)):
            # SQLite keeps -wal and -shm alongside; a stale -wal can resurrect
            # what we just deleted, so they go together.
            for path in [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]:
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError as exc:
                    if report_cb:
                        report_cb(f"could not delete {path.name}: {exc}")
                    continue
                removed.append((path, size))
    return removed


def _human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} GiB"


def _describe_caches(removed: Sequence[Tuple[Path, int]]) -> str:
    total = sum(size for _path, size in removed)
    names = ", ".join(path.name for path, _size in removed)
    return f"{names} ({_human_size(total)})"


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            answer = input(f"{question}{suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def offer_cache_retry(allow_retry: bool) -> bool:
    """Ask whether to drop the caches and compile again."""
    print()
    print("A stale Wabbajack index causes \"No Match in Stack\" for files that are")
    print("correctly downloaded, and re-downloading the mod will not clear it.")
    if not allow_retry:
        print("Retrying was turned off at the start, so this run stops here.")
        return False
    if not sys.stdin.isatty():
        print("Not prompting (stdin is not a terminal). Run again and answer yes "
              "to clearing the caches up front to try this.")
        return False
    if not _prompt_yes_no("Clear Wabbajack's cached indexes and compile again?"):
        return False
    print("The retry will be slower - Wabbajack has to re-read every archive.")
    return True


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def stream(cmd: Sequence[str], cwd: Path, log_path: Path,
           timeout_minutes: Optional[float]) -> Tuple[int, bool, List[str]]:
    """Run `cmd`, echoing every line to the console and to `log_path`.

    Returns (exit code, was aborted, lines worth repeating in the summary).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = timeout_minutes * 60 if timeout_minutes else None

    print()
    print(f"$ {' '.join(cmd)}")
    print(f"  log: {log_path}")
    print()

    lines: "queue.Queue[Optional[str]]" = queue.Queue()
    proc = subprocess.Popen(
        list(cmd), cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    def pump() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    started = time.monotonic()
    timed_out = False
    highlights: List[str] = []
    # Written as it goes, so a kill -9 or a power cut still leaves the log behind.
    with log_path.open("w", encoding="utf-8", newline="") as handle:
        def emit(line: str) -> None:
            stripped = line.strip()
            if stripped and stripped not in highlights \
                    and any(p in stripped for p in HIGHLIGHT_PATTERNS):
                highlights.append(stripped)
            handle.write(line)
            handle.flush()
            try:
                sys.stdout.write(line)
            except UnicodeEncodeError:
                sys.stdout.write(line.encode("ascii", "replace").decode("ascii"))
            sys.stdout.flush()

        try:
            while True:
                if timeout_s and time.monotonic() - started > timeout_s:
                    timed_out = True
                    print(f"\n[timeout] aborting after {timeout_minutes:g} minute(s)")
                    _terminate(proc)
                    break
                try:
                    line = lines.get(timeout=1.0)
                except queue.Empty:
                    if proc.poll() is not None and lines.empty():
                        break
                    continue
                if line is None:
                    break
                emit(line)
        except KeyboardInterrupt:
            print("\n[interrupted] stopping Wabbajack")
            _terminate(proc)
            timed_out = True

        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            code = proc.wait()

        # Anything the reader thread buffered before the pipe closed.
        while not lines.empty():
            line = lines.get()
            if line is not None:
                emit(line)

    return code, timed_out, highlights


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _resolve_settings() -> Path:
    if not DEFAULT_SETTINGS_DIR.is_dir():
        raise PreflightError(f"No compiler_settings directory at {DEFAULT_SETTINGS_DIR}")
    found = sorted(DEFAULT_SETTINGS_DIR.glob(f"*{SETTINGS_EXT}"))
    if not found:
        raise PreflightError(f"No *{SETTINGS_EXT} file in {DEFAULT_SETTINGS_DIR}")
    if len(found) == 1:
        return found[0].resolve()
    chosen = tui.choose("Several settings files found; pick one:",
                        [p.name for p in found])
    return found[chosen].resolve()


def main() -> int:
    tui.header("Compile the modlist")
    report = Report()

    try:
        settings_path = _resolve_settings()
        cli = find_cli(settings_path, report)

        # The compiled list is committed to git, so it lands in the repo by default
        # rather than next to the MO2 instance.
        print()
        output_dir = tui.ask_path(
            "Directory to write the .wabbajack file into", DEFAULT_OUTPUT_DIR,
            hint="Keep it outside the MO2 instance: Wabbajack indexes everything it "
                 "can reach, including last run's artifact.")
        explicit_output = output_dir != DEFAULT_OUTPUT_DIR
        output_dir = output_dir.resolve()

        # Wabbajack opens every file it indexes with exclusive access, and last
        # run's artifact is no exception: an output directory the compile source can
        # reach would be read into the very list that is overwriting it.
        compile_source = _source_from_settings(settings_path)
        if compile_source is not None and _reachable_from(output_dir, compile_source):
            report.error(
                f"Output directory {output_dir} is inside the compile source.",
                "Wabbajack would index the previous artifact, plus the file it is "
                "part-way through writing. Use a directory outside the MO2 instance.")

        if not report.errors and not output_dir.is_dir():
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.error(f"Could not create the output directory {output_dir}: "
                             f"{exc}")
            else:
                if explicit_output:
                    report.warn(
                        f"Created output directory {output_dir}",
                        "Wabbajack silently ignores -o when it is not an existing "
                        "directory, so it has to exist before the run.")
                else:
                    report.info(f"Created output directory {output_dir}")

        output_file: Optional[Path] = None
        if tui.ask_yes_no("Run the pre-flight checks first?", default=True):
            output_file = preflight(settings_path, output_dir, report)
        else:
            report.info("Pre-flight checks skipped")

        # Resolved before the report is printed, so a relocation shows up in it.
        # The log must stay outside the compile source: Wabbajack opens every file
        # it indexes with exclusive access and aborts on the log we hold open.
        log_dir = pick_log_dir(
            tui.ask_path("Directory for the compile log", DEFAULT_LOG_DIR),
            compile_source, report)

        dry_run = tui.ask_yes_no(
            "Dry run (pre-flight and show the command, but do not compile)?",
            default=False)
        clear_cache = False
        allow_retry = False
        timeout: Optional[float] = None
        if dry_run:
            report.info("Cache clearing ignored: a dry run changes nothing")
        else:
            clear_cache = tui.ask_yes_no(
                "Clear Wabbajack's cached indexes before compiling?", default=False)
            allow_retry = tui.ask_yes_no(
                "Offer to clear the caches and retry after a failed compile?",
                default=True)
            timeout = tui.ask_optional_float(
                "Abort the compile after how many minutes (empty = no timeout)")

        if clear_cache:
            cleared = clear_wabbajack_caches(report.warn)
            if cleared:
                report.warn(f"Cleared Wabbajack's cached indexes: "
                            f"{_describe_caches(cleared)}",
                            "This run has to re-read every archive, so it will be "
                            "slower than usual.")
            else:
                report.info("There was nothing to clear")

        report.emit(quiet=False)
        print()
        print(f"CLI: {cli}")

        if report.errors:
            print(f"\n{report.errors} pre-flight error(s); not compiling.")
            return EXIT_PREFLIGHT

        if output_file is not None:
            print(f"Output: {output_file}")

        cmd = [str(cli), "compile", "-i", str(settings_path)]
        if output_dir.is_dir():
            cmd += ["-o", str(output_dir)]

        if dry_run:
            print(f"\n[dry run] {' '.join(cmd)}")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = settings_path.stem.replace(" ", "-")

        before = (output_file.stat().st_mtime
                  if output_file is not None and output_file.is_file() else None)

        wj_log = cli.parent / "logs" / "wabbajack-cli.current.log"
        attempt = 0

        while True:
            attempt += 1
            suffix = "" if attempt == 1 else f"-retry{attempt - 1}"
            log_path = log_dir / f"compile-{stem}-{stamp}{suffix}.log"

            code, timed_out, highlights = stream(cmd, cwd=cli.parent,
                                                 log_path=log_path,
                                                 timeout_minutes=timeout)
            print()

            if timed_out:
                print(f"Aborted (timeout or Ctrl+C). Log: {log_path}")
                return EXIT_ABORTED

            print(f"Wabbajack exited with {code}: "
                  f"{EXIT_MEANING.get(code, 'unknown exit code')}.")

            for line in highlights[:3]:
                print(f"[!] {line}")
            if any("being used by another process" in h for h in highlights):
                print("    Wabbajack indexes every file under the compile source with "
                      "exclusive access, so a file it cannot open aborts the run. A "
                      "compile log left inside the source is the usual cause; this "
                      "script keeps logs elsewhere (see the log directory prompt).")

            print(f"Log: {log_path}")
            if wj_log.is_file():
                print(f"Wabbajack log: {wj_log}")

            if code == 0:
                if output_file is not None and output_file.is_file():
                    size = output_file.stat().st_size
                    fresh = before is None or output_file.stat().st_mtime > before
                    print(f"Output: {output_file} ({size / 1024 ** 2:.1f} MiB)")
                    try:
                        print("        in the repo as "
                              f"{output_file.relative_to(REPO_ROOT).as_posix()}")
                    except ValueError:
                        pass
                    if not fresh:
                        print("[warn] The output file was not modified by this run - "
                              "Wabbajack may have short-circuited.")
                else:
                    print("[warn] Reported success but no .wabbajack file was found "
                          "where the settings expect one.")
                return 0

            if attempt > 1:
                print("\nStill failing after clearing the caches, so a stale index "
                      "is probably not the cause.")
                return code

            if not offer_cache_retry(allow_retry):
                return code

            cleared = clear_wabbajack_caches()
            if not cleared:
                print("\nThere were no caches to clear; not retrying.")
                return code
            print(f"Cleared: {_describe_caches(cleared)}")
            print("\nCompiling again...")

    except ToolNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
