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


# The families with no prefix of their own (@edb, 2026-10-20: add them and accept the false
# positives). Only the value is replaced; the name it is assigned to stays readable.
NAMED_SECRETS = [
    ("stripe-key", 'STRIPE = "sk_live_' + X * 24 + '"', 'STRIPE = "[REDACTED:stripe-key]"'),
    ("stripe-key", 'k = "rk_test_' + X * 30 + '"', 'k = "[REDACTED:stripe-key]"'),
    ("stripe-key", 'hook = "whsec_' + X * 32 + '"', 'hook = "[REDACTED:stripe-key]"'),
    ("aws-secret-key", "aws_secret_access_key = " + X * 40,
     "aws_secret_access_key = [REDACTED:aws-secret-key]"),
    ("aws-secret-key", '"SecretAccessKey": "' + X * 38 + '/+"',
     '"SecretAccessKey": "[REDACTED:aws-secret-key]"'),
    ("url-credential", 'DB = "postgres://app:' + X * 12 + '@db.example.invalid/prod"',
     'DB = "postgres://app:[REDACTED:url-credential]@db.example.invalid/prod"'),
    ("connection-string-password",
     '"Server=db;Database=erp;User Id=sa;Password=' + X * 10 + ';"',
     '"Server=db;Database=erp;User Id=sa;Password=[REDACTED:connection-string-password];"'),
    ("secret-assignment", 'password = "' + X * 12 + '"', 'password = "[REDACTED:secret-assignment]"'),
    ("secret-assignment", "DB_PASSWD: '" + X * 8 + "'", "DB_PASSWD: '[REDACTED:secret-assignment]'"),
    ("secret-assignment", '{"client_secret": "' + X * 20 + '"}',
     '{"client_secret": "[REDACTED:secret-assignment]"}'),
    ("secret-assignment", "const apiKey = `" + X * 16 + "`;", "const apiKey = `[REDACTED:secret-assignment]`;"),
    ("secret-assignment", 'AuthToken := "' + X * 16 + '"', 'AuthToken := "[REDACTED:secret-assignment]"'),
]


@pytest.mark.parametrize("kind,text,expected", NAMED_SECRETS,
                         ids=[f"{k}-{i}" for i, (k, _, _) in enumerate(NAMED_SECRETS)])
def test_a_named_secret_has_only_its_value_replaced(kind, text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    'password = get_password()',
    'password = os.environ["DB_PASSWORD"]',
    'label = "Enter your password"',
    'PASSWORD_PROMPT = "Your password:"',
    'password = ""',
    'pwd = "short"',
    'pwd = os.getcwd()',
    'commit = "' + "a" * 40 + '"',
    'sk_live = load_key()\nsk_test_mode = True',
    'pk_live_' + X * 24,
    'url = "https://example.invalid/path:8080@v2"',
    'url = "https://example.invalid:8443/api"',
])
def test_code_near_the_named_families_passes_through(text):
    assert redact(text) == text


def test_a_token_marker_is_not_redacted_again_by_a_named_pattern():
    """The token pattern names the kind; the assignment around it must not rename it."""
    assert redact('API_KEY = "sk-ant-api03-' + X * 40 + '"') == 'API_KEY = "[REDACTED:anthropic-key]"'
    assert (redact("https://user:ghp_" + X * 36 + "@github.com/o/r.git")
            == "https://user:[REDACTED:github-token]@github.com/o/r.git")
