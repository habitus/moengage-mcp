#!/usr/bin/env python3
"""
MoEngage documentation MCP server (moengage.com/docs).

Zero dependencies: Python standard library only, nothing to `pip install`.

Tools:
  - search_docs(query, limit, section) -> rank pages by relevance and return
                                          title / url / section / snippet
  - read_docs(urls)                    -> full markdown of one OR many pages
  - list_sections()                    -> doc sections and page counts

Search uses the local FULL-TEXT index built by build_index.py
(~/.cache/moengage_docs/fulltext.jsonl, or a fulltext.jsonl next to this file).
If no index exists it falls back to the live llms.txt (titles + descriptions).
read_docs fetches the live page and falls back to the indexed copy when offline.

Speaks the MCP stdio protocol (newline-delimited JSON-RPC 2.0) by hand.
All diagnostics go to stderr; stdout carries ONLY protocol messages.
"""

import sys
import os
import re
import json
import time
import math
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_index as bi  # noqa: E402  (shared URL/index helpers)

CACHE_DIR = bi.STORE_DIR
INDEX_CACHE = os.path.join(CACHE_DIR, "llms.txt")
STORE_CANDIDATES = [bi.STORE_PATH, os.path.join(HERE, "fulltext.jsonl")]
INDEX_TTL = 24 * 3600
LIVE_TIMEOUT = 20
MAX_READ = 12

_INDEX = None  # fallback: [{"title","url","description","group"}]
_STORE = None  # full-text: {"docs":[...], "df":Counter, "N":int, "by_url":{}} or False


def log(*a):
    print(*a, file=sys.stderr, flush=True)


WORD_RE = re.compile(r"[a-z0-9]+")


def _stem(t):
    # light plural folding so "template" matches "templates", "campaigns" matches "campaign"
    if len(t) > 3 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def _tokens(s):
    return [_stem(t) for t in WORD_RE.findall(s.lower())]


# ---------------------------------------------------------------------------
# Full-text store (preferred)
# ---------------------------------------------------------------------------
def get_store():
    global _STORE
    if _STORE is not None:
        return _STORE or None
    path = next((p for p in STORE_CANDIDATES if os.path.exists(p)), None)
    if not path:
        _STORE = False
        return None
    docs, df, by_url = [], Counter(), {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                text = rec.get("text") or rec.get("description", "")
                ttoks = set(_tokens(rec.get("title", "")))
                desctoks = set(_tokens(rec.get("description", "")))
                metatoks = set(_tokens(" ".join(rec.get("tags", []) + [rec.get("group", "")])))
                pathtoks = set(_tokens(" ".join(bi.rel_parts(rec["url"]))))
                counts = Counter(_tokens(text))
                doc = {
                    "url": rec["url"],
                    "title": rec.get("title", ""),
                    "description": rec.get("description", ""),
                    "section": rec.get("section") or bi.section_of(rec["url"]),
                    "text": text,
                    "low": " ".join(WORD_RE.findall(text.lower())),
                    "ttoks": ttoks,
                    "desctoks": desctoks,
                    "metatoks": metatoks,
                    "pathtoks": pathtoks,
                    "counts": counts,
                    "len": max(1, sum(counts.values())),
                }
                docs.append(doc)
                by_url[rec["url"]] = doc
                for tok in set(counts) | ttoks | desctoks | metatoks | pathtoks:
                    df[tok] += 1
    except Exception as e:
        log("store load failed:", e)
        _STORE = False
        return None
    avglen = sum(d["len"] for d in docs) / max(1, len(docs))
    _STORE = {"docs": docs, "df": df, "N": len(docs), "by_url": by_url, "avglen": avglen}
    log("loaded full-text store: %d docs from %s" % (len(docs), path))
    return _STORE


def _snippet(text, qterms, width=240):
    low = text.lower()
    pos = -1
    for t in qterms:
        m = re.search(r"\b" + re.escape(t), low)
        if m and (pos == -1 or m.start() < pos):
            pos = m.start()
    start = 0 if pos == -1 else max(0, pos - 60)
    return re.sub(r"\s+", " ", text[start:start + width]).strip()


def _section_ok(doc_section, section):
    return not section or doc_section.lower() == section.lower()


def _search_fulltext(store, query, limit, section):
    df, N, avglen = store["df"], store["N"], store["avglen"]
    q = list(dict.fromkeys(_tokens(query)))
    if not q:
        return []
    ql = query.lower().strip()
    raw = WORD_RE.findall(ql)
    phrases = [" ".join(raw[i:i + 2]) for i in range(len(raw) - 1)]  # adjacent word pairs
    idf = {t: math.log(1 + (N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)) for t in q}
    k1, b = 1.2, 0.75
    scored = []
    for d in store["docs"]:
        if not _section_ok(d["section"], section):
            continue
        score, matched = 0.0, 0
        for t in q:
            w = idf[t]
            hit = False
            tf = d["counts"].get(t, 0)
            if tf:
                score += w * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * d["len"] / avglen))
                hit = True
            if t in d["ttoks"]:
                score += 3.0 * w
                hit = True
            if t in d["desctoks"]:
                score += 1.5 * w
                hit = True
            if t in d["metatoks"] or t in d["pathtoks"]:
                score += 1.0 * w
                hit = True
            matched += hit
        if not score:
            continue
        score *= 0.5 + 0.5 * matched / len(q)  # favour pages matching every term
        for ph in phrases:  # reward query words appearing together, e.g. "exit criteria"
            n = d["low"].count(ph)
            if n:
                score += 2.0 * math.log1p(min(n, 10))
        tl = d["title"].lower()
        if ql == tl:
            score += 15
        elif len(ql) > 3 and ql in tl:
            score += 8
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "title": d["title"],
            "url": d["url"],
            "section": d["section"],
            "description": d["description"],
            "snippet": _snippet(d["text"], q),
        }
        for _, d in scored[:limit]
    ]


