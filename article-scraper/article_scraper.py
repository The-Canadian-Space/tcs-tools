#!/usr/bin/env python3
"""
TCS Article Scraper — single-node replacement for the 32-node per-source
scrape/cleanup pipeline.

Called from n8n SSH node:
    python3 article_scraper.py <BASE64_JSON> [--run-id=<id>]

Input: base64-encoded JSON array of {"url", "title"} objects.

Two output modes:

1) INLINE MODE (no --run-id): Outputs a JSON array on stdout, one item per
   article. Back-compat with the prototype workflow.

2) FILE MODE (--run-id=<id>): Writes per-article content to disk and emits a
   lightweight index.json to stdout. Used by the agent-driven Daily Broadcast
   workflow where the Write News agent reads articles on demand via a tool.

   Files written under /opt/tcs/n8n/local_files/scraper-runs/<id>/:
     - index.json          : metadata for all articles
     - article-N.md        : full markdown body for article index N

   In file mode, stdout is the contents of index.json (so n8n can use the
   metadata without a second SSH call).

Each article record has shape:
    {
      "index", "title", "url", "date", "thumbnail", "images",
      "source", "word_count", "character_count",
      "has_content", "error" (optional), "unknown_site" (optional)
    }
Plus "content" in INLINE mode. In FILE mode, content lives in article-N.md.

Uses Crawl4AI for fetching + per-site CSS selectors that mirror the
original n8n cleanup node targeting.

Copies (tcs-workflows#305): this file and its tests in tcs-workflows
working-files/python-scripts/ are the source. The VPS runs
/opt/tcs/scripts/article_scraper.py, and tcs-tools article-scraper/ is the
public mirror. Change all three together, so their SHA-256 stays the same,
and keep the public rules: no n8n run numbers, no real people's contact
details in test fixtures.
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_index import upsert_entries
from urllib.parse import parse_qs, unquote, urlparse

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    CacheMode,
)


# ============================================================
# Resilience: retry + structured logging (tcs-workflows#61)
# ============================================================
# Per-article fetch retries. Attempt N sleeps N seconds before the next try
# (1s, 2s). Deliberately small: the outage class we're defending against is
# transient (EPIPE / browser-closed / network blips), not sustained bans.
MAX_FETCH_ATTEMPTS = 2

# Lowercase substrings that mark an error as worth retrying. Anything else
# (404, anti-bot, selector-empty) is failed fast on the first attempt.
RETRYABLE_ERR_PATTERNS = (
    "epipe",
    "broken pipe",
    "connection reset",
    "connectionreseterror",
    "timeouterror",
    "asyncio.timeouterror",
    "target closed",
    "browser has been closed",
    "network idle timeout",
    "temporary failure in name resolution",
    "econnrefused",
)


def _log_event(payload: dict) -> None:
    """Emit one structured JSON line to stderr for the alert path.

    stdout is reserved for the result JSON, so all observability goes to
    stderr. Never raises — logging must not be able to kill the scrape.
    """
    try:
        line = dict(payload)
        line.setdefault("scraper", "article_scraper.py")
        line.setdefault("ts", datetime.now(timezone.utc).isoformat())
        sys.stderr.write(json.dumps(line, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _is_retryable(err_msg: str) -> bool:
    if not err_msg:
        return False
    low = err_msg.lower()
    return any(pat in low for pat in RETRYABLE_ERR_PATTERNS)


# CHALLENGE_RETRY_V1 (tcs-workflows#255): from 2026-11-07 NASASpaceFlight answered some requests with a Cloudflare JS
# challenge (3 of 21 scrapes in 45 days, each in a run that fetched several NSF pages at once), and a plain fetch of
# the same page later got HTTP 200. Such a page gets one more try after a longer pause. Every other anti-bot error, and
# every other site, still fails fast (#61).
CHALLENGE_RETRY_HOSTS = ("nasaspaceflight.com",)
CHALLENGE_RETRY_PAUSE_S = 10


def _is_challenge_retryable(url: str, err_msg: str) -> bool:
    if not err_msg or "cloudflare js challenge" not in err_msg.lower():
        return False
    host = _host_for(url)
    return any(host == h or host.endswith("." + h) for h in CHALLENGE_RETRY_HOSTS)


# ============================================================
# Per-site configuration: source name + content CSS selector
# ============================================================
# Keyed by hostname (www. stripped). Values:
#   source: human-readable source name
#   selector: CSS selector for the article content block (optional)
#   wait: extra wait time in seconds (for JS-heavy sites)
#   networkidle: whether to wait for network idle before returning
#
# When selector is missing, Crawl4AI's default content extraction runs
# (pruning filter + full page markdown).
SITE_CONFIG = {
    "arstechnica.com": {
        "source": "Ars Technica",
        "selector": "div.post-content",
        "keep_overlays": True,  # remove_overlay_elements breaks Ars' layout
        # ARS_CHROME_V1 (tcs-workflows#244): each photo's lightbox copy of its caption (the figcaption stays), and the
        # Rocket Report's newsletter sign-up box. res.html keeps both, so the image list still finds the captions.
        "exclude": "div.pswp-caption-content, div.ars-newsletter-shortcode",
    },
    "esa.int": {
        "source": "ESA",
        # ESA_NEW_SITE_V1 (tcs-workflows#234): the site that went live on 2026-11-04, on www.esa.int and new.esa.int.
        # The selector is all of <main>, and "exclude" names the chrome inside it. Anything not named stays, so a
        # module nobody has seen yet shows up as noise, not as lost text. That's the right way round for source
        # material: an earlier version listed the blocks to keep, and it dropped a lead paragraph.
        # Crawl4AI applies css_selector twice: on the page, one comma-separated part at a time, joined into res.html,
        # then on that fragment for the markdown. excluded_selector only runs on the second pass. So one part keeps
        # the page's order, and res.html keeps what's excluded: the date bar and <h1> still reach extract_date()
        # and the title. It also holds the related cards, which is why ESA's date bar is read before anything else.
        "selector": "main",
        "exclude": "h1, nav.breadcrumbs, .kicker, .favorites-views__wrapper, .articles-modules__share, .grid-layout, "
                   ".cards-carousel, .gallery, .headline-text--standalone, "
                   ".interactive-media-modal:not(.interactive-media-modal--static), "
                   ".interactive-media-modal__actions, .interactive-media-modal__share-panel, "
                   ".interactive-media-modal__relations, .interactive-media-modal__related, "
                   ".interactive-media-modal__gallery, .interactive-media-modal__third-party-warning",
        "strict_scope": True,   # T3 2026-07-19: target_elements matches 8 article__block divs; css_selector isolates content
        "keep_overlays": True,  # T3 2026-07-19: images wrapped in <figure>/<a> get stripped as overlays otherwise
    },
    "europeanspaceflight.com": {
        "source": "European Spaceflight",
        "selector": "div.td-post-content",
        # ESF_IMAGES_V1 (tcs-workflows#244): the related-story cards' thumbnails reached the image list.
        "image_scope": "div.td-post-content",
    },
    "nasaspaceflight.com": {
        "source": "NASASpaceFlight",
        "selector": "div.inner-post-entry, div.entry-content",
        # NSF_CHROME_V1 (tcs-workflows#244): the hidden view counter that opened every body as "_N_", and the
        # "See Also" link widget inside the article.
        "exclude": "i.penci-post-countview-number-check, h4.widget-title, h4.widget-title + ul",
        # Do NOT set networkidle here. NSF's ad/analytics/live-widget traffic
        # never hits 500ms of silence, so Playwright's networkidle wait times
        # out at 60s more often than not — that was the Sept 2026 outage
        # (#61). With plain domcontentloaded + the 3s render delay the same
        # article came back in 7s with full content, vs 44s on a lucky
        # networkidle run and 121s (primary + fallback timeout) on a bad one.
        "wait": 3.0,
    },
    "spaceflightnow.com": {
        "source": "Spaceflight Now",
        # SFN_ARTICLE_MAIN_V1 (tcs-workflows#244): the late-October redesign dropped div.entry-content, so from about
        # 10-28 to 11-07 the scraper got the whole page: the settings drawer, the nav, another launch's countdown and
        # the footer. article.main holds the H1, the byline, the lead figure and the story body. The exclude list
        # drops the byline's own date (the Date line has it), the back link, the tag and category chips and the
        # editor's-note box. strict_scope and keep_overlays keep the lead photo, which target_elements dropped.
        "selector": "article.main",
        "exclude": "#story-byline time, #back-to-archive, #story-tags, #story-cat, .ef-note",
        "strict_scope": True,
        "keep_overlays": True,
    },
    "spacepolicyonline.com": {
        "source": "SpacePolicyOnline",
        "selector": "div.entry-content",
    },
    "spacenews.com": {
        "source": "SpaceNews",
        "selector": "div.entry-content",
    },
    "spacedaily.com": {
        "source": "Space Daily",
        "selector": "#body-2-incontainer, .main-content",
    },
    "spacewar.com": {
        "source": "SpaceWar",
        "selector": "#body-2-incontainer, .main-content",
    },
    "spacescout.info": {
        "source": "Space Scout",
        "selector": "div.cm-entry-summary, div.entry-content",
    },
    "science.nasa.gov": {
        "source": "NASA Science",
        "selector": "div.entry-content, div.single-blog-content",
        "strict_scope": True,   # T3 2026-07-19: target_elements suppresses <img> inside <a><figure> Photojournal wrappers
        "keep_overlays": True,  # T3 2026-07-19: Photojournal <a><figure><img> triplets classified as overlays otherwise
        # NASA_SCIENCE_CHROME_V1 (tcs-workflows#244): APOD's yesterday/tomorrow links (they quote another entry's
        # title) and Random APOD button, and the APOD masthead. The Photojournal Downloads block
        # (.wp-block-smd-block-post-meta) holds the page's only picture, so its text is cut in _site_post_process.
        "exclude": (".wp-block-smd-block-next-prev, .wp-block-nasa-blocks-related-link, "
                    ".hds-media-detail-hero p.label, .hds-media-detail-hero p.heading-22, "
                    ".hds-media-detail-hero .media-detail-hero__subtext"),
    },
    "nasa.gov": {
        "source": "NASA",
        "selector": "div.entry-content, div.single-blog-content",
        "strict_scope": True,   # 2026-08-06 audit: sidebar "Recent Posts"
        "keep_overlays": True,  # thumbnails from OTHER articles leaked into the current
                                # article's images[] list, causing cross-article image mixup
                                # in published blogs. Same fix pattern as science.nasa.gov.
    },
    "planetary.org": {
        "source": "The Planetary Society",
        "selector": "article",
    },
    "ulalaunch.com": {
        "source": "United Launch Alliance",
        "selector": "#hs_cos_wrapper_post_body",
    },
    "spaceq.ca": {
        "source": "SpaceQ",
        "selector": "div.entry-content",
        "exclude": "div.jp-relatedposts",   # SPACEQ_RELATED_V1 (tcs-workflows#244): Jetpack's related posts
    },
    "stokespace.com": {
        "source": "Stoke Space",   # STOKE_SOURCE_NAME_V1 (tcs-workflows#309): it was "Stoked Space"
        # 2026-09-09: Stoke redesigned WordPress -> Next.js. Old selector
        # was `div.post__content-entry`; new article body wrapper is
        # `div.news-rich-text` (also carries `px-main grid-main` utility
        # classes on the new site).
        "selector": "div.news-rich-text",
        # STOKE_IMAGES_V1 (tcs-workflows#244): its pictures are Next.js "fill" images, which are position: absolute, so
        # Crawl4AI's overlay remover deleted some or all of them: 0 to 3 images across four crawls of one release.
        # The page also carries "more news" cards with other releases' covers, and some of those reached the list.
        # keep_overlays keeps every picture, and image_scope keeps only those inside the release's <article>.
        "keep_overlays": True,
        "image_scope": "article",
    },
    "spectrum.ieee.org": {
        "source": "IEEE Spectrum",
        # 2026-09-10: Canada Rocket Company's news room republishes IEEE
        # Spectrum content (via news_index). Spectrum runs on Rebelmouse;
        # the article body is inside a single unique `<div class="body-description">`
        # wrapper (nested deep inside .widget__body). Verified against
        # https://spectrum.ieee.org/canadian-rockets.
        # SPECTRUM_FIRST_STORY_V1 (tcs-workflows#244): on 2026-11-08 the page had also loaded about five more stories
        # below this one, each with its own div.body-description, and the scrape held 13,778 words of other articles.
        # Only the current post's first story is selected now: 1 match and 1,235 words on the rendered page,
        # 2026-11-11. Single quotes only: Crawl4AI puts selectors inside double-quoted JS strings.
        "selector": "div[data-source='current_post'] div.posts-wrapper > div.widget.post-partial:first-child div.body-description",
        # The image list held the other stories' pictures too: 3 of 4 on a short page, 33 of 34 once more had loaded.
        # The story's own lead photo is in its widget, above the body.
        "image_scope": "div[data-source='current_post'] div.posts-wrapper > div.widget.post-partial:first-child",
    },
    "fireflyspace.com": {
        "source": "Firefly Aerospace",
        "selector": "div.entry-content",
    },
    "relativityspace.com": {
        "source": "Relativity Space",
        # RELATIVITY_SELECTOR — Squarespace BlogItem container; scopes to article body.
        # See 2026-06-21 audit: without this, header CTA + footer logos + Previous/Next
        # pagination + site footer all leak into the scrape.
        "selector": "article.BlogItem",
        "keep_overlays": True,
    },
    "axiomspace.com": {
        "source": "Axiom Space",
        # AXIOM_BODY_V1 (tcs-workflows#244): .w-richtext matched every Webflow rich-text block on the page, so the
        # About boilerplate, media contacts and quote attributions followed the release. .post-rich-text is the body.
        "selector": ".post-rich-text",
        "keep_overlays": True,
    },
    "einpresswire.com": {
        "source": "EIN Presswire",
        "selector": "div.press_release, div.article_column",
    },
    "nordspace.com": {
        "source": "NordSpace",
        "selector": "main, div.section_main",
    },
    "maritimelaunch.com": {
        "source": "Maritime Launch Services",
        # MARITIME_ARTICLE_V1 (tcs-workflows#244): the site moved to Next.js, the Drupal classes matched nothing, and
        # the scraper fell back to the whole page: header, breadcrumb, Related cards, footer and newsletter block.
        # main > article is the release alone. The aside inside it is a Next / Explore launch / Contact MLS box.
        # Not strict_scope: res.html stays the full page, which extract_date() and the image list read.
        # keep_overlays: the release photo is a Next.js "fill" image (position: absolute), which Crawl4AI's overlay
        # remover deleted from res.html, so the list never had it.
        "selector": "main > article",
        "exclude": "aside",
        "keep_overlays": True,
    },
    "reactiondynamics.space": {
        "source": "Reaction Dynamics",
        "selector": "main.prose",
        "networkidle": True,
        "wait": 2.0,
        # tcs-workflows#244: the release photo is a position: absolute image, which Crawl4AI's overlay remover
        # deleted, so no release had an image (0 in all saved runs, 2026-10-15 to 11-11).
        "keep_overlays": True,
        # RD_PULL_QUOTES_V1 (tcs-workflows#244): the Bay Street article repeats seven of its own sentences as pull quotes.
        "exclude": "div.max-w-3xl.mx-auto",
    },
    "canadarocketcompany.com": {
        "source": "Canada Rocket Company",
        "networkidle": True,
        "wait": 3.0,
    },
    # Not a Canadian corp site - AIAA's member magazine. It is here because
    # CRC's corp news index links out to press coverage, so fetch_news_index
    # walks into it and hit "Unknown site" (tcs-workflows#99, 2026-09-26).
    # Expect more of these as the corp sites get written up elsewhere.
    #
    # CLOUDFLARE-GATED ON USER-AGENT. Measured 2026-09-25 on the same article:
    #     python-requests/2.31.0   -> 403 (146 bytes)
    #     "Mozilla/5.0" (bare)     -> 403, Cloudflare interstitial
    #     full Chrome UA           -> 200, 137 KB
    #     full Chrome UA, from VPS -> 200, 137 KB
    # So the VPS IP is not blocked; it is pure UA fingerprinting. Crawl4AI
    # drives a real browser and is fine, but a plain-requests fetcher would
    # fail here silently - do not "simplify" this source to a requests.get.
    "aerospaceamerica.aiaa.org": {
        "source": "Aerospace America",
        # WordPress + Bunyad. The body selector matches the article body
        # (div.post-content.cf.entry-content.content-normal) and an empty ACF div.
        # The hero picture and its caption sit in div.featured, outside the body.
        # Crawl4AI's scorer drops the hero and removes its <img> from the scoped
        # page, so it is seeded from og:image. The hero's figcaption is in the
        # scope so the seeded picture gets its own caption (AA_HERO_V1); the rest
        # of div.featured is left out, or its link would reach the body empty.
        # Sidebar teasers use span[data-bgsrc] backgrounds, not <img>, so Crawl4AI
        # never lists them (tcs-workflows#244, aa F1 and F3).
        "selector": "div.featured figcaption, div.post-content.entry-content",
        "strict_scope": True,
        # AA_TITLE_META_V1 (tcs-workflows#299): the headline's <h1> is outside the body, and STRICT_SCOPE_HEAD_META
        # leaves og:title out, so a caller that passes no title got none. og:title is selected for this site alone.
        "title_meta": True,
    },
}


# ESA's main site. Its sister sites (sci.esa.int, blogs.esa.int and others) have their own templates, so they keep the
# rules every *.esa.int host had before the new site (tcs-workflows#234). div.meta.article__item is the old site's date
# line (ESA_ARTICLE_DATE_V1, tcs-workflows#176). The related-article cards carry a plain div.meta with their OWN dates,
# so they must not match.
ESA_MAIN_HOSTS = ("esa.int", "new.esa.int")
ESA_SISTER_CONFIG = {
    "source": "ESA",
    "selector": "div.article__block, div.abstract, div.meta.article__item",
    "strict_scope": True,
    "keep_overlays": True,
}


def _host_for(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def config_for(url: str) -> dict:
    """Return the SITE_CONFIG entry best matching this URL."""
    host = _host_for(url)
    # Exact match first
    if host in SITE_CONFIG:
        return SITE_CONFIG[host]
    if host.endswith(".esa.int") and host not in ESA_MAIN_HOSTS:
        return ESA_SISTER_CONFIG
    # Suffix match for subdomains
    for domain, cfg in SITE_CONFIG.items():
        if host.endswith("." + domain) or host == domain:
            return cfg
    return {}


def detect_source(url: str) -> str:
    cfg = config_for(url)
    if cfg.get("source"):
        return cfg["source"]
    return _host_for(url) or "Unknown"


# STRICT_SCOPE_HEAD_META (tcs-workflows#102): with css_selector, Crawl4AI (0.9.4) returns only the selected
# elements (document.querySelectorAll per comma-separated selector), so <head> is gone: extract_og_image() came
# back empty on every strict_scope site, and extract_date() lost article:published_time. Selecting these three
# head tags too brings them back. Meta tags carry no text and no images: on 7 pages across the 4 strict_scope
# sites (2026-10-03) content and images were byte-identical with and without them, and thumbnail and date matched
# an unscoped crawl of the same page. extract_date() also reads JSON-LD datePublished and a few rarer meta names;
# none of these four sites needs them, but a new strict_scope site might. og:title is left out on purpose, so the
# title fallback stays the <h1> it has always been.
# meta[name='parsely-pub-date'] (tcs-workflows#176): NASA news releases have no article:published_time, no
# JSON-LD date and no <time>; Parse.ly's tag is their only machine-readable publication time.
# Single quotes only: Crawl4AI puts each selector inside a double-quoted JS string.
STRICT_SCOPE_HEAD_META = ("meta[property='og:image'], meta[name='twitter:image'], meta[property='article:published_time'], "
                          "meta[name='parsely-pub-date']")
# STRICT_SCOPE_TITLE_META (tcs-workflows#299): for a strict_scope site whose <h1> is outside its selector
# ("title_meta": True), og:title is selected too, so extract_og_title() finds it. A meta tag carries no text.
STRICT_SCOPE_TITLE_META = "meta[property='og:title']"


def run_config_for(url: str) -> CrawlerRunConfig:
    """Return a CrawlerRunConfig tuned for the given URL's quirks."""
    site = config_for(url)
    kwargs = dict(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=5,
        exclude_social_media_links=True,
        process_iframes=False,
        remove_overlay_elements=not site.get("keep_overlays", False),
        page_timeout=45_000,
        verbose=False,
    )
    if site.get("selector"):
        # Scope markdown generation to the article body element(s) while still
        # returning the full page HTML (so we can extract <head> metadata).
        # target_elements accepts a list of CSS selectors.
        # STRICT_SCOPE_V1 (2026-07-19): some site templates need css_selector
        # instead of target_elements. target_elements can suppress <img> tags
        # inside <a><figure> wrappers, and it applies the selector to EVERY
        # matching element (e.g. ESA has 8 article__block divs, including nav).
        # css_selector is more restrictive but preserves image extraction.
        if site.get("strict_scope"):
            kwargs["css_selector"] = site["selector"] + ", " + STRICT_SCOPE_HEAD_META + (
                ", " + STRICT_SCOPE_TITLE_META if site.get("title_meta") else "")
        else:
            kwargs["target_elements"] = [s.strip() for s in site["selector"].split(",")]
    if site.get("exclude"):
        kwargs["excluded_selector"] = site["exclude"]
    if site.get("networkidle"):
        kwargs["wait_until"] = "networkidle"
        kwargs["page_timeout"] = 60_000
    if site.get("wait"):
        kwargs["delay_before_return_html"] = site["wait"]
    return CrawlerRunConfig(**kwargs)


