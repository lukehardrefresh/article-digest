#!/usr/bin/env python3
"""
Article Digest — pulls recent articles from 16 sources, summarizes with LLM,
maintains state for dedup, outputs an additive HTML index.

Run:
    python scripts/digest.py

Configuration via environment variables (all optional, sensible defaults):
    DIGEST_OUTPUT_DIR    Where the HTML + state live (default: ../output relative to this script)
    DIGEST_DAYS_WINDOW   Article recency window in days (default: 7)
    DIGEST_BATCH_SIZE    Articles per LLM call (default: 12)
    PAGE_READER_CMD      Command template used as a fallback for Cloudflare-protected feeds.
                         The literal token {url} is replaced with the feed URL. If unset,
                         protected sources are skipped with a warning.
                         Example:  "node page_reader.js {url}"

Original design: chat.z.ai project (digest-001, digest-002).
Portable refactor for public repo.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------- Paths ----------
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = (SCRIPT_DIR.parent / "output").resolve()
OUTPUT_DIR = Path(os.environ.get("DIGEST_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)).resolve()
STATE_FILE = OUTPUT_DIR / "state.json"
HTML_OUTPUT = OUTPUT_DIR / "article_digest.html"
LOG_FILE = OUTPUT_DIR / "digest.log"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Config ----------
DAYS_WINDOW = int(os.environ.get("DIGEST_DAYS_WINDOW", "7"))
BATCH_SIZE = int(os.environ.get("DIGEST_BATCH_SIZE", "12"))
TZ_OFFSET_HOURS = 10  # Sydney AEST = UTC+10 (no DST handling for simplicity)
PAGE_READER_CMD = os.environ.get("PAGE_READER_CMD", "").strip()

# Topic keywords — at least ONE must appear in title or description for the article to be a candidate.
# This is a pre-filter; the LLM does the final relevance decision.
TOPIC_KEYWORDS = [
    # AI / ML
    "ai", "a.i.", "artificial intelligence", "machine learning", "llm", "gpt", "chatbot", "openai",
    "anthropic", "claude", "gemini", "midjourney", "stable diffusion", "deepmind", "neural",
    "generative", "model training", "fine-tune", "fine tune", "agent", "agentic",
    # Tech & finance
    "startup", "venture", "vc ", "funding", "raised", "valuation", "ipo", "spac", "earnings",
    "fintech", "crypto", "bitcoin", "ethereum", "blockchain", "stablecoin", "token", "defi",
    "market", "stock", "investor", "private equity", "saas", "valuation",
    # Regulation
    "regulation", "regulator", "regulate", "antitrust", "competition", "ftc", "doj", "sec ",
    "eu ", "europe", "european union", "congress", "senate", "lawmaker", "legislation",
    "bill", "policy", "court", "lawsuit", "settlement", "fine", "penalty", "ban",
    "data protection", "privacy", "gdpr", "ai act", "section 230", "fcc", "ofcom",
    "cma", "dma", "dsa",
    # Business
    "ceo", "cfo", "cto", "founder", "executive", "layoffs", "fired", "resign",
    "acquisition", "merger", "m&a", "deal", "partnership", "joint venture",
    "revenue", "profit", "loss", "guidance", "outlook", "earnings",
    "apple", "microsoft", "google", "alphabet", "amazon", "meta", "facebook",
    "nvidia", "tesla", "bytedance", "tiktok", "openai", "anthropic", "mistral",
    "deepseek", "x.ai", "xai", "perplexity", "hugging face", "scale ai",
    # Emerging tech
    "quantum", "biotech", "crispr", "fusion", "climate tech", "carbon", "battery",
    "robot", "robotics", "drone", "autonomous", "self-driving", "ev ", "spacex",
    "rocket", "mars", "satellite", "starlink",
    # Security
    "hack", "breach", "vulnerability", "exploit", "ransomware", "malware", "phishing",
    "cybersecurity", "infosec", "surveillance", "nsa", "cia", "fbi",
    # Australia / AU-specific (region tag trigger, but also used as keyword so AU articles pass filter)
    "australia", "australian", "nbn", "accc", "asic", "apra", "rochimp",
    "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
    "outage", "hypervisor", "outsourcing", "accenture", "commonwealth bank", "cba",
    "telstra", "optus", "tpg", "vocus", "nextdc", " NEXTDC", "macquarie",
]

# Skip-title patterns — articles whose titles match these regexes are dropped.
SKIP_TITLE_PATTERNS = [
    r"\bpromo code", r"\bcoupon", r"\bdeals?\b", r"\bsale\b", r"\bdiscount",
    r"\bbest .*\b(2026|2025)\b", r"^\d+ best", r"best .*(laptop|headphone|tablet|vacuum|coffee|fan|gaming|webcam|router|phone|earbud|boots|speaker|monitor|keyboard|mouse)",
    r"vs\.?\s", r"review:", r"review —",
    r"^how to (watch|stream|use|disable|set up|clean|fix)",
    r"\btickets?\b", r"\bstreaming\b.*\bguide", r"\bwhere to watch",
    r"^\d+ (shows|movies|books|anime|games|songs|gifts|things)",
    r"what to (watch|read|buy|stream)",
    r"reddit thread|tiktok trend|instagram reel",
    r"\bhoroscope", r"\bastrology", r"\brecipe",
    r"best (anime|tv show|movie|streaming|book|game|podcast|gift)",
    r"should you (buy|watch|stream|play)",
]

SKIP_TITLE_RE = re.compile("|".join(SKIP_TITLE_PATTERNS), re.IGNORECASE)

SOURCES = [
    {"name": "ADMS Centre",         "short": "adms",     "rss": "https://www.admscentre.org.au/feed/",   "site": "https://www.admscentre.org.au/"},
    {"name": "Where's Your Ed At",  "short": "ed",       "rss": "https://www.wheresyoured.at/feed/",     "site": "https://www.wheresyoured.at/"},
    {"name": "Software Crisis",     "short": "swcrisis", "rss": "https://softwarecrisis.dev/index.xml",  "site": "https://softwarecrisis.dev/"},
    {"name": "Tech Policy Press",   "short": "tpp",      "rss": "https://techpolicy.press/rss/feed.xml", "site": "https://www.techpolicy.press/"},
    {"name": "WIRED",               "short": "wired",    "rss": "https://www.wired.com/feed/rss",        "site": "https://www.wired.com/"},
    {"name": "FT Alphaville",       "short": "ftav",     "rss": "https://www.ft.com/alphaville?format=rss", "site": "https://www.ft.com/alphaville"},
    {"name": "Adweek",              "short": "adweek",   "rss": "https://www.adweek.com/feed/",          "site": "https://www.adweek.com/"},
    {"name": "404 Media",           "short": "404",      "rss": "https://www.404media.co/rss/",          "site": "https://www.404media.co/"},
    {"name": "TechXplore",          "short": "texplore", "rss": "https://techxplore.com/rss-feed",       "site": "https://techxplore.com/"},
    {"name": "The Decoder",         "short": "decoder",  "rss": "https://the-decoder.com/feed/",         "site": "https://the-decoder.com/"},
    {"name": "Futurism",            "short": "futurism", "rss": "https://futurism.com/feed",             "site": "https://futurism.com/"},
    {"name": "The Next Web",        "short": "tnw",      "rss": "https://thenextweb.com/feed",           "site": "https://thenextweb.com/"},
    {"name": "Pivot to AI",         "short": "pivot",    "rss": "https://pivot-to-ai.com/feed",          "via_page_reader": True, "site": "https://pivot-to-ai.com/"},
    {"name": "Paul Kedrosky",       "short": "kedrosky", "rss": "https://paulkedrosky.com/feed/",        "site": "https://paulkedrosky.com/"},
    {"name": "Morning Brew",        "short": "mbrew",    "rss": "https://www.morningbrew.com/feed",      "via_page_reader": True, "site": "https://www.morningbrew.com/"},
    {"name": "IT News (AU)",        "short": "itnews",   "rss": "https://www.itnews.com.au/rss/rss.ashx", "site": "https://www.itnews.com.au/"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ---------- Logging ----------
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ---------- State ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "first_run": datetime.now(timezone.utc).isoformat(),
        "last_refresh": None,
        "seen_urls": [],
        "seen_headlines": [],   # list of {headline, url}
        "refreshes": [],
    }

def save_state(state):
    state["last_refresh"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# ---------- RSS fetching ----------
def fetch_rss_via_requests(url):
    """Direct HTTP fetch (works for most sources)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            return None, "no entries"
        return parsed, None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:80]}"