# ---------------------------------------------------------------------------
# Live index fallback (titles + descriptions only)
# ---------------------------------------------------------------------------
def get_index():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    items = []
    try:
        fresh = os.path.exists(INDEX_CACHE) and time.time() - os.path.getmtime(INDEX_CACHE) < INDEX_TTL
        if fresh:
            with open(INDEX_CACHE, encoding="utf-8") as f:
                items = json.load(f)
    except Exception as e:
        log("index cache read failed:", e)
    if not items:
        items = bi.discover()
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(INDEX_CACHE, "w", encoding="utf-8") as f:
                json.dump(items, f)
        except Exception as e:
            log("index cache write failed:", e)
    _INDEX = items
    log("loaded live index: %d docs" % len(items))
    return items


def _search_index(query, limit, section):
    q = _tokens(query)
    if not q:
        return []
    ql = query.lower()
    scored = []
    for it in get_index():
        if not _section_ok(bi.section_of(it["url"]), section):
            continue
        title_l, desc_l = it["title"].lower(), (it["description"] + " " + it.get("group", "")).lower()
        ttoks = set(_tokens(it["title"]))
        path_l = it["url"].lower()
        score = 0
        for t in q:
            if t in ttoks:
                score += 4
            elif t in title_l:
                score += 2
            if t in desc_l:
                score += 1
            if t in path_l:
                score += 1
        if ql in title_l:
            score += 6
        if score:
            scored.append((score, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"title": it["title"], "url": it["url"], "section": bi.section_of(it["url"]),
         "description": it["description"], "snippet": ""}
        for _, it in scored[:limit]
    ]


def search(query, limit=8, section=""):
    store = get_store()
    if store:
        return _search_fulltext(store, query, limit, section)
    return _search_index(query, limit, section)


# ---------------------------------------------------------------------------
# Read a page as markdown
# ---------------------------------------------------------------------------
def read_one(url):
    norm = bi.normalize(url)
    if not norm:
        return "Refused: only moengage.com/docs URLs are allowed. Got: " + url
    purl = bi.page_url(norm)
    try:
        raw = bi.http_get(bi.md_url(purl), timeout=LIVE_TIMEOUT, retries=2)
        if not raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            _, _, _, body = bi.parse_page(raw, {"title": "", "description": "", "url": purl})
            return body
        err = "page returned HTML instead of markdown"
    except urllib.error.HTTPError as e:
        err = "HTTP %s" % e.code
    except Exception as e:
        err = str(e)
    store = get_store()
    doc = store and store["by_url"].get(purl)
    if doc and doc["text"]:
        return "(live fetch failed: %s; showing indexed copy)\n\n%s" % (err, doc["text"])
    return "Error fetching %s: %s" % (purl, err)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
