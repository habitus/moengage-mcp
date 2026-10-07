# MoEngage Docs MCP

A small, local **MCP server** that lets Claude (Claude Code / Claude Desktop) search and read the
public MoEngage documentation at **https://www.moengage.com/docs** (user guide, developer/SDK guides,
API reference, integrations).

It works the same way as the Insider Academy MCP: the docs are mirrored into a local
JSON Lines file (`fulltext.jsonl`) which the server searches, and pages are read live as markdown.

- **Zero dependencies**: pure Python 3 standard library. No `pip install`.
- Tools exposed to Claude:
  - `search_docs(query, limit, section)`: full-text BM25 search over every page, optionally limited
    to one top-level section (e.g. `developer-guide`)
  - `read_docs(urls)`: full markdown of one or more pages (max 12 per call). Falls back to the
    indexed copy if the site can't be reached.
  - `list_sections()`: top-level sections and page counts
- Public docs only. Nothing behind the MoEngage dashboard login.

---

## Requirements
- **Python 3** on your PATH as `python3`.
- **Claude Code** CLI (`claude`), or Claude Desktop (see below).

## Install (easy way)

```bash
bash install.sh
```

It will:
1. Use the index if it already exists at `~/.cache/moengage_docs/fulltext.jsonl`, otherwise copy a
   bundled `fulltext.jsonl` from this folder, otherwise crawl the docs (one-time).
2. Register the server with Claude Code at **user scope** (available in every project).

Restart Claude / start a new session and ask something like
*"search the moengage docs for android push token registration"*.

## Install (manual)

```bash
python3 build_index.py                                   # 1. build the index
claude mcp add moengage-docs --scope user -- python3 "$(pwd)/server.py"   # 2. register
claude mcp list                                          # 3. should show moengage-docs ✓ Connected
```

> Register the absolute path on *your* machine; don't reuse someone else's.

---

## How the index is built
`build_index.py`:
1. Reads `https://www.moengage.com/docs/llms.txt`. Links in it that are themselves indexes
   (e.g. `/docs/_llms/developer-guide-sdks.md`) are followed, so every section's pages are found.
   If `llms.txt` is unavailable it falls back to `/docs/sitemap.xml`.
2. Downloads each page's markdown version (`<page>.md`), strips the repeated
   "Documentation Index" banner and frontmatter, and keeps the title, description, tags, section
   and body.
3. Writes one JSON object per line to `~/.cache/moengage_docs/fulltext.jsonl`.

Options:

| Flag | What it does |
|---|---|
| `--all-languages` | Include translated pages (`/docs/ja/...` etc.). English only by default. |
| `--refresh` | Re-download everything instead of reusing cached pages. |
| `--limit N` | Only fetch the first N pages (quick test). |
| `--out PATH` | Write the index somewhere else (e.g. `--out fulltext.jsonl` to bundle it in this folder). |
| `--workers N` | Parallel downloads (default 4; keep it low to avoid HTTP 429s). |

The crawl is resumable: re-running only fetches pages that are missing or failed.

## Refreshing the docs
```bash
python3 build_index.py            # picks up new pages, keeps cached ones
python3 build_index.py --refresh  # re-download everything
```

## Sharing a prebuilt index
To give colleagues a zero-crawl install, build into this folder and ship it with the code:
```bash
python3 build_index.py --out fulltext.jsonl
```
`install.sh` copies a bundled `fulltext.jsonl` into the cache automatically. The server also reads
`fulltext.jsonl` from this folder directly if there's nothing in the cache.

---

## Claude Desktop
Add to `claude_desktop_config.json` (`mcpServers`), using your real absolute path:

```json
{
  "mcpServers": {
    "moengage-docs": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/moengage-mcp/server.py"]
    }
  }
}
```

## Environment variables (optional)
- `MOENGAGE_DOCS_CACHE`: index/cache directory (default `~/.cache/moengage_docs`).
- `MOENGAGE_DOCS_BASE`: docs host (default `https://www.moengage.com`). Only useful for testing.

## Uninstall
```bash
claude mcp remove moengage-docs --scope user
rm -rf ~/.cache/moengage_docs
```

## Files
- `server.py`: the MCP server (stdio, JSON-RPC 2.0).
- `build_index.py`: crawler that builds the local full-text index.
- `install.sh`: one-step installer.
- `fulltext.jsonl`: *(optional)* prebuilt index, if bundled.
