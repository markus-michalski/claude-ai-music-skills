"""Album status, track creation, promo, version, ideas, and rename tools."""

import importlib.metadata
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from handlers._shared import (
    _normalize_slug, _safe_json, _find_album_or_error,
    _extract_markdown_section, _extract_code_block,
    _derive_title_from_slug, _find_wav_source_dir,
    TRACK_NOT_STARTED, TRACK_SOURCES_PENDING,
    TRACK_SOURCES_VERIFIED, TRACK_IN_PROGRESS,
    TRACK_GENERATED, TRACK_FINAL,
    ALBUM_CONCEPT, ALBUM_RESEARCH_COMPLETE, ALBUM_SOURCES_VERIFIED,
    ALBUM_IN_PROGRESS, ALBUM_COMPLETE, ALBUM_RELEASED,
    ALBUM_VALID_STATUSES, TRACK_COMPLETED_STATUSES,
    STATUS_UNKNOWN,
    _VALID_GENRES, _GENRE_ALIASES,
)
from handlers import _shared

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status transition rules — single source of truth for validation logic.
# Imported by core.py (lazy) for update_track_field.
# ---------------------------------------------------------------------------

# Valid track statuses (lowercase set for input validation)
_VALID_TRACK_STATUSES = {
    TRACK_NOT_STARTED.lower(), TRACK_SOURCES_PENDING.lower(),
    TRACK_SOURCES_VERIFIED.lower(), TRACK_IN_PROGRESS.lower(),
    TRACK_GENERATED.lower(), TRACK_FINAL.lower(),
}

# Valid album statuses (lowercase set for input validation)
_VALID_ALBUM_STATUSES = {s.lower() for s in ALBUM_VALID_STATUSES}

# Not Started → In Progress is allowed (non-documentary albums skip sources)
_VALID_TRACK_TRANSITIONS = {
    TRACK_NOT_STARTED: {TRACK_SOURCES_PENDING, TRACK_IN_PROGRESS},
    TRACK_SOURCES_PENDING: {TRACK_SOURCES_VERIFIED},
    TRACK_SOURCES_VERIFIED: {TRACK_IN_PROGRESS},
    TRACK_IN_PROGRESS: {TRACK_GENERATED},
    TRACK_GENERATED: {TRACK_FINAL},
    TRACK_FINAL: set(),  # terminal
}

# Concept → In Progress is allowed (non-documentary albums skip sources)
_VALID_ALBUM_TRANSITIONS = {
    ALBUM_CONCEPT: {ALBUM_RESEARCH_COMPLETE, ALBUM_IN_PROGRESS},
    ALBUM_RESEARCH_COMPLETE: {ALBUM_SOURCES_VERIFIED},
    ALBUM_SOURCES_VERIFIED: {ALBUM_IN_PROGRESS},
    ALBUM_IN_PROGRESS: {ALBUM_COMPLETE},
    ALBUM_COMPLETE: {ALBUM_RELEASED},
    ALBUM_RELEASED: set(),  # terminal
}

# Canonical status lookup for case-insensitive matching
_CANONICAL_TRACK_STATUS = {s.lower(): s for s in _VALID_TRACK_TRANSITIONS}
_CANONICAL_ALBUM_STATUS = {s.lower(): s for s in _VALID_ALBUM_TRANSITIONS}

# Status level mappings for album/track consistency checks
_TRACK_STATUS_LEVEL = {
    TRACK_NOT_STARTED: 0, TRACK_SOURCES_PENDING: 1, TRACK_SOURCES_VERIFIED: 2,
    TRACK_IN_PROGRESS: 3, TRACK_GENERATED: 4, TRACK_FINAL: 5,
}
_ALBUM_STATUS_LEVEL = {
    ALBUM_CONCEPT: 0, ALBUM_RESEARCH_COMPLETE: 1, ALBUM_SOURCES_VERIFIED: 2,
    ALBUM_IN_PROGRESS: 3, ALBUM_COMPLETE: 4, ALBUM_RELEASED: 5,
}


def _validate_track_transition(current: str, new: str, *, force: bool = False) -> Optional[str]:
    """Return error message if transition is invalid, or None if OK."""
    if force:
        return None
    canonical_current = _CANONICAL_TRACK_STATUS.get(current.lower().strip(), current)
    canonical_new = _CANONICAL_TRACK_STATUS.get(new.lower().strip(), new)
    allowed = _VALID_TRACK_TRANSITIONS.get(canonical_current)
    if allowed is None:
        return None  # unknown current status — don't block (recovery)
    if canonical_new not in allowed:
        return (
            f"Invalid transition: '{canonical_current}' → '{canonical_new}'. "
            f"Allowed next: {', '.join(sorted(allowed)) or 'none (terminal)'}. "
            f"Use force=True to override."
        )
    return None


def _validate_album_transition(current: str, new: str, *, force: bool = False) -> Optional[str]:
    """Return error message if transition is invalid, or None if OK."""
    if force:
        return None
    canonical_current = _CANONICAL_ALBUM_STATUS.get(current.lower().strip(), current)
    canonical_new = _CANONICAL_ALBUM_STATUS.get(new.lower().strip(), new)
    allowed = _VALID_ALBUM_TRANSITIONS.get(canonical_current)
    if allowed is None:
        return None  # unknown current status — don't block (recovery)
    if canonical_new not in allowed:
        return (
            f"Invalid transition: '{canonical_current}' → '{canonical_new}'. "
            f"Allowed next: {', '.join(sorted(allowed)) or 'none (terminal)'}. "
            f"Use force=True to override."
        )
    return None


