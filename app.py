"""site-opener: website URL -> 3-line cold-email opener for the founder.

Zero dependencies (Python 3.10+ stdlib only).

  Web:  python app.py            -> http://localhost:8000
  CLI:  python app.py https://example.com "what I'm selling"

LLM provider (pick one, both have free tiers):
  GEMINI_API_KEY  (default model gemini-2.5-flash)
  GROQ_API_KEY    (default model llama-3.3-70b-versatile)
  PROVIDER=mock   (no key; deterministic stub for testing the pipeline)
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (compatible; site-opener/0.1; +https://github.com)"
FETCH_TIMEOUT = 8
MAX_BYTES = 1_500_000
MAX_SUBPAGES = 2
MAX_SNIPPETS = 40
MAX_EVIDENCE_CHARS = 7000
MIN_EVIDENCE_CHARS = 300
CACHE_TTL = 24 * 3600
LINE_MAX_WORDS = 30

BANNED = [
    "hope this email finds you", "hope you're well", "hope you are well",
    "i came across", "i stumbled", "i was impressed", "i'm impressed",
    "i am impressed", "game-changer", "game changer", "revolutioniz",
    "cutting-edge", "synergy", "quick question", "touch base",
    "i noticed that you", "love what you're doing", "love what you are doing",
]

SUBPAGE_PRIORITY = [
    ("about", 0), ("team", 0), ("founder", 0), ("story", 0), ("company", 1),
    ("pricing", 2), ("customers", 3), ("case-stud", 3), ("changelog", 4), ("blog", 5),
]

_cache: dict[str, tuple[float, dict]] = {}


def load_dotenv(path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) -> None:
    """Minimal .env loader (KEY=value lines). Real environment variables take precedence."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()


# --------------------------------------------------------------------------- fetch
class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(SafeRedirect)


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter a website address.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urllib.parse.urlparse(raw)
    if not p.hostname or "." not in p.hostname:
        raise ValueError("That doesn't look like a website address.")
    return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", "", p.query, ""))


def assert_public(url: str) -> None:
    """Block SSRF: refuse hosts that resolve to private/loopback/link-local IPs."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Couldn't find {host}. Check the address.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("That address points to a private network.")


def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html). Raises ValueError with a human message."""
    assert_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"})
    try:
        with _opener.open(req, timeout=FETCH_TIMEOUT) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and ctype:
                raise ValueError(f"{url} isn't an HTML page ({ctype}).")
            body = r.read(MAX_BYTES)
            charset = r.headers.get_content_charset() or "utf-8"
            return r.geturl(), body.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"{url} returned HTTP {e.code}" + (" (the site blocks bots)" if e.code in (401, 403, 429) else "") + ".")
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise ValueError(f"Couldn't load {url}: {getattr(e, 'reason', e)}.")


# --------------------------------------------------------------------------- parse
BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "div", "section", "article", "blockquote",
         "td", "dd", "dt", "figcaption", "header", "footer", "main", "br", "span_block"}
SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "form", "button", "select"}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []  # (tag, text)
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.title = ""
        self._buf: list[str] = []
        self._tag_stack: list[str] = []
        self._skip = 0
        self._in_title = False
        self._cur_block = "p"

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.blocks.append((self._cur_block, text))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:description", "og:title", "og:site_name", "twitter:description", "author"):
                self.meta[key] = (a.get("content") or "").strip()
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in BLOCK:
            self._flush()
            self._cur_block = tag

    def handle_endtag(self, tag):
        if tag in SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in BLOCK:
            self._flush()
            self._cur_block = "p"

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_page(url: str, raw_html: str) -> dict:
    p = PageParser()
    try:
        p.feed(raw_html)
        p.close()
    except Exception:
        pass
    return {"url": url, "title": html.unescape(p.title.strip()), "meta": p.meta, "blocks": p.blocks, "links": p.links}


def pick_subpages(base_url: str, links: list[str]) -> list[str]:
    host = urllib.parse.urlparse(base_url).hostname
    scored: dict[str, int] = {}
    for href in links:
        u = urllib.parse.urljoin(base_url, href.split("#")[0])
        pu = urllib.parse.urlparse(u)
        if pu.scheme not in ("http", "https") or pu.hostname != host:
            continue
        path = pu.path.lower().rstrip("/")
        if not path or path.count("/") > 2 or re.search(r"\.(pdf|png|jpg|zip|xml)$", path):
            continue
        for kw, score in SUBPAGE_PRIORITY:
            if kw in path:
                key = urllib.parse.urlunparse((pu.scheme, pu.netloc, pu.path, "", "", ""))
                scored[key] = min(scored.get(key, 99), score + path.count("/"))
                break
    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1])][:MAX_SUBPAGES]


