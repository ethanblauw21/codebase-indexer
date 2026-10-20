"""Secret redaction for text leaving the MCP server (ADR-049 §5, B-018).

Applied once, in MCPServer._tool's wrapper, to every tool's returned text. The
index stays faithful and the plain tool functions stay unredacted; only what an
agent receives is scrubbed. Each match becomes a visible ``[REDACTED:<kind>]``
marker, so a redaction fails loud instead of silently dropping text.

Patterns require each token family's real prefix, length and alphabet, with no
word character in front, so ordinary code (``sk_`` names, ``sk-`` class names,
short hex, ``AKIA`` in prose) passes through untouched.
"""
import re

# Not preceded by a character that would make the prefix part of a longer word.
_START = r"(?<![A-Za-z0-9_])"
# Not followed by one that would make the token part of a longer one.
_END = r"(?![A-Za-z0-9_\-])"

# Order matters: the JSON field first, so a service-account key is reported as
# the field it sits in; the PEM block next, before any token pattern can match
# inside a key body; sk-ant- before the general sk- pattern.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key-field", re.compile(
        r"""(?P<head>(?P<q>["'])private_key(?P=q)\s*:\s*)(?P<vq>["'])(?:(?!(?P=vq))[^\\]|\\.)+(?P=vq)""")),
    ("private-key", re.compile(
        # The body is base64, line breaks (real, or a JSON string's literal \n) and
        # the Proc-Type/DEK-Info headers, so code between two header constants
        # is never taken for a key.
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
        r"(?:[A-Za-z0-9+/=\s\\:,\-]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        # No END line: the chunk was cut off inside the key; take its base64 lines.
        r"|(?:(?:\s|\\n)*[A-Za-z0-9+/=]{16,})+)")),
    ("github-token", re.compile(_START + r"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})" + _END)),
    ("slack-token", re.compile(_START + r"xox[abprs]-[A-Za-z0-9-]{10,}" + _END)),
    ("aws-access-key", re.compile(_START + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + _END)),
    ("google-api-key", re.compile(_START + r"AIza[0-9A-Za-z_\-]{35}" + _END)),
    ("anthropic-key", re.compile(_START + r"sk-ant-[A-Za-z0-9_\-]{20,}" + _END)),
    ("openai-key", re.compile(
        _START + r"sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}|[A-Za-z0-9]{20,})" + _END)),
]


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def redact(text: str) -> str:
    """``text`` with every recognized secret replaced by ``[REDACTED:<kind>]``."""
    for kind, pattern in _PATTERNS:
        if kind == "private-key-field":
            text = pattern.sub(lambda m: f"{m['head']}{m['vq']}{_marker(kind)}{m['vq']}", text)
        else:
            text = pattern.sub(_marker(kind), text)
    return text
