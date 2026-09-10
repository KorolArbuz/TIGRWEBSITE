from __future__ import annotations

import importlib.util
from pathlib import Path
import secrets

import pytest
import yaml
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_security", ROOT / "scripts/setup_security.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def env_values(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line and not line.startswith("#") and "=" in line)


@pytest.mark.parametrize("first", ["SECRET_KEY", "ADMIN_PASSWORD_HASH", "ADMIN_PASSWORD"])
def test_rotation_removes_bom_old_credentials_and_preserves_other_settings(tmp_path, first):
    target = tmp_path / ".env"
    previous = secrets.token_urlsafe(48)
    target.write_text(f"{first}={previous}\nSTORE_NAME=Synthetic fixture\nDOMAIN=shop.example.invalid\n",
                      encoding="utf-8-sig")
    encoded = setup.password_hash(secrets.token_urlsafe(32))
    setup.write_config(target, encoded, development=False, rotate=True)
    values = env_values(target)
    if values["SECRET_KEY"] == previous or values["ADMIN_PASSWORD_HASH"] != encoded:
        pytest.fail("Rotation did not replace both synthetic credentials.", pytrace=False)
    assert "ADMIN_PASSWORD" not in values
    assert values["STORE_NAME"] == "Synthetic fixture"
    assert values["DOMAIN"] == "shop.example.invalid"
    assert values["APP_ENV"] == "production" and values["SESSION_COOKIE_SECURE"] == "true"
    if previous in target.read_text(encoding="utf-8"):
        pytest.fail("Rotation retained a previous synthetic credential.", pytrace=False)


def test_setup_requires_explicit_rotation_and_preserves_existing_file(tmp_path):
    target = tmp_path / ".env"
    original = secrets.token_bytes(32)
    target.write_bytes(original)
    with pytest.raises(ValueError):
        setup.write_config(target, "unused", development=True, rotate=False)
    if target.read_bytes() != original:
        pytest.fail("Setup modified an existing file without explicit rotation.", pytrace=False)


def test_setup_unicode_password_normalization():
    password = secrets.token_urlsafe(24) + "e\u0301"
    encoded = setup.password_hash(password)
    if not encoded.startswith("$argon2id$"):
        pytest.fail("Setup did not produce Argon2id.", pytrace=False)
    assert PasswordHasher().verify(encoded, password[:-2] + "\u00e9")


def test_compose_keeps_app_private_and_trusts_only_proxy():
    local = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    production = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    assert local["services"]["shop"]["ports"] == ["127.0.0.1:8000:8000"]
    app = production["services"]["shop"]
    proxy = production["services"]["caddy"]
    assert "ports" not in app
    assert app["environment"]["APP_ENV"] == "production"
    assert app["environment"]["SESSION_COOKIE_SECURE"] == "true"
    proxy_ip = proxy["networks"]["proxy"]["ipv4_address"]
    assert f"--forwarded-allow-ips={proxy_ip}" in app["command"]
    assert "--forwarded-allow-ips=*" not in app["command"]
    for service in (local["services"]["shop"], app):
        assert service["read_only"]
        assert service["user"] == "10001:10001"
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