def build_evidence(pages: list[dict]) -> list[dict]:
    """Turn pages into numbered, deduped, length-capped snippets the model must cite."""
    seen: set[str] = set()
    per_page: list[list[dict]] = []

    def add(bucket: list, text: str, url: str, kind: str):
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        key = text.lower()
        words = len(text.split())
        if not text or key in seen or words < 3:
            return
        if kind == "body" and words < 6:
            return
        seen.add(key)
        bucket.append({"text": text[:300], "url": url})

    for pg in pages:
        bucket: list[dict] = []
        add(bucket, pg["title"], pg["url"], "title")
        for k in ("og:site_name", "description", "og:description", "author"):
            if pg["meta"].get(k):
                add(bucket, pg["meta"][k], pg["url"], "meta")
        for tag, text in pg["blocks"]:
            add(bucket, text, pg["url"], "heading" if tag in ("h1", "h2", "h3") else "body")
        per_page.append(bucket)

    # Round-robin across pages so the About page (where founders are named) isn't crowded out by the homepage.
    out, total, i = [], 0, 0
    while len(out) < MAX_SNIPPETS and any(i < len(b) for b in per_page):
        for b in per_page:
            if i < len(b) and len(out) < MAX_SNIPPETS and total + len(b[i]["text"]) <= MAX_EVIDENCE_CHARS:
                out.append({"id": f"S{len(out) + 1}", **b[i]})
                total += len(b[i]["text"])
        i += 1
    return out


def research(url: str) -> tuple[list[dict], list[str]]:
    final, raw = fetch(url)
    home = parse_page(final, raw)
    pages, visited = [home], [final]
    for sub in pick_subpages(final, home["links"]):
        try:
            f, r = fetch(sub)
            pages.append(parse_page(f, r))
            visited.append(f)
        except ValueError:
            continue
    return build_evidence(pages), visited


# --------------------------------------------------------------------------- model
SYSTEM = """You write the first 3 lines of a cold email to the FOUNDER of a company, using ONLY the website snippets provided.

Line 1: one specific, concrete observation about their company drawn from the snippets (a product detail, a customer, a positioning choice, a number). Not flattery.
Line 2: connect that observation to a plausible challenge or opportunity for a company like theirs, and, if a sender pitch is given, why it is relevant. Be honest that it is an inference.
Line 3: a low-friction question or ask. No meeting request with a time.

Hard rules:
- Every factual claim about the company must come from a cited snippet. Do not invent names, numbers, customers, funding, or headcount.
- Use numbers exactly as written on the site. Never calculate new ones (e.g. 100% - 97% = 3%).
- Address the founder by first name ONLY if a snippet clearly names them as founder/CEO; otherwise no name.
- Each line <= 30 words. Plain, specific, human. No exclamation marks.
- Never use: "hope this email finds you", "I came across", "impressed", "game-changer", "cutting-edge", "quick question".
- If the snippets are too thin to say anything specific, set "enough_info" to false.

Return JSON only:
{"company": str, "founder_first_name": str|null, "founder_evidence": [snippet ids],
 "enough_info": bool,
 "lines": [{"text": str, "evidence": [snippet ids]}, {"text": str, "evidence": [...]}, {"text": str, "evidence": [...]}]}"""


def _post_json(url: str, payload: dict, headers: dict, retries: int = 2, timeout: int = 90) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))  # provider overloaded: back off and retry
                continue
            _raise_http(e)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
            if attempt < retries:
                continue
            err = RuntimeError(f"The model API didn't respond ({getattr(e, 'reason', e)}). Try again shortly.")
            err.status = 504
            raise err


def _raise_http(e: urllib.error.HTTPError):
    detail = e.read().decode(errors="replace")[:300]
    if e.code == 429:
        msg = "The model's free-tier rate limit was hit. Try again in a minute."
    elif e.code in (500, 502, 503, 504):
        msg = f"The model API is overloaded (HTTP {e.code}). Try again shortly."
    else:
        msg = f"Model API error {e.code}: {detail}"
    err = RuntimeError(msg)
    err.status = e.code
    raise err


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_gemini_model: str | None = None   # last model that answered: tried first next time
_retired: set[str] = set()          # models that 404 for this key
_cooldown: dict[str, float] = {}    # model -> time until which it's deprioritised after a 429/5xx


_gemini_ranked: list[str] = []


