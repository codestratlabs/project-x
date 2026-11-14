"""Text and model calls for the indirect step: the filing's paragraphs (HTML through the company-finder extractor, PDFs
through pymupdf) and an OpenRouter chat call with retries, rate limit and cost. OPENROUTER_API_KEY comes from the
environment or company-finder/.env.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pipeline/: paths.py
import paths as px  # noqa: E402

ENV_FILE = px.ENV_FILE
URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4.1"
MIN_PARA_CHARS = 30


# ---------------------------------------------------------------- environment
def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPENROUTER_API_KEY"):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise SystemExit(f"OPENROUTER_API_KEY not found (set it or put it in {ENV_FILE})")
    return key


# ---------------------------------------------------------------- document
@dataclass
class Para:
    pid: int       # 1-based paragraph number
    page: int      # 1-based page
    text: str

    @property
    def tag(self) -> str:
        return f"P{self.pid}"

    def line(self) -> str:
        return f"[P{self.pid}|p{self.page}] {self.text}"


HTML_EXT = {".htm", ".html", ".xhtml", ".xml"}   # text filings read with the pipeline extractor (not pymupdf)
PAGE_CHARS = 3000   # an HTML filing has no pages: a "page" is about this many characters of text


def is_html(path) -> bool:
    return Path(str(path)).suffix.lower() in HTML_EXT


_PAGE_BREAK = re.compile(r"<[^>]*(?:page-)?break-(?:after|before)\s*:\s*(?:always|page)[^>]*>", re.I)


_PRE = re.compile(r"(<pre\b.*?</pre>)", re.I | re.S)


def _source_newlines_to_spaces(raw: str) -> str:
    """A line break in the HTML source is only a space on the page (the browser wraps the text); only <br>, a block tag or
    a page break starts a new line. Many filings break their source every ~130 characters inside one <p>, and reading
    those breaks as paragraph ends cut one paragraph into many, so a company named at its start looked far from the
    'it' / 'the company' at its end. <pre> blocks keep their line breaks (fixed-width schedules)."""
    return "".join(p if p[:4].lower() == "<pre" else re.sub(r"[\r\n]+", " ", p) for p in _PRE.split(raw))


def _load_html_paragraphs(path: str) -> list[Para]:
    """HTML / EDGAR filing, read with the pipeline's own HTML extractor (the text the earlier run matched on; pymupdf drops
    text from many EDGAR files). Pages are the filing's own page breaks (page-break-after/before: always), which is also
    how the earlier run numbered pages. A filing without page breaks gets virtual pages of about PAGE_CHARS characters."""
    import sys
    pipeline = px.FINDER
    if str(pipeline) not in sys.path:
        sys.path.insert(0, str(pipeline))
    try:
        from company_finder import extract as E
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f"HTML filings need the company-finder package ({pipeline}): {exc}")
    raw = Path(path).read_bytes().decode("utf-8", "ignore")
    raw = _source_newlines_to_spaces(raw)
    parts = _PAGE_BREAK.split(raw)
    paras: list[Para] = []
    chars = 0
    if len(parts) > 1:
        page = 0
        for part in parts:
            res = E.ExtractResult(str(path), "html")
            E.html_to_units(part, res, {})
            if not "".join(u.text for u in res.units).strip():
                continue                      # a page-break section without text is not a page (same as the earlier run)
            page += 1
            for unit in res.units:
                for line in _lines(unit.text):
                    t = re.sub(r"\s+", " ", line).strip()
                    for piece in _split_long(t):
                        if len(piece) >= MIN_PARA_CHARS:
                            paras.append(Para(len(paras) + 1, page, piece))
        return paras
    whole = E.ExtractResult(str(path), "html")
    E.html_to_units(raw, whole, {})
    for unit in whole.units:
        for line in _lines(unit.text):
            t = re.sub(r"\s+", " ", line).strip()
            for piece in _split_long(t):
                if len(piece) >= MIN_PARA_CHARS:
                    paras.append(Para(len(paras) + 1, chars // PAGE_CHARS + 1, piece))
                    chars += len(piece)
    return paras


_OPEN_END = re.compile(r"(?:^|\s)(?:the|a|an|of|and|or|to|in|for|by|with|its|our|their)\s*$", re.I)
_TERMINAL = re.compile(r"[.!?:;][\"”’')\]]*\s*$")


def _lines(unit_text: str) -> list[str]:
    """Re-flow the lines of an extracted block into paragraphs. Some filings have a hard line break at the end of every
    visual line ("assess their" / "operational resilience" / "in line" ...); a line is joined to the next one until a
    sentence ends. A line is kept alone when it ends with sentence punctuation or is already full width (a real
    paragraph); a blank line always ends a paragraph."""
    out: list[str] = []
    buf: list[str] = []
    for line in unit_text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            if buf:
                out.append(" ".join(buf))
                buf = []
            continue
        buf.append(line)
        # a full-width line ends a paragraph, unless it stops in the middle of a phrase ("... borne by the" / "Fund. ...")
        if _TERMINAL.search(line) or (len(line) >= 100 and not _OPEN_END.search(line)):
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return out


def _split_long(text: str, limit: int = 2500, target: int = 1500) -> list[str]:
    """A very long block (a whole agreement section in one line) is cut at sentence ends into pieces of about `target`."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.;:!?])\s+(?=[A-Z“\"(\d])", text):
        if cur and len(cur) + len(sent) > target:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def load_paragraphs(pdf: str) -> list[Para]:
    """Text blocks of every page, whitespace-normalised; tiny blocks (page numbers, bullets) dropped."""
    if is_html(pdf):
        return _load_html_paragraphs(str(pdf))
    paras: list[Para] = []
    doc = pymupdf.open(pdf)
    for page in doc:
        for block in page.get_text("blocks"):
            if block[6] != 0:  # image block
                continue
            text = re.sub(r"\s+", " ", block[4]).strip()
            if len(text) >= MIN_PARA_CHARS:
                paras.append(Para(len(paras) + 1, page.number + 1, text))
    return paras


