"""Offline detect-secrets scan; report only locations/types, never candidate values."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
# Used only for fixed, read-only git commands with shell=False and captured output.
import subprocess  # nosec B404
import sys
import tempfile

from detect_secrets.core.scan import scan_file
from detect_secrets.settings import default_settings

ROOT = Path(__file__).resolve().parents[1]


def git(*arguments: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is unavailable; scan NOT RUN")
    # Fixed git executable, no shell, repository-local read-only arguments.
    result = subprocess.run(  # nosec B603
        [executable, "-c", f"safe.directory={ROOT.as_posix()}", *arguments],
        cwd=ROOT, check=True, capture_output=True,
    )
    return result.stdout


def candidates(label: str, content: bytes):
    if b"\0" in content:
        return
    decoded = content.decode("utf-8", errors="replace")
    lines = decoded.splitlines()
    with tempfile.TemporaryDirectory(prefix="tigr-secret-scan-") as directory:
        candidate_file = Path(directory) / Path(label).name
        candidate_file.write_text(decoded, encoding="utf-8")
        # Third-party diagnostics must not print scanned values, even on parser errors.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            detected = list(scan_file(str(candidate_file)))
        for secret in detected:
            line = lines[secret.line_number - 1]
            if (label == ".secrets-allowlist.json" and secret.type == "Hex High Entropy String"
                    and secret.secret_value in json.loads(decoded)):
                continue  # Public exact-line fingerprints, not credentials; reasons are still scanned.
            # Public dependency integrity hashes and public Action revisions are not credentials.
            if line.strip().startswith("--hash=sha256:") or "uses: actions/" in line:
                continue
            fingerprint = hashlib.sha256(f"{label}\n{line.strip()}\n{secret.type}".encode()).hexdigest()
            yield {"path": label, "line": secret.line_number, "type": secret.type, "fingerprint": fingerprint}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    allowlist_path = ROOT / ".secrets-allowlist.json"
    allowed = json.loads(allowlist_path.read_text(encoding="utf-8")) if allowlist_path.exists() else {}
    findings = []
    checked = 0
    history_blobs = 0
    reviewed = 0
    with default_settings():
        for raw_name in git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8")
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                continue
            checked += 1
            for finding in candidates(name, path.read_bytes()):
                if finding["fingerprint"] in allowed:
                    reviewed += 1
                else:
                    findings.append({"scope": "worktree", **finding})
        if args.history:
            for record in git("rev-list", "--objects", "--all").splitlines():
                object_id, _, raw_name = record.partition(b" ")
                if not raw_name:
                    continue
                oid = object_id.decode("ascii")
                if git("cat-file", "-t", oid).strip() != b"blob":
                    continue
                history_blobs += 1
                name = raw_name.decode("utf-8", errors="replace")
                for finding in candidates(name, git("cat-file", "blob", oid)):
                    if finding["fingerprint"] in allowed:
                        reviewed += 1
                    else:
                        findings.append({"scope": "history", **finding})
    print(json.dumps({"worktree_files": checked, "history_blobs": history_blobs, "reviewed_examples": reviewed,
                      "unreviewed_candidates": findings}, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        print("Secret scan failed; NOT RUN. No candidate values were printed.", file=sys.stderr)
        raise SystemExit(2)