# ============================================================
# Metadata extraction (from the full page HTML; for strict_scope sites, from the
# STRICT_SCOPE_HEAD_META tags selected alongside the article)
# ============================================================
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def extract_date(html: str, url: str = "") -> str:
    """Best-effort date extraction with fallbacks for sites that omit meta tags.

    Order:
      0. ESA's date bar, on its main site (returned as YYYY-MM-DD)
      1. Standard meta tags (article:published_time, datePublished, etc.)
      2. <time datetime="...">, then Parse.ly's parsely-pub-date meta (NASA news releases)
      3. Site markup that shows the date only as text: ESA's old date line, and Reaction Dynamics (both
         returned as YYYY-MM-DD), and Axiom Space's press-release date and SpacePolicyOnline's byline (human format)
      4. WordPress-style URL slug /YYYY/MM/DD/ (clean ISO-like format)
      5. Spaceflight-Now-style <span class="entry-meta-date updated"> (human format, last resort)
    """
    if not html and not url:
        return ""

    if html:
        # ESA_MULTIMEDIA_DATE_V1 (tcs-workflows#232, widened by #234): every page on ESA's main site shows the date
        # only as text, beside an sr-only label:
        # <span class="favorites-views__count">15 OCT. 2026</span> <span>publication date</span>.
        # Empty copies of the same block sit in the hidden image modals, so the pattern needs the digits.
        # This comes first because the HTML holds all of <main>, so a related card's date mustn't win.
        if _host_for(url) in ESA_MAIN_HOSTS:
            m = re.search(
                r'favorites-views__count["\'][^>]*>\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\s*</span>\s*'
                r'<span[^>]*>\s*publication date',
                html,
                re.IGNORECASE,
            )
            if m and m.group(2)[:3].lower() in _MONTHS:
                return f"{m.group(3)}-{_MONTHS[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}"

        # Standard machine-readable patterns next
        patterns = [
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)["\']',
            r'<time[^>]+datetime=["\']([^"\']+)["\']',
            # After <time>, so a page that already had a date keeps it as before (tcs-workflows#176).
            r'<meta[^>]+name=["\']parsely-pub-date["\'][^>]+content=["\']([^"\']+)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                return m.group(1).strip()

        # MARITIMELAUNCH_HEADER_DATE_V1 (tcs-workflows#244): the Next.js release header shows the date only as text,
        # "Nov 2, 2026 / 2 min read". The dateline under it ("HALIFAX, NOVA SCOTIA, November 2, 2026") is the backup.
        # MARITIMELAUNCH_HEADER_DATE_V2 (tcs-workflows#273): from the 2026-11-12 release on, the header says "3 min"
        # (no "read"), and the dateline's dash sits inside the bold: "<strong>HALIFAX, ..., November 12, 2026 – </strong>".
        if _host_for(url) == "maritimelaunch.com":
            m = re.search(r'<span>([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})</span>\s*<span aria-hidden="true">/</span>\s*'
                          r'<span>\d+\s+min(?:\s+read)?</span>', html)
            if m and m.group(1).lower() in _MONTHS:
                return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
            m = re.search(r'<strong>\s*[A-Z][A-Z .,]+,\s*([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\s*'
                          r'(?:(?:[–—-]|&ndash;|&mdash;|&#821[12];)\s*)?</strong>', html)
            if m and m.group(1)[:3].lower() in _MONTHS:
                return f"{m.group(3)}-{_MONTHS[m.group(1)[:3].lower()]:02d}-{int(m.group(2)):02d}"

        # ESA_ARTICLE_DATE_V1 (tcs-workflows#176; replaces tcs-docs#122's ESA_VIEWS_LIKES_V1, a VPS-only
        # patch that was lost and could never see the date through strict_scope): ESA pages have no date
        # meta, JSON-LD or <time>. The article's own date is the first <span> of
        # <div class="meta article__item"> under the <h1>, as DD/MM/YYYY. Related-article cards use a plain
        # <div class="meta"> with their own dates, so requiring article__item keeps those out.
        m = re.search(
            r'<div[^>]*class=["\'](?=[^"\']*\bmeta\b)(?=[^"\']*\barticle__item\b)[^"\']*["\'][^>]*>\s*'
            r'<span[^>]*>\s*(\d{2})/(\d{2})/(\d{4})\s*</span>',
            html,
            re.IGNORECASE,
        )
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            return f"{y}-{mo}-{d}"

        # AXIOM_PRESS_RELEASE_DATE_V1 (tcs-workflows#176): Axiom Space pages carry no date meta either;
        # the release date sits in <div class="press-release-date">October 6, 2026</div> above the <h1>.
        m = re.search(
            r'<div[^>]*class=["\'][^"\']*\bpress-release-date\b[^"\']*["\'][^>]*>\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})\s*</div>',
            html,
        )
        if m:
            return m.group(1).strip()

        # SPACEPOLICYONLINE_POSTED_V1 (tcs-workflows#232): the byline and the meta description both say
        # "Posted: November 1, 2026 5:31 pm ET". The first date is the post's own, not "Last Updated".
        if _host_for(url) == "spacepolicyonline.com":
            m = re.search(r'\bPosted:\s*([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})', html)
            if m:
                return m.group(1).strip()

        # REACTION_DYNAMICS_TIME_V1 (tcs-workflows#232): <time>20 OCTOBER, 2026</time> with no datetime attribute.
        # The first one is the post's own date; the related-post cards come after it.
        if _host_for(url) == "reactiondynamics.space":
            m = re.search(r'<time\b[^>]*>\s*(\d{1,2})\s+([A-Za-z]{3,9}),?\s+(\d{4})\s*</time>', html)
            if m and m.group(2)[:3].lower() in _MONTHS:
                return f"{m.group(3)}-{_MONTHS[m.group(2)[:3].lower()]:02d}-{int(m.group(1)):02d}"

    # Prefer URL slug fallback (clean ISO-like YYYY-MM-DD) over visible span
    if url:
        m = re.search(r"/(\d{4})/(\d{1,2})/(\d{1,2})/", url)
        if m:
            y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
            return f"{y}-{mo}-{d}"

    # Last resort: scrape a visible date span (human format, e.g. "May 12, 2026")
    if html:
        m = re.search(
            r'<span[^>]*class=["\'][^"\']*entry-meta-date[^"\']*["\'][^>]*>(?:[^<]|<(?!a))*<a[^>]*>([^<]+)</a>',
            html,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()

    return ""


# PLACEHOLDER_PICTURES_V1 (tcs-workflows#273): pictures that stand in for a photo of the story: Maritime Launch's default
# press tile, and the NASA insignia on NASA news releases. The thumbnail (PLACEHOLDER_OG_V1), the image list and the
# article's picture count (ARTICLE_PICTURES_V1) all skip them.
_PLACEHOLDER_PICTURES = re.compile(r"Default_MLS_Hold_Press|nasa_meatball", re.IGNORECASE)


def extract_og_image(html: str) -> str:
    """Pull og:image (preferred) or twitter:image as the thumbnail."""
    if not html:
        return ""
    for pat in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            # ESA's media pages put their own host in front: https://www.esa.inthttps://media.esa.int/... (tcs-workflows#234)
            u = re.sub(r"^https?://[^/]*(?=https?://)", "", m.group(1).strip())
            # OG_IMAGE_UNESCAPE_V1 (tcs-workflows#244): the attribute's entities (Reaction Dynamics' ?auto=format&amp;w=1200).
            import html as _html
            u = _html.unescape(u)
            # PLACEHOLDER_OG_V1 (tcs-workflows#244): two sites' share image is a logo, not a photo of the story: Maritime
            # Launch's default press tile, and the NASA insignia on NASA news releases.
            if _PLACEHOLDER_PICTURES.search(u):
                return ""
            return u
    return ""


def extract_og_title(html: str) -> str:
    # STOKE_OG_TITLE_UNESCAPE: og:title attribute values can be double-escaped
    # (Stoke returns "&amp;nbsp;" instead of " "). Unescape so downstream
    # consumers get clean text.
    if not html:
        return ""
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        return ""
    import html as _html
    # STRIP_NBSP_OG_TITLE — \xa0 (non-breaking space) survives .strip(); explicit.
    return _html.unescape(m.group(1)).strip(' \xa0\t\n\r')


_DROP_PATTERNS = re.compile(
    r"("
    r"icon|logo|avatar|sprite|pixel|tracking|spacer|"
    # AXIOM_SITE_ASSETS_V1 (tcs-workflows#244): Axiom's Webflow nav/footer bucket (the CCPA "no sell" icon), a
    # Webflow sample bucket and Webflow's placeholder. Article photos are in a different bucket.
    r"671fb31e0ed79c76c93e0fee/|6618f4b8b5c1b5cf8a47b8ef/|plugins/Basic/assets/|"
    # NASA's insignia, the only picture on many news releases (tcs-workflows#244)
    r"nasa_meatball|"
    # ESA / generic thumbnail naming conventions
    r"_card_(small|medium|full|large)|"
    r"_thumb(nail)?|"
    # Banner / promo / ad slots
    r"broadstreet|adserver|advertis|"
    r"-banner-|-cal-|/ads/|"
    # ESA sitewide boilerplate
    r"/flag_[a-z]{2,3}\.|/flags/|/pillars/design/|/buttons/"
    r")",
    re.IGNORECASE,
)

# Sizing query params that scale an image down — strip them so we point at
# the full-size original instead. (Jetpack i0.wp.com, NASA.gov dynamicimage,
# and most CDNs return full-size when these are absent.)
_SIZING_PARAMS = {"w", "h", "resize", "fit", "crop", "quality"}



def _sfn_page_title(html: str) -> str:
    """SFN_PAGE_TITLE_V1 (tcs-workflows#244, F6): Spaceflight Now rewrites a story's headline once the event happens
    ("Falcon 9 to launch ..." becomes "Falcon 9 launches ..."), but its feed keeps the first one, so a scrape a day later
    had the old headline over the new story. The page's og:title is the current headline (its <title> adds
    " - Spaceflight Now"). The value is read up to its own closing quote, so an apostrophe inside it doesn't end it;
    without og:title, the h1. Entities are decoded. Empty when the page has neither."""
    import html as _html
    if not html:
        return ""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=(["\'])(.*?)\1', html, re.IGNORECASE | re.S)
    t = _html.unescape(m.group(2)) if m else ""
    if not t.strip():
        h = re.search(r'<h1[^>]*>([\s\S]*?)</h1>', html, re.IGNORECASE)
        t = _html.unescape(re.sub(r'<[^>]+>', '', h.group(1))) if h else ""
    return re.sub(r'\s+', ' ', t).strip()


def _article_picture_count(html: str, selector: str):
    """ARTICLE_PICTURES_V1 (tcs-workflows#244): how many pictures (<img>, or <picture> without one) sit inside the
    elements the site's selector matches: the article itself, not the page around it. Crawl4AI's media list covers the
    whole page, so it can't tell "the article has no pictures" from "the scraper lost them". The Healthcheck reads it:
    no images and none in the article is the page's own state, not a scrape problem (Relativity's monthly updates
    carry only a video). None when it can't be told: no selector, no bs4, or the selector matches nothing.
    Stand-in pictures (PLACEHOLDER_PICTURES_V1) don't count."""
    if not html or not selector:
        return None
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        scopes = soup.select(selector)
    except Exception:
        return None
    if not scopes:
        return None
    seen = set()
    for el in scopes:
        for pic in el.find_all(["img", "picture", "amp-img"]):
            if pic.name == "picture" and pic.find("img") is not None:
                continue   # its <img> is counted
            # PLACEHOLDER_PICTURES_V1 (tcs-workflows#273): a release whose only picture is Maritime Launch's press tile
            # or the NASA insignia has no picture of its own, so the Healthcheck mustn't warn that the list lost one.
            srcs = " ".join(str(pic.get(a) or "") for a in ("src", "data-src", "srcset"))
            if pic.name == "picture":
                srcs += " " + " ".join(str(s.get("srcset") or "") for s in pic.find_all("source"))
            if _PLACEHOLDER_PICTURES.search(srcs):
                continue
            seen.add(id(pic))
    return len(seen)


def extract_h1_title(html: str) -> str:
    """Fallback title extractor: first <h1>...</h1> when og:title is absent
    (e.g., EIN Presswire articles)."""
    if not html:
        return ""
    m = re.search(r'<h1[^>]*>([\s\S]*?)</h1>', html, re.IGNORECASE)
    if not m:
        return ""
    # Strip nested tags + entities
    raw = m.group(1)
    raw = re.sub(r'<[^>]+>', '', raw)
    raw = raw.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&#39;', "'")
    return raw.strip()

def _strip_sizing_query(url: str) -> str:
    """Remove sizing query params (w, h, resize, fit, crop, quality) from a URL.
    Keeps non-sizing params like ssl=1, v=2, etc."""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        parsed = urlparse(url)
        if not parsed.query:
            return url
        kept = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in _SIZING_PARAMS]
        return urlunparse(parsed._replace(query=urlencode(kept)))
    except Exception:
        return url


def _clean_alt(raw) -> str:
    """Normalize raw alt text for safe markdown emission. Strips whitespace,
    flattens newlines, escapes brackets, drops obviously-not-alt-text values
    (filenames, empty strings, single chars)."""
    if not raw or not isinstance(raw, str):
        return ""
    alt = raw.strip().replace("\n", " ").replace("\r", " ")
    alt = re.sub(r"\s+", " ", alt)
    # tcs-workflows#244: a stray leading period (SpaceQ's '.Illustration of ...'). '.NET' and '...' are left alone.
    alt = re.sub(r"^\.(?=[A-Z][a-z])", "", alt)
    # tcs-workflows#244: a bare timestamp is not a description (a NASA Science astronaut photo's alt).
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", alt):
        return ""
    if re.search(r"\.(jpe?g|png|gif|webp|svg|nef|cr2|cr3|arw|dng|tiff?|raw)$", alt, re.IGNORECASE):
        return ""
    if alt.lower() in {"image", "img", "photo", "picture", "thumbnail", "untitled", ""}:
        return ""
    if len(alt) < 3:
        return ""
    # ESA_TEMPLATE_ALT_V1 (tcs-docs#121, restored by tcs-workflows#176): ESA multimedia pages leak unrendered
    # CMS template placeholders into image alt, e.g. `$video.data_map.short_description.content`. The August
    # fix was a VPS-only hot patch and was lost on a redeploy; this is the same check, now in git.
    if re.match(r"^\$[a-zA-Z_][a-zA-Z_0-9]*(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+", alt):
        return ""
    # SPACESCOUT_ALT_V1 (2026-07-05 audit): Space Scout image alts sometimes
    # bleed in sidebar "Related Articles" metadata: `<headline> <author>
    # <Month> <day>, <year><Month> <day>, <year> <count>`. Detect the doubled
    # month-day-year pattern (with optional trailing number) and drop.
    if re.search(
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}",
        alt,
    ):
        return ""
    # NAV_CHROME_ALT_V1 (2026-08-06 audit): NASA + ESA `<img>`
    # elements with no `alt` attribute cause Crawl4AI's extractor to inherit
    # surrounding page text as a fallback alt — which on those sites is
    # site-wide navigation menus, download-button labels, or view/like
    # counters. Truncating at 200 chars doesn't help: the first 200 chars
    # of a nav menu is still nav menu ("Explore News & Events News & Events
    # News Releases..."). Drop when we see the tells.
    #
    # Signals we key on (all conservative — must be obvious chrome, not
    # normal photo captions):
    #   - the same 3-word phrase repeated back-to-back ("News & Events
    #     News & Events" — nav category duplication)
    #   - "HI-RES JPG (" / "HI-RES TIF (" — ESA video/image page download
    #     button labels
    #   - digit-space-"views" or digit-space-"likes" — page metadata
    if re.search(r"HI-RES\s+(?:JPG|TIF|PNG)\s*\(", alt, re.IGNORECASE):
        return ""
    if re.search(r"\b\d+\s+(?:views?|likes?)\b", alt, re.IGNORECASE):
        return ""
    # Duplicated 3+word phrase back-to-back (with optional single space
    # between). NASA nav: "News & Events News & Events".
    if re.search(r"\b((?:\w+\W+){2,4}\w+)\s*\1\b", alt):
        return ""

    # Cap alt length -- alt strings longer than 200 chars are almost
    # always content bleed (Crawl4AI's extraction inheriting the page
    # body into the alt attribute). Truncate at a word boundary with
    # ellipsis so the markdown ![alt](url) still works and accessibility
    # tooling gets a usable hint.
    MAX_ALT_LEN = 200
    if len(alt) > MAX_ALT_LEN:
        alt = alt[:MAX_ALT_LEN].rsplit(" ", 1)[0] + "…"
    # Escape markdown brackets in alt so they don't break ![alt](url) syntax.
    alt = alt.replace("[", "(").replace("]", ")")
    return alt


# IMAGE_OWN_ALT_V1 (tcs-workflows#244): an image list entry describes its own picture, or nothing. Crawl4AI fills an
# image's `desc` with the text of the nearest element around it that has any, which for a picture with an empty alt
# is often the article's opening paragraph or another picture's caption (Firefly, Space Scout, EIN Presswire, SpaceQ,
# NASA). The list now takes the img's own alt, then its own caption (a <figure>'s <figcaption>, a WordPress
# .wp-caption, EIN's gallery tile), then an italic caption line directly under it in the body, and otherwise stays
# empty. The per-site junk-image filters still read the old desc-based text, so what they drop doesn't change.
def _image_key(u: str) -> str:
    """One key per picture across the media list, the page HTML and the body: the file name, lower-cased, without the
    query, a WordPress size suffix (-1024x512) or -scaled, plus the id parameter where there is one (Spectrum names
    many files image.jpg)."""
    u = u or ""
    name = re.sub(r"[?#].*$", "", u).rsplit("/", 1)[-1].lower()
    name = re.sub(r"-scaled(?=\.[a-z0-9]{3,4}$)", "", re.sub(r"-\d+x\d+(?=\.[a-z0-9]{3,4}$)", "", name))
    img_id = re.search(r"[?&]id=([^&#]+)", u)
    return name + ("?id=" + img_id.group(1) if img_id else "")


_CAPTION_CLASSES = ".wp-caption-text, .pswp-caption-content, .hds-caption-text, .figure-caption"


def _img_src(img) -> str:
    """The img's real address: a lazy-load data: placeholder in src gives way to data-src and the like."""
    for a in ("src", "data-src", "data-lazy-src", "data-orig-file"):
        v = img.get(a) or ""
        if v and not v.startswith("data:"):
            return v
    return ""


def _caption_of(img):
    """The caption of the picture's own frame: the nearest ancestor, up to six levels, that holds no other picture
    and has a caption. A <figure>'s <figcaption>, or one beside the figure (NASA's .hds-media-wrapper), a WordPress
    .wp-caption-text, a PhotoSwipe .pswp-caption-content (Ars Technica), or EIN Presswire's gallery tile <p>. An
    ancestor with another picture in it (a gallery, the article body) ends the search, so a caption is never shared."""
    own = _image_key(_img_src(img))
    node = img
    for _ in range(6):
        node = node.parent
        if node is None or not getattr(node, "name", None) or node.name in ("article", "main", "body", "html"):
            return None
        if {_image_key(_img_src(i)) for i in node.find_all("img")} - {own, ""}:
            return None
        cap = node.find("figcaption", recursive=False) or node.select_one(_CAPTION_CLASSES)
        if cap is None and node.name == "div" and "pr_image" in (node.get("class") or []):
            cap = node.find("p", recursive=False)
        if cap is not None and cap.get_text(strip=True):
            return cap
    return None


def _html_captions(html: str) -> dict:
    """{_image_key: caption} for the pictures in the page that sit in a captioned frame. Empty without bs4."""
    if not html or "<img" not in html:
        return {}
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {}
    caps = {}
    for img in soup.find_all("img"):
        cap = _caption_of(img)
        text = _clean_alt(cap.get_text(" ", strip=True)) if cap is not None else ""
        if not text:
            continue
        srcs = [img.get(a) for a in ("src", "data-src", "data-lazy-src", "data-orig-file")]
        for a in ("srcset", "data-srcset"):
            srcs += [p.strip().split()[0] for p in (img.get(a) or "").split(",") if p.strip()]
        for s in srcs:
            if s and not s.startswith("data:"):
                caps.setdefault(_image_key(s), text)
    return caps


def _drop_caption_line(body: str, caption: str) -> str:
    """AA_HERO_V1 (tcs-workflows#244): the body without a line that is only this caption, emphasis and spacing aside.
    A seeded picture isn't in the body, so its caption would stand there alone."""
    want = " ".join((caption or "").split()).casefold()
    if not want or not body:
        return body
    lines = body.split("\n")
    keep = [ln for ln in lines if " ".join(ln.strip().strip("*_").split()).casefold() != want]
    if len(keep) == len(lines):
        return body
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).lstrip("\n")


def _featured_caption(html: str) -> str:
    """AA_HERO_V1 (tcs-workflows#244): the text of a WordPress theme's featured-picture caption
    (figcaption.featured-img-caption) in the scoped page. Crawl4AI removes a scored-out hero <img>, so the caption
    can't be found from the picture."""
    m = re.search(r"<figcaption\b[^>]*\bclass=[\"'][^\"']*\bfeatured-img-caption\b[^>]*>(.*?)</figcaption>",
                  html or "", re.S | re.I)
    if not m:
        return ""
    import html as _html
    return _clean_alt(" ".join(_html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()))


def _axiom_key(u: str) -> str:
    """_image_key plus Webflow's -p-500 size suffix and URL escapes, so a srcset variant matches its original."""
    return re.sub(r"-p-\d{3,4}(?=\.[a-z0-9]{3,4}$)", "", _image_key(unquote(u or "")))


def _axiom_images(images: list, html: str) -> list:
    """AXIOM_IMAGES_V1 (tcs-workflows#244, axm-4 and axm-5). Axiom's quote blocks carry a headshot of each person
    quoted (img.press-release-quote-headshot); those aren't the release's pictures, so they leave the list. A picture
    in the release body (.post-rich-text) that the media list missed is added, with its own caption where it has one.
    Without bs4 only the headshot rule runs."""
    if not html:
        return images
    heads = set()
    for tag in re.findall(r"<img\b[^>]*>", html, re.IGNORECASE):
        if re.search(r"""\bclass=["'][^"']*\bpress-release-quote-headshot\b""", tag, re.IGNORECASE):
            for m in re.finditer(r"""\s(?:src|data-src)=["']([^"']+)["']""", tag):
                heads.add(_axiom_key(m.group(1)))
    out = [i for i in images if not (isinstance(i, dict) and _axiom_key(i.get("url", "")) in heads)]
    if "post-rich-text" not in html:
        return out
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out
    have = {_axiom_key(i.get("url", "")) for i in out if isinstance(i, dict)}
    caps = None
    for body in soup.select(".post-rich-text"):
        for img in body.find_all("img"):
            src = _img_src(img)
            k = _axiom_key(src)
            if not src.startswith("http") or not k or k in have or k in heads:
                continue
            if caps is None:
                caps = {_axiom_key(key): v for key, v in _html_captions(html).items()}
            have.add(k)
            out.append({"url": src, "alt": caps.get(k) or _clean_alt(img.get("alt"))})
    return out


def _esa_video_alts(images: list, html: str) -> list:
    """ESA_VIDEO_TITLE_V1 (tcs-workflows#244, esa-06). On ESA's site a video's poster frame has alt="", but its video
    block names the video: <div class="video ..." data-video-title="..." data-media-id="402352">. The media id is in
    the poster's URL (/on-demand/video/YYYY/MM/<id>/), so a poster with no description of its own takes that title.
    Pictures with their own alt, and pictures that aren't posters, are left as they are."""
    if not html or "data-video-title" not in html:
        return images
    import html as _html
    titles = {}
    for tag in re.findall(r"<div\b[^>]*\bdata-video-title=[^>]*>", html, re.IGNORECASE):
        name = re.search(r"""\bdata-video-title=(?:"([^"]*)"|'([^']*)')""", tag)
        mid = re.search(r"""\bdata-media-id=["']?(\d+)""", tag)
        if name and mid:
            title = _clean_alt(_html.unescape(name.group(1) if name.group(1) is not None else name.group(2)))
            if title:
                titles.setdefault(mid.group(1), title)
    if not titles:
        return images
    out = []
    for img in images:
        if isinstance(img, dict) and not (img.get("alt") or "").strip():
            m = re.search(r"/on-demand/video/\d{4}/\d{2}/(\d+)/", img.get("url", ""))
            if m and m.group(1) in titles:
                img = {**img, "alt": titles[m.group(1)]}
        out.append(img)
    return out


# The picture (maybe inside a link), then on the same line or the next one a line that is wholly italic.
_BODY_CAPTION = re.compile(
    r"!\[[^\]\n]*\]\((?P<url>[^)\s]+)[^)\n]*\)(?:\]\([^)\n]*\))?[ \t]*\n?[ \t]*"
    r"(?:_(?P<u>[^\n]{10,400}?)_|\*(?P<s>[^*\n]{10,400})\*)[ \t]*(?=\n|\Z)")


def _body_captions(body: str) -> dict:
    """{_image_key: caption} from the italic line directly under each picture in the body: how Firefly and Space
    Scout set their captions (an empty alt, then an <em> paragraph)."""
    caps = {}
    for m in _BODY_CAPTION.finditer(body or ""):
        text = _clean_alt((m.group("u") or m.group("s") or "").strip(" *_"))
        if text:
            caps.setdefault(_image_key(m.group("url")), text)
    return caps


# MARITIME_IMAGES_V1 (tcs-workflows#244): Crawl4AI's media list for Maritime Launch held the header logo, the Related
# cards' covers and the default press tile, but not the release's own photo. The list is built from the release's
# own <article> instead: its /api/media/file/ pictures, unwrapped from Next.js's /_next/image optimizer.
MARITIME_PLACEHOLDER = "Default_MLS_Hold_Press"


def _maritime_article_images(html: str) -> list:
    chunk = _first_article_html(html)
    if not chunk:
        return []
    out, seen = [], set()
    for tag in re.findall(r"<img\b[^>]*>", chunk):
        s = re.search(r'\ssrc="([^"]+)"', tag)
        if not s:
            continue
        src = s.group(1).replace("&amp;", "&")
        p = urlparse(src)
        if p.path == "/_next/image":
            q = parse_qs(p.query).get("url")
            if not q:
                continue
            src = unquote(q[0])
        if not src.startswith("/api/media/file/") or MARITIME_PLACEHOLDER in src:
            continue
        absu = "https://www.maritimelaunch.com" + src
        if absu in seen:
            continue
        seen.add(absu)
        alt = re.search(r'\salt="([^"]*)"', tag)
        out.append({"url": absu, "alt": _clean_alt(alt.group(1) if alt else "")})
    return out


def _first_article_html(html: str) -> str:
    """The page's first <article> element, as HTML, or "" when there is none."""
    m = re.search(r"<article\b", html or "")
    if not m:
        return ""
    end = html.find("</article>", m.start())
    return html[m.start():end] if end > m.start() else html[m.start():]


def _images_in_scope(images: list, html: str, selector: str, url: str = "") -> list:
    """IMAGE_SCOPE_V1 (tcs-workflows#244): keep the pictures that sit inside the first element the site's image_scope
    selector matches. Crawl4AI's media list covers the whole page, so other stories' pictures reach it: "more news"
    cards on Stoke, and the later stories and sidebars on IEEE Spectrum. Matching is on the file name, which is also
    visible inside a /_next/image?url=... wrapper, plus the id parameter where there is one (Spectrum names many
    files image.jpg). With no bs4, or when the selector matches nothing, the list is left as it is."""
    if not images or not html or not selector:
        return images
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        scope = soup.select_one(selector)
    except Exception:
        return images
    if scope is None:
        _log_event({"event": "image_scope_unmatched", "url": url, "selector": selector})
        return images
    chunk = str(scope)
    kept = []
    for img in images:
        u = (img.get("url", "") if isinstance(img, dict) else str(img)) or ""
        name = re.sub(r"[?#].*$", "", u).rsplit("/", 1)[-1]
        img_id = re.search(r"[?&]id=([^&#]+)", u)
        if name and name in chunk and (not img_id or "id=" + img_id.group(1) in chunk):
            kept.append(img)
    return kept


def _widest_srcset_url(value: str) -> str:
    """PLANETARY_SRCSET_V1 (tcs-workflows#244): the widest candidate of a srcset value ('url 200w, url 1200w'), or the
    densest ('url 1x, url 2x'). A lazy-load placeholder (data:) never wins. Candidates are split at the comma after a
    descriptor, so a URL with a comma in it stays whole."""
    best, best_u = -1.0, ""
    for part in re.split(r"(?<=\d[wx])\s*,\s*", value.strip()):
        bits = part.strip().split()
        if not bits or bits[0].startswith("data:"):
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)[wx]", bits[1]) if len(bits) > 1 else None
        score = float(m.group(1)) if m else 0.0
        if score > best:
            best, best_u = score, bits[0]
    return best_u