def gemini_candidates(key: str) -> list[str]:
    """Ask the API which models this key can use and rank the Gemini 'flash' text models:
    newest full Flash first, then previews, then Lite. Avoids hard-coding a model Google later retires."""
    global _gemini_ranked
    if _gemini_ranked:
        return _gemini_ranked
    req = urllib.request.Request(f"{GEMINI_BASE}/models?pageSize=200", headers={"x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        models = json.loads(r.read()).get("models", [])

    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("lite" in n, "preview" in n, -(float(m.group(1)) if m else 0.0), "latest" in n)

    names = [m["name"].split("/", 1)[-1] for m in models
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if n.startswith("gemini") and "flash" in n
             and not re.search(r"image|tts|live|audio|embed|thinking|exp|omni|transcribe", n)]
    if not flash:
        raise RuntimeError("No Gemini 'flash' model is available to this API key. Set MODEL in .env.")
    _gemini_ranked = sorted(flash, key=rank)
    return _gemini_ranked


def pick_gemini_model(key: str) -> str:
    return gemini_candidates(key)[0]


def call_model(system: str, user: str) -> tuple[str, dict]:
    global _gemini_model
    provider = os.getenv("PROVIDER") or ("gemini" if os.getenv("GEMINI_API_KEY") else "groq" if os.getenv("GROQ_API_KEY") else "")
    if provider == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": user}]}],
                   "generationConfig": {"temperature": 0.5, "responseMimeType": "application/json"}}
        first = os.getenv("MODEL") or _gemini_model
        now = time.time()
        rest = [m for m in gemini_candidates(key) if m != first and m not in _retired]
        # Models that were busy recently go to the back of the queue instead of being retried first.
        rest.sort(key=lambda m: _cooldown.get(m, 0) > now)
        order = ([first] if first and first not in _retired else []) + rest
        data, deadline = None, now + 120
        for model in order:
            if time.time() > deadline:
                break
            try:
                data = _post_json(f"{GEMINI_BASE}/models/{model}:generateContent", payload, {"x-goog-api-key": key},
                                  retries=0, timeout=40)
                break
            except RuntimeError as e:  # retired (404), overloaded (5xx/timeout) or quota (429): try the next model
                status = getattr(e, "status", None)
                if status == 404:
                    _retired.add(model)  # retired for this key: never try it again (set = safe across threads)
                elif status in (429, 500, 502, 503, 504):
                    _cooldown[model] = time.time() + 300
                    if _gemini_model == model:
                        _gemini_model = None
                else:
                    raise
        if data is None:
            raise RuntimeError("Every Gemini model available to this key is busy or rate-limited right now. "
                               "Try again in a minute.")
        _gemini_model = model
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        u = data.get("usageMetadata", {})
        return text, {"model": model, "in": u.get("promptTokenCount"), "out": u.get("candidatesTokenCount")}
    if provider == "groq":
        model = os.getenv("MODEL", "llama-3.3-70b-versatile")
        data = _post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            {"model": model, "temperature": 0.5, "response_format": {"type": "json_object"},
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
        u = data.get("usage", {})
        return data["choices"][0]["message"]["content"], {"model": model, "in": u.get("prompt_tokens"), "out": u.get("completion_tokens")}
    if provider == "mock":
        return _mock(user), {"model": "mock", "in": 0, "out": 0}
    raise RuntimeError("No model configured. Set GEMINI_API_KEY or GROQ_API_KEY (both free), or PROVIDER=mock.")


def _mock(user: str) -> str:
    ids = re.findall(r"^\[(S\d+)\] (.+)$", user, re.M)
    body = [(i, t) for i, t in ids if len(t.split()) >= 8] or ids
    i, t = body[0]
    return json.dumps({"company": "the company", "founder_first_name": None, "founder_evidence": [], "enough_info": True,
                       "lines": [{"text": f"Your site leads with: {' '.join(t.split()[:14])}.", "evidence": [i]},
                                 {"text": "Teams at that stage usually feel it first in reporting, so that is my guess at where it hurts.", "evidence": []},
                                 {"text": "Is that on your radar this quarter?", "evidence": []}]})


# --------------------------------------------------------------------------- validate
_NUM = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k|m|b|thousand|million|billion)?(?![a-z])", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def _numbers(text: str) -> list[str]:
    """Numbers as written, e.g. '40,000', '$1 million', '97', '2022'."""
    return [m.group(0).strip() for m in _NUM.finditer(text)]


def _numbers_equiv(num: str) -> set[float]:
    """All values a written number can mean: '40k' -> {40, 40000}; '40,000' -> {40000}."""
    m = _NUM.fullmatch(num.strip())
    if not m:
        return set()
    base = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return {base, base * _MULT[unit]} if unit else {base}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%$.]+", " ", s.lower())


