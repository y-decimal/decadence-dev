#!/usr/bin/env python3

"""Re-query Nexus metadata for the downloads folder of this modlist.

This tool is deliberately specific to one modlist. It is hard-wired to the
`downloads` folder beside the repo root (the MO2 instance) and to the
`cyberpunk2077` Nexus game domain, so there is nothing to pass on the command
line:

    python requery_invalid_meta.py

That repairs every download whose Nexus metadata is incomplete - the
`modID == 0 or fileID == 0` case MO2's downloads list flags as "invalid" - by
hashing the archive, asking the Nexus MD5 endpoint, picking the best candidate
using MO2's own rules, and writing the result back into the `.meta` file.

Entries that were downloaded straight from a URL carry `directURL` and have no
`modID`/`fileID`. Those were never Nexus downloads, so there is nothing to
re-query; they are left untouched and listed at the end.

Credentials come from `NEXUS_API_KEY`, or failing that MO2's Windows credential
store, so normally nothing needs configuring.

There are no command-line flags: the tool asks whether to limit the run to
entries with incomplete metadata and whether to write, then goes to work.

Examples:
    python requery_invalid_meta.py
"""

from __future__ import annotations

import configparser
import ctypes
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

if os.name == "nt":
    from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tui  # noqa: E402  (lives beside this script)


# --------------------------------------------------------------------------- #
# Hard-wired configuration. The working tree of this repo IS the MO2 instance,
# so the downloads folder is beside the tools that service it. These two values
# are the whole "configuration".
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = REPO_ROOT / "downloads"

# Nexus game domain, used for the MD5 search and as the fallback for entries
# whose own `gameName` key is missing.
GAME_DOMAIN = "cyberpunk2077"


API_BASE = "https://api.nexusmods.com/v1"
USER_AGENT = "DecadenceDev-MetaRequery/1.0"
MO2_CREDENTIAL_PREFIX = "ModOrganizer2_"
MO2_LEGACY_APIKEY = MO2_CREDENTIAL_PREFIX + "APIKEY"
MO2_OAUTH_TOKENS = MO2_CREDENTIAL_PREFIX + "NEXUS_OAUTH_TOKENS"

# Matches MO2's Nexus file status values used in MD5 result disambiguation.
FILE_STATUS_REMOVED = 6
FILE_STATUS_ARCHIVED = 7


@dataclass
class MetaCandidate:
    file_path: Path
    meta_path: Path
    cfg: configparser.ConfigParser


@dataclass
class QueryResult:
    updated: bool
    reason: str


@dataclass
class AuthMaterial:
    api_key: str = ""
    access_token: str = ""
    source: str = ""


# --------------------------------------------------------------------------- #
# Nexus credentials
# --------------------------------------------------------------------------- #


def _read_windows_credential(target_name: str) -> str:
    if os.name != "nt":
        return ""

    class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.c_void_p),
        ]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    cred_ptr = ctypes.POINTER(CREDENTIALW)()
    cred_read = ctypes.windll.advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    cred_read.restype = wintypes.BOOL

    if not cred_read(target_name, 1, 0, ctypes.byref(cred_ptr)):
        return ""

    try:
        cred = cred_ptr.contents
        if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
            return ""
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le", errors="ignore")
    finally:
        ctypes.windll.advapi32.CredFree(cred_ptr)


def _parse_oauth_tokens(raw: str) -> AuthMaterial:
    if not raw:
        return AuthMaterial()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return AuthMaterial()

    access_token = str(payload.get("access_token", "") or "")
    expires_at = str(payload.get("expires_at", "") or "")
    if access_token and expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                access_token = ""
        except ValueError:
            pass

    return AuthMaterial(access_token=access_token, source=MO2_OAUTH_TOKENS)


def _resolve_auth_material() -> AuthMaterial:
    """Env var first, then the two credential entries MO2 itself uses."""
    env_api_key = os.environ.get("NEXUS_API_KEY", "").strip()
    if env_api_key:
        return AuthMaterial(api_key=env_api_key, source="NEXUS_API_KEY")

    mo2_api_key = _read_windows_credential(MO2_LEGACY_APIKEY).strip()
    if mo2_api_key:
        return AuthMaterial(api_key=mo2_api_key, source=MO2_LEGACY_APIKEY)

    oauth = _parse_oauth_tokens(_read_windows_credential(MO2_OAUTH_TOKENS))
    if oauth.access_token:
        return oauth

    return AuthMaterial()


# --------------------------------------------------------------------------- #
# Reading .meta files
# --------------------------------------------------------------------------- #


