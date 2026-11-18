"""Secret values from Secrets Manager (plain or JSON) and redaction. Every value here is fake."""
import json
import time

import pytest
from conftest import FAKE_KEY, FAKE_UA

from px_runner import secrets
from px_runner.secrets import MASK


class FakeSM:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self, SecretId):
        return {"Name": SecretId, "SecretString": self.value}


@pytest.mark.parametrize("stored", [FAKE_UA, f"  {FAKE_UA}\n", json.dumps({"SEC_USER_AGENT": FAKE_UA, "other": "x"})])
def test_fetch_plain_or_json(stored):
    assert secrets.fetch(FakeSM(stored), "px/sec-user-agent", "SEC_USER_AGENT") == FAKE_UA


@pytest.mark.parametrize("stored,message", [
    (json.dumps({"OPENROUTER_API_KEY": FAKE_KEY}), "has no field SEC_USER_AGENT"),
    ('{"SEC_USER_AGENT": "' + FAKE_UA, "not valid JSON"),
    ("   ", "is empty"),
    (None, "is empty"),
])
def test_fetch_errors_name_the_secret_never_the_value(stored, message):
    with pytest.raises(secrets.SecretError, match=message) as e:
        secrets.fetch(FakeSM(stored), "px/sec-user-agent", "SEC_USER_AGENT")
    assert FAKE_KEY not in str(e.value) and FAKE_UA not in str(e.value) and "px/sec-user-agent" in str(e.value)


def test_redact_known_values_and_key_like_text():
    line = (f"ua={FAKE_UA} key {FAKE_KEY} AKIAABCDEFGHIJKLMNOP Authorization: Bearer abcdefghijklmnop "
            f"OPENROUTER_API_KEY=whatever-secret-1 \"api_key\": \"hunter2hunter2\" "
            f"token 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c")
    out = secrets.redact(line, [FAKE_UA])
    for leaked in (FAKE_UA, "fake.contact@example.com", FAKE_KEY, "AKIAABCDEFGHIJKLMNOP", "abcdefghijklmnop",
                   "whatever-secret-1", "hunter2hunter2", "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"):
        assert leaked not in out
    assert "Bearer [redacted]" in out and "OPENROUTER_API_KEY=[redacted]" in out
    named = "x-my-long-name-with-token=xyz12345678 token=abcdefgh,api_key=12345678;secret : 'zzzzzzzzzz'"
    assert secrets.redact(named) == ("x-my-long-name-with-token=[redacted] token=[redacted],api_key=[redacted];"
                                     "secret : '[redacted]'")
    mails = "mail a@b.c+d@e.f or <first.last+tag@mail.example.co.uk>"
    assert secrets.redact(mails) == "mail [redacted][redacted] or <[redacted]>"


@pytest.mark.parametrize("line", ["a" * 25_000, "token" * 600, "api_key-" * 400, "9f8e7d6c5b4a3928" * 1_600 + "_",
                                  "x@" + "b" * 25_000, "a." * 25_000])
def test_redact_takes_linear_time_on_long_runs(line):
    """One printed line with a long run of word characters (a blob in an exception message) must not stall the task.
    Each line takes milliseconds; the earlier KEY=value and e-mail patterns backtracked and took 4 to 17 s on these."""
    t0 = time.perf_counter()
    secrets.redact(line, [FAKE_KEY])
    assert time.perf_counter() - t0 < 1.0


def test_redact_keeps_ordinary_pipeline_output():
    lines = ["== 10-K 0001437749-26-004478 rc=0",
             "/tmp/px/data/dumps/lungpacer/10-K/0001558370-24-013066_mitq-20240630x10k.htm",
             "Form D offerings: {'formd_offerings.csv': 7}",
             "  prompt_tokens            1234",
             "not built: ['DOL Form 5500']"]
    assert [secrets.redact(x, [FAKE_KEY]) for x in lines] == lines
    assert secrets.redact("abc abc", ["abc"]) == "abc abc"               # values under 4 characters are not searched
    assert MASK == "[redacted]"
