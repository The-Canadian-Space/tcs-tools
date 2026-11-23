"""Tests for image descriptions (IMAGE_OWN_ALT_V1) and NASA Science's page chrome (NASA_SCIENCE_CHROME_V1), both
tcs-workflows#244.

Run:
    python working-files/python-scripts/test_article_scraper_alt.py

Crawl4AI and lib_index are stubbed, so this runs anywhere. The caption-in-the-page tests need BeautifulSoup and are
skipped without it (the VPS's crawl4ai-env has it). The fixtures are cut down from the pages the 2026-11-08 audit
saved: an image with an empty alt got Crawl4AI's `desc`, the text around it, which on Firefly, Space Scout, SpaceQ
and NASA was the article's opening and on EIN Presswire the whole gallery's captions.
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

try:
    import bs4  # noqa: F401
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

FFY_URL = 'https://fireflyspace.com/news/firefly-aerospace-signs-rocket-engine-deal/'
FFY_IMG = 'https://fireflyspace.com/wp-content/uploads/2026/11/image.png'
FFY_OPENING = ('*Firefly CEO Jason Kim and AST SpaceMobile Chairman and CEO Abel Avellan in front of Firefly’s Miranda '
               'engine at its Rocket Ranch in Briggs, Texas; Credit: Firefly Aerospace CEDAR PARK, Texas, November 6, '
               '2026 – Firefly Aerospace (Nasdaq: FLY), a market leading space and defense technology company, '
               'today announced an agreement')
FFY_CAPTION = 'Miranda engine firing at full power at Firefly’s Rocket Ranch in Briggs, Texas; Credit: Firefly Aerospace'
SS_URL = 'https://www.spacescout.info/2026/10/kosmos-2612-iceye/'
SS_IMG = 'https://i0.wp.com/www.spacescout.info/wp-content/uploads/2026/10/image-18.png?resize=800%2C450&ssl=1'
SS_CAPTION = ('Imagery showing Kosmos 2612 just over 1 kilometer from ICEYE-X36. It should be noted that this image may '
              'not be the point of closest approach. Image credit: Vantor.')
EIN_URL = 'https://www.einpresswire.com/article/860000000/nordspace-contract'
NSF_URL = 'https://www.nasaspaceflight.com/2026/11/fixture/'
APOD_URL = 'https://science.nasa.gov/image-article/apod-2026-november-7-de-gerlache-kocher-massif/'


def _result(url, html='', markdown='', images=None):
    return types.SimpleNamespace(url=url, html=html, markdown=markdown, success=True,
                                 media={'images': list(images or [])})


def _img(src, alt='', desc=None, w=800, h=450):
    d = {'src': src, 'alt': alt, 'width': w, 'height': h}
    if desc is not None:
        d['desc'] = desc
    return d


class BodyTextIsNotAnAltTests(unittest.TestCase):
    def test_empty_alt_stays_empty_without_a_caption(self):
        res = _result(FFY_URL, images=[_img(FFY_IMG, desc=FFY_OPENING, w=624, h=351)])
        self.assertEqual(A.collect_images(res), [{'url': FFY_IMG, 'alt': ''}])

    def test_italic_line_under_the_picture_is_its_caption(self):
        body = f'Opening paragraph.\n\n![]({FFY_IMG})\n_*{FFY_CAPTION}_\n\nNext paragraph.'
        res = _result(FFY_URL, images=[_img(FFY_IMG, desc=FFY_OPENING, w=624, h=351)])
        self.assertEqual(A.collect_images(res, body), [{'url': FFY_IMG, 'alt': FFY_CAPTION}])

    def test_caption_on_the_pictures_own_line(self):
        body = f'![]({SS_IMG})_{SS_CAPTION}_\nIn an unprecedented move, a Russian military satellite ...'
        res = _result(SS_URL, images=[_img(SS_IMG, desc=SS_CAPTION + ' In an unprecedented move, a Russian')])
        self.assertEqual(A.collect_images(res, body),
                         [{'url': 'https://i0.wp.com/www.spacescout.info/wp-content/uploads/2026/10/image-18.png?ssl=1',
                           'alt': SS_CAPTION}])

    def test_italic_paragraph_after_a_blank_line_is_not_a_caption(self):
        body = f'![]({FFY_IMG})\n\n_An editor’s note in italics, which is not about the picture._'
        res = _result(FFY_URL, images=[_img(FFY_IMG, desc=FFY_OPENING)])
        self.assertEqual(A.collect_images(res, body)[0]['alt'], '')

    def test_a_partly_italic_line_is_not_a_caption(self):
        body = f'![]({FFY_IMG})\n_Credit: Firefly_ and then the article goes on in plain text.'
        res = _result(FFY_URL, images=[_img(FFY_IMG, desc=FFY_OPENING)])
        self.assertEqual(A.collect_images(res, body)[0]['alt'], '')

    def test_the_pictures_own_alt_wins(self):
        body = f'![]({FFY_IMG})\n_{FFY_CAPTION}_'
        res = _result(FFY_URL, images=[_img(FFY_IMG, alt='Miranda engine test', desc=FFY_OPENING)])
        self.assertEqual(A.collect_images(res, body)[0]['alt'], 'Miranda engine test')

    def test_a_resized_copy_in_the_body_still_matches(self):
        listed = 'https://spaceq.ca/wp-content/uploads/2026/11/share-price-1024x512.png'
        body = '![](https://spaceq.ca/wp-content/uploads/2026/11/share-price.png?resize=780%2C390&ssl=1)\n_The share price this year._'
        res = _result('https://spaceq.ca/fixture/', images=[_img(listed, desc='Maritime Launch Services has won final approval')])
        self.assertEqual(A.collect_images(res, body)[0]['alt'], 'The share price this year.')


@unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
class CaptionInThePageTests(unittest.TestCase):
    def test_figcaption(self):
        html = ('<div class="entry-content"><figure class="wp-block-image"><img alt="" src="https://example.com/a/photo-1024x683.jpg">'
                '<figcaption>Launch at dawn. Credit: Example</figcaption></figure><p>Body.</p></div>')
        res = _result('https://spacenews.com/fixture/', html=html,
                      images=[_img('https://example.com/a/photo-1024x683.jpg', desc='Body text of the whole article')])
        self.assertEqual(A.collect_images(res)[0]['alt'], 'Launch at dawn. Credit: Example')

    def test_gallery_pictures_keep_their_own_captions(self):
        html = ('<figure class="wp-block-gallery">'
                '<figure class="wp-block-image"><img alt="" src="https://example.com/one.jpg"><figcaption>First</figcaption></figure>'
                '<figure class="wp-block-image"><img alt="" src="https://example.com/two.jpg"></figure>'
                '<figcaption>Gallery caption</figcaption></figure>')
        res = _result('https://spacenews.com/fixture/', html=html,
                      images=[_img('https://example.com/one.jpg', desc='First Gallery caption'),
                              _img('https://example.com/two.jpg', desc='First Gallery caption')])
        self.assertEqual([i['alt'] for i in A.collect_images(res)], ['First', ''])

    def test_ein_gallery_tiles(self):
        tile = ('<div class="pr_image"><a href="https://img.einpresswire.com/large/{n}/x.jpg">'
                '<img src="https://img.einpresswire.com/medium/{n}/photo-{n}.jpg" width="300" /></a><p>{cap}</p></div>')
        caps = ['Hon. Minister David McGuinty Announcing $20M Contract with NordSpace in Ottawa',
                'NordSpace’s Engineers Preparing the Taiga Rocket for Launch at the Company’s Atlantic Spaceport Complex (ASX)']
        html = '<div class="pr_images_column">' + ''.join(tile.format(n=n, cap=c) for n, c in enumerate(caps, 1)) + '</div>'
        gallery_text = ' '.join(caps)
        res = _result(EIN_URL, html=html, images=[
            _img(f'https://img.einpresswire.com/medium/{n}/photo-{n}.jpg', desc=gallery_text, w=300, h=200) for n in (1, 2)])
        self.assertEqual([i['alt'] for i in A.collect_images(res)], caps)

    def test_wp_caption(self):
        html = ('<div class="wp-caption"><img src="https://example.com/old.jpg"><p class="wp-caption-text">'
                'Classic editor caption</p></div>')
        res = _result('https://spacenews.com/fixture/', html=html, images=[_img('https://example.com/old.jpg', desc='x y z')])
        self.assertEqual(A.collect_images(res)[0]['alt'], 'Classic editor caption')

    def test_nasa_caption_beside_the_figure(self):
        # www.nasa.gov, 2026-11-11: the <figure> holds only the link and the picture; the caption is its sibling.
        html = ('<div class="entry-content"><div class="hds-media hds-module wp-block-image"><div class="nasa-block-align-inline">'
                '<div class="hds-media-wrapper"><figure class="hds-media-inner"><a href="#"><img alt="" '
                'src="https://www.nasa.gov/wp-content/uploads/2026/11/jsc2026e403156.jpg?w=2048"></a></figure>'
                '<figcaption class="hds-caption"><div class="hds-caption-text">Ellen Ellis, CHAPEA Mission 2 medical '
                'officer, exercises on a stationary bike inside the habitat.</div></figcaption></div></div></div>'
                '<p>Body.</p></div>')
        res = _result('https://www.nasa.gov/centers-and-facilities/johnson/fixture/', html=html,
                      images=[_img('https://www.nasa.gov/wp-content/uploads/2026/11/jsc2026e403156.jpg?w=2048', w=2048, h=1365)])
        self.assertEqual(A.collect_images(res)[0]['alt'],
                         'Ellen Ellis, CHAPEA Mission 2 medical officer, exercises on a stationary bike inside the habitat.')

    def test_ars_photoswipe_caption(self):
        # arstechnica.com, 2026-11-11: the lead picture has no <figure>; its lightbox item carries the caption.
        html = ('<div class="relative aspect-video"><div class="ars-lightbox"><div class="ars-lightbox-item">'
                '<a class="cursor-zoom-in" href="#"><img alt="" src="https://cdn.arstechnica.net/wp-content/uploads/2026/11/'
                'lunar-horizon-2048x1366.jpg"></a><div class="pswp-caption-content">The lunar horizon, as seen by astronauts on '
                'NASA’s Artemis II mission on April 6, 2026. Credit: NASA</div></div></div></div>')
        res = _result('https://arstechnica.com/space/2026/11/fixture/', html=html,
                      images=[_img('https://cdn.arstechnica.net/wp-content/uploads/2026/11/lunar-horizon-2048x1366.jpg', w=2048, h=1366)])
        self.assertEqual(A.collect_images(res)[0]['alt'],
                         'The lunar horizon, as seen by astronauts on NASA’s Artemis II mission on April 6, 2026. Credit: NASA')

    def test_a_lazy_placeholder_is_not_another_picture(self):
        html = ('<div class="wrap"><img src="data:image/gif;base64,R0lGOD" data-src="https://example.com/real.jpg" alt="">'
                '<figcaption>Own caption</figcaption></div>')
        res = _result('https://spacenews.com/fixture/', html=html, images=[_img('https://example.com/real.jpg')])
        self.assertEqual(A.collect_images(res)[0]['alt'], 'Own caption')

    def test_a_caption_is_never_taken_from_a_frame_with_other_pictures(self):
        html = ('<div class="entry-content"><img src="https://example.com/a.jpg" alt=""><img src="https://example.com/b.jpg" alt="">'
                '<p class="wp-caption-text">Caption for one of them</p></div>')
        res = _result('https://spacenews.com/fixture/', html=html,
                      images=[_img('https://example.com/a.jpg'), _img('https://example.com/b.jpg')])
        self.assertEqual([i['alt'] for i in A.collect_images(res)], ['', ''])


class JunkFiltersReadTheOldTextTests(unittest.TestCase):
    """The per-site filters keyed on alt text matched Crawl4AI's desc too. They still do, through seen_alts."""

    def test_nsf_sponsor_image_with_only_a_desc_is_still_dropped(self):
        ad = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/11/promo.jpg'
        res = _result(NSF_URL, images=[_img(ad, desc='Stay at the Margaritaville Beach Resort for launch week')])
        seen = {}
        images = A.collect_images(res, '', seen)
        self.assertEqual(images, [{'url': ad, 'alt': ''}])
        self.assertEqual(A._filter_cruft_images(NSF_URL, images, seen), [])

    def test_scrape_one_passes_the_old_text_to_the_filter(self):
        ad = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/11/promo.jpg'
        photo = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/11/launch.jpg'
        res = _result(NSF_URL, markdown='Launch report paragraph.', images=[
            _img(ad, desc='Stay at the Margaritaville Beach Resort for launch week'),
            _img(photo, alt='Falcon 9 lifts off')])

        async def fake_fetch(crawler, u, cfg):
            return res, None, 1

        saved = A._fetch_with_retry
        A._fetch_with_retry = fake_fetch
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                item = asyncio.run(A.scrape_one(None, {'url': NSF_URL, 'title': 'T'}))
        finally:
            A._fetch_with_retry = saved
        self.assertEqual(item['images'], [{'url': photo, 'alt': 'Falcon 9 lifts off'}])


