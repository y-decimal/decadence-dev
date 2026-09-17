#!/usr/bin/env python3

"""Attach a compiled modlist to a GitHub Release for the current version.

The compiled `.wabbajack` is deliberately never committed to git: it is a few
hundred KiB of binary that changes on every compile, and it is fully
reproducible - every input to a compile is versioned (compiler settings,
profiles, download .meta files), so checking out any tag and running
compile_modlist.py rebuilds the identical list.

What a release needs is the artifact itself, attached to a tag. This tool does
exactly that:

    python publish_release.py

It reads the modlist version and the expected artifact path from the compiler
settings, creates the tag if it does not exist yet, pushes it, and creates (or
updates) a GitHub Release with the artifact and the `.meta.json` Wabbajack
writes beside it attached.

GitHub Releases need the GitHub CLI (`gh`) authenticated once with:

    gh auth login

Nothing else is required; git pushes use whatever credentials the machine
already has.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tui  # noqa: E402  (lives beside this script)

from compile_modlist import (  # noqa: E402
    SETTINGS_EXT,
    load_settings_json,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_DIR = REPO_ROOT / "compiler_settings"


class PublishError(Exception):
    """A hard stop that the user can act on."""


def find_gh() -> Optional[Path]:
    """Locate gh.exe: PATH first, then the usual standalone-install spots."""
    import shutil

    found = shutil.which("gh")
    if found:
        return Path(found)

    local = os.environ.get("LOCALAPPDATA")
    roots: List[Path] = []
    if local:
        roots.append(Path(local) / "Programs" / "gh")
    roots.append(Path(r"D:\Programs\gh"))

    for root in roots:
        if not root.is_dir():
            continue
        # Standalone zips unpack to gh_<version>_windows_amd64\bin\gh.exe
        matches = sorted(root.rglob("gh.exe"), key=lambda p: p.stat().st_mtime,
                         reverse=True)
        if matches:
            return matches[0]
    return None


def run(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _resolve_settings() -> Path:
    found = sorted(DEFAULT_SETTINGS_DIR.glob(f"*{SETTINGS_EXT}"))
    if not found:
        raise PublishError(f"No *{SETTINGS_EXT} file in {DEFAULT_SETTINGS_DIR}")
    if len(found) == 1:
        return found[0]
    chosen = tui.choose("Several settings files found; pick one:",
                        [p.name for p in found])
    return found[chosen]


def git(args: Sequence[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _origin_repo(cwd: Path) -> str:
    """`owner/name` from the origin remote, for `gh --repo`."""
    url = git(["remote", "get-url", "origin"], cwd)
    repo = url.removesuffix(".git").removesuffix("/")
    if url.startswith("git@github.com:"):
        repo = url.split("github.com:", 1)[1]
    elif "github.com" in url:
        repo = url.split("github.com/", 1)[1]
    else:
        raise PublishError(f"origin is not on GitHub: {url}")
    return repo.removesuffix(".git")


def main() -> int:
    tui.header("Publish a GitHub Release")

    try:
        gh = find_gh()
        if gh is None:
            raise PublishError(
                "gh.exe not found. Install the GitHub CLI (winget install "
                "GitHub.cli) or unzip a release into "
                "%LOCALAPPDATA%\\Programs\\gh\\, then re-run this tool.")

        auth = subprocess.run([str(gh), "auth", "status"], capture_output=True,
                              text=True)
        if auth.returncode != 0:
            raise PublishError(
                "gh is not authenticated. Run `gh auth login` (interactive) "
                "once, then re-run this tool.")

        settings_path = _resolve_settings()
        settings = load_settings_json(settings_path.read_text(
            encoding="utf-8-sig"))

        name = settings.get("ModListName") or settings_path.stem
        version = (settings.get("ModlistVersion")
                   or settings.get("Version") or "")
        version = str(version).removesuffix(".0") or "0.0.1"
        output_file = Path(settings.get("OutputFile", ""))

        artifact = tui.ask_path("The compiled .wabbajack to attach", output_file)
        if not artifact.is_file():
            raise PublishError(f"Artifact not found: {artifact}. Compile first "
                               f"(tools/compile_modlist.py).")
        meta_json = artifact.with_suffix(artifact.suffix + ".meta.json")

        tag = tui.ask("Release tag", version)
        title = tui.ask("Release title", f"{name} {tag}")
        notes = tui.ask("Release notes (one line, optional)", "")

        # The tag must point at a commit that is pushed, or the release points
        # nowhere, so push the current branch and the tag before creating it.
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], REPO_ROOT)
        if branch in ("", "HEAD"):
            raise PublishError("Not on a branch; cannot publish a tag.")
        dirty = git(["status", "--porcelain"], REPO_ROOT)
        if dirty:
            print("\nWorking tree is dirty:")
            print(dirty)
            if not tui.ask_yes_no("Commit before tagging? "
                                  "(no = abort so the release matches a commit)",
                                  default=True):
                raise PublishError("Commit the changes first.")
            message = tui.ask("Commit message", f"Release {tag}")
            git(["add", "-A"], REPO_ROOT)
            git(["commit", "-m", message], REPO_ROOT)

        existing = git(["tag", "-l", tag], REPO_ROOT)
        if not existing:
            if not tui.ask_yes_no(f"Tag {tag} does not exist yet; create it at "
                                  "HEAD?", default=True):
                raise PublishError("No tag - cannot publish.")
            git(["tag", "-a", tag, "-m", f"{name} {tag}"], REPO_ROOT)

        print(f"\npushing origin {branch} {tag} ...")
        git(["push", "origin", branch], REPO_ROOT)
        git(["push", "origin", tag], REPO_ROOT)

        attachments = [str(artifact)]
        if meta_json.is_file():
            attachments.append(str(meta_json))
        else:
            print(f"note: no metadata file beside the artifact ({meta_json.name}); "
                  "uploading the artifact only.")

        repo = _origin_repo(REPO_ROOT)
        view = subprocess.run([str(gh), "release", "view", tag, "--repo", repo],
                              capture_output=True, text=True)
        if view.returncode == 0:
            if not tui.ask_yes_no("Release exists already; upload the artifact "
                                  "again (replacing it)?", default=False):
                raise PublishError("Left the existing release untouched.")
            result = subprocess.run([str(gh), "release", "upload", tag,
                                     "--clobber", *attachments,
                                     "--repo", repo],
                                    capture_output=True, text=True)
        else:
            args = [str(gh), "release", "create", tag, *attachments,
                    "--repo", repo,
                    "--title", title]
            if notes:
                args += ["--notes", notes]
            else:
                # Non-interactive gh cannot open an editor for notes.
                args += ["--generate-notes"]
            result = subprocess.run(args, capture_output=True, text=True)

        if result.returncode != 0:
            raise PublishError(f"gh release failed:\n{(result.stderr or '').strip()}")

        url = (result.stdout or "").strip()
        print("\nRelease published.")
        if url:
            print(f"  {url}")
        return 0

    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