def _read_meta(meta_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    # Keep MO2's exact key casing ("modID", "fileID", "gameName").
    cfg.optionxform = str  # type: ignore[assignment]
    with meta_path.open("r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read()

    content = content.lstrip("\ufeff").strip()
    if not content:
        cfg["General"] = {}
        return cfg

    # QSettings INI typically writes [General]. If absent, wrap to keep parser robust.
    if "[" not in content:
        content = "[General]\n" + content

    try:
        cfg.read_string(content)
    except configparser.Error as e:
        raise ValueError(f"failed to parse {meta_path.name}: {e}") from e

    if "General" not in cfg:
        cfg["General"] = {}
    return cfg


def _meta_get(cfg: configparser.ConfigParser, key: str, default: str = "") -> str:
    return cfg["General"].get(key, default)


def _meta_get_int(cfg: configparser.ConfigParser, key: str, default: int = 0) -> int:
    raw = _meta_get(cfg, key, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _find_candidates(
    downloads_dir: Path, all_files: bool
) -> Tuple[List[MetaCandidate], List[Path]]:
    """Return (entries worth re-querying, entries skipped as direct downloads)."""
    candidates: List[MetaCandidate] = []
    direct_downloads: List[Path] = []

    for meta_path in sorted(downloads_dir.glob("*.meta")):
        file_path = meta_path.with_suffix("")
        if not file_path.is_file():
            continue

        try:
            cfg = _read_meta(meta_path)
        except ValueError as e:
            print(f"skip {meta_path.name}: {e}")
            continue

        if _meta_get(cfg, "repository", "Nexus") != "Nexus":
            continue

        mod_id = _meta_get_int(cfg, "modID", 0)
        file_id = _meta_get_int(cfg, "fileID", 0)

        # A download that was added by URL is not a Nexus download. With no ids
        # to fill in, hashing it against the Nexus MD5 endpoint is meaningless,
        # so leave it alone rather than reporting a failure that can't be fixed.
        if not mod_id and not file_id and _meta_get(cfg, "directURL", "").strip():
            direct_downloads.append(file_path)
            continue

        # MO2's incomplete criterion, as used by isInfoIncomplete().
        if not all_files and mod_id != 0 and file_id != 0:
            continue

        candidates.append(
            MetaCandidate(file_path=file_path, meta_path=meta_path, cfg=cfg)
        )

    return candidates, direct_downloads


# --------------------------------------------------------------------------- #
# Nexus lookup
# --------------------------------------------------------------------------- #


def _md5_of_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _api_get_json(url: str, auth: AuthMaterial, timeout: float = 30.0
                  ) -> Tuple[object, Dict[str, str]]:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("Application-Name", "MO2")
    req.add_header("Application-Version", "2.5.2")
    req.add_header("User-Agent", USER_AGENT)
    if auth.api_key:
        req.add_header("APIKEY", auth.api_key)
    elif auth.access_token:
        req.add_header("Authorization", f"Bearer {auth.access_token}")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return json.loads(raw.decode("utf-8")), headers


def _normalize_games(meta_game: str) -> List[str]:
    """The meta's own gameName if it has one, then the hard-wired domain.

    Falling back to GAME_DOMAIN is what lets entries with no `gameName` key at
    all - the ones that previously failed with "no game candidates" - be
    queried like any other.
    """
    games: List[str] = []
    if meta_game.strip():
        games.append(meta_game.strip())
    if GAME_DOMAIN.lower() not in {g.lower() for g in games}:
        games.append(GAME_DOMAIN)
    return games


def _select_md5_match(results: List[dict], local_file_name: str) -> Optional[dict]:
    if not results:
        return None

    chosen_idx = 0 if len(results) == 1 else -1

    if chosen_idx < 0:
        for i, item in enumerate(results):
            file_details = (item or {}).get("file_details", {}) or {}
            api_name = str(file_details.get("file_name", ""))
            if api_name.lower() == local_file_name.lower():
                if chosen_idx < 0:
                    chosen_idx = i
                else:
                    chosen_idx = -1
                    break

    if chosen_idx < 0:
        for i, item in enumerate(results):
            file_details = (item or {}).get("file_details", {}) or {}
            status = int(file_details.get("category_id", 0) or 0)
            if status not in (FILE_STATUS_REMOVED, FILE_STATUS_ARCHIVED):
                if chosen_idx < 0:
                    chosen_idx = i
                else:
                    chosen_idx = -1
                    break

    if chosen_idx < 0:
        return None
    return results[chosen_idx]


# --------------------------------------------------------------------------- #
# Writing .meta files
# --------------------------------------------------------------------------- #


def _flatten_value(value: str) -> str:
    """Collapse real newlines into MO2's literal \\n escape.

    Wabbajack parses .meta files with IniParser 2.5.3, which aborts on the
    tab-indented continuation lines that configparser emits for multi-line
    values. Nexus file descriptions are full of newlines, so every value must
    be stored on a single line.
    """
    return value.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _set_if_present(section: configparser.SectionProxy, key: str, value: object) -> None:
    if value is None:
        return
    section[key] = _flatten_value(str(value))


def _apply_match_to_meta(cfg: configparser.ConfigParser, game_name: str,
                         selected: dict) -> None:
    section = cfg["General"]
    file_details = (selected or {}).get("file_details", {}) or {}
    mod_details = (selected or {}).get("mod", {}) or {}

    version = str(file_details.get("version", "") or "")
    if not version:
        version = str(file_details.get("mod_version", "") or "")

    _set_if_present(section, "name", file_details.get("name", ""))
    _set_if_present(section, "fileID", int(file_details.get("file_id", 0) or 0))
    _set_if_present(section, "description", file_details.get("description", ""))
    _set_if_present(section, "version", version)
    _set_if_present(section, "fileCategory", int(file_details.get("category_id", 0) or 0))

    _set_if_present(section, "modID", int(mod_details.get("mod_id", 0) or 0))
    _set_if_present(section, "modName", mod_details.get("name", ""))
    _set_if_present(section, "category", int(mod_details.get("category_id", 0) or 0))
    _set_if_present(section, "author", mod_details.get("author", ""))
    _set_if_present(section, "uploader", mod_details.get("uploaded_by", ""))
    _set_if_present(section, "uploaderUrl",
                    mod_details.get("uploaded_users_profile_url", ""))

    section["gameName"] = game_name
    section["repository"] = "Nexus"


def _write_meta(meta_path: Path, cfg: configparser.ConfigParser) -> None:
    # Defensive pass: never let a value span multiple lines, because
    # configparser serialises those as indented continuation lines.
    for section in cfg.sections():
        for key, value in cfg.items(section, raw=True):
            flattened = _flatten_value(value)
            if flattened != value:
                cfg.set(section, key, flattened)

    with meta_path.open("w", encoding="utf-8") as f:
        cfg.write(f, space_around_delimiters=False)


def _query_one(c: MetaCandidate, auth: AuthMaterial, dry_run: bool) -> QueryResult:
    local_name = c.file_path.name
    md5 = _md5_of_file(c.file_path)
    games = _normalize_games(_meta_get(c.cfg, "gameName", ""))

    last_error = "no successful response"
    for game in games:
        encoded_game = urllib.parse.quote(game, safe="")
        url = f"{API_BASE}/games/{encoded_game}/mods/md5_search/{md5}"
        try:
            payload, _headers = _api_get_json(url, auth)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                last_error = f"{game}: no match (404)"
                continue
            try:
                msg = e.read().decode("utf-8", errors="ignore")
            except OSError:
                msg = str(e)
            return QueryResult(False, f"{game}: HTTP {e.code} {msg}")
        except Exception as e:  # network failures
            return QueryResult(False, f"{game}: {e}")

        if not isinstance(payload, list) or not payload:
            last_error = f"{game}: no match"
            continue

        selected = _select_md5_match(payload, local_name)
        if selected is None:
            last_error = f"{game}: ambiguous, {len(payload)} candidates"
            continue

        if dry_run:
            return QueryResult(True, f"{game}: would update")

        _apply_match_to_meta(c.cfg, game, selected)
        _write_meta(c.meta_path, c.cfg)
        return QueryResult(True, f"{game}: updated")

    return QueryResult(False, last_error)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _report_direct_downloads(direct_downloads: Sequence[Path]) -> None:
    if not direct_downloads:
        return
    print(f"\nLeft alone - direct downloads, not Nexus entries "
          f"({len(direct_downloads)}):")
    for path in direct_downloads:
        print(f"  {path.name}")


def main() -> int:
    tui.header("Re-query Nexus metadata")
    downloads_dir = DOWNLOADS_DIR

    if not downloads_dir.is_dir():
        print(f"error: downloads directory not found: {downloads_dir}", file=sys.stderr)
        return 2

    auth = _resolve_auth_material()
    if not (auth.api_key or auth.access_token):
        print("error: no Nexus credentials found. Set NEXUS_API_KEY, or log in "
              "once through MO2 so it stores them.", file=sys.stderr)
        return 2

    all_files = tui.choose(
        "Which entries should be re-queried?",
        ("Only those with incomplete metadata (modID/fileID zero)",
         "Every Nexus entry - slower, refreshes good metadata too")) != 0
    dry_run = tui.ask_yes_no(
        "Dry run (show planned updates, write nothing)?", default=True)

    candidates, direct_downloads = _find_candidates(downloads_dir, all_files)

    print(f"Downloads: {downloads_dir}")
    print(f"Game domain: {GAME_DOMAIN}")
    print(f"Auth: {auth.source or 'unknown'}")
    print(f"Mode: {'every Nexus entry' if all_files else 'incomplete metadata only'}")
    print(f"Entries to re-query: {len(candidates)}")

    if not candidates:
        print("\nNothing to re-query.")
        _report_direct_downloads(direct_downloads)
        return 0

    updated = 0
    failures: List[Tuple[Path, str]] = []

    for idx, c in enumerate(candidates, start=1):
        print(f"[{idx}/{len(candidates)}] {c.file_path.name}")
        try:
            result = _query_one(c, auth=auth, dry_run=dry_run)
        except Exception as e:
            failures.append((c.file_path, str(e)))
            print(f"  fail: {e}")
            continue

        if result.updated:
            updated += 1
            print(f"  ok: {result.reason}")
        else:
            failures.append((c.file_path, result.reason))
            print(f"  skip: {result.reason}")

    print("\nSummary")
    print(f"  updated: {updated}")
    print(f"  not resolved: {len(failures)}")
    print(f"  dry-run: {'yes' if dry_run else 'no'}")

    if failures:
        print("\nNot resolved")
        for file_path, reason in failures:
            print(f"  {file_path.name} - {reason}")

    _report_direct_downloads(direct_downloads)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