def fetch_rss_via_page_reader(url):
    """Fallback for Cloudflare-protected feeds. Calls an external command (configured via
    PAGE_READER_CMD) that returns rendered page HTML on stdout. The token {url} in the
    command is replaced with the feed URL. If PAGE_READER_CMD is not set, this fallback
    is disabled and the source is skipped."""
    if not PAGE_READER_CMD:
        return None, "page_reader fallback disabled (set PAGE_READER_CMD to enable)"
    try:
        # Substitute the URL token. Use a shell so users can pass full command strings,
        # e.g.  node page_reader.js {url}
        cmd = PAGE_READER_CMD.replace("{url}", shlex.quote(url))
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60, check=False)
        html = result.stdout if result.returncode == 0 else ""
        if not html:
            return None, f"page_reader returned no stdout (rc={result.returncode}): {result.stderr[:120]}"
        soup = BeautifulSoup(html, "html.parser")
        entries = []
        for div in soup.find_all("div"):
            h3 = div.find("h3")
            a_tags = div.find_all("a", href=True)
            time_tag = div.find("time")
            p = div.find("p")
            if not h3 or not a_tags:
                continue
            title = h3.get_text(strip=True)
            if not title:
                a_in_h3 = h3.find("a")
                if a_in_h3:
                    title = a_in_h3.get_text(strip=True)
            url_found = None
            for a in a_tags:
                href = a["href"].strip()
                if href.startswith("http"):
                    url_found = href
                    break
            if not url_found:
                continue
            if not title:
                slug = url_found.rstrip("/").split("/")[-1]
                slug = slug.split("?")[0].split("#")[0]
                title = slug.replace("-", " ").replace("_", " ").strip()
                title = " ".join(w.capitalize() for w in title.split() if w)
            published = time_tag.get_text(strip=True) if time_tag else ""
            description = p.get_text(strip=True) if p else ""
            entries.append({
                "title": title,
                "link": url_found,
                "description": description,
                "published": published,
                "published_parsed": _parse_published(published),
            })
        if not entries:
            return None, "no entries parsed from page_reader HTML"
        return {"entries": entries}, None
    except Exception as e:
        return None, f"page_reader error: {type(e).__name__}: {str(e)[:120]}"

