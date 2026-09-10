"""Create local credentials without printing secrets or silently replacing an existing .env."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unicodedata

from argon2 import PasswordHasher, Type

ROOT = Path(__file__).resolve().parents[1]


def password_hash(password: str) -> str:
    password = unicodedata.normalize("NFC", password)
    if len(password) < 16 or len(password.encode("utf-8")) > 1024:
        raise ValueError("Use a password of at least 16 characters and at most 1024 UTF-8 bytes.")
    if len(set(password.casefold())) < 8 or password.casefold() in {
        "change-me-now", "change-this-password", "replace-with-password", "passwordpassword",
    }:
        raise ValueError("Use a unique password, not an example or repeated short pattern.")
    return PasswordHasher(type=Type.ID).hash(password)


def write_config(target: Path, encoded_password: str, *, development: bool, rotate: bool) -> None:
    """Fail closed for existing files unless rotation was explicitly selected."""
    if target.is_symlink():
        raise ValueError("Refusing a symbolic-link configuration file.")
    exists = target.exists()
    if exists and not rotate:
        raise ValueError("Configuration already exists. Use --rotate to replace both credentials.")
    source = target.read_text(encoding="utf-8-sig") if exists else (ROOT / ".env.example").read_text(encoding="utf-8-sig")
    values = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "ADMIN_PASSWORD_HASH": encoded_password,
        "APP_ENV": "development" if development else "production",
        "SESSION_COOKIE_SECURE": "false" if development else "true",
    }
    if not exists:
        values["ALLOWED_HOSTS"] = "localhost,127.0.0.1"
    lines = []
    for line in source.splitlines():
        name = line.partition("=")[0].strip()
        if name == "ADMIN_PASSWORD":
            continue
        if name in values:
            continue
        lines.append(line)
    lines.extend(f"{key}={value}" for key, value in values.items())
    content = "\n".join(lines) + "\n"
    if not exists:
        # Exclusive creation prevents overwriting a concurrently created file.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".env.rotate-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true", help="Explicitly select local HTTP development.")
    parser.add_argument("--rotate", action="store_true", help="Replace both credentials in the existing local .env.")
    args = parser.parse_args()
    target = ROOT / ".env"
    if target.exists() and not args.rotate:
        parser.error(".env already exists; use --rotate only when you intend to invalidate credentials and sessions.")
    if not sys.stdin.isatty():
        parser.error("Run setup in an interactive terminal so the password can be entered without echo.")
    password = getpass.getpass("New administrator password (16+ characters): ")
    confirmation = getpass.getpass("Repeat administrator password: ")
    if unicodedata.normalize("NFC", password) != unicodedata.normalize("NFC", confirmation):
        parser.error("Passwords do not match.")
    try:
        write_config(target, password_hash(password), development=args.development, rotate=args.rotate)
    except (OSError, ValueError):
        parser.error("Setup failed: check password strength and configuration-file permissions. No credentials are printed.")
    print("Local .env saved. Keep this file private. Restart all application workers after rotation.")
    if os.name == "nt":
        print("On Windows, verify .env permissions allow only your account and system administrators.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
