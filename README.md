# Tech & Policy Article Digest

A Python pipeline that pulls recent articles from 16 sources (AI, regulation, finance,
business, emerging tech, security, with Australia-specific coverage), summarizes and tags
each with an LLM, deduplicates against history, and renders a **filterable, additive HTML
digest** that grows with every run.

Designed to run twice weekly (Monday & Thursday, 09:00 Sydney).

---

## What it produces

- `output/article_digest.html` — a single self-contained HTML page. Filterable by source
  and by tag (`AI`, `REG`, `FIN`, `BIZ`, `EMG`, `SEC`, `AUS`). Articles grouped by refresh
  date; re-runs on the same day merge into that day's group.
- `output/state.json` — dedup state (seen URLs + headlines). Lets each run skip what's
  already been seen, so the HTML index grows additively.
- `output/digest.log` — append-only run log.

## How it works

```
RSS sources (16) ──► date filter (last 7d) ──► keyword pre-filter
                                                       │
                                  dedup (URL + headline Jaccard ≥ 0.65)
                                                       │
                          LLM batch summarizer (12 at a time, with retry-on-429)
                                                       │
                            append to today's refresh ──► render HTML
```

- **`scripts/digest.py`** — the main pipeline (RSS fetch, date + keyword filtering,
  dedup, LLM orchestration, HTML rendering).
- **`scripts/llm_batch.js`** — Node helper that calls the LLM in batches and returns
  JSON `{index, tags, summary, skip}` objects. Pluggable backend (see below).
- **`scripts/refresh.sh`** — thin wrapper that loads `.env` and invokes `digest.py`.

## Sources

16 tracked by default — see the `SOURCES` list in `scripts/digest.py`. Two of them
(Pivot to AI, Morning Brew) sit behind Cloudflare and need a fallback page reader; see
the "Cloudflare-protected feeds" section below.

## Setup

### 1. Python deps

```bash
python3 -m pip install -r requirements.txt
```

### 2. Node (for the LLM helper)

Node 18 or newer (for built-in `fetch`).

The default LLM backend is **Z.AI** via `z-ai-web-dev-sdk`:

```bash
npm install z-ai-web-dev-sdk
```

If you prefer an OpenAI-compatible backend, no Node packages are required — `llm_batch.js`
uses the global `fetch`.

### 3. Configure (optional)

```bash
cp .env.example .env
# edit .env to switch provider, set keys, or point PAGE_READER_CMD at a fetcher
```

Defaults work out of the box for the Z.AI backend; `.env` is only needed to change
behavior.

## Run

```bash
./scripts/refresh.sh
```

Or directly:

```bash
python3 scripts/digest.py
```

Open `output/article_digest.html` in a browser. Re-running on a later day adds a new
dated section; re-running same-day merges new finds into that day's existing section.

## LLM backend

`llm_batch.js` reads `LLM_PROVIDER`:

| Value    | When to use                                       | Requires                                  |
|----------|---------------------------------------------------|-------------------------------------------|
| `zai`    | Default. You run inside Z.AI or have the SDK key. | `npm install z-ai-web-dev-sdk`            |
| `openai` | Any OpenAI-compatible endpoint (OpenAI, local…).  | `OPENAI_API_KEY`; optionally `OPENAI_BASE_URL`, `OPENAI_MODEL` |

Example, OpenAI-compatible:

```bash
LLM_PROVIDER=openai OPENAI_API_KEY=sk-... OPENAI_MODEL=gpt-4o-mini \
  node scripts/llm_batch.js input.json output.json
```

The summarizer degrades gracefully: on API failure it tags the article `BIZ` and uses the
description as a fallback summary (marked `_failed`), then retries failed articles in
smaller batches. The pipeline never aborts mid-run because of an LLM error.

## Cloudflare-protected feeds

Pivot to AI and Morning Brew block direct `requests.get`. The pipeline will skip them with
a warning unless you set `PAGE_READER_CMD` — a command template whose `{url}` token is
replaced with the feed URL and which must print rendered HTML to stdout. Examples:

```bash
PAGE_READER_CMD="curl -sL --compressed {url}"
PAGE_READER_CMD="node my_render_script.js {url}"
```

If you run inside an environment that exposes a `page_reader`-style tool, point this at it.
Without it, the other 14 sources still work.

## Scheduling

The intended cadence is **Monday and Thursday at 09:00 Sydney**. On a Unix host with cron
in `Australia/Sydney`:

```cron
0 9 * * 1,4  /path/to/article-digest/scripts/refresh.sh >> /path/to/article-digest/output/cron.log 2>&1
```

On systemd-based hosts you can equivalently use a `systemd.timer` with `OnCalendar=
Mon,Thu *-*-* 09:00:00 Australia/Sydney`. The pipeline is also safe to invoke manually at
any time — it only ever appends.

## Project layout

```
article-digest/
├── scripts/
│   ├── digest.py          # main pipeline
│   ├── llm_batch.js       # LLM helper (Z.AI or OpenAI-compatible)
│   └── refresh.sh         # wrapper: loads .env, runs digest.py
├── output/                # generated: HTML, state.json, digest.log (gitignored)
├── requirements.txt
├── package.json
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## Notes & known design choices

- **Additive index, not re-summarized.** Existing articles keep their original tags even
  when the tag set evolves (the `AUS` tag was added after launch and only applies to
  articles fetched from that point forward).
- **DST is not handled.** `TZ_OFFSET_HOURS` is a fixed +10 (Sydney AEST). Cron timing is
  what actually controls the wall-clock schedule.
- **Pre-filter is intentionally generous.** The keyword list is a coarse recall filter;
  the LLM makes the final relevance decision (and can `skip` anything off-topic).
- **Dedup is URL-exact + headline-similarity (Jaccard ≥ 0.65).** Catches both exact
  reposts and near-duplicate headlines across sources.

## License

MIT — see [LICENSE](LICENSE).
