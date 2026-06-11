"""
category_tagger.py — Keyword-based semantic category tagging for code chunks.

Assigns broad functional categories to symbols at index time (tag_symbol) and
detects likely categories in a query at retrieval time (classify_query).

Categories are intentionally coarse — the goal is a lightweight pre-signal that
nudges re-ranking scores, not a precise classifier.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Category vocabulary
# ---------------------------------------------------------------------------
# Each key becomes a tag string stored in the chunks.tags column.
# Keywords are matched against tokenized symbol names and leading comment text.

CATEGORIES: dict[str, frozenset[str]] = {
    "[CAT_AUTH]": frozenset({
        "auth", "authenticate", "authentication", "authorize", "authorization",
        "login", "logout", "signin", "signout", "signup", "register",
        "token", "jwt", "session", "credential", "credentials",
        "password", "passwd", "oauth", "permission", "permissions",
        "role", "roles", "privilege", "identity", "verify", "claims", "bearer",
        "access", "refresh", "revoke",
    }),
    "[CAT_PERSIST]": frozenset({
        "save", "insert", "update", "delete", "upsert", "query", "select",
        "fetch", "load", "persist", "commit", "rollback", "migrate", "migration",
        "schema", "model", "repository", "repo", "store", "db", "database",
        "sql", "orm", "record", "find", "create", "remove", "cursor",
        "read", "write", "put", "get", "set",
    }),
    "[CAT_NETWORK]": frozenset({
        "request", "response", "fetch", "http", "https", "api", "endpoint",
        "url", "uri", "header", "headers", "cookie", "cookies",
        "websocket", "socket", "client", "route", "router", "routing",
        "middleware", "proxy", "webhook", "rest", "graphql", "grpc",
        "download", "upload", "stream", "connect", "disconnect",
        "send", "receive", "post", "patch", "put",
    }),
    "[CAT_PARSE]": frozenset({
        "parse", "parser", "serialize", "serializer", "deserialize",
        "encode", "decode", "format", "marshal", "unmarshal",
        "transform", "convert", "tokenize", "lex", "lexer",
        "compile", "transpile", "csv", "json", "xml", "yaml", "toml",
        "stringify", "extract",
    }),
    "[CAT_VALIDATE]": frozenset({
        "validate", "validation", "validator", "check", "sanitize",
        "guard", "ensure", "constraint", "rule", "enforce",
        "required", "schema", "verify",
    }),
    "[CAT_ERROR]": frozenset({
        "error", "errors", "exception", "exceptions", "catch",
        "throw", "raise", "retry", "fallback", "recover", "recovery",
        "handle", "handler", "fault", "fail", "failure", "abort", "panic",
        "invalid", "missing",
    }),
    "[CAT_CACHE]": frozenset({
        "cache", "cached", "caching", "memoize", "memo", "memoized",
        "ttl", "expire", "expiry", "invalidate", "redis", "memcache", "evict",
    }),
    "[CAT_LOG]": frozenset({
        "log", "logs", "logger", "logging", "trace", "debug",
        "info", "warn", "warning", "metric", "metrics",
        "telemetry", "monitor", "monitoring", "audit", "track", "event",
    }),
    "[CAT_CONFIG]": frozenset({
        "config", "configuration", "setting", "settings", "option", "options",
        "env", "environment", "init", "initialize", "setup", "configure",
        "bootstrap", "defaults", "constants",
    }),
    "[CAT_TEST]": frozenset({
        "test", "tests", "spec", "specs", "mock", "mocks", "stub", "stubs",
        "fixture", "fixtures", "spy", "teardown", "describe",
        "expect", "assert", "suite",
    }),
}

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Splits camelCase, PascalCase, snake_case, kebab-case, and whitespace
_SPLIT_RE = re.compile(
    r'[_\-\s]+|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])'
)


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(
        t.lower()
        for t in _SPLIT_RE.split(text)
        if len(t) > 1
    )


def _leading_comment(text: str, max_lines: int = 6) -> str:
    """Return the first few lines of a chunk — usually docstring or inline comments."""
    return "\n".join(text.splitlines()[:max_lines])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tag_symbol(name: str, text: str) -> list[str]:
    """Return category tags for a symbol based on its name and opening comment lines."""
    tokens = _tokenize(name) | _tokenize(_leading_comment(text))
    return [cat for cat, kws in CATEGORIES.items() if tokens & kws]


def classify_query(query: str) -> list[str]:
    """Return category tags likely relevant to a natural-language query string."""
    tokens = _tokenize(query)
    return [cat for cat, kws in CATEGORIES.items() if tokens & kws]