class NasaScienceChromeTests(unittest.TestCase):
    def test_apod_chrome_is_excluded(self):
        kw = A.run_config_for(APOD_URL).kwargs
        for sel in ('.wp-block-smd-block-next-prev', '.wp-block-nasa-blocks-related-link',
                    '.hds-media-detail-hero p.label', '.hds-media-detail-hero p.heading-22',
                    '.hds-media-detail-hero .media-detail-hero__subtext'):
            self.assertIn(sel, kw['excluded_selector'])
        # The Photojournal Downloads block holds the page's only picture (checked 2026-11-11), so it stays in.
        self.assertNotIn('post-meta', kw['excluded_selector'])
        self.assertTrue(kw['css_selector'].startswith('div.entry-content, div.single-blog-content'))
        # Crawl4AI puts each selector inside a double-quoted JS string.
        self.assertNotIn('"', kw['excluded_selector'])

    def test_photojournal_downloads_text_is_cut(self):
        # science.nasa.gov/photojournal/comparing-off-world-rover-driving-distances/, 2026-11-11.
        body = ('##  Downloads \n![](https://assets.science.nasa.gov/content/dam/science/psd/photojournal/pia/pia26/pia26814/'
                'PIA26814.png/jcr:content/renditions/cq5dam.thumbnail.319.319.png)\n## Comparing Off-World Rover Driving Distances\n'
                'PNG (568.05 KB)\n[ ](https://assets.science.nasa.gov/content/dam/science/psd/photojournal/pia/pia26/pia26814/PIA26814.png)\n'
                '## Description\nThis infographic illustrates the distances driven by various rovers.')
        out = A._site_post_process('https://science.nasa.gov/photojournal/comparing-off-world-rover-driving-distances/', body)
        self.assertEqual(out, '## Description\nThis infographic illustrates the distances driven by various rovers.')

    def test_downloads_without_a_description_heading_is_left(self):
        body = '##  Downloads \nPNG (568.05 KB)\nSome text with no Description heading after it.'
        out = A._site_post_process('https://science.nasa.gov/photojournal/fixture/', body)
        self.assertIn('Some text with no Description heading after it.', out)

    def test_www_nasa_gov_is_unchanged(self):
        self.assertNotIn('exclude', A.config_for('https://www.nasa.gov/news-release/fixture/'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
