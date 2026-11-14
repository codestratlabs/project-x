"""Shared SEC access for the tools in this folder: User-Agent from the environment, at most 5 requests/s, retries.

SEC asks every client to name itself:  export SEC_USER_AGENT="your-org contact@your-domain"
"""
import json
import os
import threading
import time
from pathlib import Path

import sys

import requests            # verifies TLS with certifi

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import paths as P  # noqa: E402  every data folder (PX_DATA, PX_DUMPS ...)

ROOT = P.DATA                                       # holds dumps/ and input/
CONFIG = P.CONFIG
_lock, _next, _s = threading.Lock(), [0.0], []


def user_agent() -> str:
    """SEC_USER_AGENT from the environment or company-finder/.env, the same as nport.py."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    env = P.ENV_FILE
    for line in ([] if ua or not env.exists() else env.read_text(encoding="utf-8").splitlines()):
        if line.strip().startswith("SEC_USER_AGENT="):
            ua = line.split("=", 1)[1].strip().strip('"\'')
    if not ua:
        raise SystemExit('SEC_USER_AGENT is not set. SEC requires a contact in every request: export it, or add it to company-finder/.env, e.g.\n'
                         '  SEC_USER_AGENT="your-org contact@your-domain"')
    return ua


def session() -> requests.Session:
    if not _s:
        s = requests.Session()
        s.headers["User-Agent"] = user_agent()
        _s.append(s)
    return _s[0]


def get(url: str, tries: int = 6, **kw) -> requests.Response:
    """GET at most 5/s across threads; 429/403/5xx are retried with backoff. Returns the last response (check status_code)."""
    r = None
    for i in range(tries):
        with _lock:
            wait = _next[0] - time.time()
            _next[0] = max(_next[0], time.time()) + 0.2
        if wait > 0:
            time.sleep(wait)
        try:
            r = session().get(url, timeout=60, **kw)
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)
            continue
        if r.status_code in (403, 429) or r.status_code >= 500:   # 403 is also SEC's rate-limit page
            time.sleep(2 ** i + (10 if r.status_code in (403, 429) else 0))
            continue
        return r
    return r


def load_config(co: str, path: str = "") -> dict:
    p = Path(path) if path else CONFIG / f"{co}.json"
    if not p.exists():
        raise SystemExit(f"config not found: {p} (pass --config PATH)")
    return json.loads(p.read_text(encoding="utf-8"))


def write_atomic(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(p)
