"""Repository-level guards against publishing deployment identities or tokens."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".html", ".js", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}


def _publishable_source_like_files():
    # Include pending public additions, but never scan ignored local credentials
    # or private reports. Tracked files remain in scope even if later ignored.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    for relative in result.stdout.decode("utf-8").split("\0"):
        path = ROOT / relative
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def test_no_personal_gee_project_literal_is_committed():
    # Examples must use YOUR_GEE_PROJECT, <redacted-project>, or synthetic
    # gee-test fixtures. Personal gee-r<digits> deployment IDs are forbidden.
    pattern = re.compile(r"\bgee-r\d{6,}\b", re.IGNORECASE)
    findings = []
    for path in _publishable_source_like_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if pattern.search(text):
            findings.append(str(path.relative_to(ROOT)))
    assert findings == [], f"Personal Earth Engine project IDs found in: {findings}"


def test_frontend_has_no_embedded_gee_project_fallback():
    frontend = (ROOT / "webapp" / "static" / "index.html").read_text(
        encoding="utf-8", errors="strict"
    )
    assert not re.search(r"\.value\.trim\(\)\s*\|\|\s*['\"]gee-", frontend)
    assert not re.search(r"getElementById\(['\"]project['\"]\)\.value\s*=\s*['\"]gee-", frontend)
