"""Tests for tcs-workflows#299 (Aerospace America's headline) and #304 (the Planetary Society's closing cards).

Run:
    python working-files/python-scripts/test_article_scraper_299.py

Crawl4AI and lib_index (which lives only on the VPS) are stubbed, so this runs anywhere.

- #299: Aerospace America is a strict_scope site. Crawl4AI 0.9.4 returns only the selected elements (each one's
  outerHTML in a div.crawl4ai-result), and the headline's <h1> is outside the selected body. With og:title left out
  of STRICT_SCOPE_HEAD_META, a caller that passed no title (the Healthcheck's company rows) got an empty one. The
  site now selects og:title too (STRICT_SCOPE_TITLE_META), and a trailing site name is stripped if og:title has one.
- #304: in late 2026 the Planetary Society's donate call-out is headed "Powered by people like you", which the
  footer cut didn't know, and a related-episode card sits just before it. Both reached the article text (the
  Healthcheck's scrape of the 2026-11-20 Voyager interview, 2026-11-23).
"""
import asyncio
import contextlib
import io
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

AA_URL = 'https://aerospaceamerica.aiaa.org/canadian-rocket-startup-plans-engine-component-hot-fire-tests-in-december/'
AA_HEADLINE = 'Canadian rocket startup plans engine component hot-fire tests in December'
AA_BODY = ('Toronto-based Canada Rocket Company is readying this year for hot-fire testing of engine components for its '
           'R-2 rocket. ') * 8


def _aa_scoped(og_title=None):
    """The shape Crawl4AI 0.9.4 returns for AA's css_selector: the selected elements' outerHTML in one div."""
    og = '<meta property="og:title" content="' + og_title + '">\n' if og_title is not None else ''
    return ("<div class='crawl4ai-result'>\n"
            '<figcaption class="featured-img-caption">An illustration of the R-2. Credit: CRC</figcaption>\n'
            '<div class="post-content cf entry-content content-normal"><p>' + AA_BODY + '</p></div>\n'
            '<meta property="og:image" content="https://aerospaceamerica.aiaa.org/wp-content/uploads/2026/09/R-2.webp">\n'
            '<meta property="article:published_time" content="2026-09-21T09:00:00+00:00">\n' + og + '</div>')


def _result(url, html='', markdown=''):
    return types.SimpleNamespace(url=url, html=html, markdown=markdown, success=True, media={'images': []})


def _scrape(url, res, title=''):
    async def fake_fetch(crawler, u, cfg):
        return res, None, 1

    saved = A._fetch_with_retry
    A._fetch_with_retry = fake_fetch
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(A.scrape_one(None, {'url': url, 'title': title}))
    finally:
        A._fetch_with_retry = saved


class AerospaceAmericaTitleTests(unittest.TestCase):
    def test_no_title_given_the_og_title(self):
        item = _scrape(AA_URL, _result(AA_URL, _aa_scoped(AA_HEADLINE), AA_BODY))
        self.assertEqual(item['title'], AA_HEADLINE)

    def test_the_site_name_is_stripped(self):
        for suffix in (' - Aerospace America', ' | Aerospace America', ' – Aerospace America',
                       ' — Aerospace America'):
            with self.subTest(suffix=suffix):
                item = _scrape(AA_URL, _result(AA_URL, _aa_scoped(AA_HEADLINE + suffix), AA_BODY))
                self.assertEqual(item['title'], AA_HEADLINE)

    def test_a_given_title_is_kept(self):
        item = _scrape(AA_URL, _result(AA_URL, _aa_scoped(AA_HEADLINE), AA_BODY), title='Card headline')
        self.assertEqual(item['title'], 'Card headline')

    def test_without_the_tag_the_title_is_empty_as_before(self):
        # The cause, kept as a test: the scoped page holds no og:title and no <h1>.
        item = _scrape(AA_URL, _result(AA_URL, _aa_scoped(None), AA_BODY))
        self.assertEqual(item['title'], '')

    def test_the_tag_changes_nothing_else(self):
        with_tag = _scrape(AA_URL, _result(AA_URL, _aa_scoped(AA_HEADLINE), AA_BODY))
        without = _scrape(AA_URL, _result(AA_URL, _aa_scoped(None), AA_BODY))
        for key in set(with_tag) | set(without):
            if key != 'title':
                with self.subTest(key=key):
                    self.assertEqual(with_tag.get(key), without.get(key))

    def test_other_sites_keep_a_name_that_looks_like_the_suffix(self):
        url = 'https://spaceq.ca/a-story/'
        html = ('<html><head><meta property="og:title" content="A review - Aerospace America"></head>'
                '<body><div class="entry-content"><p>' + AA_BODY + '</p></div></body></html>')
        item = _scrape(url, _result(url, html, AA_BODY))
        self.assertEqual(item['title'], 'A review - Aerospace America')