def collect_images(result, body: str = "", seen_alts: dict = None) -> list:
    """Collect content image URLs (with alt text) from crawl4ai's media dict,
    deduped to one canonical URL per source image, with scaled-down variants
    upgraded to full-size where the URL pattern allows.

    Returns a list of {"url": str, "alt": str} dicts in source order. Alt text
    comes from the highest-scoring variant of each canonical group, then from
    the picture's own caption in the page or the body (IMAGE_OWN_ALT_V1).

    body is the article's final markdown, for caption lines. When seen_alts is
    a dict it gets {url: the old desc-based text}, which the junk filters read.
    """
    media = getattr(result, "media", None) or {}
    raw_imgs = media.get("images", []) or []

    # RELATIVE_URL_RESOLVE_V1 (2026-07-19, T3): some CMSes (ESA, some Drupal
    # variants) serve <img src> as absolute-path URLs (e.g. /var/esa/storage/
    # images/...) rather than protocol-absolute. Resolve them against the
    # crawled URL's origin before the format check.
    from urllib.parse import urlparse
    base_url = getattr(result, "url", "") or ""
    if base_url:
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    else:
        origin = ""

    if _host_for(base_url) == "maritimelaunch.com":
        return _maritime_article_images(getattr(result, "html", "") or "")

    ordered = []
    best = {}

    for img in raw_imgs:
        if not isinstance(img, dict):
            continue
        src = img.get("src")
        if not src:
            continue
        # PLANETARY_SRCSET_V1 (tcs-workflows#244): some lazy-loaded pictures arrive with a whole srcset as their src,
        # sometimes with a data: placeholder first. Only a value with width or density descriptors counts, so a URL
        # that merely holds a space isn't split.
        if re.search(r"\s\d+(?:\.\d+)?[wx]\s*(?:,|$)", src.strip()):
            src = _widest_srcset_url(src)
        if not src or src.startswith("data:"):
            continue
        # NEXT_IMAGE_UNWRAP_V1 (tcs-workflows#244): Next.js sites serve pictures through /_next/image?url=<path>.
        _nx = urlparse(src)
        if _nx.path == "/_next/image" and parse_qs(_nx.query).get("url"):
            src = unquote(parse_qs(_nx.query)["url"][0])
        # Resolve path-relative URLs against the source's origin
        if src.startswith("/") and not src.startswith("//") and origin:
            src = origin + src
        # Protocol-relative URLs get https, so an author who picks one gets a working link (tcs-workflows#232).
        if src.startswith("//"):
            src = "https:" + src
        if not re.match(r"^(https?:)?//", src):
            continue
        if _DROP_PATTERNS.search(src):
            continue
        w = img.get("width")
        h = img.get("height")
        if (isinstance(w, int) and w < 100) or (isinstance(h, int) and h < 100):
            continue

        src_clean = _strip_sizing_query(src)
        path_no_query = re.sub(r"\?.*$", "", src_clean)
        path_norm = re.sub(r"^https?://", "//", path_no_query)
        canonical = re.sub(r"-\d+x\d+(?=\.[a-zA-Z]{3,4}$)", "", path_norm)
        canonical = re.sub(r"-scaled(?=\.[a-zA-Z]{3,4}$)", "", canonical)
        # AXIOM_WEBFLOW_SIZE_V1 (tcs-workflows#244): Webflow names its srcset sizes -p-500, -p-800, ... -p-3200.
        canonical = re.sub(r"-p-\d{3,4}(?=\.[a-zA-Z]{3,4}$)", "", canonical)
        # ESA_NEW_SITE_V1 (tcs-workflows#234): each ESA picture arrives as up to ten variants (sizes, 2x, a crop per
        # screen width). They share an asset number in the path, so the picture gets one entry: the first variant,
        # which is the page's own <img>.
        esa_asset = re.search(r"^//media\.esa\.int/.*/(\d+)/image-thumb__\1__", path_norm)
        if esa_asset:
            canonical = "//media.esa.int/asset/" + esa_asset.group(1)
        # PLANETARY_SIZE_V1 (tcs-workflows#244): planetary.org puts the size in a folder
        # (_576x432_crop_center-center_82_line/), so one photo arrived up to nine times.
        if "planetary.s3.amazonaws.com" in canonical:
            canonical = re.sub(r"\.webp$", "", re.sub(r"_\d+x\d+_crop_center-center_82_line/", "", canonical))

        # NASA_SCIENCE_IMAGE_KEY_V1 (tcs-workflows#244): NASA Science serves one photo as /content/dam/... and as
        # /dynamicimage/assets/..., as renditions (cq5dam.thumbnail.319.319.png) and as an _lrg copy. They get one key,
        # and a rendition, which is a thumbnail, never beats the photo itself.
        nasa_sci = path_norm.startswith("//assets.science.nasa.gov/")
        if nasa_sci:
            k = unquote(path_norm).replace("\u2019", "'")
            k = re.sub(r"/jcr:content/renditions/.*$", "", k).replace("/content/dam/", "/dynamicimage/assets/")
            canonical = re.sub(r"_lrg(?=\.[a-zA-Z]{3,4}$)", "", k).lower()

        is_bare = canonical == path_norm and "?" not in src_clean
        if is_bare:
            score = float("inf")
        else:
            dim_m = (re.search(r"-(\d+)x(\d+)(?=\.[a-zA-Z]{3,4})", src_clean)
                     or re.search(r"_(\d+)x(\d+)_crop_center-center_82_line/", src_clean))
            score = int(dim_m.group(1)) * int(dim_m.group(2)) if dim_m else 1
        if nasa_sci:
            score = 0 if "/jcr:content/renditions/" in path_norm else (3 if re.search(r"_lrg\.[a-zA-Z]{3,4}$", path_norm) else 2)

        alt = _clean_alt(img.get("alt"))
        seen = _clean_alt(img.get("alt") or img.get("desc"))   # the pre-IMAGE_OWN_ALT_V1 text, for the junk filters

        # ESA_NEW_SITE_V1 (tcs-workflows#234): its cards use 144x96 thumbnails, and "fallback image N" is its CMS's
        # placeholder. Neither is usable in a post.
        if "//media.esa.int/" in src_clean and ("__fill_small/" in src_clean or seen.lower().startswith("fallback image")):
            continue

        if canonical not in best:
            ordered.append(canonical)
            best[canonical] = (src_clean, score, alt, seen)
        elif score > best[canonical][1]:
            best[canonical] = (src_clean, score, alt or best[canonical][2], seen or best[canonical][3])
        else:
            b = best[canonical]
            best[canonical] = (b[0], b[1], b[2] or alt, b[3] or seen)

    out = [{"url": best[c][0], "alt": best[c][2]} for c in ordered]
    if seen_alts is not None:
        seen_alts.update({best[c][0]: best[c][3] for c in ordered})
    html = getattr(result, "html", "") or ""
    image_scope = config_for(base_url).get("image_scope")
    if image_scope:
        out = _images_in_scope(out, html, image_scope, base_url)
    if any(not i["alt"] for i in out):
        page_caps, body_caps = _html_captions(html), _body_captions(body)
        for i in out:
            if not i["alt"]:
                k = _image_key(i["url"])
                i["alt"] = page_caps.get(k) or body_caps.get(k) or ""
    return out