def est_tokens(text: str) -> int:
    return len(text) // 4


# ---------------------------------------------------------------- API
class RateLimiter:
    def __init__(self, rpm: int):
        self.gap = 60.0 / rpm
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.gap
        if start > now:
            time.sleep(start - now)


@dataclass
class CallResult:
    content: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    seconds: float
    attempts: int
    error: str = ""
    rate_limit_hits: int = 0      # HTTP 429 responses
    server_errors: int = 0        # HTTP 5xx / API error bodies
    other_errors: int = 0         # network / parse errors
    backoff_seconds: float = 0.0  # time spent sleeping between retries
    started_at: str = ""
    label: str = ""


def call_llm(user_text: str, model: str, *, max_tokens: int, timeout: int, retries: int = 3,
             limiter: RateLimiter | None = None, label: str = "",
             system: str, schema: dict) -> CallResult:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_text}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": schema},
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    t0 = time.monotonic()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    err, n429, n5xx, nother, slept = "", 0, 0, 0, 0.0

    def backoff(sec: float) -> None:
        nonlocal slept
        sec = min(60, sec)
        slept += sec
        time.sleep(sec)

    for attempt in range(1, retries + 2):
        if limiter:
            limiter.wait()
        try:
            r = requests.post(URL, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 429:
                n429 += 1
                err = "HTTP 429 (rate limit)"
                backoff(5 * attempt * attempt)
                continue
            if r.status_code >= 500:
                n5xx += 1
                err = f"HTTP {r.status_code}"
                backoff(5 * attempt)
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                n5xx += 1
                err = str(data["error"])[:200]
                backoff(5 * attempt)
                continue
            ch = data["choices"][0]
            u = data.get("usage", {})
            return CallResult(ch["message"].get("content") or "", ch.get("finish_reason") or "",
                              u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                              float(u.get("cost") or 0), time.monotonic() - t0, attempt, "",
                              n429, n5xx, nother, slept, started, label)
        except (requests.RequestException, ValueError, KeyError) as exc:
            nother += 1
            err = f"{type(exc).__name__}: {exc}"[:200]
            backoff(3 * attempt)
    return CallResult("", "error", 0, 0, 0.0, time.monotonic() - t0, retries + 1, err,
                      n429, n5xx, nother, slept, started, label)


def call_record(res: CallResult, refs_found: int, complete: bool) -> dict:
    """One row per LLM call for the per-call log saved with the results."""
    return {"call": res.label, "started_at": res.started_at, "seconds": round(res.seconds, 1),
            "attempts": res.attempts, "rate_limit_429": res.rate_limit_hits, "server_errors": res.server_errors,
            "other_errors": res.other_errors, "backoff_seconds": round(res.backoff_seconds, 1),
            "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
            "cost_usd": round(res.cost, 6), "finish_reason": res.finish_reason,
            "output_complete": complete, "references": refs_found, "error": res.error}


def totals(results: list[CallResult]) -> dict:
    return {"prompt_tokens": sum(r.prompt_tokens for r in results),
            "completion_tokens": sum(r.completion_tokens for r in results),
            "cost_usd": round(sum(r.cost for r in results), 4),
            "attempts_total": sum(r.attempts for r in results),
            "retries": sum(r.attempts - 1 for r in results),
            "rate_limit_429": sum(r.rate_limit_hits for r in results),
            "server_errors": sum(r.server_errors for r in results),
            "other_errors": sum(r.other_errors for r in results),
            "backoff_seconds": round(sum(r.backoff_seconds for r in results), 1),
            "failed_calls": sum(1 for r in results if r.error and not r.content)}


# ---------------------------------------------------------------- parse + validate
def parse_references(content: str) -> tuple[list[dict], bool]:
    """Return (references, complete). If the JSON was cut off (token limit), salvage the complete objects."""
    try:
        return json.loads(content).get("references", []), True
    except (ValueError, AttributeError):
        pass
    out, dec = [], json.JSONDecoder()
    i = content.find("[")
    if i < 0:
        return [], False
    i += 1
    while i < len(content):
        while i < len(content) and content[i] in " \n\r\t,":
            i += 1
        try:
            obj, i = dec.raw_decode(content, i)
        except ValueError:
            break
        if isinstance(obj, dict):
            out.append(obj)
    return out, False
