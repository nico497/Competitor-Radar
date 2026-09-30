#!/usr/bin/env python3
"""
Competitor Radar
----------------
Watches what your competitors publish on their websites, classifies every new
page (type, buyer stage, topic, likely search), and writes a plain-English
breakdown of each competitor's content strategy, the plays they share, and the
gaps nobody covers.

Usage
  python radar.py            # full run: discover -> classify -> analyse -> save
  python radar.py --check    # test your sites only (no API key, no writes)

Environment
  ANTHROPIC_API_KEY   required for a full run
  RADAR_CONFIG        optional path to config (default: config.yaml)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("RADAR_CONFIG", ROOT / "config.yaml"))
DATA_PATH = ROOT / "docs" / "data.json"      # what the dashboard reads
STATE_PATH = ROOT / "state" / "state.json"   # every page seen, with its classification
API_URL = "https://api.anthropic.com/v1/messages"


# ---------------------------------------------------------------- helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime | None) -> str | None:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log(msg: str) -> None:
    print(msg, flush=True)


def short_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        hint = {403: "blocked by the site (403)", 404: "URL not found (404)",
                429: "rate limited (429)"}.get(code, f"HTTP {code}")
        return hint
    if isinstance(e, requests.Timeout):
        return "timed out"
    if isinstance(e, requests.ConnectionError):
        return "could not connect"
    return str(e)[:200]


TRACKING = re.compile(r"^(utm_|mc_|hsa_|_hs|fbclid|gclid|ref$|source$)", re.I)


def normalize_url(u: str) -> str:
    p = urlparse(u.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING.match(k)])
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), p.path or "/", "", query, ""))


def url_key(u: str) -> str:
    """Identity for de-duplication: ignores scheme, www and trailing slash."""
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def slug_title(url: str) -> str:
    seg = [s for s in urlparse(url).path.split("/") if s]
    words = re.sub(r"[-_]+", " ", seg[-1] if seg else url)
    words = re.sub(r"\.(html?|php|aspx?)$", "", words)
    return words[:1].upper() + words[1:]


def strip_html(s: str, limit: int = 400) -> str:
    text = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


class Fetcher:
    def __init__(self, user_agent: str):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def get(self, url: str) -> requests.Response:
        r = self.s.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r



def discover_feed(src: dict, f: Fetcher) -> list[dict]:
    r = f.get(src["feed"])
    parsed = feedparser.parse(r.content)
    if not parsed.entries:
        raise ValueError("no entries: not a readable RSS/Atom feed")
    items = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        items.append({
            "url": normalize_url(urljoin(r.url, link)),
            "title": strip_html(e.get("title", ""), 300) or slug_title(link),
            "published": pub,
            "snippet": strip_html(e.get("summary", ""), 1500),
            # Full text when the feed carries it (Substack, WordPress): fallback if the page blocks us
            "feed_text": strip_html((e.get("content") or [{}])[0].get("value", ""), 60000),
        })
    return items


def discover_page(src: dict, f: Fetcher) -> list[dict]:
    r = f.get(src["page"])
    soup = BeautifulSoup(r.text, "html.parser")
    pattern = re.compile(src["link_pattern"])
    listing = url_key(r.url)
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = normalize_url(urljoin(r.url, a["href"]))
        if url_key(url) == listing or not pattern.search(url):
            continue
        text = a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", "")
        # Cards often link twice (image + title): keep the most descriptive text.
        if len(text) > len(found.get(url, "")):
            found[url] = text
        else:
            found.setdefault(url, "")
    return [{"url": u, "title": (t[:300] or slug_title(u)), "published": None, "snippet": ""}
            for u, t in found.items()]


def _xml_children(root: ET.Element, name: str):
    return [el for el in root.iter() if el.tag.split("}")[-1] == name]


def _xml_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if child.tag.split("}")[-1] == name:
            return (child.text or "").strip()
    return None




_NO_FORCED_TOOL: set[str] = set()   # models that reject tool_choice "tool"


def _api_error(r: requests.Response) -> str:
    try:
        return f"Anthropic API {r.status_code}: {r.json()['error']['message']}"
    except Exception:  # noqa: BLE001
        return f"Anthropic API {r.status_code}: {r.text[:200]}"


def call_claude(model: str, system: str, user: str, tool: dict, max_tokens: int = 1500) -> dict:
    """One structured call: the model answers by calling `tool`. Returns the tool input."""
    headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    messages = [{"role": "user", "content": user}]
    nudged = False
    attempt = 0
    while attempt < 6:
        attempt += 1
        forced = model not in _NO_FORCED_TOOL
        body = {
            "model": model, "max_tokens": max_tokens, "messages": messages, "tools": [tool],
            "system": system if forced else f"{system}\n\nRespond only by calling the {tool['name']} tool.",
            "tool_choice": {"type": "tool", "name": tool["name"]} if forced else {"type": "auto"},
        }
        r = requests.post(API_URL, headers=headers, json=body, timeout=180)
        if r.status_code in (429, 500, 502, 503, 529):
            wait = int(r.headers.get("retry-after", 0) or 0) or 10 * attempt
            log(f"    API busy ({r.status_code}), retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 400 and forced and "tool_choice" in r.text:
            # Some models don't support forcing a tool. Ask for it in the prompt instead.
            _NO_FORCED_TOOL.add(model)
            continue
        if r.status_code >= 400:
            raise RuntimeError(_api_error(r))
        content = r.json().get("content", [])
        for block in content:
            if block.get("type") == "tool_use":
                return block["input"]
        if not nudged:
            nudged = True
            messages = messages + [{"role": "assistant", "content": content or "..."},
                                   {"role": "user", "content": f"Please answer by calling the {tool['name']} tool."}]
            continue
        raise RuntimeError("model returned no structured output")
    raise RuntimeError("Anthropic API kept failing, try again later")



STYLE_RULES = """Writing style: Smart Brevity, plain English.
- Easy English. Short sentences. Everyday words. Write so a smart 15-year-old gets it on first read.
- No jargon. If a technical term can't be avoided, explain it in a few words in brackets,
  e.g. "fan-out queries (the extra searches AI tools run behind the scenes)".
