"""Bottom-of-funnel content detector.

Reads the page list the Competitor Radar already keeps (state/state.json), tags each
URL with a bottom-of-funnel content type using the URL alone, and writes docs/bofu.json.

URL rules are deterministic, so every count on the dashboard can be traced back to the
exact pages behind it, and every site is judged by the same rules. Nothing here calls an AI.

    python bofu/bofu.py
"""
import argparse
import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

TYPES = [
    ("vs_you", "Us vs. them"),
    ("alt", "Alternatives"),
    ("switch", "Switching and migration"),
    ("pricing", "Rival pricing breakdowns"),
    ("vs_rivals", "Rival vs. rival"),
    ("best", "Best-of and roundups"),
    ("case", "Case and customer stories"),
]

VENDOR_RX = {v: re.compile(rf"\b(?:{rx})\b") for v, rx in CFG["vendors"].items()}
AUDIENCE_RX = [(a, re.compile(rf"\b(?:{rx})\b")) for a, rx in CFG["audiences"]]


def audience_of(slug: str) -> str:
    for name, rx in AUDIENCE_RX:
        if rx.search(slug):
            return name
    return "General (no segment)"

LOCALE_RX = re.compile(r"^/(?:fr|es|de|pt|it|ja|nl)(?:-[a-z0-9]{2,4})?(?:/|$)")
SKIP_RX = re.compile(r"/(?:webinars?|glossary|tag|category|blog-category|events|press|podcast)(?:/|$)|-glossary(?:/|$)")
HUB_RX = re.compile(r"^/(?:[a-z-]+/)*(?:compare|alternatives|alternatives-comparison|customers|case-studies|customer-stories|reviews|versus)$")
CASE_FOLDER_RX = re.compile(r"/(?:case-stud(?:y|ies)|customers?|customer-stories|customer-story|success-stories)/[^/]+")
CASE_SLUG_RX = re.compile(r"\b(?:customer case study|case study|customer story|customer stories)\b")
SWITCH_RX = re.compile(r"\b(?:switch|switching|migrate|migrating|migration)\b")
SWITCH_GENERIC_RX = re.compile(r"\bswitch(?:ing)?\b(?: sales tax| tax)? (?:software|providers?|platforms?|vendors?)\b|^ switch $")
PRICE_RX = re.compile(r"\b(?:pricing|price|prices|cost|costs)\b")
OWN_PRICING_RX = re.compile(r"^/(?:[a-z-]+/)*(?:pricing|plans|[a-z]+-pricing|contact-sales-pricing)(?:\.html)?$")
BEST_RX = re.compile(r"(?<!is )\b(?:best|top \d+|\d+ best|top ten)\b(?! practices| practice| ways| way| tips)")
PRODUCT_NOUN_RX = re.compile(r"\b(?:software|tools?|apps?|solutions?|platforms?|providers?|automation|extensions|plugins|vendors|comparison|compare)\b")
TAXY_RX = re.compile(r"\b(?:tax|taxes|vat|gst|compliance)\b")


def clean_path(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0].lower()
    return re.sub(r"/+$", "", path) or "/"


def slugify(path: str) -> str:
    p = re.sub(r"\.html?$", "", path)
    return " " + " ".join(t for t in re.split(r"[^a-z0-9]+", p) if t) + " "


def classify(site: str, path: str):
    """Return (type, targets) or (None, [])."""
    slug = slugify(path)
    named = [v for v, rx in VENDOR_RX.items() if rx.search(slug)]
    rivals_named = [v for v in named if v != site]
    in_compare_folder = bool(re.search(r"/(?:compare|alternatives|versus)/[^/]+", path))
    has_vs = bool(re.search(r"\b(?:vs|versus)\b", slug))

    # Customer stories are found by folder first: they are normally grouped together.
    if CASE_FOLDER_RX.search(path) or CASE_SLUG_RX.search(slug):
        return "case", []

    # Switching: a rival is named, or the page is plainly about switching providers.
    if SWITCH_RX.search(slug) and (rivals_named or SWITCH_GENERIC_RX.search(slug)):
        return "switch", rivals_named

    # Head-to-head comparisons.
    comparison = (
        (has_vs and len(named) >= 2)
        or (in_compare_folder and rivals_named)
        or (re.search(r"\b(?:comparison|compare)\b", slug) and len(named) >= 2)
    )
    if comparison:
        involves_you = site in named or in_compare_folder
        return ("vs_you" if involves_you else "vs_rivals"), rivals_named

    if re.search(r"\balternatives?\b", slug) and rivals_named:
        return "alt", rivals_named

    if PRICE_RX.search(slug) and rivals_named:
        return "pricing", rivals_named

    if not has_vs and TAXY_RX.search(slug) and PRODUCT_NOUN_RX.search(slug) and (
        BEST_RX.search(slug) or re.search(r"\b(?:comparison|compare)\b", slug)
    ):
        return "best", []

    return None, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(HERE.parent / "state" / "state.json"))
    ap.add_argument("--out", default=str(HERE.parent / "docs" / "bofu.json"))
    args = ap.parse_args()

    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    sites = [CFG["you"]] + CFG["competitors"]

    pages, tracked, considered, hubs = [], collections.Counter(), collections.Counter(), collections.defaultdict(list)
    excluded = collections.Counter()
    own_pricing = {}

    for rec in state["pages"].values():
        site = rec["site"]
        if site not in sites:
            continue
        tracked[site] += 1
        path = clean_path(rec["url"])
        if rec.get("prog"):
            excluded["templated pages"] += 1
            continue
        if LOCALE_RX.match(path):
            excluded["translated pages"] += 1
            continue
        if SKIP_RX.search(path):
            excluded["webinars, glossary, events and category pages"] += 1
            continue
        considered[site] += 1

        if OWN_PRICING_RX.search(path) and not re.search(r"/(?:blog|resources|article)/", path):
            if site not in own_pricing or len(path) < len(clean_path(own_pricing[site])):
                own_pricing[site] = rec["url"]
            continue
        if HUB_RX.search(path):
            hubs[site].append(rec["url"])
            continue

        kind, targets = classify(site, path)
        if kind:
            row = {"site": site, "url": rec["url"], "path": path, "type": kind, "targets": targets}
            if kind == "best":
                row["audience"] = audience_of(slugify(path))
            pages.append(row)

    pages.sort(key=lambda p: (sites.index(p["site"]), p["type"], p["path"]))
    out = {
        "meta": {
            "title": CFG["title"],
            "niche": CFG["niche"],
            "you": CFG["you"],
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "state_updated": state.get("tracking_since"),
            "repo": state.get("repo"),
        },
        "types": TYPES,
        "sites": [{"name": s, "tracked": tracked[s], "considered": considered[s],
                   "own_pricing": own_pricing.get(s), "hubs": hubs.get(s, [])} for s in sites],
        "vendors": list(CFG["vendors"].keys()),
        "excluded": dict(excluded),
        "pages": pages,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # Plain-text summary for the terminal.
    grid = collections.Counter((p["site"], p["type"]) for p in pages)
    print(f"{'':28s}" + "".join(f"{s[:8]:>9s}" for s in sites))
    for key, label in TYPES:
        print(f"{label:28s}" + "".join(f"{grid[(s, key)]:>9d}" for s in sites))
    print("excluded:", dict(excluded))
    print("own pricing pages:", own_pricing)
    print("hubs:", {s: len(v) for s, v in hubs.items()})


if __name__ == "__main__":
    main()
