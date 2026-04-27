# migration/profiles.py — Named migration profiles (client groups + options)
#
# Profiles are stored as plain JSON (no credentials — those stay in session state).
# A profile captures: which clients, which route, and all migration options.

from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from config import PROFILES_FILE


@dataclass
class MigrationProfile:
    name: str
    description: str = ""
    src_env: str = "dev"
    dst_env: str = "qa"
    client_ids: list[int] = field(default_factory=list)   # Empty = user supplies at runtime
    conflict_mode: str = "replace"
    delta_mode: bool = False
    do_backup: bool = True
    excluded_columns: dict[str, list[str]] = field(default_factory=dict)  # {table: [col, ...]}
    row_filters: dict[str, str] = field(default_factory=dict)              # {table: "sql expr"}
    pre_checks: list[str] = field(default_factory=lambda: ["source_has_data", "row_count_positive"])


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_raw() -> dict[str, dict]:
    if not Path(PROFILES_FILE).exists():
        return {}
    try:
        return json.loads(Path(PROFILES_FILE).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw(data: dict[str, dict]) -> None:
    Path(PROFILES_FILE).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def save_profile(profile: MigrationProfile) -> None:
    """Save (or overwrite) a profile by name."""
    raw = _load_raw()
    raw[profile.name] = asdict(profile)
    _save_raw(raw)


def load_all_profiles() -> list[MigrationProfile]:
    """Return all saved profiles sorted by name."""
    raw = _load_raw()
    profiles = []
    for data in raw.values():
        try:
            profiles.append(MigrationProfile(**data))
        except TypeError:
            continue
    return sorted(profiles, key=lambda p: p.name)


def get_profile(name: str) -> MigrationProfile | None:
    """Return a single profile by name, or None if not found."""
    raw = _load_raw()
    data = raw.get(name)
    if data is None:
        return None
    try:
        return MigrationProfile(**data)
    except TypeError:
        return None


def delete_profile(name: str) -> bool:
    """Delete a profile. Returns True if it existed."""
    raw = _load_raw()
    if name not in raw:
        return False
    del raw[name]
    _save_raw(raw)
    return True


def profile_names() -> list[str]:
    return sorted(_load_raw().keys())