- Lead with the point. No warm-up, no "this study shows", no hedging words, no hype.
- Keep numbers exactly as published, but at most two numbers per sentence.
- Respect every word limit."""


def _clean(v) -> str:
    """Strip stray tool-call markup a model sometimes leaves inside a string."""
    v = re.sub(r"</?parameter[^>]*>", " ", str(v or ""))
    return re.sub(r"\s+", " ", v).strip()



def load_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)



# ---------------------------------------------------------------- site discovery

DEFAULT_EXCLUDE = re.compile(
    r"/(tags?|categor(y|ies)|authors?|page/\d+|wp-content|wp-json|feed|search|cart|checkout|"
    r"login|log-in|signin|sign-in|signup|sign-up|register|account|legal|privacy|terms|cookies?|"
    r"careers|jobs|press-kit|thank-you|thanks|unsubscribe)(/|$)"
    r"|/(de|fr|es|it|pt|nl|ja|ko|zh|sv|da|pl|tr|ru|id|pt-br|es-es|es-mx|fr-fr|de-de|en-gb|en-au|zh-cn|ja-jp)(/|$)"
    r"|\.(pdf|jpe?g|png|gif|webp|svg|xml|zip|mp4)$",
    re.I)

PREFERRED_CHILD = re.compile(r"post|blog|article|resource|guide|learn|insight|news|page|compare|vs|alternative|glossary|case|customer", re.I)
SKIP_CHILD = re.compile(r"image|video|author|tag|categor|product_cat|attachment", re.I)


def discover_sitemap(src: dict, f: Fetcher) -> list[dict]:
    """Walk a sitemap (or sitemap index) and return every page URL with its lastmod date."""
    include = re.compile(src["link_pattern"]) if src.get("link_pattern") else None
    child_pat = re.compile(src["sitemap_pattern"]) if src.get("sitemap_pattern") else None
    queue, entries, fetched = [src["sitemap"]], {}, 0
    while queue and fetched < 25:
        url = queue.pop(0)
        root = ET.fromstring(f.get(url).content)
        fetched += 1
        if root.tag.split("}")[-1] == "sitemapindex":
            kids = [k for k in (_xml_text(s, "loc") for s in _xml_children(root, "sitemap")) if k]
            kids = [k for k in kids if not SKIP_CHILD.search(k) and (not child_pat or child_pat.search(k))]
            kids.sort(key=lambda k: 0 if PREFERRED_CHILD.search(k) else 1)
            queue += kids
            continue
        for u in _xml_children(root, "url"):
            loc = _xml_text(u, "loc")
            if not loc or (include and not include.search(loc)):
                continue
            entries[normalize_url(loc)] = parse_iso(_xml_text(u, "lastmod"))
        if len(entries) > 20000:
            break
    return [{"url": u, "title": slug_title(u), "published": d, "snippet": ""} for u, d in entries.items()]


def discover(src: dict, f: Fetcher) -> tuple[str, str, list[dict]]:
    """Returns (method, url used, items). Auto-detects a sitemap or feed from `site`."""
    if src.get("sitemap"):
        return "sitemap", src["sitemap"], discover_sitemap(src, f)
    if src.get("feed"):
        return "feed", src["feed"], discover_feed(src, f)
    if src.get("page"):
        if not src.get("link_pattern"):
            raise ValueError("'page' needs a link_pattern")
        return "page", src["page"], discover_page(src, f)
    site = src.get("site")
    if not site:
        raise ValueError("needs a site (or a sitemap / feed / page)")
    base = site.rstrip("/")
    candidates = []
    try:
        robots = f.get(base + "/robots.txt").text
        candidates += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except Exception:  # noqa: BLE001
        pass
    candidates += [base + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]
    tried = []
    for sm in dict.fromkeys(candidates):
        try:
            items = discover_sitemap({**src, "sitemap": sm}, f)
            if items:
                return "sitemap", sm, items
        except Exception as e:  # noqa: BLE001
            tried.append(f"{sm}: {short_error(e)}")
    for fd in ("/feed", "/blog/feed", "/rss.xml", "/blog/rss.xml", "/feed.xml", "/atom.xml", "/index.xml"):
        try:
            items = discover_feed({"feed": base + fd}, f)
            if items:
                return "feed", base + fd, items
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("no sitemap or feed found. Add a sitemap: or feed: line for this site in config.yaml")


def keep_url(url: str, src: dict, site_host: str) -> bool:
    p = urlparse(url)
    if p.netloc.lower().removeprefix("www.") != site_host:
        return False            # other domains / subdomains (docs., help., app.)
    if p.path.strip("/") == "":
        return False            # homepage
    if src.get("default_excludes", True) and DEFAULT_EXCLUDE.search(p.path):
        return False
    if src.get("include_pattern") and not re.search(src["include_pattern"], url):
        return False
    if src.get("exclude_pattern") and re.search(src["exclude_pattern"], url):
        return False
    return True


def site_host(s: dict, f: Fetcher) -> str:
    """The site's real host, after redirects (e.g. numeralhq.com -> numeral.com)."""
    try:
        return urlparse(f.get(s["site"]).url).netloc.lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return urlparse(s["site"]).netloc.lower().removeprefix("www.")


LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.I)
EDITORIAL = {"blog", "blogs", "resources", "resource", "resource-center", "learn", "insights", "articles",
             "news", "posts", "library", "academy", "hub", "knowledge", "content", "stories", "customers",
             "case-studies", "compare", "vs", "alternatives", "glossary"}


def section_of(url: str) -> str:
    """First meaningful path segment, skipping locale prefixes like /us/en/."""
    parts = [x for x in urlparse(url).path.split("/") if x]
    while parts and LOCALE.match(parts[0]) and len(parts) > 1:
        parts = parts[1:]
    return parts[0].lower() if parts else ""


def programmatic_sections(urls: list[str], min_pages: int) -> dict[str, int]:
    """Big templated sections (calculators, rate lookups, location pages): count them, don't classify each."""
    counts: dict[str, int] = {}
    for u in urls:
        sec = section_of(u)
        counts[sec] = counts.get(sec, 0) + 1
    return {k: v for k, v in counts.items() if v >= min_pages and k and k not in EDITORIAL}


# ---------------------------------------------------------------- reading + classifying

def _ld_dates(soup) -> tuple[datetime | None, datetime | None]:
    """datePublished / dateModified from JSON-LD blocks (incl. @graph)."""
    pub = mod = None

    def walk(x):
        nonlocal pub, mod
        if isinstance(x, dict):
            pub = pub or parse_iso(str(x.get("datePublished") or "")) if x.get("datePublished") else pub
            mod = mod or parse_iso(str(x.get("dateModified") or "")) if x.get("dateModified") else mod
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
    return pub, mod


def read_page(url: str, f: Fetcher) -> dict:
    r = f.get(url)
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    desc = meta("description", "og:description") or ""
    ld_pub, ld_mod = _ld_dates(soup)
    published = parse_iso(meta("article:published_time", "datePublished")) or ld_pub
    modified = parse_iso(meta("article:modified_time", "og:updated_time", "dateModified")) or ld_mod
    heads = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"])][:15]
    if not published:
        tag = soup.find("time", attrs={"datetime": True})
        published = parse_iso(tag["datetime"]) if tag else None
    for t in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "aside", "form"]):
        t.decompose()
    node = max(soup.find_all("article") + soup.find_all("main"), key=lambda n: len(n.get_text()), default=None) or soup.body or soup
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    if not published:
        published = byline_date(text[:1500])
    # Fingerprint of the main text: tells a real edit apart from a date bump.
    fp = hashlib.sha1(re.sub(r"\d", "", text[:30000]).encode("utf-8", "ignore")).hexdigest()[:16]
    return {"title": title, "desc": desc, "published": published, "modified": modified, "heads": heads,
            "text": text[:2500], "words": len(text.split()), "fp": fp}


MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def byline_date(text: str) -> datetime | None:
    """Last resort: a visible date near the top of the article, e.g. 'May 20, 2026' or '20 May 2026'."""
    for m in re.finditer(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2}),? (20\d\d)\b|\b(\d{1,2}) ([A-Z][a-z]{2,8}),? (20\d\d)\b", text):
        mon, day, year = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
        month = MONTHS.get(mon[:3].lower())
        if month:
            try:
                return datetime(int(year), month, int(day), tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def date_kind(p: dict, info: dict, now: datetime) -> None:
    """Label a page 'new', 'updated' or 'unknown', with the date of that event."""
    if not info:
        return
    p["pub"] = iso(info.get("published"))
    p["mod"] = iso(info.get("modified"))
    p["fp"] = info.get("fp")
    p["dates_checked"] = iso(now)
    decide_kind(p, now)


def decide_kind(p: dict, now: datetime) -> None:
    pub, mod = parse_iso(p.get("pub")), parse_iso(p.get("mod"))
    if pub and pub > now + timedelta(days=1):
        pub = None                              # future dates are placeholders
    if mod and mod > now + timedelta(days=1):
        mod = None
    first_seen = parse_iso(p.get("first_seen")) or now
    recent = now - timedelta(days=45)
    if (p.get("kind") == "updated" and p.get("fp_verified")) or (p.get("kind") == "new" and p.get("feed_dated")):
        return                                  # a verified edit, or a feed's own publish date: keep it
    if p.get("bulk_pub") and pub:
        # Dozens of pages stamped with the same publish day: a bulk launch or a date reset.
        p["kind"], p["bulk_type"], p["event"] = "bulk", "launch", iso(min(pub, now))
        return
    if not p.get("baseline", True):
        # Seen for the first time after tracking began. New, unless the page says it's old.
        if pub and pub < first_seen - timedelta(days=14):
            if mod and mod >= first_seen - timedelta(days=14) and not p.get("bulk_mod"):
                p["kind"], p["event"] = "updated", iso(min(mod, now))
            else:
                p["kind"], p["event"] = "unknown", iso(pub)
        else:
            p["kind"], p["event"] = "new", p["first_seen"]
        return
    # Pages that existed before tracking began. Only the page's own dates count:
    # sitemap dates move with every redeploy, so on their own they prove nothing.
    if pub and pub >= recent and (not mod or mod <= pub + timedelta(days=3)):
        p["kind"], p["event"] = "new", iso(min(pub, now))
    elif mod and pub and mod > pub + timedelta(days=3):
        if p.get("bulk_mod"):
            # Dozens of pages "modified" on one day: a site-wide change (template, links, migration).
            p["kind"], p["bulk_type"], p["event"] = "bulk", "edit", iso(min(mod, now))
        else:
            p["kind"], p["event"] = "updated", iso(min(mod, now))
    elif pub:
        p["kind"], p["event"] = ("new" if pub >= recent else "unknown"), iso(min(pub, now))
    else:
        p["kind"], p["event"] = "unknown", (iso(mod) if mod else p.get("date"))


def mark_bulk_dates(pages: dict, now: datetime, min_pages: int = 20) -> dict[str, list[dict]]:
    """Find days when dozens of a site's pages share a publish or modified date. Those are
    bulk events (a launch, a republish, a template change), reported once instead of being
    counted as dozens of individual new or updated pages."""
    groups: dict[tuple, list] = {}
    for p in pages.values():
        if p.get("prog"):
            continue
        for field, tag in (("pub", "launch"), ("mod", "edit")):
            d = parse_iso(p.get(field))
            if d and d <= now + timedelta(days=1):
                groups.setdefault((p["site"], tag, p[field][:10]), []).append(p)
        p["bulk_pub"] = p["bulk_mod"] = False
    events: dict[str, list[dict]] = {}
    for (site, tag, day), ps in groups.items():
        if len(ps) >= min_pages:
            for p in ps:
                p["bulk_pub" if tag == "launch" else "bulk_mod"] = True
            events.setdefault(site, []).append({"type": tag, "day": day, "pages": len(ps)})
    return events


def classify_tool(types: list[list[str]]) -> dict:
    s = {"type": "string"}
    return {
        "name": "classify_page",
        "description": "Classify one page from a competitor's website.",
        "input_schema": {
            "type": "object",
            "properties": {
                "is_content": {"type": "boolean", "description": "False for utility pages: login, pricing tables with no copy, legal, jobs, contact, thank-you, empty tag pages."},
                "type": {"type": "string", "enum": [t[0] for t in types],
                         "description": "; ".join(f"{k} = {label}" for k, label in types)},
                "stage": {"type": "string", "enum": ["learning", "comparing", "buying", "customers"],
                          "description": "Who it's for: learning = early research; comparing = weighing options; buying = ready to choose (pricing, demos, vs pages, case studies); customers = existing users."},
                "topic": {**s, "description": "Main topic, max 5 plain words."},
                "search": {**s, "description": "The Google search this page most likely targets, max 8 words. Empty if none."},
                "why": {**s, "description": "Why they published it, in plain English. One sentence, max 16 words."},
            },
            "required": ["is_content", "type", "stage", "topic", "search", "why"],
        },
    }


CLASSIFY_SYSTEM = """You classify pages from a company's website for a competitor content report.
Judge from the URL, title, headings and opening text. Be decisive. Plain English, respect word limits.
"why" explains the business reason (e.g. "Catches buyers comparing them with a bigger rival."), never restates the title."""


def classify(page: dict, comp: str, cfg: dict) -> dict:
    info = page.get("info") or {}
    user = (f"Company: {comp}\nNiche: {cfg['niche']}\nURL: {page['url']}\n"
            f"Title: {info.get('title') or page.get('title','')}\nDescription: {info.get('desc','')}\n"
            f"Headings: {' | '.join(info.get('heads', []))}\nOpening text: {info.get('text','')[:1800]}")
    out = call_claude(cfg["models"]["classify"], CLASSIFY_SYSTEM, user, classify_tool(cfg["types"]), 400)
    keys = [t[0] for t in cfg["types"]]
    typ = str(out.get("type", "")).strip().lower()
    stage = str(out.get("stage", "")).strip().lower()
    return {
        "is_content": out.get("is_content", True) not in (False, "false", "False"),
        "type": typ if typ in keys else keys[-1],
        "stage": stage if stage in ("learning", "comparing", "buying", "customers") else "learning",
        "topic": _clean(out.get("topic"))[:60],
        "search": _clean(out.get("search"))[:80],
        "why": _clean(out.get("why"))[:200],
    }


# ---------------------------------------------------------------- analysis

ANALYSIS_TOOL = {
    "name": "write_analysis",
    "description": "Write the competitor content report.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "The big picture, max 30 words, two short sentences."},
            "points": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                       "description": "Up to 3 takeaways for the reader, one sentence each, max 22 words. End each with the company names it's about in square brackets, e.g. 'Payloom is targeting rivals' customers. [Payloom]'"},
            "plays": {"type": "array", "items": {"type": "string"},
                      "description": "One line per company in the data: 'Name :: their play, max 16 words :: who it's aimed at, max 6 words'."},
            "shared": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                       "description": "Plays that 2+ companies share: 'Short title :: what they do and what it means for the reader, max 35 words :: Name1, Name2'."},
            "gaps": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                     "description": "Topics or formats nobody (or only one company) covers that the reader could own: 'Short title :: why it's open, max 30 words'."},
        },
        "required": ["summary", "points", "plays", "shared", "gaps"],
    },
}

