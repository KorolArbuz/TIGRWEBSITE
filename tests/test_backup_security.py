import sqlite3
from contextlib import closing

import pytest

from scripts.backup_data import backup_data


def test_backup_preserves_committed_wal_and_files_without_overwrite(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    (source / "media").mkdir()
    (source / "media" / "synthetic.png").write_bytes(b"synthetic backup fixture")
    (source / "media" / ".import.lock").touch()
    destination = tmp_path / "backup"
    with closing(sqlite3.connect(source / "shop.db")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, consent INTEGER)")
        db.execute("INSERT INTO sample VALUES (1,1)")
        db.commit()
        backup_data(source, destination)
    with closing(sqlite3.connect(destination / "shop.db")) as copied:
        assert copied.execute("SELECT consent FROM sample WHERE id=1").fetchone() == (1,)
    assert (destination / "BACKUP_COMPLETE").is_file()
    assert (destination / "media" / "synthetic.png").is_file()
    assert not (destination / "media" / ".import.lock").exists()
    with pytest.raises(FileExistsError):
        backup_data(source, destination)
    with pytest.raises(ValueError):
        backup_data(source, source / "recursive-backup")
