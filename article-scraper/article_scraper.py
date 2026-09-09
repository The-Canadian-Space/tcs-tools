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
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_index import upsert_entries
from urllib.parse import urlparse

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    CacheMode,
)


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
    },
    "esa.int": {
        "source": "ESA",
        "selector": "div.article__block, div.abstract",
        "strict_scope": True,   # T3 2026-07-19: target_elements matches 8 article__block divs; css_selector isolates content
        "keep_overlays": True,  # T3 2026-07-19: images wrapped in <figure>/<a> get stripped as overlays otherwise
    },
    "europeanspaceflight.com": {
        "source": "European Spaceflight",
        "selector": "div.td-post-content",
    },
    "nasaspaceflight.com": {
        "source": "NASASpaceFlight",
        "selector": "div.inner-post-entry, div.entry-content",
        "networkidle": True,
        "wait": 3.0,
    },
    "spaceflightnow.com": {
        "source": "Spaceflight Now",
        "selector": "div.entry-content",
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
    },
    "nasa.gov": {
        "source": "NASA",
        "selector": "div.entry-content, div.single-blog-content",
        "strict_scope": True,   # 2026-08-06 audit (DB run 3279): sidebar "Recent Posts"
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
    },
    "stokespace.com": {
        "source": "Stoked Space",
        # 2026-09-09: Stoke redesigned WordPress -> Next.js. Old selector
        # was `div.post__content-entry`; new article body wrapper is
        # `div.news-rich-text` (also carries `px-main grid-main` utility
        # classes on the new site).
        "selector": "div.news-rich-text",
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
        "selector": ".post-rich-text, .w-richtext",
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
        "selector": "div.field--name-field-band-body, div.node__content",
    },
    "reactiondynamics.space": {
        "source": "Reaction Dynamics",
        "selector": "main.prose",
        "networkidle": True,
        "wait": 2.0,
    },
    "canadarocketcompany.com": {
        "source": "Canada Rocket Company",
        "networkidle": True,
        "wait": 3.0,
    },
}