ANALYSIS_SYSTEM_BASE = """You analyse what a set of companies published on their websites and explain their content strategy to a busy reader.

""" + STYLE_RULES + """

Rules:
- Use only the data provided. Name companies. Use their real counts.
- Describe what they publish and the likely intent. Don't claim what works: there is no traffic or ranking data.
- Small numbers are weak signals. Don't call 1-2 pages a strategy.
- A shared play needs at least 2 companies. A gap is something the reader could own.
- New pages and updates of old pages are different plays: a wave of updates means a refresh push."""


def _split(line: str, n: int) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*::\s*", _clean(line))]
    return (parts + [""] * n)[:n]


def analyse(stats: list[dict], cfg: dict) -> dict:
    you = next((s["name"] for s in stats if s["you"]), None)
    blocks = []
    for s in stats:
        mix = ", ".join(f"{label}: {s['mix'].get(k, 0)}" for k, label in cfg["types"] if s["mix"].get(k))
        stages = ", ".join(f"{k}: {v}" for k, v in s["stages"].items() if v)
        titles = "\n".join(f"    - [{p['type']}, {p.get('kind', 'unknown')}] {p['title']}" for p in s["sample"])
        blocks.append(f"## {s['name']}{' (THE READER)' if s['you'] else ''}\n"
                      f"Pages in last {cfg['window_days']} days: {s['n']} ({s.get('new', 0)} new, {s.get('updated', 0)} updated old pages, rest bulk-dated or undated). " + (f"Previous {cfg['window_days']} days: {s['n_prev']}" if cfg.get("_history_ok") else "(no reliable previous-period count yet)") + "\n"
                      + ("Bulk events (many pages sharing one publish or modified date: a bulk launch, republish or site-wide change, NOT individual articles): "
                         + ", ".join(f"{b['type']}: {b['pages']} pages on {b['day']}" for b in s.get('bulk', [])) + "\n" if s.get("bulk") else "")
                      + (f"Templated/programmatic sections (not in the counts above): " + ", ".join(f"/{x['section']}/ ({x['pages']} pages)" for x in s.get('programmatic', [])) + "\n" if s.get("programmatic") else "")
                      + f"Mix: {mix or 'none'}\nWho it's for: {stages or 'n/a'}\nTop topics: {', '.join(s['topics']) or 'n/a'}\nPages:\n{titles or '    (none)'}")
    reader = (f"The reader is {you}. Compare competitors against them; gaps are openings for {you}."
              if you else "The reader is a company in this market.")
    system = ANALYSIS_SYSTEM_BASE + "\n- " + reader
    user = f"Market: {cfg['niche']}\nReader: {cfg['audience']}\n\n" + "\n\n".join(blocks)
    out = {}
    for attempt in range(2):
        out = call_claude(cfg["models"]["analyse"], system, user, ANALYSIS_TOOL, 4000)
        if _clean(out.get("summary")):
            break
        log(f"  analysis came back without a summary (keys: {list(out)[:8]}), retrying")
    if not _clean(out.get("summary")):
        raise RuntimeError(f"analysis returned no summary; keys={list(out)[:8]} preview={json.dumps(out)[:300]}")
    names = [s["name"] for s in stats]

    def as_list(v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = [x for x in v.split("\n") if x.strip()]
        return [x if isinstance(x, str) else " :: ".join(str(y) for y in (x.values() if isinstance(x, dict) else x)) for x in (v or [])]

    points = []
    for p in as_list(out.get("points"))[:3]:
        m = re.search(r"\[([^\]]+)\]\s*\.?\s*$", p)
        who = [n for n in names if m and n.lower() in m.group(1).lower()]
        text = _clean(p[: m.start()] if m else p)
        if text:
            points.append({"text": text, "names": who})
    plays = {}
    for line in as_list(out.get("plays")):
        name, play, aimed = _split(line, 3)
        match = next((n for n in names if n.lower() == name.lower()), None) or \
            next((n for n in names if n.lower() in name.lower()), None)
        if match:
            plays[match] = {"play": play, "aimed": aimed}
    shared = []
    for line in as_list(out.get("shared"))[:3]:
        title, text, who = _split(line, 3)
        who_list = [n for n in names if n.lower() in who.lower()]
        if title and len(who_list) >= 2:
            shared.append({"title": title, "text": text, "names": who_list})
    gaps = []
    for line in as_list(out.get("gaps"))[:3]:
        title, text = _split(line, 2)
        if title:
            gaps.append({"title": title, "text": text})
    return {"summary": _clean(out.get("summary")), "points": points, "plays": plays, "shared": shared, "gaps": gaps}


# ---------------------------------------------------------------- config

DEFAULT_TYPES = [
    ["compare", "Comparison / alternatives"],
    ["howto", "How-to / explainer"],
    ["list", "Listicle"],
    ["research", "Original research"],
    ["case", "Case study"],
    ["opinion", "Opinion / thought leadership"],
    ["news", "Product / company news"],
    ["landing", "Landing / service page"],
]
DEFAULTS = {
    "window_days": 30,
    "first_run_days": 60,
    "max_classify_per_run": 200,
    "max_classify_per_site": 40,
    "keep_pages": 1500,
    "user_agent": "Mozilla/5.0 (compatible; CompetitorRadar/1.0; content research bot)",
    "models": {"classify": "claude-haiku-4-5-20251001", "analyse": "claude-sonnet-5-5"},
}


def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    merged = {**DEFAULTS, **cfg}
    merged["models"] = {**DEFAULTS["models"], **(cfg.get("models") or {})}
    merged["types"] = [list(t) if isinstance(t, (list, tuple)) else [str(t).lower().replace(" ", "-"), str(t)]
                       for t in (cfg.get("types") or DEFAULT_TYPES)][:8]
    for k in ("niche", "audience", "competitors"):
        if not merged.get(k):
            sys.exit(f"config.yaml is missing '{k}'")
    sites = ([{**merged["you"], "you": True}] if merged.get("you") else []) + \
            [{**c, "you": False} for c in merged["competitors"]]
    names = [s.get("name") for s in sites]
    if None in names or len(set(names)) != len(names):
        sys.exit("Every site needs a unique 'name'")
    for s in sites:
        if not (s.get("site") or s.get("sitemap") or s.get("feed") or s.get("page")):
            sys.exit(f"{s['name']}: add a 'site' URL")
        if not s.get("site"):
            u = urlparse(s.get("sitemap") or s.get("feed") or s.get("page"))
            s["site"] = f"{u.scheme}://{u.netloc}"
    merged["sites"] = sites
    return merged


# ---------------------------------------------------------------- run

def check_sites(cfg: dict) -> None:
    f = Fetcher(cfg["user_agent"])
    for s in cfg["sites"]:
        host = site_host(s, f)
        try:
            method, used, items = discover(s, f)
            kept = [i for i in items if keep_url(i["url"], s, host)]
            dated = sum(1 for i in kept if i["published"])
            log(f"OK   {s['name']:<22} {method:<8} {len(kept):>5} pages ({dated} dated)  {used}")
        except Exception as e:  # noqa: BLE001
            log(f"FAIL {s['name']:<22} {short_error(e)}")


def run(cfg: dict, offline: bool = False, reanalyse: bool = False) -> dict:
    if (reanalyse or not offline) and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY (GitHub: Settings > Secrets and variables > Actions).")
    f = Fetcher(cfg["user_agent"])
    now = utcnow()
    window = timedelta(days=int(cfg["window_days"]))
    horizon = now - timedelta(days=max(int(cfg["first_run_days"]), 2 * int(cfg["window_days"])))
    state = load_json(STATE_PATH, {"pages": {}, "sites": {}})
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and state.get("repo") and state["repo"] != repo:
        log(f"New copy of {state['repo']}: starting clean")
        state = {"pages": {}, "sites": {}}
    if repo:
        state["repo"] = repo
    state.setdefault("tracking_since", iso(now))
    pages, errors, health = state["pages"], [], {}
    first_sites, prefetched, sections, recheck = [], {}, {}, []

    todo, queue = [], []
    if offline:
        # Rebuild the dashboard from saved state: no fetching, no AI calls.
        prev = load_json(DATA_PATH, {})
        for ps in prev.get("sites", []):
            health[ps["name"]] = {k: ps.get(k) for k in ("ok", "error", "method", "url", "pages", "undated")}
            sections[ps["name"]] = {x["section"]: x["pages"] for x in ps.get("programmatic", [])}
        for p in pages.values():
            if p.get("dates_checked"):
                decide_kind(p, now)
    else:
        # 1. Discover every site's pages
        log("== Discover")
        for s in cfg["sites"]:
            name, host = s["name"], site_host(s, f)
            first = not state["sites"].get(name, {}).get("initialized")
            try:
                method, used, items = discover(s, f)
            except Exception as e:  # noqa: BLE001
                health[name] = {"ok": False, "error": short_error(e), "method": None, "url": s["site"]}
                log(f"  FAIL {name}: {health[name]['error']}")
                continue
            items = [i for i in items if keep_url(i["url"], s, host)]
            prog = programmatic_sections([i["url"] for i in items], int(s.get("programmatic_min", cfg.get("programmatic_min", 100))))
            if prog:
                log(f"  {name}: templated sections counted, not classified: {prog}")
            # Some sites stamp every page with the deploy date. If most pages share a
            # very recent lastmod, those dates say nothing about when pages were published.
            recent = sum(1 for i in items if i["published"] and i["published"] >= now - timedelta(days=2))
            unreliable = len(items) >= 20 and recent > 0.5 * len(items)
            if unreliable:
                log(f"  {name}: sitemap dates look like deploy dates, ignoring them")
                for i in items:
                    i["published"] = None
            new = refreshed = 0
            for it in items:
                k = url_key(it["url"])
                lastmod = it["published"]
                rec = pages.get(k)
                if rec is None:
                    # A page's date is its sitemap/feed date when there is one, else the day we first saw it.
                    date = lastmod if lastmod and lastmod <= now else (None if first else now)
                    pages[k] = {"site": name, "url": it["url"], "title": it["title"], "date": iso(date),
                                "lastmod": iso(lastmod), "first_seen": iso(now), "cls": None, "baseline": first}
                    if not first:
                        pages[k].update(kind="new", event=iso(now))
                    elif method == "feed" and date:
                        pages[k].update(kind="new", event=iso(date), feed_dated=True)   # feed dates are publish dates
                    new += 1
                else:
                    if lastmod and rec.get("lastmod") and lastmod > parse_iso(rec["lastmod"]) + timedelta(hours=1):
                        recheck.append(rec)          # verified below: real edit or just a date bump?
                        refreshed += 1
                    rec["lastmod"] = iso(lastmod) if lastmod else rec.get("lastmod")
                sec = section_of(it["url"])
                pages[k]["prog"] = sec if sec in prog else None
            undated = sum(1 for i in items if not i["published"])
            if first:
                first_sites.append(name)
                if undated > len(items) / 2:
                    # No dates in the sitemap: read a sample of pages to find their publish dates.
                    sample = [i for i in items if not i["published"]]
                    sample.sort(key=lambda i: 0 if PREFERRED_CHILD.search(i["url"]) else 1)
                    for it in sample[: int(cfg.get("first_run_sample", 40))]:
                        try:
                            info = read_page(it["url"], f)
                        except Exception:  # noqa: BLE001
                            continue
                        prefetched[url_key(it["url"])] = info
                        rec = pages.get(url_key(it["url"]))
                        if rec and info["published"] and not rec.get("date"):
                            rec["date"] = iso(min(info["published"], now))
                        time.sleep(0.3)
            sections[name] = prog
            health[name] = {"ok": True, "error": None, "method": method, "url": used, "pages": len(items),
                            "undated": undated, "new_urls": 0 if first else new}
            if items:
                state["sites"][name] = {"initialized": True}   # only once we've actually seen pages
            log(f"  OK   {name}: {method}, {len(items)} pages, {new} new, {refreshed} updated")

    if not offline:
        # 2. Classify recent pages that haven't been classified yet (newest first, capped)
        todo = [p for p in pages.values() if p["cls"] is None and not p.get("prog") and p.get("date") and parse_iso(p["date"]) >= horizon
                and health.get(p["site"], {}).get("ok")]
        todo.sort(key=lambda p: p["date"], reverse=True)
        per_site, queue = {}, []
        cap_site = int(cfg.get("max_classify_per_site", 40))
        for p in todo:           # newest first, but no single site can use the whole budget
            if per_site.get(p["site"], 0) < cap_site and len(queue) < int(cfg["max_classify_per_run"]):
                queue.append(p)
                per_site[p["site"]] = per_site.get(p["site"], 0) + 1
        log(f"== Classify {len(queue)} pages" + (f" ({len(todo) - len(queue)} queued for next run)" if len(todo) > len(queue) else ""))
        for p in queue:
            try:
                info = prefetched.get(url_key(p["url"])) or read_page(p["url"], f)
                p["title"] = info["title"] or p["title"]
                date_kind(p, info, now)
                if info["published"] and not p.get("lastmod"):
                    p["date"] = iso(info["published"])
            except Exception as e:  # noqa: BLE001
                info = {}
                p["read_error"] = short_error(e)
            try:
                p["cls"] = classify({**p, "info": info}, p["site"], cfg)
            except Exception as e:  # noqa: BLE001
                errors.append(f"classify: {str(e)[:200]}")
                log(f"    classify failed: {e}")
            time.sleep(0.3)

        # 2b. New vs updated. Verify date bumps against the text fingerprint, and date
        #     pages classified before this existed. Page fetches only, no AI calls.
        checks = [p for p in recheck if not p.get("prog")][: int(cfg.get("max_rechecks", 300))]
        real = 0
        for p in checks:
            try:
                info = read_page(p["url"], f)
            except Exception:  # noqa: BLE001
                continue
            if p.get("fp") and info["fp"] != p["fp"]:
                p.update(kind="updated", event=iso(now), fp_verified=True)
                real += 1
            elif not p.get("fp"):
                p.pop("kind", None)
                p["date"] = p.get("lastmod") or p["date"]      # its sitemap date just moved to now
                date_kind(p, info, now)
            p["fp"], p["dates_checked"] = info["fp"], iso(now)
            time.sleep(0.2)
        backfill = [p for p in pages.values() if p.get("cls") and not p.get("dates_checked") and not p.get("prog")
                    and p.get("date") and parse_iso(p["date"]) >= horizon][: int(cfg.get("max_backfill", 400))]
        for p in backfill:
            try:
                date_kind(p, read_page(p["url"], f), now)
            except Exception:  # noqa: BLE001
                p["dates_checked"] = iso(now)
            time.sleep(0.2)
        log(f"== New vs updated: {real} of {len(checks)} date bumps were real edits; dated {len(backfill)} older pages")

    bulk_events = mark_bulk_dates(pages, now)
    if bulk_events:
        log(f"== Bulk dates (reported as events, not as new pages): {bulk_events}")
    for p in pages.values():
        if p.get("dates_checked"):
            decide_kind(p, now)

    # 3. Stats per site for the window
    stats = []
    for s in cfg["sites"]:
        name = s["name"]
        allmine = [p for p in pages.values() if p["site"] == name]
        mine = [p for p in allmine if p.get("date") and not p.get("prog")]
        prog_info = [{"section": sec, "pages": n,
                      "new": sum(1 for p in allmine if p.get("prog") == sec and not p.get("baseline", True)
                                 and parse_iso(p["first_seen"]) >= now - window)}
                     for sec, n in sorted(sections.get(name, {}).items(), key=lambda x: -x[1])]
        evdate = lambda p: parse_iso(p.get("event") or p["date"])  # noqa: E731
        cur = [p for p in mine if evdate(p) >= now - window]
        prev = [p for p in mine if now - 2 * window <= parse_iso(p["date"]) < now - window
                and (not p.get("cls") or p["cls"]["is_content"])]
        content = [p for p in cur if p.get("cls") and p["cls"]["is_content"]]
        mix = {t[0]: sum(1 for p in content if p["cls"]["type"] == t[0]) for t in cfg["types"]}
        stages = {k: sum(1 for p in content if p["cls"]["stage"] == k) for k in ("learning", "comparing", "buying", "customers")}
        topics = {}
        for p in content:
            topics[p["cls"]["topic"].lower()] = topics.get(p["cls"]["topic"].lower(), 0) + 1
        refreshed = sum(1 for p in mine if p.get("kind") == "updated" and evdate(p) >= now - window)
        stats.append({
            "name": name, "site": s["site"], "you": s["you"], "n": len([p for p in cur if not p.get("cls") or p["cls"]["is_content"]]),
            "n_prev": len(prev), "refreshed": refreshed, "mix": mix, "stages": stages,
            "topics": [t for t, _ in sorted(topics.items(), key=lambda x: -x[1])[:5]],
            "unclassified": sum(1 for p in cur if not p.get("cls")),
            "new": sum(1 for p in cur if p.get("kind") == "new" and (not p.get("cls") or p["cls"]["is_content"])),
            "updated": sum(1 for p in cur if p.get("kind") == "updated" and (not p.get("cls") or p["cls"]["is_content"])),
            "bulk": sorted((e for e in bulk_events.get(name, []) if parse_iso(e["day"] + "T00:00:00+00:00") >= now - window),
                           key=lambda e: e["day"]),
            "programmatic": prog_info,
            "sample": [{"type": p["cls"]["type"], "title": p["title"], "kind": p.get("kind", "unknown")}
                       for p in sorted(content, key=lambda p: p.get("event") or p["date"], reverse=True)[:40]],
            **{k: v for k, v in health.get(name, {"ok": False, "error": "not checked"}).items()},
        })

    # Period-on-period comparisons only once we have two full windows of our own history.
    cfg["_history_ok"] = (now - parse_iso(state["tracking_since"])).days >= 2 * int(cfg["window_days"])

    # 4. Analysis (one call to the stronger model)
    log("== Analyse")
    data = load_json(DATA_PATH, {})
    analysis = data.get("analysis") or {}
    prev_plays = {ps["name"]: {"play": ps.get("play", ""), "aimed": ps.get("aimed", "")} for ps in data.get("sites", [])}
    if offline and not reanalyse:
        pass
    elif any(s["n"] for s in stats):
        try:
            analysis = analyse([s for s in stats if s.get("ok")], cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(f"analysis: {str(e)[:200]}")
            log(f"  analysis failed, kept last one: {e}")
    else:
        analysis = {"summary": f"No new pages from these sites in the last {cfg['window_days']} days.",
                    "points": [], "plays": {}, "shared": [], "gaps": []}
    for s in stats:
        s.update(analysis.get("plays", {}).get(s["name"]) or prev_plays.get(s["name"]) or {"play": "", "aimed": ""})
        s.pop("sample", None)

    # 5. Save
    recent = [p for p in pages.values() if p.get("cls") and p["cls"]["is_content"] and p.get("date")
              and parse_iso(p["date"]) >= horizon]
    recent.sort(key=lambda p: p.get("event") or p["date"], reverse=True)
    out_pages = [{"site": p["site"], "url": p["url"], "title": p["title"], "date": p.get("event") or p["date"],
                  "kind": p.get("kind", "unknown"), "published": p.get("pub"),
                  "first_seen": p["first_seen"], **{k: v for k, v in p["cls"].items() if k != "is_content"}}
                 for p in recent[: int(cfg["keep_pages"])]]
    first_run = bool(first_sites)
    data = {
        "meta": {"title": cfg.get("title", "Competitor Radar"), "niche": cfg["niche"], "updated": iso(now),
                 "window_days": int(cfg["window_days"]), "repo": repo, "first_run": first_run,
                 "errors": list(dict.fromkeys(errors))[:5],
                 "pending": (data.get("meta") or {}).get("pending", 0) if offline else max(0, len(todo) - len(queue)),
                 "analysis_stale": (offline and not reanalyse) or any(e.startswith("analysis:") for e in errors),
                 "per_run": int(cfg["max_classify_per_run"]), "tracking_since": state["tracking_since"],
                 "tracking_days": (now - parse_iso(state["tracking_since"])).days},
        "types": cfg["types"],
        "analysis": analysis,        # plays kept too, so a failed run can reuse them
        "sites": stats,
        "pages": out_pages,
    }
    save_json(DATA_PATH, data)
    save_json(STATE_PATH, state)
    lines = ["## Competitor Radar run"] + [f"- {s['name']}: {s['n']} pages in window" + ("" if s.get("ok") else f" (FAILED: {s.get('error')})") for s in stats]
    if errors:
        lines += ["", "**Errors**"] + [f"- {e}" for e in data["meta"]["errors"]]
    log("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="Competitor Radar")
    ap.add_argument("--check", action="store_true", help="test your sites only (no API key, no writes)")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the dashboard from saved data (no fetching, no AI)")
    ap.add_argument("--reanalyse", action="store_true", help="rebuild from saved data and rewrite the analysis (one AI call)")
    args = ap.parse_args()
    cfg = load_config()
    if os.environ.get("RADAR_CLASSIFY_LIMIT", "").strip().isdigit():
        # One-off catch-up: lift the per-run and per-site caps to clear a backlog.
        cfg["max_classify_per_run"] = cfg["max_classify_per_site"] = int(os.environ["RADAR_CLASSIFY_LIMIT"])
        cfg["max_backfill"] = 1500           # also date every already-classified page (fetches only, no AI)
    if args.check:
        check_sites(cfg)
    else:
        run(cfg, offline=args.rebuild or args.reanalyse, reanalyse=args.reanalyse)


if __name__ == "__main__":
    main()