PL_URL = 'https://www.planetary.org/articles/voyager-1-at-one-light-day-a-conversation-with-voyager-project-scientist-linda-spilker'
PL_STORY = ('**Sarah Al-Ahmed:** Thank you so much, Linda.\n'
            '**Linda Spilker:** Thank you, Sarah. I just want to say: Go, Voyager.\n'
            '**Sarah Al-Ahmed:** Go, Voyager.')
PL_RELATED = ('### [Voyager 1 reaches one light-day from Earth](https://www.planetary.org/planetary-radio/2026-voyager-1-one-light-day)\n'
              'Voyager 1 is now one light-day from Earth.\n')
PL_DONATE = ('### Powered by people like you\n'
             'Together, we make space exploration and discoveries possible. Please make your gift today.\n'
             '[Donate](https://connect.planetary.org/77?s_cid=2026yearend&s_src=2026yearend&s_subsrc=card)\n')
PL_FOOTER = ('Read more: [ Voyager](https://www.planetary.org/articles?keywords=voyager)\n'
             'You are here: [Home](https://www.planetary.org/) > [Articles](https://www.planetary.org/articles/)\n')


class PlanetaryCardsTests(unittest.TestCase):
    def test_the_interviews_tail_is_cut_to_the_last_line(self):
        md = PL_STORY + '\n\n' + PL_RELATED + PL_DONATE
        self.assertEqual(A._site_post_process(PL_URL, md), PL_STORY)

    def test_with_the_footer_after_it_too(self):
        md = PL_STORY + '\n\n' + PL_RELATED + PL_DONATE + PL_FOOTER
        self.assertEqual(A._site_post_process(PL_URL, md), PL_STORY)

    def test_the_donate_card_alone(self):
        self.assertEqual(A._site_post_process(PL_URL, PL_STORY + '\n\n' + PL_DONATE), PL_STORY)

    def test_any_campaign_heading(self):
        md = PL_STORY + '\n\n' + PL_DONATE.replace('Powered by people like you', 'Make your gift go twice as far')
        self.assertEqual(A._site_post_process(PL_URL, md), PL_STORY)

    def test_two_related_cards(self):
        second = PL_RELATED.replace('2026-voyager-1-one-light-day', 'voyager-at-45').replace('reaches one', 'at 45')
        md = PL_STORY + '\n\n' + PL_RELATED + second + PL_DONATE
        self.assertEqual(A._site_post_process(PL_URL, md), PL_STORY)

    def test_a_linked_heading_inside_the_story_stays(self):
        story = ('Intro.\n### [The Grand Tour](https://www.planetary.org/space-missions/voyager)\n'
                 'Voyager 2 flew past four planets.\n\n' + PL_STORY)
        self.assertEqual(A._site_post_process(PL_URL, story + '\n\n' + PL_RELATED + PL_DONATE), story)

    def test_no_footer_no_cut(self):
        # Without a donate card or footer marker, a closing linked heading isn't treated as a card.
        md = PL_STORY + '\n\n' + PL_RELATED.rstrip()
        self.assertEqual(A._site_post_process(PL_URL, md), md)

    def test_a_donate_link_far_from_its_heading_isnt_the_card(self):
        md = ('### Mission notes\nOne.\nTwo.\nThree.\nFour.\n'
              '[Donate](https://connect.planetary.org/77)\nMore story.')
        self.assertEqual(A._site_post_process(PL_URL, 'Intro.\n\n' + md), 'Intro.\n\n' + md)

    def test_the_old_heading_still_cuts(self):
        md = (PL_STORY + '\n\n### You Power the Next Great Discovery!\nFund future scientific research today.\n'
              '[Donate](https://connect.planetary.org/73?s_cid=step2026)\n' + PL_FOOTER)
        self.assertEqual(A._site_post_process(PL_URL, md), PL_STORY)

    def test_other_sites_untouched(self):
        url = 'https://spacenews.com/a-story/'
        md = PL_STORY + '\n\n' + PL_RELATED + PL_DONATE
        self.assertEqual(A._site_post_process(url, md), md)



class StokeSourceNameTests(unittest.TestCase):
    """tcs-workflows#309: the company is Stoke Space; the label in each article file's header said "Stoked Space"."""

    def test_the_label_is_the_companys_name(self):
        for url in ('https://www.stokespace.com/news/nova-pathfinder', 'https://stokespace.com/news/x'):
            with self.subTest(url=url):
                self.assertEqual(A.detect_source(url), 'Stoke Space')

    def test_no_label_says_stoked(self):
        self.assertFalse([k for k, v in A.SITE_CONFIG.items() if 'stoked' in str(v.get('source', '')).lower()])


if __name__ == '__main__':
    unittest.main(verbosity=2)
