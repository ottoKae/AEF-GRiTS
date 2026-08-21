"""Conservative redaction for logs and public error messages."""

from __future__ import annotations

import re


_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)((?:refresh_token|access_token|client_secret|id_token)\s*[=:]\s*)['\"]?[^\s,'\"}]+"
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"\bgee-[a-z0-9][a-z0-9-]{4,}\b", re.IGNORECASE), "<redacted-project>"),
)


def redact_text(value: object) -> str:
    text = str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
