"""
One-shot migration for the "packs (and their leaderboards) have no versions"
change.

Run it once against your live database:

    python scripts/migrate_packs_v1.py

It is idempotent — re-running it does nothing to data already in the new shape.

What it does
------------
PHASE 1 (packs)  — always runs:
    Rewrites every pack document from the old versioned shape

        { pack_id, author, latest_version, deleted, created_at, updated_at,
          versions: [ { version, name, description, thumbnail, levels, created_at }, ... ] }

    into the new flat shape

        { pack_id, author, name, description, thumbnail, levels,
          deleted, created_at, updated_at }

    The frozen content is taken from the pack's *latest* version (its current
    visible state). Flip FREEZE_TO_FIRST_VERSION below if you'd rather freeze to
    version 1 instead.

PHASE 2 (times)  — optional cleanup, controlled by STRIP_TIME_VERSIONS:
    Removes the now-unused `version` field from every leaderboard submission.

    IMPORTANT: this phase is *cleanup only*. The thing that actually resurfaces
    speedruns orphaned by an old version bump is the code change in
    api/routes/times.py — once the leaderboard queries stop filtering on
    `version`, all of a pack's history (regardless of the version it was submitted
    under) competes on the single (pack_id, mode) board. This $unset just deletes
    a dead field so the stored documents match the new schema. Skipping it changes
    no behaviour. Set STRIP_TIME_VERSIONS = False to leave `times` untouched.

    Note on merged boards: because version no longer partitions boards, runs that
    were once on separate per-version boards now share one board. If a pack ever
    genuinely changed its levels across versions, those runs measured different
    courses; merging them is acceptable now that a pack is one fixed course, but
    be aware of it.

The leaderboard indexes are handled automatically by the app on startup (see
_ensure_index in api/main.py), so this script does not touch them.
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Freeze each pack to its latest version (current state). Set True to freeze to
# version 1 (original "as first submitted") instead.
FREEZE_TO_FIRST_VERSION = False

# Delete the dead `version` field from time documents (cleanup only). Set False
# to leave the `times` collection untouched.
STRIP_TIME_VERSIONS = True


def _pick_source_version(pack: dict) -> dict | None:
    """Choose which version's content to freeze into the flat document."""
    versions = pack.get("versions") or []
    if not versions:
        return None
    if FREEZE_TO_FIRST_VERSION:
        return min(versions, key=lambda v: v.get("version", 0))
    latest = pack.get("latest_version")
    for v in versions:
        if v.get("version") == latest:
            return v
    # latest_version pointed at nothing valid — fall back to the highest version.
    return max(versions, key=lambda v: v.get("version", 0))


def migrate_packs(db) -> None:
    migrated = skipped = malformed = 0
    for pack in db.packs.find({}):
        # Already in the new shape?
        if "versions" not in pack and "levels" in pack:
            skipped += 1
            continue

        src = _pick_source_version(pack)
        if src is None:
            # No versions array and no flat levels — leave it alone, flag it.
            malformed += 1
            print(f"  ! skipping malformed pack {pack.get('pack_id')} (_id={pack.get('_id')})")
            continue

        set_fields = {
            "name": src.get("name", ""),
            "description": src.get("description", ""),
            "thumbnail": src.get("thumbnail"),
            "levels": src.get("levels", []),
            "updated_at": pack.get("updated_at") or datetime.now(timezone.utc),
        }
        db.packs.update_one(
            {"_id": pack["_id"]},
            {"$set": set_fields, "$unset": {"versions": "", "latest_version": ""}},
        )
        migrated += 1

    print(f"packs: migrated={migrated} skipped(already-flat)={skipped} malformed={malformed}")


def strip_time_versions(db) -> None:
    result = db.times.update_many(
        {"version": {"$exists": True}},
        {"$unset": {"version": ""}},
    )
    print(f"times: removed the version field from {result.modified_count} submissions")


def main() -> None:
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    client = MongoClient(mongo_uri)
    db = client["LVLNet2"]

    print("Phase 1: flattening packs...")
    migrate_packs(db)

    if STRIP_TIME_VERSIONS:
        print("Phase 2: stripping the dead version field from times...")
        strip_time_versions(db)
    else:
        print("Phase 2: skipped (STRIP_TIME_VERSIONS = False)")

    print("done")


if __name__ == "__main__":
    main()