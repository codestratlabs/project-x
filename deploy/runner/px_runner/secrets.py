"""Secrets from AWS Secrets Manager, and redaction of anything that looks like one.

A task reads only the secret it needs (tasks.TASK_SECRET) and hands it to the pipeline subprocess as an environment
variable. Values are never printed, logged or written to S3: every line the runner prints or records passes redact(),
and error messages here name the secret id, never its value.
"""
from __future__ import annotations

import json
import re

SEC_USER_AGENT = "SEC_USER_AGENT"
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
NAMES = (SEC_USER_AGENT, OPENROUTER_API_KEY)
MASK = "[redacted]"


class SecretError(RuntimeError):
    """A secret is missing a usable value (the message never holds the value)."""


def fetch(client, secret_id: str, name: str) -> str:
    """The value of `name` in secret `secret_id`: the secret string itself, or field `name` of a JSON object."""
    value = (client.get_secret_value(SecretId=secret_id).get("SecretString") or "").strip()
    if value.startswith("{"):
        try:
            doc = json.loads(value)
        except ValueError:
            raise SecretError(f"secret {secret_id} starts with '{{' but is not valid JSON") from None
        value = str((doc.get(name) if isinstance(doc, dict) else "") or "").strip()
        if not value:
            raise SecretError(f"secret {secret_id} has no field {name}")
    if not value:
        raise SecretError(f"secret {secret_id} is empty")
    return value


# Every pattern runs in linear time on any line: redact() sees every line a script prints, and a pattern that
# backtracks over a long run of word characters (a hex blob in an exception message) would stall the task.
_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"), MASK),                          # OpenRouter / OpenAI style keys
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),                               # AWS access key ids
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + MASK),            # Authorization: Bearer ...
    # KEY=value, "key": "value": the name is a whole run of word characters and '-' holding the keyword, tried only
    # where such a run starts and taken whole (++), never backtracked into
    (re.compile(r"(?i)(?<![\w-])((?=[\w-]*?(?:api[_-]?key|secret|token|password|passwd|user[_-]?agent))[\w-]++"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{8,}"), r"\1" + MASK),
    (re.compile(r"[\w.+-]{1,64}@[\w-]++(?:\.[\w-]++)+"), MASK),        # e-mail (the SEC user agent has one)
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{32,}\b"), MASK),   # long random-looking tokens
]


def redact(text: str, known=()) -> str:
    """text with the known secret values and anything that looks like a key, token or e-mail replaced by [redacted]."""
    for value in sorted({v for v in known if v and len(v) >= 4}, key=len, reverse=True):
        text = text.replace(value, MASK)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text