def _check_album_track_consistency(album: dict, new_status: str) -> Optional[str]:
    """Check if album status is consistent with its tracks' statuses.

    Returns error message if inconsistent, or None if OK.

    Rules:
    - Album "In Progress" -> at least 1 track past "Not Started"
    - Album "Complete" -> ALL tracks at Generated or Final
    - Album "Released" -> ALL tracks at Final
    - Levels 0-2 (Concept/Research/Sources Verified) -> no track requirements
    - Empty albums (no tracks) -> always pass
    """
    canonical = _CANONICAL_ALBUM_STATUS.get(new_status.lower().strip(), new_status)
    album_level = _ALBUM_STATUS_LEVEL.get(canonical)
    if album_level is None or album_level <= 2:
        return None  # no track requirements for early statuses

    tracks = album.get("tracks", {})
    if not tracks:
        return None  # empty albums always pass

    if canonical == ALBUM_IN_PROGRESS:
        has_active = any(
            _TRACK_STATUS_LEVEL.get(t.get("status", TRACK_NOT_STARTED), 0) > 0
            for t in tracks.values()
        )
        if not has_active:
            return (
                "Cannot set album to 'In Progress' — all tracks are still 'Not Started'. "
                "At least one track must have progressed."
            )

    elif canonical == ALBUM_COMPLETE:
        below = [
            slug for slug, t in tracks.items()
            if _TRACK_STATUS_LEVEL.get(t.get("status", TRACK_NOT_STARTED), 0) < _TRACK_STATUS_LEVEL[TRACK_GENERATED]
        ]
        if below:
            return (
                f"Cannot set album to 'Complete' — {len(below)} track(s) below 'Generated': "
                f"{', '.join(sorted(below)[:5])}. All tracks must be Generated or Final."
            )

    elif canonical == ALBUM_RELEASED:
        non_final = [
            slug for slug, t in tracks.items()
            if t.get("status", TRACK_NOT_STARTED) != TRACK_FINAL
        ]
        if non_final:
            return (
                f"Cannot set album to 'Released' — {len(non_final)} track(s) not Final: "
                f"{', '.join(sorted(non_final)[:5])}. All tracks must be Final."
            )

    return None


# ---------------------------------------------------------------------------
# Promo and release constants
# ---------------------------------------------------------------------------

# Expected promo files (from templates/promo/)
_PROMO_FILES = [
    "campaign.md", "twitter.md", "instagram.md",
    "tiktok.md", "facebook.md", "youtube.md",
]

# Album art file patterns for release readiness check
_ALBUM_ART_PATTERNS = [
    "album.png", "album.jpg", "album-art.png", "album-art.jpg",
    "artwork.png", "artwork.jpg", "cover.png", "cover.jpg",
]

