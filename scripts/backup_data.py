"""Create a new private backup. Stop all web/CLI writers before running."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def backup_data(source: Path, destination: Path) -> None:
    if source.is_symlink() or source.is_junction():
        raise ValueError("Select the real data directory, not a link")
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Backup destination must be outside the data directory")
    database = source / "shop.db"
    if not database.is_file() or database.is_symlink() or database.is_junction():
        raise ValueError("Expected a regular shop.db in the source data directory")
    # Reject symlinks rather than following data paths outside the selected tree.
    for tree in (source / "media", source / "imports"):
        if (tree.is_symlink() or tree.is_junction()
                or (tree.exists() and any(path.is_symlink() or path.is_junction() for path in tree.rglob("*")))):
            raise ValueError("Review symbolic links before backing up data")
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    # An interrupted backup remains visibly incomplete; existing backups are never overwritten.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination / "shop.db")) as snapshot:
            original.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
    for name in ("media", "imports"):
        tree = source / name
        if tree.exists():
            shutil.copytree(tree, destination / name, ignore=shutil.ignore_patterns(".import.lock", ".upload.lock"))
    (destination / "BACKUP_COMPLETE").write_text(
        "SQLite backup and data file copy complete. Web and CLI writers must have been stopped.\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    backup_data(args.data_dir, args.destination)
    print("Backup complete. Keep it private; credentials must be backed up separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