def get_content(result) -> str:
    """Pick the best markdown representation of the article body."""
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    if isinstance(md, str):
        return md.strip()
    for attr in ("raw_markdown", "fit_markdown", "markdown_with_citations"):
        val = getattr(md, attr, None)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# ============================================================
# Core scrape
# ============================================================
async def _fetch(crawler: AsyncWebCrawler, url: str, cfg: CrawlerRunConfig):
    """Fetch once, returning (result, error_message_or_none). Never raises."""
    try:
        res = await crawler.arun(url=url, config=cfg)
    except Exception as e:
        # Keep the "Crawler exception:" prefix — downstream may grep for it —
        # but include the class name so EPIPE / TimeoutError etc are visible
        # to _is_retryable and to whoever reads the alert.
        return None, f"Crawler exception: {type(e).__name__}: {e}"
    if not getattr(res, "success", False):
        err = getattr(res, "error_message", None) or "Crawler returned unsuccessful result"
        return res, err
    return res, None


async def _fetch_with_retry(
    crawler: AsyncWebCrawler,
    url: str,
    cfg: CrawlerRunConfig,
    max_attempts: int = MAX_FETCH_ATTEMPTS,
):
    """Wrap _fetch with a small linear-backoff retry on transient failures.

    Returns (result, error_message_or_none, attempts_made). Retries only when
    the error looks transient (see RETRYABLE_ERR_PATTERNS) or when the crawler
    returned nothing at all. Never raises.
    """
    attempts_made = 0
    res = None
    err = None
    for attempt in range(1, max_attempts + 1):
        attempts_made = attempt
        res, err = await _fetch(crawler, url, cfg)
        if err is None:
            return res, None, attempts_made
        challenge = _is_challenge_retryable(url, err)
        if attempt < max_attempts and (_is_retryable(err) or res is None or challenge):
            backoff = CHALLENGE_RETRY_PAUSE_S if challenge else attempt
            _log_event({
                "event": "scrape_retry",
                "url": url,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error": err,
                "backoff_s": backoff,
                "reason": "cloudflare_challenge" if challenge else "transient",
            })
            await asyncio.sleep(backoff)
            continue
        break
    return res, err, attempts_made