# Streaming lyrics placeholder markers
_STREAMING_PLACEHOLDER_MARKERS = [
    "Plain lyrics here",
    "Capitalize first letter of each line",
    "No end punctuation",
    "Write out all repeats fully",
    "Blank lines between sections only",
]


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def update_album_status(album_slug: str, status: str, force: bool = False) -> str:
    """Update an album's status in its README.md file.

    Modifies the album details table (| **Status** | Value |) and updates
    the state cache to reflect the change.

    Args:
        album_slug: Album slug (e.g., "my-album")
        status: New status. Valid options:
            "Concept", "Research Complete", "Sources Verified",
            "In Progress", "Complete", "Released"
        force: Override transition validation (for recovery/correction only)

    Returns:
        JSON with update result or error
    """
    from tools.state.parsers import parse_album_readme
    from tools.state.indexer import write_state

    # Validate status
    if status.lower().strip() not in _VALID_ALBUM_STATUSES:
        return _safe_json({
            "error": (
                f"Invalid status '{status}'. Valid options: "
                + ", ".join(ALBUM_VALID_STATUSES)
            ),
        })

    normalized, album, error = _find_album_or_error(album_slug)
    if error:
        return error

    # Validate status transition
    current_status = album.get("status", ALBUM_CONCEPT)
    err = _validate_album_transition(current_status, status, force=force)
    if err:
        return _safe_json({"error": err})

    # Documentary album gate: albums with SOURCES.md cannot skip Concept → In Progress (configurable)
    if not force:
        state_config = (_shared.cache.get_state()).get("config", {})
        gen_cfg = state_config.get("generation", {})
        require_source_path = gen_cfg.get("require_source_path_for_documentary", True)
        if require_source_path:
            canonical_status = _CANONICAL_ALBUM_STATUS.get(status.lower().strip(), status)
            canonical_current = _CANONICAL_ALBUM_STATUS.get(
                current_status.lower().strip(), current_status)
            if canonical_current == ALBUM_CONCEPT and canonical_status == ALBUM_IN_PROGRESS:
                album_path = album.get("path", "")
                if album_path:
                    sources_path = Path(album_path) / "SOURCES.md"
                    if sources_path.exists():
                        return _safe_json({
                            "error": "Cannot skip to 'In Progress' — this album has SOURCES.md "
                                     "(documentary). Transition through 'Research Complete' → "
                                     "'Sources Verified' → 'In Progress' instead, or use "
                                     "force=True to override. To disable this check, set "
                                     "generation.require_source_path_for_documentary: false "
                                     "in config.",
                        })

    # Album/track consistency gate: album status must not exceed track statuses
    if not force:
        consistency_err = _check_album_track_consistency(album, status)
        if consistency_err:
            return _safe_json({"error": consistency_err})

    # Source verification gate: all tracks must be verified before album
    # can advance to Sources Verified
    if status.lower().strip() == ALBUM_SOURCES_VERIFIED.lower() and not force:
        tracks = album.get("tracks", {})
        unverified = [
            s for s, t in tracks.items()
            if t.get("status", TRACK_NOT_STARTED) in
            {TRACK_NOT_STARTED, TRACK_SOURCES_PENDING}
        ]
        if unverified:
            return _safe_json({
                "error": (
                    f"Cannot mark album as Sources Verified — {len(unverified)} track(s) "
                    f"still unverified: {', '.join(unverified[:5])}"
                ),
            })

    # Release readiness gate: audio, mastered files, and album art must exist
    canonical_status = _CANONICAL_ALBUM_STATUS.get(status.lower().strip(), status)
    if canonical_status == ALBUM_RELEASED and not force:
        release_issues = []
        state_config = (_shared.cache.get_state()).get("config", {})
        tracks = album.get("tracks", {})

        # Check 1: All tracks Final (explicit message, complements consistency check)
        non_final = [s for s, t in tracks.items() if t.get("status") != TRACK_FINAL]
        if non_final:
            release_issues.append(
                f"{len(non_final)} track(s) not Final: {', '.join(sorted(non_final)[:5])}"
            )

        # Check 2: Audio files exist
        audio_root = state_config.get("audio_root", "")
        artist_name = state_config.get("artist_name", "")
        genre = album.get("genre", "")
        audio_path = Path(audio_root) / "artists" / artist_name / "albums" / genre / normalized
        if not audio_path.is_dir() or not list(_find_wav_source_dir(audio_path).glob("*.wav")):
            release_issues.append("No WAV files in audio directory")

        # Check 3: Mastered audio exists
        mastered_dir = audio_path / "mastered"
        if not mastered_dir.is_dir() or not list(mastered_dir.glob("*.wav")):
            release_issues.append("No mastered audio files")

        # Check 4: Album art exists
        if not any((audio_path / p).exists() for p in _ALBUM_ART_PATTERNS):
            release_issues.append("No album art found")

        # Check 5: Streaming lyrics ready
        streaming_issues = []
        for t_slug, t_data in tracks.items():
            track_path_str = t_data.get("path", "")
            if not track_path_str:
                streaming_issues.append(f"{t_slug}: no track path")
                continue
            try:
                tfile = Path(track_path_str).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                streaming_issues.append(f"{t_slug}: cannot read track file")
                continue
            section = _extract_markdown_section(tfile, "Streaming Lyrics")
            if not section:
                streaming_issues.append(f"{t_slug}: missing Streaming Lyrics section")
                continue
            block = _extract_code_block(section)
            if not block or not block.strip():
                streaming_issues.append(f"{t_slug}: empty Streaming Lyrics")
                continue
            if any(m.lower() in block.lower() for m in _STREAMING_PLACEHOLDER_MARKERS):
                streaming_issues.append(f"{t_slug}: placeholder content in Streaming Lyrics")
        if streaming_issues:
            release_issues.append(
                f"Streaming lyrics not ready for {len(streaming_issues)} track(s): "
                + ", ".join(streaming_issues[:5])
            )

        if release_issues:
            return _safe_json({
                "error": (
                    f"Cannot release album — {len(release_issues)} issue(s) found"
                ),
                "issues": release_issues,
                "hint": "Use force=True to override.",
            })

    album_path = album.get("path", "")
    if not album_path:
        return _safe_json({"error": f"No path stored for album '{normalized}'"})

    readme_path = Path(album_path) / "README.md"
    if not readme_path.exists():
        return _safe_json({"error": f"README.md not found at {readme_path}"})

    # Read file
    try:
        text = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _safe_json({"error": f"Cannot read README.md: {e}"})

    # Find and replace the Status row
    pattern = re.compile(
        r'^(\|\s*\*\*Status\*\*\s*\|)\s*.*?\s*\|',
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return _safe_json({"error": "Status field not found in album README.md table"})

    old_status = album.get("status", STATUS_UNKNOWN)
    new_row = f"{match.group(1)} {status} |"
    updated_text = text[:match.start()] + new_row + text[match.end():]

    # Write back
    try:
        readme_path.write_text(updated_text, encoding="utf-8")
    except OSError as e:
        return _safe_json({"error": f"Cannot write README.md: {e}"})

    logger.info("Updated album '%s' status to '%s'", normalized, status)

    # Update cache — mutate the album dict already in state (obtained from
    # _find_album_or_error) and write the same state object; do NOT re-fetch
    # via cache.get_state() which could return a different object if the cache
    # was invalidated between calls.
    try:
        parsed = parse_album_readme(readme_path)
        album["status"] = parsed.get("status", status)
        state = _shared.cache._state  # same object album references into
        if state:
            write_state(state)
    except Exception as e:
        logger.warning("File written but cache update failed for album %s: %s", normalized, e)

    return _safe_json({
        "success": True,
        "album_slug": normalized,
        "old_status": old_status,
        "new_status": status,
    })


async def create_track(
    album_slug: str,
    track_number: str,
    title: str,
    documentary: bool = False,
) -> str:
    """Create a new track file in an album from the track template.

    Copies the track template, fills in track number and title placeholders,
    and optionally keeps documentary sections (Source, Original Quote).

    Args:
        album_slug: Album slug (e.g., "my-album")
        track_number: Two-digit track number (e.g., "01", "02")
        title: Track title (e.g., "My New Track")
        documentary: Keep source/quote sections (default: strip them)

    Returns:
        JSON with created file path or error
    """
    normalized, album, error = _find_album_or_error(album_slug)
    if error:
        return error

    album_path = album.get("path", "")
    if not album_path:
        return _safe_json({"error": f"No path stored for album '{normalized}'"})

    tracks_dir = Path(album_path) / "tracks"
    if not tracks_dir.is_dir():
        return _safe_json({"error": f"tracks/ directory not found in {album_path}"})

    # Normalize track number to zero-padded two digits
    num = track_number.strip().lstrip("0") or "0"
    padded = num.zfill(2)

    # Build slug from number and title
    title_slug = _normalize_slug(title)
    filename = f"{padded}-{title_slug}.md"
    track_path = tracks_dir / filename

    if track_path.exists():
        return _safe_json({
            "created": False,
            "error": f"Track file already exists: {track_path}",
            "path": str(track_path),
        })

    # Read template
    template_path = _shared.PLUGIN_ROOT / "templates" / "track.md"
    if not template_path.exists():
        return _safe_json({"error": f"Track template not found at {template_path}"})

    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _safe_json({"error": f"Cannot read track template: {e}"})

    # Fill in placeholders
    album_title = album.get("title", normalized)
    content = template.replace("[Track Title]", title)
    content = content.replace("| **Track #** | XX |", f"| **Track #** | {padded} |")
    content = content.replace("[Album Name](../README.md)", f"[{album_title}](../README.md)")
    content = content.replace("[Character/Perspective]", "—")
    content = content.replace("[Track's role in the album narrative]", "—")

    # Fill frontmatter placeholders
    content = content.replace("track_number: 0", f"track_number: {int(padded)}")
    content = content.replace(
        "explicit: false",
        f"explicit: {'true' if album.get('explicit', False) else 'false'}",
    )

    # Strip documentary sections if not needed
    if not documentary:
        # Remove from <!-- SOURCE-BASED TRACKS --> to <!-- END SOURCE SECTIONS -->
        source_start = content.find("<!-- SOURCE-BASED TRACKS")
        source_end = content.find("<!-- END SOURCE SECTIONS -->")
        if source_start != -1 and source_end != -1:
            content = content[:source_start] + content[source_end + len("<!-- END SOURCE SECTIONS -->"):]

        # Remove Documentary/True Story sections
        doc_start = content.find("<!-- DOCUMENTARY/TRUE STORY")
        doc_end = content.find("<!-- END DOCUMENTARY SECTIONS -->")
        if doc_start != -1 and doc_end != -1:
            content = content[:doc_start] + content[doc_end + len("<!-- END DOCUMENTARY SECTIONS -->"):]

    # Write file
    try:
        track_path.write_text(content, encoding="utf-8")
    except OSError as e:
        return _safe_json({"error": f"Cannot write track file: {e}"})

    logger.info("Created track %s in album '%s'", filename, normalized)

    return _safe_json({
        "created": True,
        "path": str(track_path),
        "album_slug": normalized,
        "track_slug": f"{padded}-{title_slug}",
        "filename": filename,
    })


# ---------------------------------------------------------------------------
# Promo Directory Tools
# ---------------------------------------------------------------------------


async def get_promo_status(album_slug: str) -> str:
    """Get the status of promo/ directory files for an album.

    Checks which promo files exist and whether they have content beyond
    the template placeholder text.

    Args:
        album_slug: Album slug (e.g., "my-album")

    Returns:
        JSON with promo directory status and per-file details
    """
    normalized, album, error = _find_album_or_error(album_slug)
    if error:
        return error

    album_path = album.get("path", "")
    if not album_path:
        return _safe_json({"error": f"No path stored for album '{normalized}'"})

    promo_dir = Path(album_path) / "promo"
    if not promo_dir.is_dir():
        return _safe_json({
            "found": True,
            "album_slug": normalized,
            "promo_exists": False,
            "files": [],
            "populated": 0,
            "total": len(_PROMO_FILES),
        })

    files = []
    populated = 0
    for fname in _PROMO_FILES:
        fpath = promo_dir / fname
        if not fpath.exists():
            files.append({"file": fname, "exists": False, "populated": False, "word_count": 0})
            continue

        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            files.append({"file": fname, "exists": True, "populated": False, "word_count": 0})
            continue

        # Count non-template words (skip lines that are template placeholders)
        words = 0
        for line in text.split("\n"):
            stripped = line.strip()
            # Skip headings, table formatting, empty lines, and common placeholders
            if (not stripped or stripped.startswith("#") or stripped.startswith("|")
                    or stripped.startswith("---") or stripped.startswith("```")):
                continue
            # Skip lines that are clearly template placeholders
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            words += len(stripped.split())

        # Consider "populated" if there are meaningful words beyond basic structure
        is_populated = words > 20
        if is_populated:
            populated += 1

        files.append({
            "file": fname,
            "exists": True,
            "populated": is_populated,
            "word_count": words,
        })

    return _safe_json({
        "found": True,
        "album_slug": normalized,
        "promo_exists": True,
        "files": files,
        "populated": populated,
        "total": len(_PROMO_FILES),
        "ready": populated == len(_PROMO_FILES),
    })


async def get_promo_content(album_slug: str, platform: str) -> str:
    """Read the content of a specific promo file for an album.

    Args:
        album_slug: Album slug (e.g., "my-album")
        platform: Platform name — one of: campaign, twitter, instagram,
                  tiktok, facebook, youtube

    Returns:
        JSON with file content or error
    """
    # Validate platform
    platform_key = platform.lower().strip()
    filename = f"{platform_key}.md"
    if filename not in _PROMO_FILES:
        return _safe_json({
            "error": f"Unknown platform '{platform}'. Valid options: "
                     + ", ".join(f.replace(".md", "") for f in _PROMO_FILES),
        })

    normalized, album, error = _find_album_or_error(album_slug)
    if error:
        return error

    album_path = album.get("path", "")
    if not album_path:
        return _safe_json({"error": f"No path stored for album '{normalized}'"})

    promo_path = Path(album_path) / "promo" / filename
    if not promo_path.exists():
        return _safe_json({
            "found": False,
            "error": f"Promo file not found: {promo_path}",
            "album_slug": normalized,
            "platform": platform_key,
        })

    try:
        content = promo_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _safe_json({"error": f"Cannot read promo file: {e}"})

    return _safe_json({
        "found": True,
        "album_slug": normalized,
        "platform": platform_key,
        "path": str(promo_path),
        "content": content,
    })


# ---------------------------------------------------------------------------
# Plugin Version Tool
# ---------------------------------------------------------------------------


async def get_plugin_version() -> str:
    """Get the current and stored plugin version.

    Compares the plugin version stored in state.json with the current
    version from .claude-plugin/plugin.json. Useful for upgrade detection.

    Returns:
        JSON with stored_version, current_version, and needs_upgrade flag
    """
    state = _shared.cache.get_state()
    stored = state.get("plugin_version")

    # Read current version from plugin.json
    plugin_json = _shared.PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    current = None
    try:
        if plugin_json.exists():
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
            current = data.get("version")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cannot read plugin.json: %s", e)

    needs_upgrade = False
    if stored is None and current is not None:
        needs_upgrade = True  # First run
    elif stored and current and stored != current:
        needs_upgrade = True

    return _safe_json({
        "stored_version": stored,
        "current_version": current,
        "needs_upgrade": needs_upgrade,
        "plugin_root": str(_shared.PLUGIN_ROOT),
    })


# ---------------------------------------------------------------------------
# Venv Health Check
# ---------------------------------------------------------------------------


def _parse_requirements(path: Path) -> dict:
    """Parse requirements.txt into {package_name: version} dict.

    Handles ``==`` pins only (our format), skips comments and blank lines.
    Strips extras markers (e.g., ``mcp[cli]==1.23.0`` → ``mcp: 1.23.0``).
    Lowercases package names for consistent comparison.

    Returns:
        dict mapping lowercased package names to pinned version strings.
        Empty dict on missing or unreadable file.
    """
    result = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    for line in text.splitlines():
        line = line.strip()
        # Strip inline comments
        if "#" in line:
            line = line[:line.index("#")].strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name, _, version = line.partition("==")
        # Strip extras: mcp[cli] → mcp
        if "[" in name:
            name = name[:name.index("[")]
        name = name.strip().lower()
        version = version.strip()
        if name and version:
            result[name] = version
    return result


async def check_venv_health() -> str:
    """Check if venv packages match requirements.txt pinned versions.

    Compares installed package versions in the plugin venv against
    the pinned versions in requirements.txt. Useful for detecting
    version drift after plugin upgrades.

    Returns:
        JSON with status ("ok", "stale", "no_venv", "error"),
        mismatches, missing packages, counts, and fix command.
    """
    venv_python = Path.home() / ".bitwize-music" / "venv" / "bin" / "python3"
    if not venv_python.exists():
        return _safe_json({
            "status": "no_venv",
            "message": "Venv not found at ~/.bitwize-music/venv",
        })

    req_path = _shared.PLUGIN_ROOT / "requirements.txt"
    requirements = _parse_requirements(req_path)
    if not requirements:
        return _safe_json({
            "status": "error",
            "message": f"Cannot read or parse {req_path}",
        })

    mismatches = []
    missing = []
    ok_count = 0

    for pkg, required_version in sorted(requirements.items()):
        try:
            installed_version = importlib.metadata.version(pkg)
            if installed_version == required_version:
                ok_count += 1
            else:
                mismatches.append({
                    "package": pkg,
                    "required": required_version,
                    "installed": installed_version,
                })
        except importlib.metadata.PackageNotFoundError:
            missing.append({
                "package": pkg,
                "required": required_version,
            })

    checked = len(requirements)
    status = "ok" if not mismatches and not missing else "stale"

    result = {
        "status": status,
        "checked": checked,
        "ok_count": ok_count,
        "mismatches": mismatches,
        "missing": missing,
    }

    if status == "stale":
        result["fix_command"] = (
            f"~/.bitwize-music/venv/bin/pip install -r {req_path}"
        )

    return _safe_json(result)


# ---------------------------------------------------------------------------
# Idea Management Tools
# ---------------------------------------------------------------------------


def _resolve_ideas_path() -> Optional[Path]:
    """Resolve the path to IDEAS.md using config."""
    state = _shared.cache.get_state()
    config = state.get("config", {})
    content_root = config.get("content_root", "")
    if not content_root:
        return None
    return Path(content_root) / "IDEAS.md"


async def create_idea(
    title: str,
    genre: str = "",
    idea_type: str = "",
    concept: str = "",
) -> str:
    """Add a new album idea to IDEAS.md.

    Appends a new idea entry using the standard format. Creates IDEAS.md
    from template if it doesn't exist.

    Args:
        title: Idea title (e.g., "Cyberpunk Dreams")
        genre: Target genre (e.g., "electronic", "hip-hop")
        idea_type: Idea type (e.g., "Documentary", "Thematic", "Narrative")
        concept: One-sentence concept pitch

    Returns:
        JSON with success or error
    """
    if not title.strip():
        return _safe_json({"error": "Title cannot be empty"})

    ideas_path = _resolve_ideas_path()
    if not ideas_path:
        return _safe_json({"error": "Cannot resolve IDEAS.md path (no content_root in config)"})

    # Read existing content or start from scratch
    if ideas_path.exists():
        try:
            text = ideas_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return _safe_json({"error": f"Cannot read IDEAS.md: {e}"})
    else:
        text = "# Album Ideas\n\n---\n\n## Ideas\n"

    # Check for duplicate title
    if f"### {title.strip()}\n" in text:
        return _safe_json({
            "created": False,
            "error": f"Idea '{title.strip()}' already exists in IDEAS.md",
        })

    # Build the new idea block
    lines = [f"\n### {title.strip()}\n"]
    if genre:
        lines.append(f"**Genre**: {genre}")
    if idea_type:
        lines.append(f"**Type**: {idea_type}")
    if concept:
        lines.append(f"**Concept**: {concept}")
    lines.append("**Status**: Pending\n")
    new_block = "\n".join(lines)

    # Append to file
    updated = text.rstrip() + "\n" + new_block

    try:
        ideas_path.parent.mkdir(parents=True, exist_ok=True)
        ideas_path.write_text(updated, encoding="utf-8")
    except OSError as e:
        return _safe_json({"error": f"Cannot write IDEAS.md: {e}"})

    logger.info("Created idea '%s' in IDEAS.md", title.strip())

    # Rebuild ideas in cache
    try:
        _shared.cache.rebuild()
    except Exception as e:
        logger.warning("Idea created but cache rebuild failed: %s", e)

    return _safe_json({
        "created": True,
        "title": title.strip(),
        "genre": genre,
        "type": idea_type,
        "status": "Pending",
        "path": str(ideas_path),
    })


async def update_idea(title: str, field: str, value: str) -> str:
    """Update a field in an existing idea in IDEAS.md.

    Args:
        title: Exact idea title to find (e.g., "Cyberpunk Dreams")
        field: Field to update — "status", "genre", "type", or "concept"
        value: New value for the field

    Returns:
        JSON with success or error
    """
    valid_fields = {"status", "genre", "type", "concept"}
    field_key = field.lower().strip()
    if field_key not in valid_fields:
        return _safe_json({
            "error": f"Unknown field '{field}'. Valid options: {', '.join(sorted(valid_fields))}",
        })

    # Map field key to bold label used in IDEAS.md
    field_labels = {
        "status": "Status",
        "genre": "Genre",
        "type": "Type",
        "concept": "Concept",
    }
    label = field_labels[field_key]

    ideas_path = _resolve_ideas_path()
    if not ideas_path:
        return _safe_json({"error": "Cannot resolve IDEAS.md path (no content_root in config)"})

    if not ideas_path.exists():
        return _safe_json({"error": "IDEAS.md not found"})

    try:
        text = ideas_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _safe_json({"error": f"Cannot read IDEAS.md: {e}"})

    # Find the idea section by title
    title_pattern = re.compile(r'^###\s+' + re.escape(title.strip()) + r'\s*$', re.MULTILINE)
    title_match = title_pattern.search(text)
    if not title_match:
        return _safe_json({
            "found": False,
            "error": f"Idea '{title.strip()}' not found in IDEAS.md",
        })

    # Find the field within this idea's section (between this ### and next ###)
    section_start = title_match.end()
    next_section = re.search(r'^###\s+', text[section_start:], re.MULTILINE)
    section_end = section_start + next_section.start() if next_section else len(text)
    section_text = text[section_start:section_end]

    field_pattern = re.compile(
        r'^(\*\*' + re.escape(label) + r'\*\*\s*:\s*)(.+)$',
        re.MULTILINE,
    )
    field_match = field_pattern.search(section_text)
    if not field_match:
        return _safe_json({
            "error": f"Field '{label}' not found in idea '{title.strip()}'",
        })

    # Replace the field value
    old_value = field_match.group(2).strip()
    abs_start = section_start + field_match.start()
    abs_end = section_start + field_match.end()
    new_line = f"{field_match.group(1)}{value}"
    updated_text = text[:abs_start] + new_line + text[abs_end:]

    try:
        ideas_path.write_text(updated_text, encoding="utf-8")
    except OSError as e:
        return _safe_json({"error": f"Cannot write IDEAS.md: {e}"})

    logger.info("Updated idea '%s' field '%s' to '%s'", title.strip(), label, value)

    # Rebuild ideas in cache
    try:
        _shared.cache.rebuild()
    except Exception as e:
        logger.warning("Idea updated but cache rebuild failed: %s", e)

    return _safe_json({
        "success": True,
        "title": title.strip(),
        "field": label,
        "old_value": old_value,
        "new_value": value,
    })


# ---------------------------------------------------------------------------
# Rename Tools
# ---------------------------------------------------------------------------


async def rename_album(old_slug: str, new_slug: str, new_title: str = "") -> str:
    """Rename album slug, title, and directories.

    Renames the album across all mirrored path trees (content, audio,
    documents), updates the README.md title, and refreshes the state cache.

    Args:
        old_slug: Current album slug (e.g., "old-album-name")
        new_slug: New album slug (e.g., "new-album-name")
        new_title: New display title (if empty, derived from new_slug via title case)

    Returns:
        JSON with rename result or error
    """
    from tools.state.indexer import write_state

    normalized_old = _normalize_slug(old_slug)
    normalized_new = _normalize_slug(new_slug)

    if normalized_old == normalized_new:
        return _safe_json({"error": "Old and new slugs are the same after normalization."})

    # Get state and validate old album exists
    state = _shared.cache.get_state()
    albums = state.get("albums", {})

    if normalized_old not in albums:
        return _safe_json({
            "error": f"Album '{old_slug}' not found.",
            "available_albums": list(albums.keys()),
        })

    if normalized_new in albums:
        return _safe_json({
            "error": f"Album '{new_slug}' already exists.",
        })

    album = albums[normalized_old]

    # Get config for path resolution
    config = state.get("config", {})
    if not config:
        return _safe_json({"error": "No config in state. Run rebuild_state first."})

    content_root = config.get("content_root", "")
    audio_root = config.get("audio_root", "")
    documents_root = config.get("documents_root", "")
    artist = config.get("artist_name", "")
    genre = album.get("genre", "")

    if not artist:
        return _safe_json({"error": "No artist_name in config."})

    # Resolve paths
    content_dir_old = Path(content_root) / "artists" / artist / "albums" / genre / normalized_old
    content_dir_new = Path(content_root) / "artists" / artist / "albums" / genre / normalized_new
    audio_dir_old = Path(audio_root) / "artists" / artist / "albums" / genre / normalized_old
    audio_dir_new = Path(audio_root) / "artists" / artist / "albums" / genre / normalized_new
    docs_dir_old = Path(documents_root) / "artists" / artist / "albums" / genre / normalized_old
    docs_dir_new = Path(documents_root) / "artists" / artist / "albums" / genre / normalized_new

    # Content directory MUST exist
    if not content_dir_old.is_dir():
        return _safe_json({
            "error": f"Content directory not found: {content_dir_old}",
        })

    # Derive title
    title = new_title.strip() if new_title else _derive_title_from_slug(normalized_new)

    # Rename content directory
    content_moved = False
    audio_moved = False
    documents_moved = False

    try:
        shutil.move(str(content_dir_old), str(content_dir_new))
        content_moved = True
    except OSError as e:
        return _safe_json({
            "error": f"Failed to rename content directory: {e}",
            "content_moved": False,
            "audio_moved": False,
            "documents_moved": False,
        })

    # Rename audio directory if it exists
    if audio_dir_old.is_dir():
        try:
            shutil.move(str(audio_dir_old), str(audio_dir_new))
            audio_moved = True
        except OSError as e:
            logger.warning("Content dir renamed but audio dir failed: %s", e)

    # Rename documents directory if it exists
    if docs_dir_old.is_dir():
        try:
            shutil.move(str(docs_dir_old), str(docs_dir_new))
            documents_moved = True
        except OSError as e:
            logger.warning("Content dir renamed but documents dir failed: %s", e)

    # Update README.md title (H1 heading) if it exists
    readme_path = content_dir_new / "README.md"
    if readme_path.exists():
        try:
            text = readme_path.read_text(encoding="utf-8")
            heading_pattern = re.compile(r'^#\s+(.+)$', re.MULTILINE)
            match = heading_pattern.search(text)
            if match:
                updated_text = text[:match.start()] + f"# {title}" + text[match.end():]
                readme_path.write_text(updated_text, encoding="utf-8")
        except OSError as e:
            logger.warning("Directories moved but README title update failed: %s", e)

    # Update state cache
    tracks_updated = 0
    try:
        album_data = albums.pop(normalized_old)
        album_data["path"] = str(content_dir_new)
        album_data["title"] = title

        # Update track paths
        for track_slug, track_data in album_data.get("tracks", {}).items():
            old_track_path = track_data.get("path", "")
            if old_track_path:
                track_data["path"] = old_track_path.replace(
                    str(content_dir_old), str(content_dir_new)
                )
                tracks_updated += 1

        albums[normalized_new] = album_data
        write_state(state)
    except Exception as e:
        logger.warning("Directories moved but cache update failed: %s", e)

    logger.info("Renamed album '%s' to '%s'", normalized_old, normalized_new)

    return _safe_json({
        "success": True,
        "old_slug": normalized_old,
        "new_slug": normalized_new,
        "title": title,
        "content_moved": content_moved,
        "audio_moved": audio_moved,
        "documents_moved": documents_moved,
        "tracks_updated": tracks_updated,
    })


async def rename_track(
    album_slug: str,
    old_track_slug: str,
    new_track_slug: str,
    new_title: str = "",
) -> str:
    """Rename track slug, title, and file.

    Renames the track markdown file, updates the title in the metadata table,
    and refreshes the state cache.

    Args:
        album_slug: Album containing the track (e.g., "my-album")
        old_track_slug: Current track slug or prefix (e.g., "01-old-name" or "01")
        new_track_slug: New track slug (e.g., "01-new-name")
        new_title: New display title (if empty, derived from new_slug)

    Returns:
        JSON with rename result or error
    """
    from tools.state.parsers import parse_track_file
    from tools.state.indexer import write_state

    normalized_album, album, error = _find_album_or_error(album_slug)
    if error:
        return error

    tracks = album.get("tracks", {})
    normalized_old = _normalize_slug(old_track_slug)
    normalized_new = _normalize_slug(new_track_slug)

    if normalized_old == normalized_new:
        return _safe_json({"error": "Old and new track slugs are the same after normalization."})

    # Find old track (exact or prefix match)
    track_data = tracks.get(normalized_old)
    matched_slug = normalized_old
    if not track_data:
        prefix_matches = {s: d for s, d in tracks.items() if s.startswith(normalized_old)}
        if len(prefix_matches) == 1:
            matched_slug = next(iter(prefix_matches))
            track_data = prefix_matches[matched_slug]
        elif len(prefix_matches) > 1:
            return _safe_json({
                "error": f"Multiple tracks match '{old_track_slug}': {', '.join(prefix_matches.keys())}",
            })
        else:
            return _safe_json({
                "error": f"Track '{old_track_slug}' not found in album '{album_slug}'.",
                "available_tracks": list(tracks.keys()),
            })

    # Check new slug doesn't already exist
    if normalized_new in tracks:
        return _safe_json({
            "error": f"Track '{new_track_slug}' already exists in album '{album_slug}'.",
        })

    old_path = Path(track_data.get("path", ""))
    if not old_path.exists():
        return _safe_json({
            "error": f"Track file not found on disk: {old_path}",
        })

    # Build new path
    new_path = old_path.parent / f"{normalized_new}.md"

    # Derive title
    title = new_title.strip() if new_title else _derive_title_from_slug(normalized_new)

    # Rename file
    try:
        shutil.move(str(old_path), str(new_path))
    except OSError as e:
        return _safe_json({"error": f"Failed to rename track file: {e}"})

    # Update title in metadata table
    try:
        text = new_path.read_text(encoding="utf-8")
        title_pattern = re.compile(
            r'^(\|\s*\*\*Title\*\*\s*\|)\s*.*?\s*\|',
            re.MULTILINE,
        )
        match = title_pattern.search(text)
        if match:
            new_row = f"{match.group(1)} {title} |"
            updated_text = text[:match.start()] + new_row + text[match.end():]
            # Also update H1 heading if present
            heading_pattern = re.compile(r'^#\s+(.+)$', re.MULTILINE)
            h1_match = heading_pattern.search(updated_text)
            if h1_match:
                updated_text = updated_text[:h1_match.start()] + f"# {title}" + updated_text[h1_match.end():]
            new_path.write_text(updated_text, encoding="utf-8")
        else:
            logger.warning("Title field not found in track metadata table for %s", matched_slug)
    except OSError as e:
        logger.warning("File renamed but title update failed: %s", e)

    # Update state cache — use the same state object that _find_album_or_error
    # returned references into; do NOT re-fetch via cache.get_state() which
    # could return a different object if the cache was invalidated.
    try:
        old_track_data = tracks.pop(matched_slug)
        old_track_data["path"] = str(new_path)
        old_track_data["title"] = title
        # Re-parse the track for fresh metadata
        try:
            parsed = parse_track_file(new_path)
            old_track_data.update({
                "status": parsed.get("status", old_track_data.get("status")),
                "explicit": parsed.get("explicit", old_track_data.get("explicit")),
                "has_suno_link": parsed.get("has_suno_link", old_track_data.get("has_suno_link")),
                "sources_verified": parsed.get("sources_verified", old_track_data.get("sources_verified")),
                "mtime": new_path.stat().st_mtime,
            })
        except Exception:
            pass
        tracks[normalized_new] = old_track_data
        state = _shared.cache._state  # same object that album/tracks reference into
        if state:
            write_state(state)
    except Exception as e:
        logger.warning("File renamed but cache update failed: %s", e)

    logger.info("Renamed track '%s' to '%s' in album '%s'", matched_slug, normalized_new, normalized_album)

    return _safe_json({
        "success": True,
        "album_slug": normalized_album,
        "old_slug": matched_slug,
        "new_slug": normalized_new,
        "title": title,
        "old_path": str(old_path),
        "new_path": str(new_path),
    })


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(mcp):
    """Register status, track creation, promo, version, ideas, and rename tools with the MCP server."""
    mcp.tool()(update_album_status)
    mcp.tool()(create_track)
    mcp.tool()(get_promo_status)
    mcp.tool()(get_promo_content)
    mcp.tool()(get_plugin_version)
    mcp.tool()(check_venv_health)
    mcp.tool()(create_idea)
    mcp.tool()(update_idea)
    mcp.tool()(rename_album)
    mcp.tool()(rename_track)
