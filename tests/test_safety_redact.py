"""ADR-049 §5 (B-018): secrets are redacted from tool output, with a marker naming
the kind, and ordinary code passes through untouched.

Every fixture here is obviously fake: the right prefix and length, filled with x.
"""
import pytest

from redact import redact

X = "x"

FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----"

SECRETS = [
    ("private-key", FAKE_PEM),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----"),
    ("private-key", "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----"),
    ("github-token", "ghp_" + X * 36),
    ("github-token", "gho_" + X * 36),
    ("github-token", "ghs_" + X * 36),
    ("github-token", "github_pat_" + X * 22 + "_" + X * 59),
    ("slack-token", "xoxb-0000000000-" + X * 12),
    ("slack-token", "xoxp-" + X * 20),
    ("slack-token", "xoxa-" + X * 20),
    ("aws-access-key", "AKIA" + "X" * 16),
    ("google-api-key", "AIza" + X * 35),
    ("openai-key", "sk-" + X * 48),
    ("openai-key", "sk-proj-" + X * 40),
    ("anthropic-key", "sk-ant-api03-" + X * 40),
]


@pytest.mark.parametrize("kind,secret", SECRETS, ids=[f"{k}-{i}" for i, (k, _) in enumerate(SECRETS)])
def test_each_secret_kind_is_replaced_by_a_marker_naming_it(kind, secret):
    text = f'TOKEN = "{secret}"\nprint(TOKEN)\n'
    out = redact(text)
    assert secret not in out
    assert out == f'TOKEN = "[REDACTED:{kind}]"\nprint(TOKEN)\n'


def test_a_service_account_private_key_field_is_redacted_as_the_field():
    text = ('{"type": "service_account", "private_key_id": "abc123",\n'
            ' "private_key": "-----BEGIN PRIVATE KEY-----\\nFAKE\\n-----END PRIVATE KEY-----\\n",\n'
            ' "client_email": "svc@example.invalid"}')
    out = redact(text)
    assert "FAKE" not in out and "BEGIN" not in out
    assert '"private_key": "[REDACTED:private-key-field]"' in out
    assert '"private_key_id": "abc123"' in out and "svc@example.invalid" in out


def test_a_python_dict_private_key_field_is_redacted():
    out = redact("creds = {'private_key': 'FAKEFAKEFAKE', 'project': 'demo'}")
    assert out == "creds = {'private_key': '[REDACTED:private-key-field]', 'project': 'demo'}"


def test_a_key_cut_off_by_the_chunk_boundary_is_still_redacted():
    """A chunk can end inside a key, so there is no END line to match up to."""
    text = "KEY = '''\n-----BEGIN PRIVATE KEY-----\n" + "FAKE" * 16 + "\n" + "FAKE" * 16 + "\n"
    out = redact(text)
    assert "FAKE" not in out and out == "KEY = '''\n[REDACTED:private-key]\n"


def test_text_after_a_cut_off_key_survives():
    text = "-----BEGIN PRIVATE KEY-----\n" + "FAKE" * 16 + "\ndef after():\n    pass\n"
    assert redact(text) == "[REDACTED:private-key]\ndef after():\n    pass\n"


@pytest.mark.parametrize("text", [
    "sk_live = load_key()\nsk_test_mode = True\nmask_sk_value = sk_[0]",
    "class='sk-button-primary-large-variant'",
    "risk-assessment-and-mitigation-plan-for-q3",
    "digest = '0xdeadbeef'; short = 'deadbeefcafebabe0123'",
    "The AKIA prefix marks a long-term AWS access key; ASIA marks a temporary one.",
    "Google keys start with AIza, so check the prefix.",
    "ghp_token = os.environ['GITHUB_TOKEN']",
    "if token.startswith('xoxb-'): send()",
    "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA\n-----END PUBLIC KEY-----",
    'PEM_HEADER = "-----BEGIN PRIVATE KEY-----"\nPEM_FOOTER = "-----END PRIVATE KEY-----"',
    'cfg = {"private_key": key_from_vault(), "private_key_id": "abc"}',
    "task-" + "a" * 30,
])
def test_ordinary_code_passes_through_untouched(text):
    assert redact(text) == text


def test_a_secret_inside_a_longer_token_is_not_split():
    """A prefix in the middle of a longer identifier is not the start of a token."""
    text = "my_ghp_" + X * 36 + " xAKIA" + "X" * 16
    assert redact(text) == text
