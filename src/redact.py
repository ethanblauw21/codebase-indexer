"""Secret redaction for text leaving the MCP server (ADR-049 §5, B-018).

Applied once, in MCPServer._tool's wrapper, to every tool's returned text. The
index stays faithful and the plain tool functions stay unredacted; only what an
agent receives is scrubbed. Each match becomes a visible ``[REDACTED:<kind>]``
marker, so a redaction fails loud instead of silently dropping text.

Patterns require each token family's real prefix, length and alphabet, with no
word character in front, so ordinary code (``sk_`` names, ``sk-`` class names,
short hex, ``AKIA`` in prose) passes through untouched.
Families with no prefix (an AWS secret key, a password) are matched by the name
they are assigned to, and only the value is replaced.
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
    # Stripe secret and restricted keys, and webhook signing secrets. The 24-character
    # tail keeps `sk_live = ...` and `sk_test_mode` names untouched; publishable pk_ keys
    # are public by design.
    ("stripe-key", re.compile(_START + r"(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}|whsec_[A-Za-z0-9]{32,})" + _END)),
    # The patterns below have no prefix of their own, so they key on the name the value is
    # assigned to and replace only the value (the "val" group), skipping a value an earlier
    # pattern already replaced. They accept some false positives: a long literal assigned to
    # a name like `secret_name` is redacted too.
    # An AWS secret key is 40 characters of base64; only a name saying so separates it
    # from a git SHA. Before the generic assignment, which would also match the name.
    ("aws-secret-key", re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:aws_?)?secret_?access_?key[\"']?\s*[:=]\s*[\"']?"
        r"(?P<val>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])")),
    # user:password@ in a URL (database URLs, git remotes with a token).
    ("url-credential", re.compile(
        r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@\"'<>]+:(?!\[REDACTED:)(?P<val>[^\s@/\"'<>]+)@")),
    # Password=...; in a connection string. The trailing ; keeps `pwd = get()` out.
    ("connection-string-password", re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:password|pwd)\s*=\s*(?!\[REDACTED:)(?P<val>[^;\"'\s]+)(?=\s*;)")),
    # A quoted literal of 8 or more non-space characters assigned to (or keyed by) a name
    # containing password, passwd, secret, api_key, apikey, access_token or auth_token.
    # A value with a space is prose (a label or prompt), not a credential.
    ("secret-assignment", re.compile(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_\-]*(?:passw(?:or)?d|secret|api_?key|access_?token|auth_?token)"
        r"[A-Za-z0-9_\-]*[\"']?\s*(?::=|=>|[:=])\s*(?P<q>[\"'`])(?!\[REDACTED:)"
        r"(?P<val>[^\s\"'`\\]{8,})(?P=q)")),
]


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _replace_val(kind: str):
    def sub(m: re.Match) -> str:
        whole = m.group(0)
        start, end = m.start("val") - m.start(), m.end("val") - m.start()
        return whole[:start] + _marker(kind) + whole[end:]
    return sub


def redact(text: str) -> str:
    """``text`` with every recognized secret replaced by ``[REDACTED:<kind>]``."""
    for kind, pattern in _PATTERNS:
        if kind == "private-key-field":
            text = pattern.sub(lambda m: f"{m['head']}{m['vq']}{_marker(kind)}{m['vq']}", text)
        elif "val" in pattern.groupindex:
            text = pattern.sub(_replace_val(kind), text)
        else:
            text = pattern.sub(_marker(kind), text)
    return text
