# Competitor Radar

**See what your competitors publish, and what it says about their strategy.**

List your competitors and Competitor Radar watches their websites. Every week it finds their new pages, sorts each one by type, and writes a plain-English breakdown:

- **The last 30 days:** a two-sentence summary plus the 3 things worth knowing.
- **What each competitor is doing:** how much they published, their content mix, and their play in one line.
- **Shared plays:** what several competitors are all doing.
- **Gaps:** topics and formats nobody covers, which are your openings.
- **Latest pages:** every new page, with its type, who it's for, the search it likely targets, and why they published it.

It runs free on GitHub, with no server and nothing to host. You only pay for your own Claude API usage, which is usually cents per run.

<!-- After your first run, add a screenshot: ![Competitor Radar](docs/screenshot.png) -->

---

## How it works

```
Every Monday (GitHub Actions)
  1. Discover   find each site's sitemap (or feed) and spot new pages
  2. Classify   a cheap model tags each new page: type, buyer stage, topic, likely search
  3. Analyse    a stronger model reads the tags and writes the breakdown
  4. Publish    results are committed to the repo; the dashboard (GitHub Pages) updates
```

**Page types:** Comparison / alternatives, How-to / explainer, Listicle, Original research, Case study, Opinion / thought leadership, Product / company news, Landing / service page. You can change these in `config.yaml`.

**Buyer stage:** Learning, Comparing options, Ready to buy, Existing customers.

## Setup (about 10 minutes)

1. **Copy the repo.** Click **Use this template** (or fork it). Your copy starts clean.
2. **Add your API key.** Get one at [console.anthropic.com](https://console.anthropic.com). In your repo, go to **Settings → Secrets and variables → Actions → New repository secret**, name it `ANTHROPIC_API_KEY`, and paste the key.
3. **Turn on the dashboard.** Go to **Settings → Pages**, set Source to *Deploy from a branch*, choose `main` and `/docs`, then click Save.
4. **List your competitors** in `config.yaml`:

   ```yaml
   you:                      # optional: your own site, so gaps are relative to you
     name: Acme
     site: https://acme.com

   competitors:
     - name: Rival One
       site: https://rivalone.com
     - name: Rival Two
       site: https://rivaltwo.com
       include_pattern: /blog/   # optional: only count blog pages
   ```

5. **Run it:** go to **Actions → Competitor Radar → Run workflow**. Then open `https://<you>.github.io/<repo>/`.

**Test your sites first** (no API key needed):

```bash
pip install -r requirements.txt
python radar.py --check
```

```
OK   Rival One       sitemap    412 pages (412 dated)  https://rivalone.com/sitemap_index.xml
FAIL Rival Two       no sitemap or feed found. Add a sitemap: or feed: line for this site in config.yaml
```

## Options per site

| Option | What it does |
|---|---|
| `site` | The homepage. The radar looks for a sitemap in robots.txt, then `/sitemap.xml`, then common feed URLs. |
| `sitemap` / `feed` | Use this sitemap or feed instead of auto-detecting. |
| `include_pattern` | Only count URLs matching this regex (e.g. `/blog/\|/resources/`). |
| `exclude_pattern` | Ignore URLs matching this regex (e.g. `/docs/`). |
| `programmatic_min` | A section with this many pages (default 100), like `/sales-tax-calculator/`, is treated as templated. It's reported as "Also runs templated pages" instead of being classified page by page. |
| `default_excludes: false` | Keep tag, category, login, legal and translated pages, which are skipped by default. |

## Cost

- **Classifying:** one small call per new page on the cheap model. The first run tags up to 200 pages from the last 60 days. After that, it's only new pages.
- **Analysis:** one call per run on the stronger model.

`max_classify_per_run` caps spend. Extra pages wait for the next run. For current rates, see [anthropic.com/pricing](https://www.anthropic.com/pricing).

## Limits

- **It shows what competitors publish, not what works.** There's no traffic or ranking data.
- **"Their play" is an AI reading of their output.** Treat it as a strong guess, and small numbers as weak signals.
- **The first run leans on sitemap dates**, which can include pages that were only updated. From the second run, "new" means genuinely new.
- **Sites with no sitemap or feed, or that block bots,** show up as failing in Sources. Add a `sitemap:` or `feed:` URL, or drop them.

## Files

```
radar.py                     the whole pipeline (one file)
config.yaml                  your market and competitors
docs/index.html              the dashboard (static, no build step)
docs/data.json               results (written by the workflow)
state/state.json             every page seen, so nothing is classified twice
.github/workflows/radar.yml  weekly schedule
```

## License

MIT