def _host_for(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def config_for(url: str) -> dict:
    """Return the SITE_CONFIG entry best matching this URL.

    URL-path overrides: a small number of sites need different selectors for
    different page templates within the same host. ESA Multimedia pages
    (/ESA_Multimedia/Videos/ + /ESA_Multimedia/Images/) are gallery landing
    pages with a tiny <div class="modal__tab-description"> content block
    surrounded by the global site nav; the default esa.int selector
    (div.article__block, div.abstract) doesn't exist on these pages, so
    Crawl4AI falls back to whole-page extraction and we get hundreds of
    navigation links instead of content. (Audit run 2026-06-20: 12 of 20
    esa.int articles broken this way.)
    """
    host = _host_for(url)
    # URL-path overrides (must come before generic host lookup)
    if host == "esa.int" and (
        "/ESA_Multimedia/Videos/" in url
        or "/ESA_Multimedia/Images/" in url
    ):
        return {
            "source": "ESA",
            "selector": "div.modal__tab-description",
            "keep_overlays": True,  # video/image modal layout is decorative
        }
    # Exact match first
    if host in SITE_CONFIG:
        return SITE_CONFIG[host]
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
            kwargs["css_selector"] = site["selector"]
        else:
            kwargs["target_elements"] = [s.strip() for s in site["selector"].split(",")]
    if site.get("networkidle"):
        kwargs["wait_until"] = "networkidle"
        kwargs["page_timeout"] = 60_000
    if site.get("wait"):
        kwargs["delay_before_return_html"] = site["wait"]
    return CrawlerRunConfig(**kwargs)


# ============================================================
# Metadata extraction (always from full page HTML, not scoped)
# ============================================================
def extract_date(html: str, url: str = "") -> str:
    """Best-effort date extraction with fallbacks for sites that omit meta tags.

    Order:
      1. Standard meta tags (article:published_time, datePublished, etc.)
      2. <time datetime="...">
      3. WordPress-style URL slug /YYYY/MM/DD/ (clean ISO-like format)
      4. Spaceflight-Now-style <span class="entry-meta-date updated"> (human format, last resort)
    """
    if not html and not url:
        return ""

    if html:
        # Standard machine-readable patterns first
        patterns = [
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+itemprop=["\']datePublished["\'][^>]+content=["\']([^"\']+)["\']',
            r'<time[^>]+datetime=["\']([^"\']+)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                return m.group(1).strip()

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
            return m.group(1).strip()
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
    if re.search(r"\.(jpe?g|png|gif|webp|svg)$", alt, re.IGNORECASE):
        return ""
    if alt.lower() in {"image", "img", "photo", "picture", "thumbnail", "untitled", ""}:
        return ""
    if len(alt) < 3:
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
    # NAV_CHROME_ALT_V1 (2026-08-06 audit, DB run 3279): NASA + ESA `<img>`
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


def collect_images(result) -> list:
    """Collect content image URLs (with alt text) from crawl4ai's media dict,
    deduped to one canonical URL per source image, with scaled-down variants
    upgraded to full-size where the URL pattern allows.

    Returns a list of {"url": str, "alt": str} dicts in source order. Alt text
    comes from the highest-scoring variant of each canonical group.

    Filtering, rewriting, and dedup behave as before; only the return shape
    has changed (was list[str], now list[dict]).
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

    ordered = []
    best = {}

    for img in raw_imgs:
        if not isinstance(img, dict):
            continue
        src = img.get("src")
        if not src or src.startswith("data:"):
            continue
        # Resolve path-relative URLs against the source's origin
        if src.startswith("/") and not src.startswith("//") and origin:
            src = origin + src
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

        is_bare = canonical == path_norm and "?" not in src_clean
        if is_bare:
            score = float("inf")
        else:
            dim_m = re.search(r"-(\d+)x(\d+)(?=\.[a-zA-Z]{3,4})", src_clean)
            score = int(dim_m.group(1)) * int(dim_m.group(2)) if dim_m else 1

        alt = _clean_alt(img.get("alt") or img.get("desc"))

        if canonical not in best:
            ordered.append(canonical)
            best[canonical] = (src_clean, score, alt)
        elif score > best[canonical][1]:
            new_alt = alt or best[canonical][2]
            best[canonical] = (src_clean, score, new_alt)
        elif not best[canonical][2] and alt:
            best[canonical] = (best[canonical][0], best[canonical][1], alt)

    return [{"url": best[c][0], "alt": best[c][2]} for c in ordered]


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
    """Fetch once, returning (result, error_message_or_none)."""
    try:
        res = await crawler.arun(url=url, config=cfg)
    except Exception as e:
        return None, f"Crawler exception: {e}"
    if not getattr(res, "success", False):
        err = getattr(res, "error_message", None) or "Crawler returned unsuccessful result"
        return res, err
    return res, None


def _site_post_process(url: str, content: str) -> str:
    """Apply per-domain content cleanups identified in the 2026-06-20 audit.

    Each block runs only if the URL's host matches. Returns the cleaned
    content string. Safe to call on any URL — unknown hosts pass through.
    """
    if not url or not content:
        return content
    host = _host_for(url)
    out = content

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
            r"\n+\*\*Media Contacts?\*\*",
            r"\n+###\s+Media Contacts?:?",
            r"\n+##\s+Media Contacts?:?",
        ]:
            m = re.search(pat, out)
            if m:
                out = out[: m.start()].rstrip()
                break
        # SPACEQ_ESA_NASA_HYGIENE_V1 — 2026-06-27 audit: NASA release pages
        # (especially photojournal entries) bundle a "## Downloads" section
        # at end with JPEG/PNG file listings + MB sizes. Editorial metadata,
        # not article body. Strip from the heading to end.
        m = re.search(r"\n+##\s+Downloads\s*\n", out)
        if m:
            out = out[: m.start()].rstrip()

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
        # Trailing "**About Stoke Space**" boilerplate appears on every
        # Stoke article. Identified in 2026-06-21 audit.
        # Pattern: \n+**About Stoke Space**  \n  \n<one-paragraph body>
        out = re.sub(
            r"\n+\*\*About Stoke Space\*\*[\s\S]+\Z",
            "",
            out,
        ).rstrip()

    # ---- fireflyspace.com ----
    if "fireflyspace.com" in host:
        # Trailing "### ABOUT FIREFLY AEROSPACE" company boilerplate
        # paragraph (identical on every Firefly article — corporate footer,
        # not editorial). Followed by Filed Under: + optional back-link.
        # Strip from the heading to end of body.
        m = re.search(r"\n+###\s+ABOUT\s+FIREFLY\s+AEROSPACE", out, re.IGNORECASE)
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
        if m:
            out = out[: m.start()].rstrip()

    # ---- esa.int (general pages) ----  SPACEQ_ESA_NASA_HYGIENE_V1
    if "esa.int" in host:
        # 2026-06-27 audit: regular ESA pages (not the /ESA_Multimedia/ subset
        # handled via config_for) include trailing social-share + breadcrumb +
        # "About ESA" boilerplate. Strip from the social-share marker to end.
        # Patterns observed in runs 1901 + 1932:
        #   "Like" / "Thank you for liking" widget at body end
        #   ESA / Applications / Satellite navigation / ... breadcrumb
        #   "## About ESA" section
        out = re.sub(r"\n+(Like|Thank you for liking|You have already)[^\n]*", "", out)
        out = re.sub(r"\n+ESA\s+/\s+[A-Z][^\n]{0,200}", "", out)
        m = re.search(r"\n+##\s+About ESA\b", out)
        if m:
            out = out[: m.start()].rstrip()

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


def _filter_cruft_images(url: str, images: list) -> list:
    """Filter out per-domain image cruft from the images list.

    Currently handles spacenews.com author byline thumbnails. Other
    domains pass through unchanged.
    """
    if not url or not images:
        return images
    host = _host_for(url)
    if "spacenews.com" in host:
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
                alt = (img.get("alt", "") or "").strip().lower()
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
            # Not article content. Audited 2026-08-06 (run 3279 article-1
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
        cruft_url_markers = ["Hawaiian_Shirt", "Margaritaville", "/Nova.gif"]
        cruft_alt_markers = ["hawaiian", "margaritaville", "novatech"]
        filtered = []
        for img in images:
            if isinstance(img, dict):
                u = img.get("url", "") or ""
                alt = (img.get("alt", "") or "").strip().lower()
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
                alt = (img.get("alt", "") or "").strip().lower()
            else:
                u = str(img)
                alt = ""
            if any(m in u for m in cruft_url_markers):
                continue
            if any(m in alt for m in cruft_alt_markers):
                continue
            filtered.append(img)
        return filtered
    return images


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

    # First attempt — with per-site targeting
    res, err = await _fetch(crawler, url, cfg)
    # Retry once if the first attempt failed outright
    if err and not res:
        await asyncio.sleep(1.0)
        res, err = await _fetch(crawler, url, cfg)
    if err and not res:
        return _error_item(url, title, err)

    html = getattr(res, "html", "") or "" if res else ""
    content = get_content(res) if res else ""

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
        res2, _err2 = await _fetch(crawler, url, fallback_cfg)
        if res2 and getattr(res2, "success", False):
            if not html:
                html = getattr(res2, "html", "") or ""
            content = get_content(res2) or content
            if content:
                res = res2

    if not content:
        return _error_item(url, title, err or "No content extracted")

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
    content = _site_post_process(url, content)

    final_title = title or extract_og_title(html) or extract_h1_title(html) or ""
    # CRC page titles include ' - Canada Rocket Company' suffix; strip it.
    if final_title and 'canadarocketcompany.com' in url.lower():
        final_title = re.sub(r'\s*[-\u2013\u2014]\s*Canada Rocket Company\s*$', '', final_title).strip()
    # STOKE_TITLE_SUFFIX_STRIP — every Stoke og:title ends with
    # ' | Stoke Space / 100% reusable rockets / USA'. Strip.
    if final_title and 'stokespace.com' in url.lower():
        final_title = re.sub(r'\s*\|\s*Stoke Space.*$', '', final_title, flags=re.IGNORECASE).strip()

    return {
        "title": final_title,
        "url": url,
        "date": extract_date(html, url=url),
        "thumbnail": extract_og_image(html),
        "images": _filter_cruft_images(url, collect_images(res)),
        "source": detect_source(url),
        "content": content,
        "word_count": len(content.split()),
        "character_count": len(content),
        "has_content": True,
    }


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

    # Crawl4AI's logger writes to stdout. Redirect during scrape.
    devnull = open(os.devnull, "w")
    with contextlib.redirect_stdout(devnull):
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

    devnull.close()

    # Convert any raised exceptions into error items (shouldn't normally happen)
    out = []
    for art, res in zip(articles, results):
        if isinstance(res, Exception):
            out.append(_error_item(art["url"], art.get("title", ""), f"Unhandled: {res}"))
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
        print(json.dumps({"error": "No input provided. Expected base64 JSON as first arg."}))
        sys.exit(1)

    try:
        raw = base64.b64decode(positional[0]).decode("utf-8")
        articles = json.loads(raw)
    except Exception as e:
        print(json.dumps({"error": f"Failed to decode input: {e}"}))
        sys.exit(1)

    if not isinstance(articles, list) or not articles:
        print(json.dumps({"error": "Input must be a non-empty list of articles"}))
        sys.exit(1)

    try:
        results = asyncio.run(scrape_all(articles))
    except Exception as e:
        print(json.dumps({"error": f"Scrape failed: {e}"}))
        sys.exit(1)

    if run_id:
        # File mode: write per-article files, emit index to stdout
        index = _write_file_output(results, run_id, index_prefix=index_prefix)
        print(json.dumps(index, ensure_ascii=False))
    else:
        # Inline mode: back-compat with prototype
        print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
