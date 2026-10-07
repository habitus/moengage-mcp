#!/usr/bin/env python3
"""
Build a local full-text index of the MoEngage documentation (moengage.com/docs).

Discovery:
  1. Fetch https://www.moengage.com/docs/llms.txt
  2. Any link in it that is itself an index (e.g. /docs/_llms/developer-guide-sdks.md
     or another llms.txt) is expanded recursively; every other /docs link is a page.
  3. If llms.txt yields nothing, fall back to /docs/sitemap.xml.

Each page is downloaded as markdown (the site serves `<page>.md`), the
repeated "Documentation Index" banner and frontmatter are stripped, and one JSON
object per line is written to:
    ~/.cache/moengage_docs/fulltext.jsonl

Translated pages (/docs/ja/..., /docs/es/..., etc.) are skipped by default;
pass --all-languages to include them.

Resumable: pages already in the store with text are not re-fetched.
Pass --refresh to re-download everything.
Zero dependencies: standard library only.
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.environ.get("MOENGAGE_DOCS_BASE", "https://www.moengage.com").rstrip("/")
DOCS_PREFIX = "/docs"
INDEX_URL = BASE + DOCS_PREFIX + "/llms.txt"
SITEMAP_URL = BASE + DOCS_PREFIX + "/sitemap.xml"
STORE_DIR = os.environ.get(
    "MOENGAGE_DOCS_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "moengage_docs")
)
STORE_PATH = os.path.join(STORE_DIR, "fulltext.jsonl")
USER_AGENT = "moengage-docs-mcp/1.0"
WORKERS = 4          # gentle: docs hosts rate-limit heavy concurrency (HTTP 429)
MAX_RETRIES = 5      # retry 429/5xx with exponential backoff
MAX_INDEX_DEPTH = 3  # how deep nested llms index files are followed

# Language prefixes the docs platform uses for translated pages.
LOCALES = {
    "ja", "es", "pt", "pt-br", "fr", "de", "it", "ko", "zh", "zh-cn", "zh-tw", "zh-hans",
    "zh-hant", "id", "vi", "th", "ar", "tr", "ru", "nl", "pl", "sv", "hi", "ms",
}

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)(?:\s*:\s*(.*))?")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TAGS_RE = re.compile(r"^(?:tags|keywords):\s*\[(.*?)\]", re.MULTILINE)
TITLE_RE = re.compile(r'^title:\s*["\']?(.*?)["\']?\s*$', re.MULTILINE)
DESC_RE = re.compile(r'^description:\s*["\']?(.*?)["\']?\s*$', re.MULTILINE)
BANNER_RE = re.compile(r"^> ## Documentation Index\n(?:^>.*\n?)*", re.MULTILINE)
LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, timeout=30, retries=MAX_RETRIES):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/markdown,text/plain,*/*"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1,2,4,8s backoff
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def normalize(url, base_url=INDEX_URL):
    """Absolute, fragment/query-free URL on the docs host, or None if off-site."""
    url = urllib.parse.urljoin(base_url, url.strip())
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower()
    base_host = urllib.parse.urlsplit(BASE).netloc.lower()
    if host.replace("www.", "", 1) != base_host.replace("www.", "", 1):
        return None
    path = p.path
    if not (path == DOCS_PREFIX or path.startswith(DOCS_PREFIX + "/")):
        return None
    return urllib.parse.urlunsplit((urllib.parse.urlsplit(BASE).scheme, urllib.parse.urlsplit(BASE).netloc, path, "", ""))


def page_url(url):
    """Canonical page URL without the .md suffix."""
    return url[:-3] if url.endswith(".md") else url.rstrip("/")


def md_url(url):
    return url if url.endswith(".md") else url.rstrip("/") + ".md"


def rel_parts(url):
    path = urllib.parse.urlsplit(url).path[len(DOCS_PREFIX):].strip("/")
    return [s for s in path.split("/") if s]


def locale_of(url):
    parts = rel_parts(url)
    if parts and parts[0].lower() in LOCALES:
        return parts[0].lower()
    return "en"


def section_of(url):
    parts = rel_parts(url)
    if parts and parts[0].lower() in LOCALES:
        parts = parts[1:]
    return parts[0] if parts else ""


def is_index_url(url):
    path = urllib.parse.urlsplit(url).path
    return "/_llms/" in path or path.endswith("llms.txt") or path.endswith("llms-full.txt")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def parse_index(text, source_url, items, seen_pages, seen_indexes, depth, all_languages):
    """Collect page items from an llms-style index, following nested indexes."""
    heading = ""
    for line in text.splitlines():
        h = HEADING_RE.match(line.strip())
        if h:
            heading = h.group(1).strip()
            continue
        for m in LINK_RE.finditer(line):
            url = normalize(m.group(2), source_url)
            if not url:
                continue
            if is_index_url(url):
                if url.endswith("llms-full.txt") or url in seen_indexes or depth >= MAX_INDEX_DEPTH:
                    continue
                seen_indexes.add(url)
                try:
                    sub = http_get(url)
                except Exception as e:
                    log("  could not fetch sub-index %s: %s" % (url, e))
                    continue
                parse_index(sub, url, items, seen_pages, seen_indexes, depth + 1, all_languages)
                continue
            purl = page_url(url)
            if purl in seen_pages:
                continue
            if not all_languages and locale_of(purl) != "en":
                continue
            seen_pages.add(purl)
            items.append({
                "title": m.group(1).strip(),
                "url": purl,
                "description": (m.group(3) or "").strip(),
                "group": heading,
            })


def discover(all_languages=False):
    items, seen_pages, seen_indexes = [], set(), {INDEX_URL}
    try:
        log("Fetching index %s ..." % INDEX_URL)
        parse_index(http_get(INDEX_URL), INDEX_URL, items, seen_pages, seen_indexes, 0, all_languages)
    except Exception as e:
        log("llms.txt unavailable: %s" % e)
    if items:
        return items
    log("Falling back to sitemap %s ..." % SITEMAP_URL)
    todo, done = [SITEMAP_URL], set()
    while todo:
        sm = todo.pop()
        if sm in done:
            continue
        done.add(sm)
        try:
            xml = http_get(sm)
        except Exception as e:
            log("  sitemap fetch failed %s: %s" % (sm, e))
            continue
        for loc in LOC_RE.findall(xml):
            if loc.endswith(".xml"):
                todo.append(loc)
                continue
            url = normalize(loc)
            if not url:
                continue
            purl = page_url(url)
            if purl in seen_pages or (not all_languages and locale_of(purl) != "en"):
                continue
            seen_pages.add(purl)
            items.append({"title": "", "url": purl, "description": "", "group": ""})
    return items


# ---------------------------------------------------------------------------
# Fetch + parse a page
# ---------------------------------------------------------------------------
def parse_page(raw, item):
    title, description, tags = item["title"], item["description"], []
    # The banner precedes the frontmatter on these pages, so strip it first.
    raw = BANNER_RE.sub("", raw.replace("\r\n", "\n")).lstrip()
    fm = FRONTMATTER_RE.match(raw)
    body = raw
    if fm:
        block = fm.group(1)
        tm = TITLE_RE.search(block)
        if tm and tm.group(1).strip():
            title = tm.group(1).strip()
        dm = DESC_RE.search(block)
        if dm and not description:
            description = dm.group(1).strip()
        gm = TAGS_RE.search(block)
        if gm:
            tags = [t.strip().strip('"').strip("'") for t in gm.group(1).split(",") if t.strip()]
        body = raw[fm.end():]
    body = body.strip()
    if not title:
        h1 = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = h1.group(1).strip() if h1 else rel_parts(item["url"])[-1].replace("-", " ").title()
    return title, description, tags, body


def fetch_doc(item):
    rec = {
        "url": item["url"],
        "title": item["title"],
        "description": item["description"],
        "group": item.get("group", ""),
        "section": section_of(item["url"]),
        "locale": locale_of(item["url"]),
        "tags": [],
        "text": "",
    }
    try:
        raw = http_get(md_url(item["url"]))
    except Exception as e:
        rec["error"] = str(e)
        return rec
    if raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        rec["error"] = "got HTML instead of markdown"
        return rec
    rec["title"], rec["description"], rec["tags"], rec["text"] = parse_page(raw, item)
    return rec


def load_existing():
    good = {}
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("text") and not rec.get("error"):
                        good[rec["url"]] = rec
        except Exception as e:
            log("could not read existing store: %s" % e)
    return good


def main():
    global STORE_PATH, STORE_DIR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all-languages", action="store_true", help="include translated pages (ja, es, ...)")
    ap.add_argument("--refresh", action="store_true", help="re-download every page, ignoring the cache")
    ap.add_argument("--limit", type=int, default=0, help="only fetch the first N pages (for testing)")
    ap.add_argument("--out", help="write the store here instead of %s" % STORE_PATH)
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()
    if args.out:
        STORE_PATH = os.path.abspath(args.out)
        STORE_DIR = os.path.dirname(STORE_PATH)

    items = discover(args.all_languages)
    if not items:
        log("No pages discovered. Is %s reachable from this machine?" % BASE)
        sys.exit(1)
    if args.limit:
        items = items[: args.limit]
    good = {} if args.refresh else load_existing()
    todo = [it for it in items if it["url"] not in good]
    log("Found %d pages. %d already cached, fetching %d (workers=%d)..."
        % (len(items), len(items) - len(todo), len(todo), args.workers))

    os.makedirs(STORE_DIR, exist_ok=True)
    results = dict(good)
    done = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = [ex.submit(fetch_doc, it) for it in todo]
        for fut in as_completed(futures):
            rec = fut.result()
            results[rec["url"]] = rec
            done += 1
            if done % 100 == 0:
                log("  %d/%d (%ds)" % (done, len(todo), int(time.time() - start)))

    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        for it in items:
            rec = results.get(it["url"])
            if rec:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, STORE_PATH)
    ok = sum(1 for it in items if results.get(it["url"], {}).get("text"))
    log("Done: %d pages, %d with text, %d failing, %ds -> %s"
        % (len(items), ok, len(items) - ok, int(time.time() - start), STORE_PATH))
    if len(items) - ok:
        log("Re-run to retry the failing pages (cached pages are skipped).")


if __name__ == "__main__":
    main()