def _parse_published(s):
    """Parse a date string into a datetime."""
    if not s:
        return None
    for fmt in ["%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None

def fetch_rss(source):
    if source.get("via_page_reader"):
        return fetch_rss_via_page_reader(source["rss"])
    return fetch_rss_via_requests(source["rss"])

# ---------- Date filtering ----------
def is_recent(published_str=None, published_parsed=None, days=DAYS_WINDOW):
    """Return True if published is within last `days` days. If unparseable, return True (include)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if published_parsed:
        try:
            if hasattr(published_parsed, "tm_year"):
                dt = datetime(published_parsed.tm_year, published_parsed.tm_mon,
                              published_parsed.tm_mday, published_parsed.tm_hour,
                              published_parsed.tm_min, published_parsed.tm_sec,
                              tzinfo=timezone.utc)
                return dt >= cutoff
            # datetime instance
            if published_parsed.tzinfo is None:
                published_parsed = published_parsed.replace(tzinfo=timezone.utc)
            return published_parsed >= cutoff
        except Exception:
            pass
    if not published_str:
        return True
    for fmt in ["%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(published_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= cutoff
        except ValueError:
            continue
    return True  # if no date format matches, include (safer for sources without dates)

# ---------- Dedup ----------
def normalize_url(u):
    """Strip query params and fragments for URL matching."""
    try:
        p = urlparse(u)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()
    except Exception:
        return u.lower().rstrip("/")

def normalize_headline(h):
    h = h.lower()
    h = re.sub(r"[^\w\s]", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h

def headline_similarity(a, b):
    """Jaccard similarity on word sets."""
    wa, wb = set(normalize_headline(a).split()), set(normalize_headline(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def is_duplicate(article, state, threshold=0.65):
    """Return True if article URL or headline is a near-duplicate of seen items."""
    norm_url = normalize_url(article["url"])
    for s in state["seen_urls"]:
        if normalize_url(s) == norm_url:
            return True
    for s in state["seen_headlines"]:
        if headline_similarity(article["title"], s["headline"]) >= threshold:
            return True
    return False

# ---------- Pre-filter (keyword based, drops obviously off-topic articles) ----------
def passes_keyword_filter(article):
    title = article.get("title", "")
    desc = article.get("description", "")
    text = (title + " " + desc).lower()
    if SKIP_TITLE_RE.search(title):
        return False
    for kw in TOPIC_KEYWORDS:
        kw_low = kw.lower().strip()
        if not kw_low:
            continue
        if len(kw_low) <= 4:
            if re.search(r"\b" + re.escape(kw_low) + r"\b", text):
                return True
        else:
            if kw_low in text:
                return True
    return False

# ---------- LLM summarization + tagging ----------
SYSTEM_PROMPT = """You are a senior analyst curating a weekly tech & policy digest.
For each article, you produce:
1. A 1-2 sentence summary (max 50 words). Concise, fact-dense, no marketing fluff.
2. Topic tags from this fixed set: AI, REG, FIN, BIZ, EMG, SEC, AUS
   - AI = artificial intelligence, ML, LLMs, agents, AI industry
   - REG = regulation, antitrust, privacy, policy, government
   - FIN = finance, fintech, markets, deals, funding, crypto
   - BIZ = business, M&A, leadership, strategy, product launches
   - EMG = emerging tech, biotech, climate tech, space, robotics
   - SEC = security, privacy breaches, surveillance, infosec
   - AUS = primarily about Australia or Australian companies/policy/people (use even if other tags apply; region marker, not a topic)
   Pick 1-3 tags from AI/REG/FIN/BIZ/EMG/SEC, and ADD AUS if the article is primarily Australian. Max 4 tags total.
   If article doesn't fit any of AI/REG/FIN/BIZ/EMG/SEC, set SKIP=true.

Respond as a JSON array ONLY. No markdown fences, no prose before or after.
Object shape: {"index": <int>, "tags": ["AI","REG"], "summary": "...", "skip": false}

Skip articles that are clearly off-topic (lifestyle, recipes, celebrity gossip, pure product reviews with no business/policy angle, sports)."""

def make_user_prompt(articles):
    lines = ["Summarize each article. Output JSON array only, no markdown fences.\n"]
    for i, a in enumerate(articles):
        lines.append(f"[{i}] TITLE: {a['title']}")
        if a.get("description"):
            lines.append(f"    DESC: {a['description'][:400]}")
        if a.get("source"):
            lines.append(f"    SRC: {a['source']}")
        lines.append("")
    return "\n".join(lines)

def call_llm_batch(articles):
    """Call the Node.js LLM helper. Returns list of {index, tags, summary, skip} or None."""
    if not articles:
        return []
    in_path = OUTPUT_DIR / "llm_in.json"
    out_path = OUTPUT_DIR / "llm_out.json"
    in_path.write_text(json.dumps([
        {"title": a["title"], "description": a["description"], "source": a["source_name"]}
        for a in articles
    ], ensure_ascii=False), encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(SCRIPT_DIR / "llm_batch.js"), str(in_path), str(out_path)],
            capture_output=True, text=True, timeout=180, check=False
        )
        if result.returncode != 0:
            log(f"  llm_batch.js failed: {result.stderr[:200]}")
            return None
        if not out_path.exists():
            log(f"  llm_out.json missing")
            return None
        return json.loads(out_path.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        log("  llm_batch.js timed out")
        return None
    except Exception as e:
        log(f"  llm_batch.js error: {type(e).__name__}: {str(e)[:100]}")
        return None

# ---------- HTML output ----------
def render_html(state):
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = sum(len(r["articles"]) for r in state["refreshes"])

    src_counts = {}
    for r in state["refreshes"]:
        for a in r["articles"]:
            src_counts[a["source_short"]] = src_counts.get(a["source_short"], 0) + 1

    refreshes_html = []
    for r in sorted(state["refreshes"], key=lambda x: x["date"], reverse=True):
        date_str = r["date"]
        n = len(r["articles"])
        if n == 0:
            continue
        articles_html = []
        for a in sorted(r["articles"], key=lambda x: (x["source_short"], x.get("published", ""))):
            tags_html = "".join(f'<span class="tag tag-{t.lower()}">{t}</span>' for t in a.get("tags", []))
            pub = a.get("published", "")
            pub_short = pub[:10] if pub else ""
            articles_html.append(f"""
            <article class="entry" data-source="{a['source_short']}" data-tags="{','.join(a.get('tags',[]))}" data-date="{date_str}">
              <div class="entry-head">
                <a class="entry-title" href="{a['url']}" target="_blank" rel="noopener">{a['title']}</a>
                <div class="entry-meta">
                  <span class="src-badge src-{a['source_short']}">{a['source_name']}</span>
                  {f'<span class="pub-date">{pub_short}</span>' if pub_short else ''}
                  <span class="tags">{tags_html}</span>
                </div>
              </div>
              <p class="entry-summary">{a.get('summary','')}</p>
            </article>""")
        refreshes_html.append(f"""
        <section class="refresh-group" data-date="{date_str}">
          <h2 class="refresh-header">{date_str} <span class="count">({n} new)</span></h2>
          <div class="entries">
            {''.join(articles_html)}
          </div>
        </section>""")

    sources_filter_html = "".join(
        f'<label class="filter-chip"><input type="checkbox" data-filter-source="{s["short"]}" checked> <span class="src-badge src-{s["short"]}">{s["name"]}</span> <span class="count">({src_counts.get(s["short"],0)})</span></label>'
        for s in SOURCES
    )

    tag_filter_html = "".join(
        f'<label class="filter-chip"><input type="checkbox" data-filter-tag="{t}" checked> <span class="tag tag-{t.lower()}">{t}</span></label>'
        for t in ["AI", "REG", "FIN", "BIZ", "EMG", "SEC", "AUS"]
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tech & Policy Digest — additive index</title>
<style>
  :root {{
    --bg: #fafaf7;
    --card: #ffffff;
    --text: #1a1a1a;
    --muted: #666;
    --border: #e3e3dc;
    --accent: #2563eb;
    --accent-bg: #eff6ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', 'Helvetica Neue', Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px 96px; }}
  header.digest-header {{
    border-bottom: 1px solid var(--border);
    padding-bottom: 24px;
    margin-bottom: 32px;
  }}
  header.digest-header h1 {{
    font-size: 28px;
    margin: 0 0 6px 0;
    letter-spacing: -0.02em;
  }}
  header.digest-header .sub {{
    color: var(--muted);
    font-size: 14px;
  }}
  header.digest-header .stats {{
    display: flex; gap: 24px; margin-top: 14px;
    font-size: 13px; color: var(--muted);
  }}
  header.digest-header .stats strong {{ color: var(--text); }}

  .filters {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 24px;
  }}
  .filters h3 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 8px 0; }}
  .filter-chip {{
    display: inline-flex; align-items: center; gap: 4px;
    margin: 3px 8px 3px 0; font-size: 12px; cursor: pointer;
  }}
  .filter-chip input {{ margin: 0; }}

  .refresh-group {{ margin-bottom: 36px; }}
  .refresh-header {{
    font-size: 18px; margin: 0 0 12px 0;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--accent);
    letter-spacing: -0.01em;
  }}
  .refresh-header .count {{ color: var(--muted); font-size: 14px; font-weight: normal; }}

  .entry {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 16px;
    margin-bottom: 8px;
    transition: border-color 0.15s;
  }}
  .entry:hover {{ border-color: var(--accent); }}
  .entry-head {{ margin-bottom: 4px; }}
  .entry-title {{
    font-size: 15px; font-weight: 600; color: var(--text);
    text-decoration: none;
  }}
  .entry-title:hover {{ color: var(--accent); text-decoration: underline; }}
  .entry-meta {{
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    font-size: 11px; color: var(--muted);
    margin-top: 4px;
  }}
  .src-badge {{
    display: inline-block;
    padding: 1px 7px;
    border-radius: 3px;
    font-size: 10px; font-weight: 600;
    background: #f0eee6;
    color: #4a4a4a;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .src-wired    {{ background: #000; color: #fff; }}
  .src-ftav     {{ background: #fff1e5; color: #993201; }}
  .src-404      {{ background: #fef3c7; color: #92400e; }}
  .src-tpp      {{ background: #fce7f3; color: #831843; }}
  .src-adweek   {{ background: #e0e7ff; color: #3730a3; }}
  .src-texplore {{ background: #dcfce7; color: #166534; }}
  .src-decoder  {{ background: #cffafe; color: #155e75; }}
  .src-futurism {{ background: #fee2e2; color: #991b1b; }}
  .src-tnw      {{ background: #fae8ff; color: #701a75; }}
  .src-pivot    {{ background: #f3e8ff; color: #581c87; }}
  .src-adms     {{ background: #dbeafe; color: #1e40af; }}
  .src-ed       {{ background: #fed7aa; color: #9a3412; }}
  .src-swcrisis {{ background: #d1fae5; color: #065f46; }}
  .src-kedrosky {{ background: #e5e7eb; color: #374151; }}
  .src-mbrew    {{ background: #fef9c3; color: #854d0e; }}
  .src-itnews   {{ background: #dbeafe; color: #1e3a8a; border: 1px solid #1e40af; }}

  .pub-date {{ font-variant-numeric: tabular-nums; }}
  .tag {{
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 9px; font-weight: 700;
    margin-left: 2px;
    letter-spacing: 0.05em;
  }}
  .tag-ai  {{ background: #dbeafe; color: #1e40af; }}
  .tag-reg {{ background: #fee2e2; color: #991b1b; }}
  .tag-fin {{ background: #dcfce7; color: #166534; }}
  .tag-biz {{ background: #fef3c7; color: #92400e; }}
  .tag-emg {{ background: #fae8ff; color: #701a75; }}
  .tag-sec {{ background: #f1f5f9; color: #475569; }}
  .tag-aus {{ background: #fca5a5; color: #7f1d1d; border: 1px solid #991b1b; }}

  .entry-summary {{
    font-size: 14px;
    color: #333;
    margin: 6px 0 0 0;
    line-height: 1.5;
  }}

  .hidden {{ display: none !important; }}

  footer.digest-footer {{
    margin-top: 48px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-size: 12px;
    color: var(--muted);
  }}

  @media (max-width: 640px) {{
    .container {{ padding: 16px; }}
    header.digest-header h1 {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
  <div class="container">
    <header class="digest-header">
      <h1>Tech &amp; Policy Article Digest</h1>
      <div class="sub">AI · regulation · finance · business · emerging tech — curated from {len(SOURCES)} sources, refreshed 2× weekly</div>
      <div class="stats">
        <span>Last refresh: <strong>{today}</strong></span>
        <span>Total articles: <strong>{total}</strong></span>
        <span>Refreshes: <strong>{len(state['refreshes'])}</strong></span>
        <span>Sources tracked: <strong>{len(SOURCES)}</strong></span>
      </div>
    </header>

    <div class="filters">
      <h3>Sources</h3>
      {sources_filter_html}
      <h3 style="margin-top:12px;">Tags</h3>
      {tag_filter_html}
    </div>

    {''.join(refreshes_html) if refreshes_html else '<p style="color:var(--muted);">No articles yet.</p>'}

    <footer class="digest-footer">
      <p>Articles link to their original source. Summaries generated by AI and may contain errors — verify with the original source before relying on details.</p>
    </footer>
  </div>

<script>
(function() {{
  const sourceCbs = document.querySelectorAll('input[data-filter-source]');
  const tagCbs = document.querySelectorAll('input[data-filter-tag]');
  function apply() {{
    const activeSources = new Set([...sourceCbs].filter(c => c.checked).map(c => c.dataset.filterSource));
    const activeTags = new Set([...tagCbs].filter(c => c.checked).map(c => c.dataset.filterTag));
    document.querySelectorAll('.entry').forEach(el => {{
      const src = el.dataset.source;
      const tags = (el.dataset.tags || '').split(',').filter(Boolean);
      const srcOk = activeSources.has(src);
      const tagOk = tags.length === 0 || tags.some(t => activeTags.has(t));
      el.classList.toggle('hidden', !(srcOk && tagOk));
    }});
    document.querySelectorAll('.refresh-group').forEach(g => {{
      const visible = g.querySelectorAll('.entry:not(.hidden)').length;
      g.style.display = visible === 0 ? 'none' : '';
    }});
  }}
  sourceCbs.forEach(c => c.addEventListener('change', apply));
  tagCbs.forEach(c => c.addEventListener('change', apply));
}})();
</script>
</body>
</html>"""
    HTML_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUTPUT.write_text(html, encoding="utf-8")
    log(f"HTML written to {HTML_OUTPUT}")

# ---------- Main pipeline ----------
def main():
    log(f"=== Digest run start ===")
    state = load_state()
    log(f"State loaded: {len(state['seen_urls'])} seen URLs, {len(state['refreshes'])} prior refreshes")

    today = datetime.now().strftime("%Y-%m-%d")
    refresh = None
    for r in state["refreshes"]:
        if r["date"] == today:
            refresh = r
            break
    if refresh is None:
        refresh = {"date": today, "articles": []}
        state["refreshes"].append(refresh)

    # Step 1: fetch RSS for all sources
    all_candidates = []
    for src in SOURCES:
        log(f"Fetching {src['name']}...")
        parsed, err = fetch_rss(src)
        if err:
            log(f"  ERR: {err}")
            continue
        entries = parsed.entries if hasattr(parsed, "entries") else parsed.get("entries", [])
        log(f"  got {len(entries)} entries")
        recent_count = 0
        for e in entries:
            if hasattr(e, "title"):
                title = e.title or ""
                link = e.link or ""
                desc = e.description if hasattr(e, "description") else ""
                pub = e.published if hasattr(e, "published") else ""
                pub_parsed = e.published_parsed if hasattr(e, "published_parsed") else None
            else:
                title = e.get("title", "")
                link = e.get("link", "")
                desc = e.get("description", "")
                pub = e.get("published", "")
                pub_parsed = e.get("published_parsed")
            if not title or not link:
                continue
            if not is_recent(pub, pub_parsed):
                continue
            recent_count += 1
            all_candidates.append({
                "title": title,
                "url": link,
                "description": desc,
                "published": pub,
                "source_name": src["name"],
                "source_short": src["short"],
            })
        log(f"  recent ({DAYS_WINDOW}d): {recent_count}")

    log(f"Total candidates: {len(all_candidates)}")

    # Step 2: dedup against seen + keyword pre-filter
    new_articles = []
    prefilter_dropped = 0
    for a in all_candidates:
        if is_duplicate(a, state):
            continue
        if not passes_keyword_filter(a):
            prefilter_dropped += 1
            continue
        new_articles.append(a)
    log(f"After dedup + keyword pre-filter: {len(new_articles)} new articles (dropped {prefilter_dropped} off-topic)")

    # Step 3: LLM summarization in batches
    failed_batches = 0
    for i in range(0, len(new_articles), BATCH_SIZE):
        batch = new_articles[i:i+BATCH_SIZE]
        log(f"LLM batch {i//BATCH_SIZE + 1}/{(len(new_articles)+BATCH_SIZE-1)//BATCH_SIZE} ({len(batch)} articles)")
        results = call_llm_batch(batch)
        if results is None:
            log(f"  LLM failed; using fallback summaries")
            for j, a in enumerate(batch):
                a["tags"] = ["BIZ"]
                a["summary"] = (a["description"][:200] if a.get("description") else a["title"]) + "."
                a["_failed"] = True
            failed_batches += 1
            time.sleep(8)
            continue
        results_by_idx = {}
        any_failed = False
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict) and "index" in item:
                    results_by_idx[item["index"]] = item
                    if item.get("_failed"):
                        any_failed = True
        for j, a in enumerate(batch):
            r = results_by_idx.get(j)
            if r is None:
                a["tags"] = ["BIZ"]
                a["summary"] = (a["description"][:200] if a.get("description") else a["title"]) + "."
                a["_failed"] = True
            else:
                a["tags"] = r.get("tags", []) or ["BIZ"]
                a["summary"] = r.get("summary", "") or (a["description"][:200] if a.get("description") else a["title"]) + "."
                if r.get("_failed"):
                    a["_failed"] = True
                    any_failed = True
                if r.get("skip", False):
                    a["_skip"] = True
        if any_failed:
            failed_batches += 1
            time.sleep(8)
        else:
            time.sleep(3)
    log(f"LLM phase done. Failed batches: {failed_batches}/{(len(new_articles)+BATCH_SIZE-1)//BATCH_SIZE}")

    # Step 3b: Retry failed articles in smaller batches
    failed_articles = [a for a in new_articles if a.get("_failed") and not a.get("_skip")]
    if failed_articles:
        log(f"Retrying {len(failed_articles)} failed articles in smaller batches (3 at a time)")
        retry_batch_size = 3
        for i in range(0, len(failed_articles), retry_batch_size):
            batch = failed_articles[i:i+retry_batch_size]
            log(f"  retry batch {i//retry_batch_size + 1} ({len(batch)} articles)")
            for a in batch:
                a.pop("_failed", None)
            results = call_llm_batch(batch)
            if results is None:
                log(f"    retry failed; keeping fallback summaries")
                for a in batch:
                    a["_failed"] = True
                time.sleep(10)
                continue
            results_by_idx = {item["index"]: item for item in results if isinstance(item, dict) and "index" in item}
            for j, a in enumerate(batch):
                r = results_by_idx.get(j)
                if r and not r.get("_failed"):
                    a["tags"] = r.get("tags", []) or ["BIZ"]
                    a["summary"] = r.get("summary", "") or a["summary"]
                    if r.get("skip", False):
                        a["_skip"] = True
                    a.pop("_failed", None)
                else:
                    a["_failed"] = True
            time.sleep(5)
        still_failed = sum(1 for a in new_articles if a.get("_failed"))
        log(f"Retry done. Still failed: {still_failed}")

    # Filter out skipped articles
    kept = [a for a in new_articles if not a.get("_skip")]
    log(f"Kept after LLM filtering: {len(kept)} (skipped {len(new_articles)-len(kept)})")

    # Step 4: update state (APPEND to today's refresh, don't replace — bugfix from digest-001)
    refresh["articles"].extend(kept)
    for a in kept:
        state["seen_urls"].append(a["url"])
        state["seen_headlines"].append({"headline": a["title"], "url": a["url"]})

    # Consolidate refreshes that share the same date (defensive — in case prior runs created duplicates)
    by_date = defaultdict(list)
    for r in state["refreshes"]:
        by_date[r["date"]].extend(r["articles"])
    state["refreshes"] = [{"date": d, "articles": arts} for d, arts in by_date.items()]

    save_state(state)

    # Step 5: render HTML
    render_html(state)

    log(f"=== Done. Added {len(kept)} new articles. Total tracked: {len(state['seen_urls'])} ===")

if __name__ == "__main__":
    main()