def _limit(v, default, hi):
    try:
        return max(1, min(int(v or default), hi))
    except (TypeError, ValueError):
        return default


def tool_search_docs(args):
    query = (args.get("query") or "").strip()
    section = (args.get("section") or "").strip()
    limit = _limit(args.get("limit"), 8, 25)
    results = search(query, limit, section)
    if not results:
        where = " in section '%s'" % section if section else ""
        return "No matching pages found for '%s'%s. Try broader or different keywords." % (query, where)
    out = ["%d result(s) for '%s':" % (len(results), query), ""]
    for r in results:
        out.append("- %s  [%s]\n  %s" % (r["title"], r["section"] or "-", r["url"]))
        if r.get("description"):
            out.append("  %s" % r["description"])
        if r.get("snippet"):
            out.append("  …%s…" % r["snippet"])
    return "\n".join(out)


def tool_read_docs(args):
    urls = args.get("urls") or args.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return "No URLs provided. Pass a list of https://www.moengage.com/docs/... URLs."
    chunks = ["===== %s =====\n%s" % (u, read_one(str(u))) for u in urls[:MAX_READ]]
    if len(urls) > MAX_READ:
        chunks.append("(only the first %d of %d URLs were read)" % (MAX_READ, len(urls)))
    return "\n\n".join(chunks)


def tool_list_sections(args):
    store = get_store()
    if store:
        counts = Counter(d["section"] or "(root)" for d in store["docs"])
    else:
        counts = Counter(bi.section_of(it["url"]) or "(root)" for it in get_index())
    lines = ["%d pages across %d sections:" % (sum(counts.values()), len(counts)), ""]
    lines += ["- %s: %d pages" % (s, n) for s, n in counts.most_common()]
    lines.append("\nPass a section name to search_docs(section=...) to narrow results.")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "search_docs",
        "description": (
            "Search the MoEngage documentation (moengage.com/docs: user guide, developer/SDK "
            "guides, API reference, integrations) by keyword or topic. Searches the FULL TEXT "
            "of every page and returns titles, URLs, section and a matching snippet. Use this "
            "FIRST, then call read_docs on the URLs you need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or topic, e.g. 'android push token registration' or 'flows exit criteria'.",
                },
                "limit": {"type": "integer", "description": "Max results (default 8, max 25)."},
                "section": {
                    "type": "string",
                    "description": "Optional top-level section to restrict to, e.g. 'developer-guide' or "
                                   "'user-guide' (see list_sections).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_docs",
        "description": (
            "Fetch the full markdown of one OR MORE MoEngage doc pages. Pass one URL for a "
            "simple question, or several URLs from search_docs to answer a question spanning "
            "multiple pages. Max %d pages per call." % MAX_READ
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of https://www.moengage.com/docs/... URLs to read.",
                }
            },
            "required": ["urls"],
        },
    },
    {
        "name": "list_sections",
        "description": "List the top-level sections of the MoEngage docs with page counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "search_docs": tool_search_docs,
    "read_docs": tool_read_docs,
    "list_sections": tool_list_sections,
}


# ---------------------------------------------------------------------------
# MCP stdio JSON-RPC loop
# ---------------------------------------------------------------------------
def respond(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    log("moengage-docs MCP server starting")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            log("bad json:", e)
            continue
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            respond(mid, {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "moengage-docs", "version": "1.0.0"},
            })
        elif method in ("notifications/initialized", "initialized"):
            pass
        elif method == "tools/list":
            respond(mid, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            handler = HANDLERS.get(name)
            if handler is None:
                respond(mid, error={"code": -32601, "message": "Unknown tool: %s" % name})
                continue
            try:
                text = handler(params.get("arguments") or {})
                respond(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                log("tool '%s' error:" % name, e)
                respond(mid, {"content": [{"type": "text", "text": "Error: %s" % e}], "isError": True})
        elif method == "ping":
            respond(mid, {})
        elif mid is not None:
            respond(mid, error={"code": -32601, "message": "Unknown method: %s" % method})
    log("moengage-docs MCP server stopped")


if __name__ == "__main__":
    main()
