"""Tests for STRICT_SCOPE_HEAD_META (tcs-workflows#102): strict_scope sites get their og:image and date back.

Run:
    python working-files/python-scripts/test_article_scraper_strict_scope.py

Crawl4AI and lib_index (which lives only on the VPS) are stubbed, so this runs anywhere. What the real
crawler returns was measured on the VPS (2026-10-03, crawl4ai 0.9.4): with css_selector it returns
<div class='crawl4ai-result'> wrapping each selected element's outerHTML, so selected <meta> tags arrive
as plain elements in that div.
"""
import os
import sys
import types
import unittest

_crawl4ai = types.ModuleType('crawl4ai')


class _Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


_crawl4ai.AsyncWebCrawler = object
_crawl4ai.BrowserConfig = _Config
_crawl4ai.CrawlerRunConfig = _Config
_crawl4ai.CacheMode = types.SimpleNamespace(BYPASS='bypass')
sys.modules.setdefault('crawl4ai', _crawl4ai)
_lib_index = types.ModuleType('lib_index')
_lib_index.upsert_entries = lambda *a, **k: None
sys.modules.setdefault('lib_index', _lib_index)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import article_scraper as A  # noqa: E402

STRICT_URLS = [
    'https://www.esa.int/Space_Safety/Hera/A_hitchhiker_s_guide_to_Hera_s_target_asteroids',
    'https://science.nasa.gov/image-article/apod-2026-october-3-selfie-at-vera-rubin-ridge/',
    'https://www.nasa.gov/blogs/spacestation/2026/10/02/station-crew-expands-as-nasas-spacex-crew-12-prepares-to-depart/',
    'https://aerospaceamerica.aiaa.org/canadian-rocket-startup-plans-engine-component-hot-fire-tests-in-december/',
    'https://spaceflightnow.com/2026/11/06/fixture-headline/',
]

# The shape Crawl4AI 0.9.4 returns for css_selector: each match's outerHTML, joined, in one div.
SCOPED = """<div class='crawl4ai-result'>
<div class="post-content entry-content"><p>Body text.</p><time datetime="2026-01-01">January</time></div>
<meta property="og:image" content="https://example.org/wp-content/uploads/2026/09/lead-scaled.webp">
<meta property="article:published_time" content="2026-09-21T09:00:00+00:00">
</div>"""


class StrictScopeConfigTests(unittest.TestCase):
    def test_every_strict_site_selects_the_head_tags(self):
        for url in STRICT_URLS:
            with self.subTest(url=url):
                site = A.config_for(url)
                self.assertTrue(site.get('strict_scope'), 'expected a strict_scope site')
                kw = A.run_config_for(url).kwargs
                # STRICT_SCOPE_TITLE_META (tcs-workflows#299): a "title_meta" site selects og:title as well.
                extra = ', ' + A.STRICT_SCOPE_TITLE_META if site.get('title_meta') else ''
                self.assertEqual(kw['css_selector'], site['selector'] + ', ' + A.STRICT_SCOPE_HEAD_META + extra)
                self.assertNotIn('target_elements', kw)

    def test_only_aerospace_america_selects_og_title(self):
        # tcs-workflows#299: AA's <h1> is outside its selector. The other strict sites keep the <h1> fallback.
        titled = sorted(k for k, v in A.SITE_CONFIG.items() if v.get('title_meta'))
        self.assertEqual(titled, ['aerospaceamerica.aiaa.org'])
        self.assertTrue(A.SITE_CONFIG['aerospaceamerica.aiaa.org'].get('strict_scope'))
        self.assertEqual(A.STRICT_SCOPE_TITLE_META, "meta[property='og:title']")
        self.assertNotIn('"', A.STRICT_SCOPE_TITLE_META)
        for url in STRICT_URLS:
            with self.subTest(url=url):
                css = A.run_config_for(url).kwargs['css_selector']
                self.assertEqual('og:title' in css, 'aerospaceamerica' in url)

    def test_strict_scope_sites_are_the_ones_that_were_checked(self):
        # spaceflightnow.com joined with tcs-workflows#244: its thumbnail and date were checked on the live page.
        strict = sorted(k for k, v in A.SITE_CONFIG.items() if v.get('strict_scope'))
        self.assertEqual(strict, sorted(['esa.int', 'science.nasa.gov', 'nasa.gov', 'aerospaceamerica.aiaa.org',
                                         'spaceflightnow.com']),
                         'A new strict_scope site: check that its thumbnail and date match an unscoped crawl '
                         '(see STRICT_SCOPE_HEAD_META), then add it here.')

    def test_other_sites_unchanged(self):
        # This also checked an ESA Multimedia page, through an override that wasn't strict. tcs-workflows#234 retired
        # the override: every page on www.esa.int is strict now.
        for url in ('https://spacenews.com/some-article/',
                    'https://www.planetary.org/articles/some-article'):
            with self.subTest(url=url):
                kw = A.run_config_for(url).kwargs
                self.assertNotIn('css_selector', kw)
                self.assertTrue(kw.get('target_elements'))
                self.assertFalse(any('meta' in s for s in kw['target_elements']))

    def test_selector_is_safe_inside_crawl4ais_js_string(self):
        # Crawl4AI runs document.querySelectorAll("<selector>") per comma-separated part.
        self.assertNotIn('"', A.STRICT_SCOPE_HEAD_META)
        parts = [s.strip() for s in A.STRICT_SCOPE_HEAD_META.split(',')]
        self.assertEqual(parts, ["meta[property='og:image']", "meta[name='twitter:image']",
                                 "meta[property='article:published_time']", "meta[name='parsely-pub-date']"])

    def test_og_title_left_out_so_titles_stay_as_they_were(self):
        self.assertNotIn('og:title', A.STRICT_SCOPE_HEAD_META)


class ExtractionFromScopedHtmlTests(unittest.TestCase):
    def test_thumbnail_comes_back(self):
        self.assertEqual(A.extract_og_image(SCOPED), 'https://example.org/wp-content/uploads/2026/09/lead-scaled.webp')

    def test_meta_date_wins_over_a_time_tag_in_the_body(self):
        self.assertEqual(A.extract_date(SCOPED, url='https://aerospaceamerica.aiaa.org/x/'), '2026-09-21T09:00:00+00:00')

    def test_twitter_image_when_there_is_no_og_image(self):
        html = "<div class='crawl4ai-result'><meta name=\"twitter:image\" content=\"https://e.org/t.jpg\"></div>"
        self.assertEqual(A.extract_og_image(html), 'https://e.org/t.jpg')

    def test_without_the_tags_nothing_is_invented(self):
        html = "<div class='crawl4ai-result'><div class=\"entry-content\"><p>Only body.</p></div></div>"
        self.assertEqual(A.extract_og_image(html), '')
        self.assertEqual(A.extract_date(html, url='https://science.nasa.gov/image-article/x/'), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
