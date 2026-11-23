# Article Scraper

Single-script article extractor invoked over SSH from the V3 blog workflows.
Replaces the older 32-node per-source scrape/cleanup pipeline.

## Where it runs, and the copies

This folder is the public mirror. There are three copies of the scraper, kept
byte for byte the same (the same SHA-256):

- **the source, with its tests:** tcs-workflows `working-files/python-scripts/`
  (a private repo);
- **what production runs,** on the OVH VPS:

  ```
  /opt/tcs/scripts/article_scraper.py
  /opt/tcs/scripts/lib_index.py
  /opt/tcs/scripts/crawl4ai-env/       # Python venv with Crawl4AI + Playwright
  ```
- **this folder.**

A change goes to all three together (tcs-workflows#305). Before 2026-11, this
copy fell two months behind; the header of `article_scraper.py` now says so.

## How it's called

Every V3 blog workflow (Daily Broadcast, NASA Overview, SpaceX Report, Rocket
Lab Roundup, Commercial Space, Bright Blue Origin, Canada From Orbit) invokes
it via an n8n SSH node with:

```
/opt/tcs/scripts/crawl4ai-env/bin/python \
  /opt/tcs/scripts/article_scraper.py <BASE64_JSON> [--run-id=<id>] [--index-prefix=<pref>]
```

Input: base64-encoded JSON array of `{"url", "title"}` objects.

Two output modes:

- **INLINE** (no `--run-id`): full article content emitted on stdout as JSON —
  legacy shape for the prototype workflow.
- **FILE** (with `--run-id`): per-article markdown written under
  `/opt/tcs/n8n/local_files/scraper-runs/<id>/`, with a slim `index.json`
  emitted on stdout. This is what all V3 workflows use — the Write News
  LangChain agent then fetches individual articles on demand via the
  `read_article` tool.

## Stack

- **[Crawl4AI](https://github.com/unclecode/crawl4ai)** 0.9.6 — Playwright-backed
  headless-browser scraper with LLM-ready markdown output
- **Playwright** 1.65.2 (+ `playwright-stealth` 2.0.3) — the browser Crawl4AI
  drives
- **httpx** 0.28.1, **requests** 2.34.2, **beautifulsoup4** 4.15.0 — supporting
  HTTP + parsing libs

The `crawl4ai-env` venv on the VPS is Python 3.14 with the packages above (as of
2026-11-23).
Nothing else in this venv — no trafilatura, no Jina Reader.

Separately, some n8n workflows fetch pages through other services, where
Crawl4AI's self-hosted browser gets caught by anti-bot fingerprinting (as of
2026-11-23):
- **CRW** (self-hosted on the VPS) — the SpaceX Report (`spacex.com/updates` and
  `starlink.com/updates`), the Rocket Lab Roundup's articles, and Canada From
  Orbit's Postmedia pages
- **[Firecrawl](https://www.firecrawl.dev/)** — the Rocket Lab Roundup, the Bright
  Blue Origin's news index, and the Space Daily article tool
- **[ScraperAPI](https://www.scraperapi.com/)** — the Bright Blue Origin's
  articles

Those calls happen from n8n HTTP Request nodes, not from `article_scraper.py`.
The scraper itself has no external-service dependencies.

## Per-site config

`SITE_CONFIG` at the top of `article_scraper.py` is a hostname → config dict
covering the 26 domains we currently ingest from. Each entry can specify:

- `source` — the name written into each article's Source line, which the authors
  read
- `selector` — CSS selector scoping content extraction (Crawl4AI's
  `target_elements`)
- `strict_scope` — use Crawl4AI's `css_selector` instead: only the selected
  elements come back, plus four head tags (og:image, twitter:image,
  article:published_time, parsely-pub-date) for the thumbnail and the date
- `title_meta` — with `strict_scope`, select og:title too, for a site whose
  `<h1>` is outside the selector (Aerospace America)
- `exclude` — CSS selector for parts to drop (Crawl4AI's `excluded_selector`)
- `image_scope` — only pictures inside this element are kept
- `wait` — extra seconds before extraction (JS-heavy sites)
- `networkidle` — wait for network-idle before extraction
- `keep_overlays` — disable Crawl4AI's overlay-removal step (breaks Ars, ESA,
  NASA Science, etc. that wrap article images in `<figure>` or `<a>` elements
  the overlay filter classifies as UI)

Sites currently configured: Ars Technica, ESA, European Spaceflight,
NASASpaceflight, Spaceflight Now, SpacePolicyOnline, SpaceNews, Space Daily,
SpaceWar, Space Scout, NASA Science, NASA, Planetary Society, ULA, SpaceQ,
Stoke Space, IEEE Spectrum, Firefly Aerospace, Relativity Space, Axiom Space,
EIN Presswire, NordSpace, Maritime Launch Services, Reaction Dynamics, Canada
Rocket Company, Aerospace America.

## WAF-protected sites (Pattern B)

A handful of sites (CSA `asc-csa.gc.ca` in particular) return
`script_heavy_shell` errors under Crawl4AI's headless browser due to WAF
fingerprinting. Those sites use "Pattern B" in the V3 workflows: a plain n8n
HTTP Request node with browser-like headers, followed by an n8n Code node that
runs regex-based extraction. No external scraping service is involved — it's
just `requests`-equivalent behavior inside n8n itself.

## Editing and tests

Change the source copy in tcs-workflows, with a test. The suites stub Crawl4AI,
so they run anywhere; the tests that act out Crawl4AI's selection need
BeautifulSoup, and are skipped without it:

```
cd article-scraper
python test_article_scraper_244.py     # and each other test_article_scraper_*.py
```

(Each test file's own "Run:" line gives its path in tcs-workflows.)

Unit tests can't see what the real crawler does with a selector, so each per-site
fix is also staged on the VPS against the live page, from a scratch folder,
before the production copy is replaced (with a backup). Then the same file comes
here.

Test fixtures never carry real people's contact details: phone numbers are
555-01xx, and addresses are made up.