def _site_post_process(url: str, content: str) -> str:
    """Apply per-domain content cleanups identified in the 2026-06-20 audit.

    Each block runs only if the URL's host matches. Returns the cleaned
    content string. Safe to call on any URL — unknown hosts pass through.
    """
    if not url or not content:
        return content
    host = _host_for(url)
    out = content

    # DATA_URI_IMAGES_V1 (tcs-workflows#244), every site: a lazy-load placeholder (data:image/svg+xml...) is never the
    # photo, and an author who picks one gets a blank image. The real URL, where the page gives one, is in the list.
    # A linked placeholder goes with its link, so no empty [](url) is left behind.
    out = re.sub(r"\[\s*!\[[^\]]*\]\(data:[^)]*\)\s*\]\([^)]*\)", "", out)
    out = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", out)

    # ---- spacenews.com ----
    if "spacenews.com" in host:
        # (1) Newsletter signup form. Pattern always:
        #     **Sign up for First Up:** ...
        #     By submitting this form, you agree to ... opt-out at any time.
        #     [blank line]
        out = re.sub(
            r"\n+\*\*Sign up for First Up:\*\*[\s\S]*?opt-out at any time\.\s*",
            "\n",
            out,
        )
        # (2) Drop WordPress author thumbnail images embedded in content
        #     (the body version; see _filter_cruft_images for the images list)
        out = re.sub(
            r"!\[[^\]]*\]\([^)]*150x150[^)]*\)\s*",
            "",
            out,
        )
        # (3) Trailing "### _Related_" with nothing after. Affects 35/36
        #     SpaceNews articles in the audit. Strip the heading + anything
        #     after that's just whitespace.
        out = re.sub(r"\n+###\s+_Related_\s*\Z", "", out)
        # SPN_PAGE_END_V1 (tcs-workflows#244): a Space Minds episode page runs on past the description into a promo
        # block and a sign-up form (spn-1), and a press release ends with its Media Contact block (spn-5).
        m = re.search(r'\n+(?:###\s+About Space Minds\b|##\s+Space Minds Podcast\b|"\*" indicates required fields)', out)
        if m:
            out = out[: m.start()].rstrip()
        m = re.search(r"\n+Media Contacts?:", out)
        if m:
            out = out[: m.start()].rstrip()

        # (4) Paywall detection. SpaceNews paywalled articles show:
        #       ### To continue reading this article:
        #       Register for a SpaceNews.com account or [sign in] to your existing account.
        #       ...
        #       Or get unlimited access to this and every article on SpaceNews.com...
        #       $X /year  Subscribe now
        #     Verified phrases against fix2-3 sfn-test-100 audit. Keyword
        #     "to continue reading this article" is the strongest single
        #     signal (paywalled-stub articles always include it; full articles
        #     never do). Size threshold 2500 chars catches paywalled stubs
        #     (typically 1-3KB) without flagging legitimately short articles.
        plain_low = out.lower()
        paywall_markers = [
            "to continue reading this article",
            "register for a spacenews.com account",
            "or get unlimited access to this and every article",
            "this is a subscriber-only",
            "support quality space journalism",
            "subscribe to read",
            "subscribe for full access",
        ]
        if len(out) < 2500 and any(m in plain_low for m in paywall_markers):
            out = "[PAYWALL_BLOCKED]\n\n" + out

    # ---- europeanspaceflight.com ----
    if "europeanspaceflight.com" in host:
        # Donation widget at article end. Strip from "## Keep European
        # Spaceflight Independent" through end of content (the widget is
        # always last). Affects all 6 articles in the audit.
        m = re.search(r"\n+##\s+Keep European Spaceflight Independent", out)
        if m:
            out = out[: m.start()].rstrip()

    # ---- nasa.gov / science.nasa.gov ----
    if host.endswith("nasa.gov"):
        # Media Contact blocks at article end. Patterns:
        #   **Media Contact**
        #   **Media Contacts**
        #   ### Media Contacts:
        # Strip from the FIRST occurrence through end (everything after is
        # contact info, phones, emails — editorial metadata, not article body).
        for pat in [
            r"\n+\*\*Media Contacts?\s*:?\*\*",
            r"\n+###\s+Media Contacts?:?",
            r"\n+##\s+Media Contacts?:?",
        ]:
            # Case-insensitive since tcs-workflows#244: releases also write "**Media contact**".
            m = re.search(pat, out, re.IGNORECASE)
            if m:
                out = out[: m.start()].rstrip()
                break
        # NASA_RELEASE_END_V1 (tcs-workflows#244): news releases end at "-end-", and the contacts follow it. Spanish
        # releases end at "-fin-".
        m = re.search(r"\n+-(?:end|fin)-[ \t]*(?=\n|\Z)", out)
        if m:
            out = out[: m.start()].rstrip()
        # NASA_BLOG_FOOTER_V1 (tcs-workflows#244): blog posts end with the author's avatar, the date, Categories, share
        # icons and a "More from" footer with the previous post's headline, all after the article's last paragraph.
        m = re.search(r"\n+!\[\]\(https://secure\.gravatar\.com/|\n+###\s+Categories\s*\n|\n+##\s+More from\b", out)
        if m:
            out = out[: m.start()].rstrip()
        # Newsletter and "learn more" lines above the byline (tcs-workflows#244).
        out = re.sub(r"\n+_Learn more about station activities[^\n]*", "", out)
        out = re.sub(r"\n+_Get the latest from NASA delivered every week\.[^\n]*", "", out)
        # NASA_BODY_CHROME_V2 (tcs-workflows#244):
        # - a blog post repeats its headline as "# Title" right above the hero picture (H);
        # - the hero's credit label "NASA+" sits on a line of its own (G);
        # - social-follow lists ("Facebook logo", ...) end some NASA Science bodies (nasa-03);
        # - a blog post's "Want to read more posts ..." promo cards end it, after an empty heading (nasa-09);
        # - APOD's explanation ends with another entry's teaser, "Tomorrow's picture: ..." (nasa-07);
        # - APOD's credits table wraps its "A service of" row onto a line with no leading pipe (nasa-14).
        out = re.sub(r"\A#\s+[^\n]+\n+(?=\[!\[)", "", out)
        out = re.sub(r"\n+NASA\+[ \t]*(?=\n|\Z)", "\n", out)
        m = re.search(r"\n+(?:Facebook|Instagram|Linkedin|LinkedIn|X|Twitter|YouTube) logo[ \t]*(?=\n)", out)
        if m:
            out = out[: m.start()].rstrip()
        m = re.search(r"\n+\s*\*\s+###\s+Want to (?:read|learn) more", out)
        if m:
            out = re.sub(r"\n+##\s*\Z", "", out[: m.start()].rstrip())
        out = re.sub(r"\*\*Tomorrow[\u2019']s picture:\*\*[^\n]*", "", out)
        out = re.sub(r"(\|[^\n]*?),[ \t]*\n(?=\[)", r"\1, ", out)
        # NASA_INSIGNIA_HEADER_V1 (tcs-workflows#244): many releases open with the NASA insignia as their only picture.
        out = re.sub(r"\A\s*\[!\[NASA insignia\.\]\([^)]*nasa_meatball[^)]*\)\]\([^)]*\)\s*\n(?:Credit: NASA[ \t]*\n)?", "", out)
        # NASA_SCIENCE_CHROME_V1 (tcs-workflows#244): a Photojournal page opens with its Downloads block (a
        # thumbnail, the title again, the file size, a download link) before "## Description".
        out = re.sub(r"\A\s*##\s+Downloads[ \t]*\n[\s\S]*?(?=^##\s+Description\b)", "", out, flags=re.M)
        # SPACEQ_ESA_NASA_HYGIENE_V1 — 2026-06-27 audit: NASA release pages
        # (especially photojournal entries) bundle a "## Downloads" section
        # at end with JPEG/PNG file listings + MB sizes. Editorial metadata,
        # not article body. Strip from the heading to end.
        m = re.search(r"\n+##\s+Downloads\s*\n", out)
        if m:
            out = out[: m.start()].rstrip()
        # NASA_HEADLESS_CONTACT_V1 (tcs-workflows#244, nasa-04): some releases end with their contacts and no heading
        # or "-end-": names, a centre, phones and addresses on hard-broken lines. A trailing block like that goes when
        # it holds a phone number or a .gov address, up to six blocks from the end. The phone pattern needs word
        # boundaries, so a DOI such as s41558-017-0014-8 doesn't count.
        for _ in range(6):
            m = re.search(r"\n((?:[^\n]*[ ]{2}\n)+[^\n]*)\s*\Z", out)
            if m and re.search(r"\b\d{3}-\d{3}-\d{4}\b|[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.gov\b", m.group(1)):
                out = out[: m.start()].rstrip()
            else:
                break

    # ---- nasaspaceflight.com ----
    if "nasaspaceflight.com" in host:
        # Ad blocks identified in audit: Hawaiian_Shirt_Promo + Margaritaville
        # Both appear as image references with these filename markers.
        out = re.sub(
            r"!\[[^\]]*\]\([^)]*Hawaiian_Shirt[^)]*\)\s*",
            "",
            out,
        )
        out = re.sub(
            r"!\[[^\]]*Margaritaville[^\]]*\]\([^)]*\)\s*",
            "",
            out,
        )
        out = re.sub(
            r"!\[[^\]]*\]\([^)]*Margaritaville[^)]*\)\s*",
            "",
            out,
        )
        # NSF_CHROME_V1 (tcs-workflows#244), the body side:
        # - linked ads (the NSF shop, Nord-Lock, the YouTube channel), and the same pictures unlinked (NSF-02);
        # - the view counter that opened every body as "_N_" (NSF-05) and the "See Also" list (NSF-06), when the
        #   exclude doesn't reach them (the fallback crawl has none);
        # - empty emphasis lines (NSF-10);
        # - Google's "Discover more" box of search links, when the page's ad script puts it between paragraphs.
        out = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\((?:https?://)?(?:shop\.nasaspaceflight\.com|www\.nord-lock\.com|"
                     r"www\.youtube\.com/@NASASpaceflight)[^)]*\)\s*", "", out)
        out = re.sub(r"!\[[^\]]*\]\([^)]*(?:L2_Membership_ShopBanner|superbolt-1|YouTube_Forum-Ad|UPF_50_Sunshirts)[^)]*\)\s*", "", out)
        out = re.sub(r"\A\s*_\d{1,4}_[ \t]*\n", "", out)
        out = re.sub(r"(?m)^#{3,4} See Also[ \t]*\n(?:[ \t]*\*[ \t]+\[[^\n]*(?:\n|\Z))+", "", out)
        out = re.sub(r"(?m)^(?:\*\*|_)[ \t]*\n", "", out)
        out = re.sub(r"(?m)^Discover more[ \t]*\n(?:[A-Z][A-Za-z]*(?: [A-Za-z]+){0,4}[ \t]*\n){1,5}", "", out)
        # NASASF_TAGS_V1 (2026-07-05 audit): trailing tag-link chain at article
        # end — pattern is 2+ consecutive [tag](url) links pointing at
        # /tag/... paths, no spaces between. Editorial nav, not content.
        # Example: [Axiom](.../tag/axiom/)[CLD](.../tag/cld/)[Crew Dragon](.../tag/crew-dragon/)
        out = re.sub(
            r"\n*(?:\[[^\]]+\]\([^)]*nasaspaceflight\.com/tag/[^)]*\)){2,}\s*\Z",
            "",
            out,
        )

    # ---- spacepolicyonline.com ----
    if "spacepolicyonline.com" in host:
        # Leading social share buttons appear as a run of empty-link markdown
        # like `[](https://spo.com/#facebook "Facebook")[](...)[](...)`
        # all jammed onto ONE line at body start. Original version only
        # stripped one-per-line; this version handles consecutive runs too.
        out = re.sub(r"\A\s*(?:\[\]\([^)]*\)\s*){2,}", "", out)
        # Also strip remaining single empty-link lines at the start
        for _ in range(6):
            stripped = re.sub(r"\A\s*\[\]\([^)]+\)\s*\n", "", out)
            if stripped == out:
                break
            out = stripped
        # SPO_LAST_UPDATED_V1 (2026-07-05 audit): trailing "Last Updated" footer.
        # Pattern: `##### Last Updated: Jul 02, 2026 11:53 pm ET` — editorial
        # metadata line at end of body. Strip from the h5 to EOF.
        out = re.sub(
            r"\n*#{3,5}\s+Last\s+Updated:[^\n]*\s*\Z",
            "",
            out,
        )
        # SPO_UPDATE_NOTE_V1 (tcs-workflows#244): the italic "This article has been updated ..." note above it.
        out = re.sub(r"\n+[*_]This article has been updated[^\n]*[*_][ \t]*\Z", "", out)

    # ---- reactiondynamics.space ----
    # RD_BOILERPLATE_V1 (tcs-workflows#244): a release ends with "About Reaction Dynamics" and a named media contact.
    if "reactiondynamics.space" in host:
        m = re.search(r"\n+\*\*About Reaction Dynamics\*\*", out)
        if m:
            out = out[: m.start()].rstrip()

    # ---- axiomspace.com ----
    # AXIOM_SPACERS_V1 (tcs-workflows#244): Webflow's zero-width joiners (axm-7), and paragraphs that came one per
    # line with no blank line between them, so markdown read a run of them as one block (axm-8).
    if "axiomspace.com" in host:
        out = out.replace("\u200d", "")
        out = re.sub(r"(?<!\n)\n(?!\n)", "\n\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()

    # ---- arstechnica.com ----
    # ARS_CHROME_V1 (tcs-workflows#244): the Rocket Report's 2018 section graphics, and its newsletter box when the
    # exclude doesn't reach it (the fallback crawl has none).
    if "arstechnica.com" in host:
        out = re.sub(r"^!\[\]\(https://cdn\.arstechnica\.net/wp-content/uploads/2018/05/(?:smalll|mediuml|heavyl|rocket)\.png\)[ \t]*\n?",
                     "", out, flags=re.M)
        out = re.sub(r"^#{5}\s+Rocket Report\s*\n+Keep up with the latest in space exploration and rocket launches\.\s*\n+"
                     r"\[Sign Me Up!\]\([^)]*\)[ \t]*\n?", "", out, flags=re.M)

    # ---- spaceq.ca ----
    # SPACEQ_RELATED_V1 (tcs-workflows#244): every body ended with Jetpack's empty "Related" heading. The block is
    # excluded on the primary crawl; this covers the fallback crawl, which has no exclude.
    if "spaceq.ca" in host:
        out = re.sub(r"\n+###\s+_Related_\s*\Z", "", out).rstrip()

    # ---- spacescout.info ----
    if "spacescout.info" in host:
        # Trailing "Related" sidebar of unrelated articles. Strip from a
        # standalone Related heading or h3 to end of body.
        m = re.search(r"\n+(?:#{2,4}\s+)?Related\s*\n", out)
        if m:
            out = out[: m.start()].rstrip()

    # ---- spaceflightnow.com ----
    if "spaceflightnow.com" in host:
        # Strip zero-width / invisible unicode chars that the source page
        # uses as paragraph spacers (audit flagged one occurrence).
        out = re.sub(r"[\u200b-\u200f\u2028-\u202f\ufeff]", "", out)

    # ---- stokespace.com ----  STOKE_FIREFLY_POSTPROCESS
    if "stokespace.com" in host:
        # Trailing company boilerplate on every Stoke press release (2026-06-21 audit). Since the 2026-09 redesign the
        # heading is "**About Stoke**"; it was "**About Stoke Space**" (tcs-workflows#244).
        out = re.sub(
            r"\n+\*\*About Stoke(?: Space)?\*\*[\s\S]+\Z",
            "",
            out,
        ).rstrip()

    # ---- fireflyspace.com ----
    if "fireflyspace.com" in host:
        # Trailing "### ABOUT FIREFLY AEROSPACE" company boilerplate
        # paragraph (identical on every Firefly article — corporate footer,
        # not editorial). Followed by Filed Under: + optional back-link.
        # Strip from the heading to end of body.
        # FIREFLY_BOILERPLATE_V2 (tcs-workflows#244): the live template sets the heading as a bold paragraph, so the
        # h3 rule never matched, and 40-71% of each body was boilerplate. The cut starts at whichever comes first:
        # About Firefly Aerospace, About SciTec (Firefly's subsidiary) or the Forward-Looking Statements paragraph.
        m = re.search(r"\n+(?:#{2,6}\s+)?\**\s*(?:About (?:Firefly Aerospace|SciTec)|Forward-Looking Statements?)\s*\**[ \t]*\n",
                      out, re.IGNORECASE)
        if m:
            out = out[: m.start()].rstrip()
        # Some articles have only the Filed Under footer without the heading.
        out = re.sub(r"\n+Filed\s+Under:[^\n]*", "", out)
        # Strip trailing empty-link nav back-link
        out = re.sub(r"\[\]\([^)]*fireflyspace\.com[^)]*\)\s*\Z", "", out).rstrip()

    # ---- relativityspace.com ----  RELATIVITY_BLOG_META_STRIP
    if "relativityspace.com" in host:
        # Squarespace Blog-meta trailing footer pattern. Identified in 2026-06-21
        # audit. Looks like (one or more occurrences):
        #   [Press Release](https://...press-release/category/Press+Release)
        #   [Author Name](https://...press-release?author=...)
        #   Month Day, Year
        #   [Tag](https://...press-release/tag/Tag)
        # All on one trailing block at end of body.
        # Match from the first "[Press Release](...press-release/category/" link
        # to end of body — the Blog-meta strip always begins with that link.
        m = re.search(
            r"\n+\[Press Release\]\([^)]*press-release/category/[^)]*\)",
            out,
            re.IGNORECASE,
        )
        if not m:  # RELATIVITY_BYLINE_V2 (tcs-workflows#244): a post whose Blog-meta has only the author and date
            m = re.search(r"\n+\[[^\]\n]+\]\(https?://[^)\s]*press-release\?author=[^)\s]*\)[^\n]*\s*\Z", out)
        if m:
            out = out[: m.start()].rstrip()

    # ---- esa.int (general pages) ----  SPACEQ_ESA_NASA_HYGIENE_V1
    if "esa.int" in host:
        # 2026-06-27 audit: regular ESA pages (not the /ESA_Multimedia/ subset
        # handled via config_for) include trailing social-share + breadcrumb +
        # "About ESA" boilerplate. Strip from the social-share marker to end.
        # Patterns observed in two earlier runs:
        #   "Like" / "Thank you for liking" widget at body end
        #   ESA / Applications / Satellite navigation / ... breadcrumb
        #   "## About ESA" section
        # Each widget line stands alone (tcs-workflows#234), so a sentence that starts "Like Earth, ..." stays.
        out = re.sub(r"\n+(?:Like|Thank you for liking|You have already liked[^\n]{0,60})[ \t]*(?=\n|$)", "", out)
        # ESA_TEMPLATE_ALT_V1, body part (tcs-workflows#176): the CMS placeholders that _clean_alt drops from the
        # image list also reach the body, as the alt text of a linked video thumbnail:
        #   [ ![Play](.../play-button.svg) ![$video.data_map.short_description.content](...png) ](.../ESA_Multimedia/Videos/...)
        # Keep the image and the link, and drop only the placeholder. The alt must be nothing but the placeholder,
        # so an alt that merely starts with a dollar sign ("$US 120M contract") stays.
        out = re.sub(r"!\[\$[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*\]\(", "![](", out)
        # ESA_ARTICLE_DATE_V1 (tcs-workflows#176): the article's own date line is selected for extract_date();
        # its text ("23/09/2026 2652 views 40 likes") is page chrome, not article text.
        out = re.sub(r"\n*^[ \t]*\d{2}/\d{2}/\d{4}(?:[ \t]+\d+[ \t]*views?)?(?:[ \t]+\d+[ \t]*likes?)?[ \t]*$", "", out, flags=re.M)
        # The old site's breadcrumb had at least two slashes, so an image credit like "ESA / Hubble & NASA" stays.
        out = re.sub(r"\n+ESA(?:[ \t]+/[ \t]+[^/\n]+){2,}[ \t]*(?=\n|$)", "", out)
        if host in ESA_MAIN_HOSTS:
            # ESA_NEW_SITE_V1 (tcs-workflows#234): the video player's rows of underscores, the CMS's placeholder
            # pictures, and a media page's tag links, which go to ESA's search. Credits and License lines stay.
            out = re.sub(r"\n*^[ \t]*__(?:[ \t]+__)*[ \t]*$", "", out, flags=re.MULTILINE)
            out = re.sub(r"!\[fallback image \d+\]\([^)\s]*\)", "", out)
            out = re.sub(r"\n*^[ \t]*[*-][ \t]+\[[^\]\n]*\]\(https?://[^)\s]*esa\.int/search\?[^)\s]*\)[ \t]*$", "", out, flags=re.MULTILINE)
        # ESA_BODY_CHROME_V2 (tcs-workflows#244): <hr> rules come through as "* * *", and one page had a bare "--" line.
        out = re.sub(r"^(?:\* \* \*|--)[ \t\xa0]*(?:\n|\Z)", "", out, flags=re.M)
        # The boilerplate at the end: "About ESA", and on Galileo articles "About Galileo" (esa-07).
        m = re.search(r"\n+##\s+About (?:ESA|Galileo)\b", out)
        if m:
            out = out[: m.start()].rstrip()

    # ---- maritimelaunch.com ----  MARITIME_BODY_V1 (tcs-workflows#244)
    if "maritimelaunch.com" in host:
        # The article opens with the site logo, a News & Media link, "Press Release" and the H1; the release itself
        # starts at the italic dek. The "Nov 2, 2026/2 min read" line is the header's date, which the Date line has.
        # Since 2026-11-12 the header says "3 min", with no "read" (tcs-workflows#273).
        m = re.search(r"^# [^\n]*\n", out, re.M)
        if m:
            out = out[m.end():]
        out = re.sub(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}/\d+ min(?: read)?[ \t]*\n", "", out, flags=re.M)
        # The default press tile is not a photo of the story (the image list and thumbnail skip it too).
        out = re.sub(r"!\[[^\]]*\]\([^)]*" + MARITIME_PLACEHOLDER + r"[^)]*\)[ \t]*\n?", "", out)
        out = out.lstrip()

    # ---- planetary.org ----  PLANETARY_BODY_V1 (tcs-workflows#244)
    if "planetary.org" in host:
        # 'article' also holds its own footer (tags, breadcrumb, author box, Related Articles or More Downlink) and the
        # donate call-out just before it. Cut at the first footer marker.
        m = re.search(
            r"\n+(?:#{3}\s+You Power the Next Great Discovery!|_{0,2}Read more:|You are here:"
            r"|#{3}\s+Related Articles|#{3}\s+More Downlink)",
            out,
        )
        # PLANETARY_DONATE_CARD_V1 (tcs-workflows#304): the call-out's heading changes with each campaign ("Powered by
        # people like you" in late 2026), but it always ends in a Donate link to connect.planetary.org.
        d = re.search(r"\n+#{3}[^\n]*\n(?:[^\n]*\n){0,3}?\[Donate\]\(https?://connect\.planetary\.org/", out)
        if d and (not m or d.start() < m.start()):
            m = d
        if m:
            out = out[: m.start()].rstrip()
            # Related-item cards just before the footer: a heading that is only a link to another planetary.org page,
            # and its one-line blurb. Only at the very end, so a linked heading inside the story stays.
            for _ in range(5):
                cut = re.sub(r"\n+#{3}[ \t]*\[[^\]\n]+\]\(https?://(?:www\.)?planetary\.org/[^)\s]+\)[ \t]*"
                             r"(?:\n(?!#)[^\n]*)?$", "", out)
                if cut == out:
                    break
                out = cut.rstrip()
        # The Downlink's membership appeal, in the middle of the issue.
        out = re.sub(r"\n+## Help save space missions\.[\s\S]*?(?=\n## )", "\n", out)
        # Site chrome and bullet icons, then the empty links they and the lazy-load placeholders leave. The lookbehind
        # keeps a picture with no alt text: ![](url) holds "[](url)" too.
        out = re.sub(r"!\[[^\]]*\]\([^)]*(?:/sites/planetary/images/|email-tps-logo|-illustration\.png)[^)]*\)\s*", "", out)
        out = re.sub(r"(?<!!)\[\s*\]\([^)]*\)", "", out)
    if "aerospaceamerica.aiaa.org" in host:
        # AA_RELATED_READING_V1 (tcs-workflows#244, aa F2): an in-body "RELATED READING" link to another article, and
        # the stray bold marker the list item leaves.
        out = re.sub(r"(?m)^[ \t]*\*[ \t]+\*\*\[RELATED READING:[^\]\n]*\]\([^)\n]*\)[ \t]*\n(?:[ \t]*\*\*[ \t]*\n)?",
                     "", out)
        # An empty link (a picture's frame with the picture gone) carries nothing; the hero's did on 2026-11-11.
        out = re.sub(r"(?<!!)\[\s*\]\([^)\n]*\)[ \t]*", "", out)

    return out


def _normalize_image_url(u: str) -> str:
    """Strip WP/CDN optimization params so the same image isn't dedup-missed
    across variants. SPACEQ_ESA_NASA_HYGIENE_V1.

    Examples:
      'https://x.com/img.jpg?resize=780%2C373&ssl=1' -> 'https://x.com/img.jpg'
      'https://x.com/img@2x.jpg' -> 'https://x.com/img.jpg'
    """
    if not u:
        return u
    # Strip query params we know are display-modifiers (preserve genuine
    # content params; this is conservative — only well-known WP/Jetpack ones).
    u = re.sub(r'\?(?:resize|w|h|fit|ssl|q|quality|crop)=[^&]*(?:&(?:resize|w|h|fit|ssl|q|quality|crop)=[^&]*)*', '', u)
    # Strip dangling ? if all params were removed
    u = re.sub(r'\?$', '', u)
    # Strip @2x / @3x / @1x retina suffixes before extension
    u = re.sub(r'@[123]x(?=\.[a-z]+(?:\?|$))', '', u)
    return u


def _filter_cruft_images(url: str, images: list, seen_alts: dict = None) -> list:
    """Filter out per-domain image cruft from the images list.

    Currently handles spacenews.com author byline thumbnails. Other
    domains pass through unchanged.

    The alt checks read seen_alts (collect_images' {url: old desc-based text}) where it has the URL, so they match
    what they matched before IMAGE_OWN_ALT_V1 (tcs-workflows#244).
    """
    if not url or not images:
        return images
    seen_alts = seen_alts or {}
    host = _host_for(url)
    if "spacenews.com" in host:
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
                alt = (seen_alts.get(u, img.get("alt", "")) or "").strip().lower()
            else:
                u = str(img)
                alt = ""
            # WP author thumbnail size 150x150 — these are author headshots,
            # not article content. Also drop alt-text starts with "by "
            # (e.g. "by Jeff Foust").
            if "150x150" in u:
                continue
            if alt.startswith("by "):
                continue
            # apogee.spacenews.com hosts SpaceNews's ad + design assets
            # (empty-alt design-system images, programmatic ad creative).
            # Not article content. Audited 2026-08-06 (one run's article-1
            # picked one of these as its hero, ahead of the actual Electron
            # launch photo).
            if "apogee.spacenews.com" in u:
                continue
            filtered.append(img)
        return filtered
    if "nasa.gov" in host and "science.nasa.gov" not in host:
        # NASA_BLOG_SIDEBAR_V1 (2026-08-06 audit, hc-2026-08-06 verify run):
        # NASA blog pages ship a "Recent Posts" sidebar with thumbnails from
        # OTHER articles. strict_scope + keep_overlays (added the same day)
        # fixed the ordering — the article's own OG is now reliably first —
        # but didn't eliminate the bleed. Crawl4AI's media.images collector
        # scans the full rendered page, not just the selector's subtree.
        #
        # The article's own OG thumbnail and the sidebar thumbnails share
        # the exact same filename shape:
        #   /wp-content/uploads/YYYY/MM/blog-<slug>-preview.jpg
        # so we can't URL-filter by pattern alone. Heuristic: keep at most
        # ONE such thumbnail per article — the first one, which after
        # strict_scope is reliably the article's own. Drop the rest.
        seen_blog_preview = False
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
            else:
                u = str(img)
            if re.search(r"/blog-[^/]+-preview\.jpg", u):
                if seen_blog_preview:
                    continue
                seen_blog_preview = True
            filtered.append(img)
        return filtered
    if "nasaspaceflight.com" in host:
        # Drop sponsor/affiliate ad images: Hawaiian_Shirt promo, Margaritaville
        # Beach Resort, NovaTech sidebar promo. These leak into every NSF article
        # because they're inside the entry-content div.
        # NSF_CHROME_V1 (tcs-workflows#244): the shop banner, sponsor and forum ads, and the sidebar's live-stream
        # pictures, whose only text is the sidebar's (seen through seen_alts).
        cruft_url_markers = ["Hawaiian_Shirt", "Margaritaville", "/Nova.gif", "L2_Membership_ShopBanner", "superbolt-1",
                             "YouTube_Forum-Ad", "UPF_50_Sunshirts", "/Towel-"]
        cruft_alt_markers = ["hawaiian", "margaritaville", "novatech", "active discussions", "nsf starbase live"]
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
                alt = (seen_alts.get(u, img.get("alt", "")) or "").strip().lower()
            else:
                u = str(img)
                alt = ""
            if any(m in u for m in cruft_url_markers):
                continue
            if any(m in alt for m in cruft_alt_markers):
                continue
            filtered.append(img)
        return filtered
    if "spaceq.ca" in host:
        # SPACEQ_ESA_NASA_HYGIENE_V1 — 2026-06-27 audit: SpaceQ articles
        # consistently bundle 2-3 newsletter/promo banners as images.
        # Pattern markers from observed runs (1842, 1888, 1901, 1932, 1944, 1976).
        cruft_url_markers = ["Discover-our-growing", "subscribe-banner", "Latest-News"]
        cruft_alt_markers = ["discover our", "latest news", "subscribe", "newsletter"]
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
                alt = (seen_alts.get(u, img.get("alt", "")) or "").strip().lower()
            else:
                u = str(img)
                alt = ""
            if any(m in u for m in cruft_url_markers):
                continue
            if any(m in alt for m in cruft_alt_markers):
                continue
            filtered.append(img)
        return filtered
    if "relativityspace.com" in host:
        # RELATIVITY_LOGO_V1 (tcs-workflows#244): the site's header logo reached the list, as the only picture.
        return [img for img in images if isinstance(img, dict) and not (
            "Relativity_White_RGB" in (img.get("url", "") or "")
            or ((seen_alts.get(img.get("url", ""), img.get("alt", "")) or "").strip().lower() == "relativity space"))]
    if "arstechnica.com" in host:
        # ARS_CHROME_V1 (tcs-workflows#244): the author's headshot ("Photo of Eric Berger"), the Most Read sidebar's
        # thumbnails, the comments' loading GIF, and the Rocket Report's 2018 section graphics.
        kept = []
        for img in images:
            u = (img.get("url", "") if isinstance(img, dict) else str(img)) or ""
            alt = ((seen_alts.get(u, img.get("alt", "")) if isinstance(img, dict) else "") or "").strip()
            if (re.fullmatch(r"Photo of [A-Z][a-z]+(?: [A-Z][a-z]+){1,2}", alt)
                    or alt.lower().startswith("listing image for first story in most read")
                    or alt.lower() == "loading" or "/wp-content/themes/" in u
                    or re.search(r"/wp-content/uploads/2018/05/(?:smalll|mediuml|heavyl|rocket)\.png$", u)):
                continue
            kept.append(img)
        return kept
    if "science.nasa.gov" in host:
        # NASA_SCIENCE_PROMO_V1 (tcs-workflows#244): the picture of a blog post's "Want to read more posts ..." promo
        # card. The card's text reaches the picture only as Crawl4AI's desc, so this reads seen_alts.
        return [img for img in images if not re.match(
            r"want to (?:read|learn) more",
            ((seen_alts.get(img.get("url", ""), img.get("alt", "")) if isinstance(img, dict) else "") or "").strip(),
            re.IGNORECASE)]
    if "planetary.org" in host:
        # PLANETARY_CRUFT_V1 (tcs-workflows#244): the header menu tiles and the icon library sit under
        # /sites/planetary/images/, Downlink bullets are *-illustration.png, and author and reviewer headshots are
        # *-portrait*. No square-crop rule: some real photos are 576x576 crops.
        return [img for img in images if not re.search(
            r"/sites/planetary/images/|-illustration\.png$|-portrait[-_.]",
            (img.get("url", "") if isinstance(img, dict) else str(img)) or "", re.IGNORECASE)]
    if "aerospaceamerica.aiaa.org" in host:
        # AA_CRUFT_V1 (tcs-workflows#244, aa F5): the header nameplate and the magazine-cover promo (alt "Demo"). Not in
        # the output today; Crawl4AI's scorer would keep both if a browser pass left them in the scope.
        return [img for img in images if isinstance(img, dict)
                and not re.search(r"/nameplate\.|/AA-[A-Za-z]+-\d{4}-cover", img.get("url", "") or "", re.IGNORECASE)
                and (seen_alts.get(img.get("url", ""), img.get("alt", "")) or "").strip().lower() != "demo"]
    return images


# NASA_TEASER_IMAGES_V1 (tcs-workflows#244): a NASA blog post's "More from" footer links the previous and next posts
# with their thumbnails, which then reached the image list under this post's headline. The teaser links are in the
# raw markdown (before _site_post_process cuts the footer), so the filter reads that.
_TEASER_LINK_IMG = re.compile(r"(?:Previous|Next) Post\]\([^)]*\)\s*\[!\[[^\]]*\]\((https?://[^)\s]+)")


def _drop_teaser_images(url: str, raw_markdown: str, images: list) -> list:
    if not _host_for(url).endswith("nasa.gov") or not raw_markdown:
        return images
    teaser = {re.sub(r"\?.*$", "", u) for u in _TEASER_LINK_IMG.findall(raw_markdown)}
    if not teaser:
        return images
    return [i for i in images if re.sub(r"\?.*$", "", (i.get("url", "") if isinstance(i, dict) else str(i)) or "") not in teaser]


def _selector_match_count(html: str, selector: str):
    """SCRAPE_SCOPE_V1 (tcs-workflows#249): how many elements the site's selector matches in the page HTML, or None
    when that can't be checked here (no bs4, no HTML). On Crawl4AI 0.9.4 a selector that matches nothing gives an
    empty body, and scrape_one's unscoped retry takes the whole page: that's how Spaceflight Now's and Maritime
    Launch's whole-page scrapes happened (checked 2026-11-11). This count is a second check, for a body that comes
    back although the selector matched nothing."""
    if not html or not selector:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        return sum(len(soup.select(part.strip())) for part in selector.split(",") if part.strip())
    except Exception:
        return None


def _press_release_by(url: str, page_html: str) -> str:
    """SPN_PRESS_RELEASE_V1 (tcs-workflows#244): SpaceNews carries companies' press releases under its own name. The
    page says so in its parsely-section meta ("Press Release"), with the company as parsely-author. Returns the
    company, "a company" when the page names none, or "" for anything that isn't a SpaceNews press release."""
    if _host_for(url) != "spacenews.com" or not page_html:
        return ""
    if not re.search(r"""<meta[^>]+name=["']parsely-section["'][^>]+content=["']Press Release["']""", page_html, re.I):
        return ""
    import html as _html
    m = re.search(r"""<meta[^>]+name=["']parsely-author["'][^>]+content=["']([^"']+)["']""", page_html, re.I)
    return (_html.unescape(m.group(1)).strip() if m else "") or "a company"


async def scrape_one(crawler: AsyncWebCrawler, article: dict) -> dict:
    """Scrape a single article, with one retry attempt and a fallback to unscoped extraction."""
    url = article["url"]
    title = article.get("title", "")

    site = config_for(url)

    # Flag unknown sites — we don't have a proven selector, so the output
    # will likely be noisy. Workflow should route these to a review queue.
    if not site:
        host = _host_for(url)
        item = _error_item(
            url,
            title,
            f"Unknown site: {host or '(no host)'} — add to SITE_CONFIG in article_scraper.py",
        )
        item["unknown_site"] = True
        return item

    cfg = run_config_for(url)

    # First attempt — with per-site targeting. _fetch_with_retry handles the
    # transient-failure retry (1s, 2s backoff) that used to live inline here.
    res, err, attempts = await _fetch_with_retry(crawler, url, cfg)
    if err:
        # Log the primary-phase failure whether the crawler raised (res is
        # None) or handed back a failed result object (res.success False —
        # what Crawl4AI does for navigation timeouts). The unscoped fallback
        # below may still recover; if it does, scrape_recovered says so.
        _log_event({
            "event": "scrape_fail",
            "url": url,
            "phase": "primary",
            "attempts": attempts,
            "error": err,
        })
        if not res:
            return _error_item(url, title, err)
        # ANTIBOT_NO_FALLBACK_V1 (tcs-workflows#255): a page blocked by anti-bot protection isn't mis-scoped, so the
        # whole-page fallback below would only fetch the same challenge again (with its own retry). On 2026-11-12 that
        # made four requests per blocked NSF page. Stop after the first fetch and its retry.
        if "blocked by anti-bot" in err.lower():
            return _error_item(url, title, err)

    html = getattr(res, "html", "") or "" if res else ""
    content = get_content(res) if res else ""
    # SCRAPE_SCOPE_V1 (tcs-workflows#249): "article" when the site's selector found the article, "whole_page" when the
    # body is the whole page. It goes into the result and the index entry, so the Healthcheck can flag it.
    scope, scope_reason = "article", ""

    # If scoped extraction yielded nothing, retry without target_elements
    # (lets Crawl4AI's default pruning filter try the full page)
    if not content:
        fallback_kwargs = dict(
            cache_mode=CacheMode.BYPASS,
            word_count_threshold=5,
            exclude_social_media_links=True,
            process_iframes=False,
            remove_overlay_elements=not site.get("keep_overlays", False),
            page_timeout=45_000,
            verbose=False,
        )
        if site.get("networkidle"):
            fallback_kwargs["wait_until"] = "networkidle"
            fallback_kwargs["page_timeout"] = 60_000
        if site.get("wait"):
            fallback_kwargs["delay_before_return_html"] = site["wait"]
        fallback_cfg = CrawlerRunConfig(**fallback_kwargs)
        res2, err2, attempts2 = await _fetch_with_retry(crawler, url, fallback_cfg)
        if res2 and getattr(res2, "success", False):
            if not html:
                html = getattr(res2, "html", "") or ""
            content = get_content(res2) or content
            if content:
                res = res2
                scope, scope_reason = "whole_page", "unscoped_retry"
                _log_event({
                    "event": "scrape_recovered",
                    "url": url,
                    "phase": "fallback",
                    "attempts": attempts2,
                })
        if not content:
            _log_event({
                "event": "scrape_fail",
                "url": url,
                "phase": "fallback",
                "attempts": attempts2,
                "error": err or err2 or "No content extracted",
            })

    if not content:
        return _error_item(url, title, err or "No content extracted")

    # A second check, for a first pass that gave a body although the selector matched nothing (the retry above is
    # the usual way a whole page arrives). res.html is the full page for target_elements sites, so the selector's
    # matches can be counted in it. strict_scope sites get only the selected elements back, so they're not counted.
    if scope == "article" and site.get("selector") and not site.get("strict_scope"):
        matched = _selector_match_count(html, site["selector"])
        if matched == 0:
            scope, scope_reason = "whole_page", "selector_matched_nothing"
    if scope == "whole_page":
        _log_event({"event": "scrape_whole_page", "url": url, "reason": scope_reason})

    # Detect anti-bot challenge pages masquerading as content
    low = content.lower()
    anti_bot_markers = [
        "security verification",
        "checking your browser",
        "performing security check",
        "verifying you are human",
        "enable javascript and cookies to continue",
    ]
    if len(content) < 500 and any(m in low for m in anti_bot_markers):
        return _error_item(url, title, "Blocked by anti-bot (Cloudflare/similar)")

    # EIN Presswire: the article body is duplicated in the page (header preview
    # + main body + 'You just read:' footer repeat with related-links).
    # Strip everything from the 'You just read:' marker onward to halve the
    # content size without losing unique prose.
    if 'einpresswire.com' in url.lower() and content:
        marker_idx = content.find('You just read')
        if marker_idx == -1:
            marker_idx = content.find('You just read:')
        if marker_idx > 200:   # only truncate if we found it AFTER the real content
            content = content[:marker_idx].rstrip()

    # Canada Rocket Company (Framer SPA): page renders bilingual content
    # (English release + French translation) AND a Framer nav/footer block
    # (back-link to previous article + framerusercontent.com logo + email
    # + Etobicoke address). Truncate at the earliest French/footer marker
    # to leave just the English release.
    if 'canadarocketcompany.com' in url.lower() and content:
        cutoffs = []
        # French translation markers
        for pat in [
            r'\*\*\s*COMMUNIQU\u00c9 DE PRESSE',
            r'\*\*[A-Z\u00c0-\u017f][A-Z\u00c0-\u017f \t]+S\u00c9LECTIONN\u00c9E',
            r'\*\*Le\s+\d{1,2}\s+(?:janvier|f\u00e9vrier|mars|avril|mai|juin|juillet|ao\u00fbt|septembre|octobre|novembre|d\u00e9cembre)\s+\d{4}',
            r'## \*\*Canada Rocket Company r\u00e9cup\u00e8re',  # CRC1 French H2
            r'\*\*POUR DIFFUSION IMM\u00c9DIATE\*\*',           # standalone French 'for immediate release' stub
        ]:
            m = re.search(pat, content)
            if m and m.start() > 200:
                cutoffs.append(m.start())
        # Framer nav back-link
        nav_re = re.compile(r'\[[\u2039\u203a<>]\s*[^\]]+\]\(https?://(?:www\.)?canadarocketcompany\.com/news/[^)]+\)')
        m = nav_re.search(content)
        if m and m.start() > 200:
            cutoffs.append(m.start())
        if cutoffs:
            content = content[:min(cutoffs)].rstrip()
            # Strip trailing empty emphasis markers ('__', '**', '---') and whitespace
            for _ in range(5):
                _stripped = re.sub(r'(?:^|\n)\s*(?:_{2,}|\*{2,}|-{3,})\s*$', '', content).rstrip()
                if _stripped == content: break
                content = _stripped

    # Site-specific cleanups identified in the 2026-06-20 scrape audit.
    # Each block runs only if the URL host matches. See _site_post_process for details.
    raw_content = content   # the NASA teaser filter reads the footer that the cleanup cuts
    content = _site_post_process(url, content)

    # STOKE_H1_TITLE_V1 (tcs-workflows#244): Stoke's og:title and <title> can carry another story's headline; its h1 is right.
    page_title = (extract_h1_title(html) if "stokespace.com" in url.lower() else "") or extract_og_title(html) or extract_h1_title(html)
    final_title = title or page_title or ""
    if _host_for(url) == "spaceflightnow.com":
        final_title = _sfn_page_title(html) or final_title   # SFN_PAGE_TITLE_V1 (F6): the current headline
    # CRC page titles include ' - Canada Rocket Company' suffix; strip it.
    if final_title and 'canadarocketcompany.com' in url.lower():
        final_title = re.sub(r'\s*[-\u2013\u2014]\s*Canada Rocket Company\s*$', '', final_title).strip()
    # STOKE_TITLE_SUFFIX_STRIP — every Stoke og:title ends with
    # ' | Stoke Space / 100% reusable rockets / USA'. Strip.
    if final_title and 'stokespace.com' in url.lower():
        final_title = re.sub(r'\s*\|\s*Stoke Space.*$', '', final_title, flags=re.IGNORECASE).strip()
    # MLS_TITLE_SUFFIX_STRIP (tcs-workflows#286): Maritime Launch's og:title ends with ' | Maritime Launch Services'
    # (its h1 is the bare headline). Canada From Orbit reads the h1 itself; this is for callers that pass no title.
    if final_title and _host_for(url) == "maritimelaunch.com":
        final_title = re.sub(r'\s*\|\s*Maritime Launch Services\s*$', '', final_title, flags=re.IGNORECASE).strip()
    # AA_TITLE_SUFFIX_STRIP (tcs-workflows#299): the site name, if og:title carries one.
    if final_title and _host_for(url) == "aerospaceamerica.aiaa.org":
        final_title = re.sub(r'\s*[-|–—]\s*Aerospace America\s*$', '', final_title, flags=re.IGNORECASE).strip()

    seen_alts = {}   # IMAGE_OWN_ALT_V1: the old alt text, for the junk filters only
    images = _filter_cruft_images(url, _drop_teaser_images(url, raw_content, collect_images(res, content, seen_alts)),
                                  seen_alts)
    if _host_for(url) == "axiomspace.com":
        images = _axiom_images(images, html)
    if _host_for(url).endswith("esa.int"):
        images = _esa_video_alts(images, html)
    thumb = extract_og_image(html)
    # LOGO_THUMBNAIL_V1 (tcs-workflows#244): a logo is never a usable thumbnail (SpaceQ's og:image is its logo on some
    # pages). The article's first picture stands in, or none.
    if thumb and _DROP_PATTERNS.search(thumb):
        thumb = images[0]["url"] if images else ""
    # NASA_OG_IMAGE_V1 (tcs-workflows#244): some NASA Science pages give Crawl4AI no picture, though their og:image is
    # the article's photo. It's listed when the list would otherwise be empty.
    # SpaceNews too: a featured photo outside div.entry-content is only its og:image (spn-3).
    # AA_HERO_V1: Aerospace America too; its hero sits in div.featured and Crawl4AI's scorer drops it (aa F1). A seeded
    # picture takes its own caption from the scoped page when there is one, and that caption line leaves the body.
    if not images and thumb and (_host_for(url).endswith("nasa.gov")
                                 or _host_for(url) in ("spacenews.com", "aerospaceamerica.aiaa.org")):
        seeded_alt = _html_captions(html).get(_image_key(thumb), "")
        if not seeded_alt and _host_for(url) == "aerospaceamerica.aiaa.org":
            seeded_alt = _featured_caption(html)
        images = [{"url": _strip_sizing_query(thumb), "alt": seeded_alt}]
        if seeded_alt:
            content = _drop_caption_line(content, seeded_alt)
    item = {
        "title": final_title,
        "url": url,
        "date": extract_date(html, url=url),
        "thumbnail": thumb,
        "images": images,
        "source": detect_source(url),
        "content": content,
        "word_count": len(content.split()),
        "character_count": len(content),
        "has_content": True,
        "scope": scope,
    }
    pictures = _article_picture_count(html, site.get("selector", "")) if scope == "article" else None
    if pictures is not None:
        item["article_pictures"] = pictures   # ARTICLE_PICTURES_V1
    if scope_reason:
        item["scope_reason"] = scope_reason
    press_release_by = _press_release_by(url, html)
    if press_release_by:
        item["press_release_by"] = press_release_by
    return item


async def scrape_all(articles: list) -> list:
    """Scrape every article concurrently."""
    browser_cfg = BrowserConfig(
        headless=True,
        verbose=False,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    # Crawl4AI's logger writes to stdout. Redirect during scrape. The redirect
    # must wrap the entire crawl (including the retry loop below), otherwise a
    # log line from the library lands on stdout and corrupts the result JSON.
    devnull = open(os.devnull, "w")
    results = None
    try:
        with contextlib.redirect_stdout(devnull):
            # Two attempts at bringing the browser up. The Sept outages surfaced
            # as an exception raised out of the `async with` when the underlying
            # Playwright process failed to start — before any article was even
            # attempted. Give it one more shot after a short pause.
            for init_attempt in (1, 2):
                try:
                    async with AsyncWebCrawler(config=browser_cfg) as crawler:
                        # Run up to 3 in parallel — JS-heavy sites need room to breathe
                        sem = asyncio.Semaphore(3)

                        async def _guarded(art):
                            async with sem:
                                return await scrape_one(crawler, art)

                        results = await asyncio.gather(
                            *(_guarded(a) for a in articles),
                            return_exceptions=True,
                        )
                    break
                except Exception as e:
                    if results is not None:
                        # The crawl itself finished; the exception came from
                        # browser teardown in __aexit__. Don't re-scrape — we
                        # already have every article's result. Log and move on.
                        _log_event({
                            "event": "crawler_teardown_fail",
                            "error": f"{type(e).__name__}: {e}",
                        })
                        break
                    _log_event({
                        "event": "crawler_init_fail",
                        "attempt": init_attempt,
                        "max_attempts": 2,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    if init_attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    raise
    finally:
        devnull.close()

    # Convert any raised exceptions into error items (shouldn't normally happen)
    out = []
    for art, res in zip(articles, results):
        if isinstance(res, Exception):
            err_msg = f"Unhandled: {type(res).__name__}: {res}"
            _log_event({
                "event": "scrape_fail",
                "url": art["url"],
                "phase": "gather_exception",
                "error": err_msg,
            })
            out.append(_error_item(art["url"], art.get("title", ""), err_msg))
        else:
            out.append(res)
    return out


def _error_item(url: str, title: str, error: str) -> dict:
    return {
        "title": title,
        "url": url,
        "date": "",
        "thumbnail": "",
        "images": [],
        "source": detect_source(url),
        "content": "",
        "word_count": 0,
        "character_count": 0,
        "has_content": False,
        "error": error,
    }


# ============================================================
# File-output mode helpers
# ============================================================
SCRAPER_RUNS_ROOT = "/opt/tcs/n8n/local_files/scraper-runs"


def _safe_run_id(run_id: str) -> str:
    """Strip anything that isn't alphanumeric / dash / underscore."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", run_id) or "default"


def _write_file_output(results: list, run_id: str, index_prefix: str = "") -> dict:
    """Write per-article .md files + index.json to the run directory.

    When index_prefix is empty, articles are indexed 0, 1, 2, ... and the
    index.json is rewritten from scratch. When index_prefix is set (e.g.
    'sq'), articles are indexed 'sq-0', 'sq-1', ... and merged into any
    existing index.json so multiple scraper runs can share a run dir.

    Returns the index dict (which is also written to index.json and echoed
    to stdout).
    """
    run_dir = os.path.join(SCRAPER_RUNS_ROOT, _safe_run_id(run_id))
    os.makedirs(run_dir, exist_ok=True)

    # EMPTY_SOURCE_FILTER_V1 (2026-07-05): pre-filter stub / empty-source articles.
    # If a source returned "No tweets from X" or a similarly tiny stub, drop it entirely
    # rather than write a file + index entry that the LLM will read as if it were content.
    STUB_PATTERNS = (
        "no tweets from ", "no content", "no new articles", "no updates",
        "no official updates", "no starlink updates", "no news",
    )
    def _is_stub(r):
        content = (r.get("content") or "").strip()
        if not r.get("has_content"):
            return False   # already flagged upstream, will not be written anyway
        if len(content) < 200:
            low = content.lower()
            for pat in STUB_PATTERNS:
                if pat in low:
                    return True
        return False

    filtered_results = []
    for r in results:
        if _is_stub(r):
            # Mark it so downstream knows this slot was intentionally dropped
            r = dict(r)
            r["has_content"] = False
            r["stub_dropped"] = True
            r["content"] = ""
            continue  # DO NOT append — skip entirely; keeps index.json clean
        filtered_results.append(r)
    results = filtered_results

    index_entries = []
    for i, r in enumerate(results):
        # Add index + content_file metadata to every entry
        entry = dict(r)
        entry["index"] = f"{index_prefix}-{i}" if index_prefix else i

        # Write full content to article-<index>.md (only if we have content)
        content = entry.pop("content", "")  # remove from entry; file has it
        if r.get("has_content") and content:
            content_path = os.path.join(run_dir, f"article-{entry['index']}.md")
            with open(content_path, "w", encoding="utf-8") as f:
                # Front-matter for the agent, then body
                f.write(f"# {entry.get('title', '')}\n\n")
                if entry.get("source"):
                    f.write(f"**Source:** {entry['source']}\n")
                if entry.get("date"):
                    f.write(f"**Date:** {entry['date']}\n")
                if entry.get("url"):
                    f.write(f"**URL:** {entry['url']}\n")
                if entry.get("press_release_by"):
                    f.write(f"**Note:** Press release from {entry['press_release_by']}, carried by SpaceNews. "
                            "Not SpaceNews reporting.\n")
                f.write("\n---\n\n")

                # Images section — surface the article's image URLs even when
                # crawl4ai's markdown conversion stripped them from the body.
                # These are scoped to THIS article only; do not reuse across stories.
                article_images = entry.get("images") or []
                if article_images:
                    f.write("## Images available in this article\n\n")
                    f.write(
                        "_Choose the most relevant for the published post. "
                        "These belong to this story only — do not reuse across other stories._\n\n"
                    )
                    for img in article_images:
                        # Forward-compat: tolerate both new list[dict] and
                        # legacy list[str] shapes.
                        if isinstance(img, dict):
                            url = img.get("url", "")
                            alt = img.get("alt", "")
                        else:
                            url = img
                            alt = ""
                        f.write(f"![{alt}]({url})\n\n")
                    f.write("---\n\n")

                f.write(content)
            entry["content_file"] = content_path
        else:
            entry["content_file"] = None

        index_entries.append(entry)

    # Atomic flocked upsert via lib_index — every writer in the system
    # uses this so parallel n8n branches can't clobber each other.
    return upsert_entries(run_dir, index_entries)


# ============================================================
# Entrypoint
# ============================================================
def main():
    # Parse args: first positional is base64 JSON.
    # --run-id=<id> and --index-prefix=<prefix> are optional.
    args = sys.argv[1:]
    run_id = None
    index_prefix = ""
    positional = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a.startswith("--run-id="):
            run_id = a.split("=", 1)[1]
        elif a == "--run-id" and i + 1 < len(args):
            run_id = args[i + 1]
            skip_next = True
        elif a.startswith("--index-prefix="):
            index_prefix = a.split("=", 1)[1]
        elif a == "--index-prefix" and i + 1 < len(args):
            index_prefix = args[i + 1]
            skip_next = True
        else:
            positional.append(a)

    if not positional:
        _log_event({"event": "input_error", "reason": "no_input"})
        print(json.dumps({"error": "No input provided. Expected base64 JSON as first arg."}))
        sys.exit(1)

    try:
        raw = base64.b64decode(positional[0]).decode("utf-8")
        articles = json.loads(raw)
    except Exception as e:
        _log_event({
            "event": "input_error",
            "reason": "decode_failed",
            "error": f"{type(e).__name__}: {e}",
        })
        print(json.dumps({"error": f"Failed to decode input: {e}"}))
        sys.exit(1)

    if not isinstance(articles, list) or not articles:
        _log_event({"event": "input_error", "reason": "empty_or_not_list"})
        print(json.dumps({"error": "Input must be a non-empty list of articles"}))
        sys.exit(1)

    started = datetime.now(timezone.utc)
    _log_event({"event": "scrape_start", "run_id": run_id, "article_count": len(articles)})

    try:
        results = asyncio.run(scrape_all(articles))
    except Exception as e:
        # Total outage — crawler never came up, or something escaped the
        # per-article guards. Emit both the fatal event AND a scrape_complete
        # (success=0) so anything keyed on "scrape_complete" sees a terminal
        # signal rather than an apparently-still-running job.
        err_msg = f"{type(e).__name__}: {e}"
        _log_event({
            "event": "scrape_fatal",
            "run_id": run_id,
            "error": err_msg,
            "article_count": len(articles),
        })
        _log_event({
            "event": "scrape_complete",
            "run_id": run_id,
            "total": len(articles),
            "success": 0,
            "failed": len(articles),
            "fatal": True,
            "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
            "failures": [{"url": a.get("url", ""), "error": err_msg} for a in articles],
        })
        # Keep the original single-object error shape on stdout — the n8n
        # SSH node's downstream parser already handles this form.
        print(json.dumps({"error": f"Scrape failed: {e}"}))
        sys.exit(1)

    # Per-run summary for the alert path. Any article that came back without
    # has_content counts as a failure, whether it was a hard crawl error, an
    # unknown site, an anti-bot block, or an empty extraction.
    success_count = sum(1 for r in results if r.get("has_content"))
    failed_count = len(results) - success_count
    _log_event({
        "event": "scrape_complete",
        "run_id": run_id,
        "total": len(results),
        "success": success_count,
        "failed": failed_count,
        "fatal": False,
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "failures": [
            {"url": r.get("url", ""), "error": r.get("error", "unknown")}
            for r in results
            if not r.get("has_content")
        ],
    })

    if run_id:
        # File mode: write per-article files, emit index to stdout
        index = _write_file_output(results, run_id, index_prefix=index_prefix)
        print(json.dumps(index, ensure_ascii=False))
    else:
        # Inline mode: back-compat with prototype
        print(json.dumps(results, ensure_ascii=False))

    # Exit non-zero ONLY when every article failed. Partial success is a
    # successful run from the pipeline's point of view — downstream already
    # filters on has_content — and the scrape_complete event above carries
    # the per-article failures for the alert path.
    if success_count == 0 and failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
