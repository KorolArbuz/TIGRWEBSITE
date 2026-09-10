from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, Iterator

from .database import connect
from .xlsx_importer import ImportLimitError, ImportResult, import_xlsx

MAX_IMPORT_FILES = 10
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 127 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILES = 10
MAX_ARCHIVE_AGE_SECONDS = 7 * 24 * 60 * 60
_MANAGED_NAME = re.compile(r"xlsx-[a-f0-9]{32}\.xlsx")
_STAGING_NAME = re.compile(r"stage-[a-f0-9]{32}\.xlsx")


class ImportBusyError(ValueError):
    """Another process currently owns the catalog import slot."""


@contextmanager
def _file_slot(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ImportBusyError("Другой импорт уже выполняется; повторите позже") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ImportBusyError("Другой импорт уже выполняется; повторите позже") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def import_slot(upload_root: str | Path) -> Iterator[None]:
    """OS locks are shared by web workers and CLI, and released on process exit."""
    with _file_slot(Path(upload_root) / ".import.lock"):
        yield


@contextmanager
def upload_slot(upload_root: str | Path) -> Iterator[None]:
    """Admit one web multipart upload across workers before parsing/spooling."""
    with _file_slot(Path(upload_root) / ".upload.lock"):
        yield


def _managed_files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    return [path for path in directory.iterdir()
            if pattern.fullmatch(path.name) and path.is_file() and not path.is_symlink()]


def _prune_archives(directory: Path, *, reserve_bytes: int = 0, reserve_files: int = 0) -> None:
    """Only files created in the new managed namespace are eligible for retention."""
    archives = sorted(_managed_files(directory, _MANAGED_NAME), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in archives)
    count = len(archives)
    oldest = time.time() - MAX_ARCHIVE_AGE_SECONDS
    for path in archives:
        info = path.stat()
        if (info.st_mtime >= oldest and total + reserve_bytes <= MAX_ARCHIVE_BYTES
                and count + reserve_files <= MAX_ARCHIVE_FILES):
            break
        path.unlink()
        total -= info.st_size
        count -= 1


def run_imports(
    database_path: str | Path,
    upload_root: str | Path,
    imports_root: str | Path,
    files: list[tuple[str, BinaryIO]],
    *,
    price_multiplier: Decimal | str = Decimal("1"),
    deactivate_missing: bool = False,
    max_file_bytes: int = MAX_UPLOAD_FILE_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES,
) -> tuple[list[ImportResult], list[str]]:
    """Run in a worker thread; create/use/close its own SQLite connection.

    Upload budgets are checked for the entire batch before any catalog change.
    Each XLSX is a separate atomic import; a malformed file does not undo an
    earlier successfully imported file. The caller owns and closes input streams.
    """
    if not 1 <= len(files) <= MAX_IMPORT_FILES:
        raise ImportLimitError("За один запрос можно загрузить от 1 до 10 XLSX")
    max_file_bytes = min(max_file_bytes, MAX_UPLOAD_FILE_BYTES)
    max_total_bytes = min(max_total_bytes, MAX_UPLOAD_TOTAL_BYTES)
    with import_slot(upload_root):
        directory = Path(imports_root) / "managed-v1"
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("Недопустимый каталог архивов")
        # Under the exclusive lock, any stage left by a crashed process is stale.
        for stale in _managed_files(directory, _STAGING_NAME):
            stale.unlink()
        _prune_archives(directory)
        staged: list[tuple[str, Path]] = []
        completed: list[ImportResult] = []
        failed: list[str] = []
        total = 0
        try:
            for filename, source in files:
                filename = Path(filename.replace("\\", "/")).name
                if not filename.lower().endswith(".xlsx") or len(filename) > 200:
                    raise ValueError("Выберите файл с расширением XLSX и коротким именем")
                destination = directory / f"stage-{uuid.uuid4().hex}.xlsx"
                staged.append((filename, destination))
                file_size = 0
                source.seek(0)
                with destination.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        file_size += len(chunk)
                        total += len(chunk)
                        if file_size > max_file_bytes or total > max_total_bytes:
                            raise ImportLimitError("Превышен лимит файла или суммарного размера XLSX")
                        output.write(chunk)
                if file_size == 0:
                    raise ValueError("Пустой файл XLSX")
            connection = connect(database_path)
            try:
                for filename, path in staged:
                    try:
                        result = import_xlsx(
                            connection, path, upload_root, original_filename=filename,
                            price_multiplier=price_multiplier, deactivate_missing=deactivate_missing,
                        )
                    except ImportLimitError:
                        raise
                    except ValueError as exc:
                        failed.append(f"{filename}: {exc}")
                        continue
                    try:
                        _prune_archives(directory, reserve_bytes=path.stat().st_size, reserve_files=1)
                        path.replace(directory / f"xlsx-{uuid.uuid4().hex}.xlsx")
                    except OSError:
                        result.warnings = [*(result.warnings or []), "Каталог обновлён; архив исходного файла не сохранён"]
                    completed.append(result)
            finally:
                connection.close()
        finally:
            for _filename, path in staged:
                path.unlink(missing_ok=True)
        return completed, failed