def validate(draft: dict, evidence: list[dict], pitch: str = "") -> list[str]:
    """Code-side guardrails. Returns a list of problems (empty == pass)."""
    problems = []
    ids = {s["id"] for s in evidence}
    corpus = _norm(" ".join(s["text"] for s in evidence))
    site_numbers = set().union(*(_numbers_equiv(x) for x in _numbers(" ".join(s["text"] for s in evidence) + " " + pitch)))
    lines = draft.get("lines") or []
    if len(lines) != 3:
        return [f"Expected exactly 3 lines, got {len(lines)}."]
    for n, ln in enumerate(lines, 1):
        text = (ln.get("text") or "").strip()
        cited = ln.get("evidence") or []
        if not text:
            problems.append(f"Line {n} is empty.")
            continue
        if len(text.split()) > LINE_MAX_WORDS:
            problems.append(f"Line {n} is {len(text.split())} words; max {LINE_MAX_WORDS}.")
        if "!" in text:
            problems.append(f"Line {n} uses an exclamation mark.")
        for b in BANNED:
            if b in text.lower():
                problems.append(f"Line {n} uses the cliché '{b}'.")
        bad = [c for c in cited if c not in ids]
        if bad:
            problems.append(f"Line {n} cites unknown snippet(s) {bad}.")
        if n == 1 and not [c for c in cited if c in ids]:
            problems.append("Line 1 must cite at least one snippet.")
        # every number in the line must appear somewhere in the evidence
        for num in _numbers(text):
            if not (_numbers_equiv(num) & site_numbers):
                problems.append(f"Line {n} mentions '{num}', which isn't written on the site.")
    fname = draft.get("founder_first_name")
    if fname and fname.lower() not in corpus:
        problems.append(f"Founder name '{fname}' isn't on the site; drop it.")
    return problems


# --------------------------------------------------------------------------- pipeline
def make_opener(raw_url: str, pitch: str = "", use_cache: bool = True) -> dict:
    t0 = time.time()
    url = normalize_url(raw_url)
    ck = json.dumps([urllib.parse.urlparse(url).netloc, pitch.strip().lower()])
    if use_cache and ck in _cache and time.time() - _cache[ck][0] < CACHE_TTL:
        return {**_cache[ck][1], "cached": True}

    evidence, visited = research(url)
    if sum(len(s["text"]) for s in evidence) < MIN_EVIDENCE_CHARS:
        raise ValueError("The site has almost no readable text (likely rendered by JavaScript or blocked). "
                         "Not drafting, because anything written would be made up.")

    snippet_block = "\n".join(f"[{s['id']}] {s['text']}" for s in evidence)
    user = (f"Website: {url}\nSender pitch: {pitch.strip() or '(none given: keep line 2 about them, not a product)'}\n\n"
            f"Snippets:\n{snippet_block}")

    usage, problems, draft = [], [], {}
    for attempt in range(3):
        msg = user if attempt == 0 else (user + "\n\nYour previous draft failed these checks; fix them:\n- " + "\n- ".join(problems))
        text, u = call_model(SYSTEM, msg)
        usage.append(u)
        try:
            draft = json.loads(re.sub(r"^```(json)?|```$", "", text.strip()).strip())
        except json.JSONDecodeError:
            problems = ["Output was not valid JSON."]
            continue
        if draft.get("enough_info") is False:
            raise ValueError("The model judged the site too thin to write something specific. Not drafting.")
        problems = validate(draft, evidence, pitch)
        if not problems:
            break

    by_id = {s["id"]: s for s in evidence}
    result = {
        "url": url,
        "pages_read": visited,
        "company": draft.get("company"),
        "founder_first_name": draft.get("founder_first_name"),
        "lines": [{"text": l.get("text", ""), "evidence": [by_id[c] for c in (l.get("evidence") or []) if c in by_id]}
                  for l in (draft.get("lines") or [])],
        "warnings": problems,
        "attempts": len(usage),
        "usage": usage,
        "seconds": round(time.time() - t0, 1),
        "cached": False,
    }
    if not problems:
        _cache[ck] = (time.time(), result)
    return result


# --------------------------------------------------------------------------- web
PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/opener":
            return self._send(404, b"not found", "text/plain")
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 10_000)
            req = json.loads(self.rfile.read(n) or b"{}")
            res = make_opener(str(req.get("url", "")), str(req.get("pitch", ""))[:500])
            self._send(200, json.dumps(res).encode(), "application/json")
        except ValueError as e:
            self._send(422, json.dumps({"error": str(e)}).encode(), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    if len(sys.argv) > 1:
        res = make_opener(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", use_cache=False)
        print("\n".join(l["text"] for l in res["lines"]))
        print("\n---\n" + json.dumps({k: res[k] for k in ("pages_read", "warnings", "attempts", "usage", "seconds")}, indent=2))
        return
    port = int(os.getenv("PORT", "8000"))
    print(f"site-opener on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
