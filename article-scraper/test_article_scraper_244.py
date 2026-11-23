"""Tests for the scraper fixes in tcs-workflows#244 and #273, and the scope marker in tcs-workflows#249.

Run:
    python working-files/python-scripts/test_article_scraper_244.py

Crawl4AI and lib_index (which lives only on the VPS) are stubbed, so this runs anywhere. The tests that count
selector matches or act out Crawl4AI's selection need BeautifulSoup, and they're skipped without it (the VPS's
crawl4ai-env has it). The fixtures are cut down from the pages as served between 2026-11-06 and 2026-11-10:

- Spaceflight Now's late-October redesign dropped div.entry-content. article.main holds the story.
- Maritime Launch moved to Next.js: the date is text in the header, the pictures go through /_next/image, and its
  og:image is a default press tile.
- NASA blog posts end with an avatar, Categories and a "More from" footer, whose teaser thumbnails reached the image
  list. News releases end at "-end-", and many open with the NASA insignia as their only picture.
- The Planetary Society's <article> also holds its footer, a donate call-out and, in the Downlink, a membership
  appeal. Its pictures arrive as srcsets, and as one copy per size folder.
- When target_elements match nothing, Crawl4AI converts the whole page, with no error. That's the #249 scope marker.
- From its 2026-11-12 release on, Maritime Launch's header says "3 min" (no "read"), the dateline's dash sits inside the
  bold, and a release without a photo carries the default press tile (#273, page as served on 2026-11-14).
- Maritime Launch's og:title ends with " | Maritime Launch Services"; its h1 is the bare headline (#286, page as served
  on 2026-11-18).

Unit tests can't see what the real crawler does with a selector, so each fix was also checked on the live pages.
"""
import asyncio
import contextlib
import io
import json
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

SFN_URL = 'https://spaceflightnow.com/2026/11/06/fixture-headline/'
MLS_URL = ('https://www.maritimelaunch.com/news/'
           'Maritime-Launch-Services-Receives-Final-Approval-To-List-On-The-Toronto-Stock-Exchange')
NASA_BLOG_URL = 'https://www.nasa.gov/blogs/spacestation/2026/11/06/spacewalk-cleanup/'
NASA_RELEASE_URL = 'https://www.nasa.gov/news-release/nasa-selects-three-companies/'
PLANETARY_URL = 'https://www.planetary.org/articles/perseverance-samples'
DOWNLINK_URL = 'https://www.planetary.org/the-downlink/space-artwork'

# Cut down from spaceflightnow.com as served on 2026-11-07.
SFN_PAGE = """<!DOCTYPE html>
<html lang="en-US">
<head>
<meta property="article:published_time" content="2026-11-07T02:18:04+00:00" />
<meta property="og:image" content="https://spaceflightnow.com/wp-content/uploads/2026/11/hero.jpg" />
</head>
<body>
<div class="site-topbar"><div class="site-topbar-inner">SATURDAY, NOVEMBER 78:00 AM EST Settings? Time zone</div></div>
<div class="mnl-top"><span class="mnl-label">Next Launch</span><span id="mnl-date">Nov 8, 3:25 PM EST</span></div>
<main class="shell"><div class="layout layout--flush">
<article class="main" id="post-1">
<div class="story-cat" id="story-cat">Falcon 9</div>
<h1 id="story-title">Fixture headline</h1>
<div class="story-byline" id="story-byline"><b><a href="/news-archive/?by=Jane%20Doe">Jane Doe</a></b><time
 data-sfn-time="date" datetime="2026-11-07T02:18:04+00:00">November 7, 2026</time><time data-sfn-time="time"
 datetime="2026-11-07T02:18:04+00:00">2:18 a.m.</time></div>
<figure class="art has-photo story-hero" id="story-hero"><img width="678" height="388"
 src="https://spaceflightnow.com/wp-content/uploads/2026/11/hero.jpg" alt="Hero alt text"></figure>
<p class="story-cap">Hero caption text. Image: Example Co.</p>
<div class="story-body" id="story-body">
<p>First paragraph of the fixture article.</p>
<figure class="wp-caption"><img src="https://spaceflightnow.com/wp-content/uploads/2026/11/inline.jpg"
 alt="Inline photo alt" width="678" height="381"><figcaption>Inline caption.</figcaption></figure>
<p>Second and last paragraph.</p>
</div>
<div class="story-tags" id="story-tags"><a href="https://spaceflightnow.com/category/falcon-9/">Falcon 9</a></div>
<a href="https://spaceflightnow.com/news-archive/" class="back-to-archive" id="back-to-archive">Back to News Archive</a>
</article>
<aside class="rail"><section class="rail-set"><h2 class="rail-title">Related Stories</h2>
<ul><li>Other story headline</li></ul></section></aside>
</div></main>
<footer class="site-footer">&copy; 1999-2026 Spaceflight Now Inc</footer>
</body>
</html>"""

MLS_HEADER = ('<div class="mt-8 flex flex-wrap justify-center gap-3 font-mono uppercase"><span>Nov 2, 2026</span>'
              '<span aria-hidden="true">/</span><span>2 min read</span></div>')

# tcs-workflows#273: the header and dateline from 2026-11-12 on, and a release whose only picture is the press tile.
MLS_URL_273 = ('https://www.maritimelaunch.com/news/'
               'Maritime-Launch-Services-Appoints-Sarah-McLean-as-Chief-Operating-Officer')
MLS_HEADER_273 = ('<div class="mt-8 flex flex-wrap justify-center gap-3 font-mono uppercase"><span>Nov 12, 2026</span>'
                  '<span aria-hidden="true">/</span><span>3 min</span></div>')
MLS_TILE_IMG = ('<img alt="Maritime Launch" data-nimg="fill" src="/_next/image?url=%2Fapi%2Fmedia%2Ffile%2F'
                'Default_MLS_Hold_Press.png%3Fprefix%3Duploads&amp;w=1920&amp;q=75">')
MLS_PAGE_273 = (
    '<html><head><meta property="og:image" content="https://www.maritimelaunch.com/api/media/file/'
    'Default_MLS_Hold_Press.png?prefix=uploads"/></head><body><header><img alt="Maritime Launch Services" '
    'src="/api/media/file/maritime-launch-services-logo.svg?prefix=uploads"></header><main><article>'
    '<h1>Maritime Launch Services Appoints Sarah McLean as Chief Operating Officer</h1>' + MLS_HEADER_273 +
    '<div class="relative aspect-[16/9]">' + MLS_TILE_IMG + '</div>'
    '<p><strong>HALIFAX, NOVA SCOTIA, November 12, 2026 – </strong>Maritime Launch Services Inc. is pleased to '
    'announce the appointment.</p></article></main></body></html>')
MLS_MARKDOWN_273 = (
    '# Maritime Launch Services Appoints Sarah McLean as Chief Operating Officer\n'
    'Sarah McLean appointed as Maritime Launch Services Chief Operating Officer (COO)\n'
    'Nov 12, 2026/3 min\n'
    '**HALIFAX, NOVA SCOTIA, November 12, 2026 –** Maritime Launch Services Inc. is pleased to announce the '
    'appointment.\n')

MLS_PAGE_IMAGES = (
    '<header><img alt="Maritime Launch Services" src="/api/media/file/maritime-launch-services-logo.svg?prefix=uploads">'
    '</header><main><article><div class="relative aspect-[16/9]"><img alt="TSX" decoding="async" data-nimg="fill" '
    'src="/_next/image?url=%2Fapi%2Fmedia%2Ffile%2FTSX.jpg%3Fprefix%3Duploads&amp;w=1920&amp;q=75"></div></article>'
    '<section class="bg-mls-neutral-100"><article><img alt="Cover applepod" loading="lazy" '
    'src="/_next/image?url=%2Fapi%2Fmedia%2Ffile%2F800x800bb.jpg%3Fprefix%3Duploads&amp;w=1920&amp;q=75"></article>'
    '</section></main>')

MLS_MARKDOWN = (
    '[![Maritime Launch Services](https://www.maritimelaunch.com/api/media/file/maritime-launch-services-logo.svg'
    '?prefix=uploads)](https://www.maritimelaunch.com/)\n'
    '[News & Media](https://www.maritimelaunch.com/news)\n'
    'Press Release\n'
    '# Maritime Launch Services Receives Final Approval To List On The Toronto Stock Exchange\n'
    '_Common shares to commence trading on TSX on November 3 under the symbol “MAXQ”_\n'
    'Nov 2, 2026/2 min read\n'
    '**HALIFAX, NOVA SCOTIA, November 2, 2026** – Maritime Launch Services Inc. (CBOE: MAXQ, OTCQB: MAXQF)')

NASA_TEASER_LINE = ('[Previous Post](https://www.nasa.gov/blogs/spacestation/2026/11/05/x/)[![](https://www.nasa.gov/'
                    'wp-content/uploads/2026/11/blog-useva101-110526h.jpg?w=200&h=200&crop=1)]'
                    '(https://www.nasa.gov/blogs/spacestation/2026/11/05/x/)')

NASA_BLOG_MARKDOWN = (
    'Last paragraph of the article.\n\n'
    '![](https://secure.gravatar.com/avatar/x?s=300)\n'
    'Mark A. Garcia\n'
    'November 6, 2026 12:48PM\n'
    '### Categories\n'
    '[Expedition 75](https://www.nasa.gov/missions/station/expedition-75/)\n'
    '  * [ ](https://x.com/intent/tweet?via=NASA)\n\n'
    '## More from International Space Station\n'
    + NASA_TEASER_LINE + '\n'
    '### [Spacewalkers Install](https://www.nasa.gov/blogs/spacestation/2026/11/05/x/)\n'
    'November 5, 2026')

PL_SIZES = 'https://planetary.s3.amazonaws.com/web/assets/email/newsletter/_{}_crop_center-center_82_line/cats-head-nebula.jpg'


def _result(url, html='', markdown='', images=None, success=True):
    return types.SimpleNamespace(url=url, html=html, markdown=markdown, success=success,
                                 media={'images': list(images or [])})


def _urls(images):
    return [i['url'] for i in images]


class SpaceflightNowTests(unittest.TestCase):
    def test_config_selects_the_story_and_keeps_the_lead_photo(self):
        site = A.config_for(SFN_URL)
        self.assertEqual(site['selector'], 'article.main')
        kw = A.run_config_for(SFN_URL).kwargs
        self.assertEqual(kw['css_selector'], 'article.main, ' + A.STRICT_SCOPE_HEAD_META)
        self.assertNotIn('target_elements', kw)
        self.assertEqual(kw['excluded_selector'], '#story-byline time, #back-to-archive, #story-tags, #story-cat, .ef-note')
        self.assertFalse(kw['remove_overlay_elements'])
        # Crawl4AI puts each selector inside a double-quoted JS string.
        self.assertNotIn('"', kw['css_selector'] + kw['excluded_selector'])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_selection_gives_the_story_alone(self):
        # Acts out what Crawl4AI does with the config: select article.main, then drop the excluded elements.
        from bs4 import BeautifulSoup
        site = A.config_for(SFN_URL)
        article = BeautifulSoup(SFN_PAGE, 'html.parser').select_one(site['selector'])
        self.assertIsNotNone(article)
        for part in site['exclude'].split(','):
            for el in article.select(part.strip()):
                el.decompose()
        self.assertEqual(article.get_text('\n', strip=True).split('\n'), [
            'Fixture headline', 'Jane Doe', 'Hero caption text. Image: Example Co.',
            'First paragraph of the fixture article.', 'Inline caption.', 'Second and last paragraph.'])
        self.assertIsNotNone(article.select_one('figure#story-hero img'))

    def test_thumbnail_and_date_come_from_the_head_tags(self):
        self.assertEqual(A.extract_og_image(SFN_PAGE), 'https://spaceflightnow.com/wp-content/uploads/2026/11/hero.jpg')
        self.assertEqual(A.extract_date(SFN_PAGE, url=SFN_URL), '2026-11-07T02:18:04+00:00')


class SpectrumTests(unittest.TestCase):
    def test_one_selector_for_the_first_story(self):
        url = 'https://spectrum.ieee.org/canadian-rockets'
        kw = A.run_config_for(url).kwargs
        self.assertEqual(kw['target_elements'], [
            "div[data-source='current_post'] div.posts-wrapper > div.widget.post-partial:first-child div.body-description"])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_later_stories_on_the_page_dont_match(self):
        story = ('<div class="widget post-partial"><div class="widget__body"><div class="body-description">{}</div>'
                 '</div></div>')
        html = ("<div data-source='current_post'><div class='posts-wrapper'>" + story.format('This story.')
                + story.format('Next story.') + '</div></div>'
                + "<div data-source='more_posts'><div class='posts-wrapper'>" + story.format('Another.') + '</div></div>')
        site = A.config_for('https://spectrum.ieee.org/canadian-rockets')
        self.assertEqual(A._selector_match_count(html, site['selector']), 1)

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_image_list_is_the_first_storys_own(self):
        lib = 'https://spectrum.ieee.org/media-library/'
        lead = lib + 'two-slender-space-rockets-against-a-solid-background.jpg?id=67644822&width=1200&height=750'
        own_generic = lib + 'image.png?id=111&width=800'
        story = ('<div class="widget post-partial"><img src="{}"><div class="widget__body"><div class="body-description">'
                 '<p>{}</p>{}</div></div></div>')
        html = ("<div data-source='current_post'><div class='posts-wrapper'>"
                + story.format(lead.replace('&', '&amp;'), 'This story.', '<img src="' + own_generic + '">')
                + story.format(lib + 'image.png?id=222&amp;width=800', 'Next story.', '')
                + "</div></div><aside><img src='" + lib + "hadron-collider.jpg?id=68618939&amp;width=600'></aside>")
        res = _result('https://spectrum.ieee.org/canadian-rockets', html=html, images=[
            {'src': lead, 'alt': 'Two slender space rockets'},
            {'src': own_generic, 'alt': 'A chart in this story'},
            {'src': lib + 'image.png?id=222&width=800', 'alt': 'Another story'},
            {'src': lib + 'hadron-collider.jpg?id=68618939&width=600', 'alt': 'A worker measuring'}])
        self.assertEqual(_urls(A.collect_images(res)), [lead, own_generic])


class MaritimeLaunchTests(unittest.TestCase):
    def test_config(self):
        kw = A.run_config_for(MLS_URL).kwargs
        self.assertEqual(kw['target_elements'], ['main > article'])
        self.assertEqual(kw['excluded_selector'], 'aside')
        self.assertNotIn('css_selector', kw)
        # Next.js "fill" images are position: absolute, and the overlay remover deleted the release photo.
        self.assertFalse(kw['remove_overlay_elements'])

    def test_placeholder_tile_leaves_the_body(self):
        tile = ('![Maritime Launch](https://www.maritimelaunch.com/_next/image?url=%2Fapi%2Fmedia%2Ffile%2F'
                'Default_MLS_Hold_Press.png%3Fprefix%3Duploads&w=1920&q=75)')
        photo = ('![TSX](https://www.maritimelaunch.com/_next/image?url=%2Fapi%2Fmedia%2Ffile%2FTSX.jpg'
                 '%3Fprefix%3Duploads&w=1920&q=75)')
        out = A._site_post_process(MLS_URL, MLS_MARKDOWN.replace('Press Release\n', 'Press Release\n' + tile + '\n')
                                   + '\n' + tile + '\n' + photo + '\nLast line.')
        self.assertNotIn('Default_MLS_Hold_Press', out)
        self.assertIn(photo, out)
        self.assertTrue(out.endswith(photo + '\nLast line.'), out[-200:])

    def test_date_from_the_header(self):
        self.assertEqual(A.extract_date(MLS_HEADER, url=MLS_URL), '2026-11-02')
        self.assertEqual(A.extract_date(MLS_HEADER.replace('Nov 2, 2026', 'Oct 15, 2026'), url=MLS_URL), '2026-10-15')

    def test_date_from_the_dateline_when_the_header_is_missing(self):
        html = '<p><strong>HALIFAX, NOVA SCOTIA, November 2, 2026</strong> – Maritime Launch Services Inc.</p>'
        self.assertEqual(A.extract_date(html, url=MLS_URL), '2026-11-02')

    def test_header_date_is_read_on_this_site_only(self):
        self.assertEqual(A.extract_date(MLS_HEADER, url='https://spacenews.com/some-article/'), '')

    def test_body_starts_at_the_dek(self):
        out = A._site_post_process(MLS_URL, MLS_MARKDOWN)
        self.assertTrue(out.startswith('_Common shares to commence trading on TSX on November 3'), out[:80])
        self.assertNotRegex(out, r'(?m)^# ')
        self.assertNotIn('min read', out)
        self.assertIn('**HALIFAX, NOVA SCOTIA, November 2, 2026**', out)

    def test_images_come_from_the_release_alone(self):
        res = _result(MLS_URL, html=MLS_PAGE_IMAGES, images=[
            {'src': 'https://www.maritimelaunch.com/api/media/file/800x800bb.jpg?prefix=uploads', 'alt': 'Cover'}])
        self.assertEqual(A.collect_images(res), [
            {'url': 'https://www.maritimelaunch.com/api/media/file/TSX.jpg?prefix=uploads', 'alt': 'TSX'}])

    def test_placeholder_tile_is_not_an_image(self):
        html = ('<main><article><img alt="MLS" src="/_next/image?url=%2Fapi%2Fmedia%2Ffile%2FDefault_MLS_Hold_Press.png'
                '%3Fprefix%3Duploads&amp;w=1920&amp;q=75"></article></main>')
        self.assertEqual(A.collect_images(_result(MLS_URL, html=html)), [])
        meta = ('<meta property="og:image" content="https://www.maritimelaunch.com/api/media/file/'
                'Default_MLS_Hold_Press.png?prefix=uploads">')
        self.assertEqual(A.extract_og_image(meta), '')

    def test_next_image_urls_are_unwrapped_on_any_site(self):
        res = _result('https://example.com/news/x', images=[{
            'src': '/_next/image?url=%2Fapi%2Fmedia%2Ffile%2FTSX.jpg%3Fprefix%3Duploads&w=1920&q=75',
            'alt': 'Rocket on the pad', 'width': 1920, 'height': 1080}])
        self.assertEqual(_urls(A.collect_images(res)), ['https://example.com/api/media/file/TSX.jpg?prefix=uploads'])

    def test_date_from_the_header_without_read(self):
        # tcs-workflows#273: "3 min" from 2026-11-12 on; the older "2 min read" still works.
        self.assertEqual(A.extract_date(MLS_HEADER_273, url=MLS_URL_273), '2026-11-12')
        self.assertEqual(A.extract_date(MLS_HEADER, url=MLS_URL), '2026-11-02')

    def test_date_from_a_dateline_whose_dash_is_inside_the_bold(self):
        for dash in ('– ', '—', '-', '&ndash; ', '&#8211;', ''):
            html = ('<p><strong>HALIFAX, NOVA SCOTIA, November 12, 2026 ' + dash + '</strong>Maritime Launch '
                    'Services Inc.</p>')
            self.assertEqual(A.extract_date(html, url=MLS_URL_273), '2026-11-12', repr(dash))

    def test_body_drops_the_header_date_without_read(self):
        out = A._site_post_process(MLS_URL_273, MLS_MARKDOWN_273)
        self.assertTrue(out.startswith('Sarah McLean appointed as'), out[:80])
        self.assertNotIn('Nov 12, 2026/3 min', out)
        self.assertIn('**HALIFAX, NOVA SCOTIA, November 12, 2026 –**', out)

    def test_a_release_with_only_the_press_tile_has_no_images(self):
        self.assertEqual(A.collect_images(_result(MLS_URL_273, html=MLS_PAGE_273)), [])
        self.assertEqual(A.extract_og_image(MLS_PAGE_273), '')


STOKE_URL = 'https://www.stokespace.com/news/stoke-space-expanding-cape-canaveral-launch-complex'
SANITY = 'https://cdn.sanity.io/images/9rxb7bkm/production/'
# Cut down from the release as served on 2026-11-11: the hero in the release's <article>, then "more news" cards.
STOKE_PAGE = ('<main id="maincontent"><div><article class="mb-(--section-gap) flex"><div class="aspect-[2/1]"><img alt="" '
              'data-nimg="fill" style="position:absolute;height:100%;width:100%" src="/_next/image?url=https%3A%2F%2F'
              'cdn.sanity.io%2Fimages%2F9rxb7bkm%2Fproduction%2Fa2c2c3e116624a9fab63208ffa9e799a4d927fdd-2000x1334.jpg'
              '%3Fauto%3Dformat&amp;w=3840&amp;q=75"></div><div class="news-rich-text"><p>Release text.</p></div>'
              '</article><section class="mb-(--section-gap)"><ul class="px-main gap-grid"><li class="group relative">'
              '<img alt="" data-nimg="fill" src="' + SANITY + '20df3d7c86c7316537920f3231f6629c208c95d8-3840x2160.png'
              '?auto=format"></li></ul></section></div></main>')


class StokeTests(unittest.TestCase):
    def test_config(self):
        self.assertEqual(A.config_for(STOKE_URL).get('image_scope'), 'article')
        self.assertFalse(A.run_config_for(STOKE_URL).kwargs['remove_overlay_elements'])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_only_the_releases_own_pictures(self):
        res = _result(STOKE_URL, html=STOKE_PAGE, images=[
            {'src': SANITY + 'a2c2c3e116624a9fab63208ffa9e799a4d927fdd-2000x1334.jpg?auto=format', 'alt': ''},
            {'src': SANITY + '20df3d7c86c7316537920f3231f6629c208c95d8-3840x2160.png?auto=format', 'alt': ''}])
        self.assertEqual(_urls(A.collect_images(res)),
                         [SANITY + 'a2c2c3e116624a9fab63208ffa9e799a4d927fdd-2000x1334.jpg?auto=format'])

    def test_a_page_with_no_article_is_left_alone(self):
        images = [{'url': SANITY + 'a.jpg', 'alt': ''}, {'url': SANITY + 'b.jpg', 'alt': ''}]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(A._images_in_scope(images, '<main><img src="x"></main>', 'article', STOKE_URL), images)
            self.assertEqual(A._images_in_scope(images, '', 'article', STOKE_URL), images)
        if HAVE_BS4:
            self.assertEqual([json.loads(line)['event'] for line in err.getvalue().splitlines()], ['image_scope_unmatched'])

    def test_other_sites_keep_the_whole_list(self):
        res = _result('https://spacenews.com/some-article/', html=STOKE_PAGE, images=[
            {'src': SANITY + '20df3d7c86c7316537920f3231f6629c208c95d8-3840x2160.png', 'alt': 'Card'}])
        self.assertEqual(len(A.collect_images(res)), 1)


class OverlayTests(unittest.TestCase):
    def test_sites_whose_photos_are_absolute_keep_overlays(self):
        # Next.js "fill" images and similar are position: absolute, and Crawl4AI's overlay remover deleted them.
        for url in (MLS_URL, STOKE_URL, 'https://www.reactiondynamics.space/en/news/reaction-dynamics-unveils-borealis'):
            with self.subTest(url=url):
                self.assertFalse(A.run_config_for(url).kwargs['remove_overlay_elements'])

    def test_other_sites_still_remove_overlays(self):
        self.assertTrue(A.run_config_for('https://spacenews.com/some-article/').kwargs['remove_overlay_elements'])


class NasaTests(unittest.TestCase):
    def test_blog_footer_is_cut(self):
        self.assertEqual(A._site_post_process(NASA_BLOG_URL, NASA_BLOG_MARKDOWN), 'Last paragraph of the article.')

    def test_blog_date_is_the_posts_own(self):
        html = ('<div class="single-blog-content"><div class="mobile-credits"><time datetime="2026-11-06T12:48:13-05:00">'
                'November 6, 2026 12:48PM</time></div><footer class="more-from-footer"><time '
                'datetime="2026-11-05T15:52:33-05:00">November 5, 2026</time></footer></div>')
        self.assertEqual(A.extract_date(html, url=NASA_BLOG_URL), '2026-11-06T12:48:13-05:00')

    def test_teaser_thumbnails_are_dropped(self):
        own = {'url': 'https://www.nasa.gov/wp-content/uploads/2026/11/blog-useva101-110526d-1.jpg',
               'alt': 'NASA astronaut rides the Canadarm2'}
        teaser = {'url': 'https://www.nasa.gov/wp-content/uploads/2026/11/blog-useva101-110526h.jpg', 'alt': 'Spacewalk'}
        hero = {'url': 'https://images-assets.nasa.gov/image/iss075e0227271/iss075e0227271~large.jpg'
                       '?w=1920&h=1280&fit=clip&crop=faces%2Cfocalpoint', 'alt': 'The station'}
        self.assertEqual(A._drop_teaser_images(NASA_BLOG_URL, NASA_BLOG_MARKDOWN, [own, teaser, hero]), [own, hero])
        # Other sites, and pages without a teaser, are left alone.
        self.assertEqual(A._drop_teaser_images('https://spacenews.com/x/', NASA_BLOG_MARKDOWN, [own, teaser]),
                         [own, teaser])
        self.assertEqual(A._drop_teaser_images(NASA_BLOG_URL, 'No footer here.', [own, teaser]), [own, teaser])

    def test_release_insignia_is_not_a_picture(self):
        md = ('[![NASA insignia.](https://www.nasa.gov/wp-content/uploads/2022/02/nasa_meatball_large.jpg?w=1920)]'
              '(https://www.nasa.gov/wp-content/uploads/2022/02/nasa_meatball_large.jpg)\n'
              'Credit: NASA\n'
              'NASA selected three companies on Wednesday.')
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'NASA selected three companies on Wednesday.')
        res = _result(NASA_RELEASE_URL, images=[{
            'src': 'https://www.nasa.gov/wp-content/uploads/2022/02/nasa_meatball_large.jpg?w=1920', 'alt': 'NASA insignia.'}])
        self.assertEqual(A.collect_images(res), [])
        meta = ('<meta property="og:image" content="https://www.nasa.gov/wp-content/uploads/2022/02/'
                'nasa_meatball_large.jpg?w=1200">')
        self.assertEqual(A.extract_og_image(meta), '')

    def test_release_ends_at_the_end_marker(self):
        md = ('Facilitated through the agency...\n\n-end-\nDana Mercer / Lee Ortega\nHeadquarters, Washington\n'
              '202-555-0110\ndana.mercer@agency.gov')
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'Facilitated through the agency...')
        md = 'Body sentence.\n-end-\n**Media contact**\nSam Rivera\n818-555-0123'
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'Body sentence.')

    def test_media_contact_heading_in_any_case(self):
        md = 'Body sentence.\n\n**Media contact**\nSam Rivera'
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'Body sentence.')

    def test_newsletter_lines_go(self):
        md = ('Para one.\n\n_Learn more about station activities by following the space station blog._\n\nPara two.\n\n'
              '_Get the latest from NASA delivered every week. [Subscribe here](https://www.nasa.gov/newsletters/)._')
        self.assertEqual(A._site_post_process(NASA_BLOG_URL, md), 'Para one.\n\nPara two.')

    def test_a_contact_block_with_no_heading_is_cut(self):
        # nasa-04: the MMX release on science.nasa.gov, as saved on 2026-11-03, after its social links are cut.
        url = 'https://science.nasa.gov/directorates/smd/planetary-science-division/nasa-to-help-unlock-mysteries/'
        kept = ('For more information about the MMX mission, visit: \n'
                '[**https://science.nasa.gov/mission/mmx**](https://science.nasa.gov/mission/mmx)')
        md = (kept + '\nRobin Hale / Casey Lin  \nHeadquarters, Washington  \n240-555-0131 / 202-555-0142  \n'
              'robin.hale@agency.gov / casey.lin@agency.gov\nFacebook logo\n[@NASA](https://facebook.com/NASA)')
        self.assertEqual(A._site_post_process(url, md), kept)

    def test_two_contact_blocks_go(self):
        md = ('Last sentence of the release.\n'
              'Jordan Pike / Morgan Tate  \nHeadquarters, Washington  \n202-555-0110  \n'
              'jordan.pike@agency.gov / morgan.tate@agency.gov\n'
              'Avery Stone  \nJohnson Space Center, Houston  \n281-555-0157  \navery.stone@agency.gov')
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'Last sentence of the release.')

    def test_a_spanish_release_ends_at_fin(self):
        md = 'Última frase.\n\n-fin-\nRobin Hale\nSede central de la NASA, Washington\n240-555-0131'
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), 'Última frase.')

    def test_article_text_with_a_number_or_line_breaks_stays(self):
        for md in ('The pattern was detailed in a [2017 study](https://doi.org/10.1038/s41558-017-0014-8).\n'
                   'Morton expects the fires to continue.',
                   'For details, call the newsroom at 202-555-0110.',
                   'Para one.\nA line with a hard break  \nand its last line.'):
            with self.subTest(md=md[:30]):
                self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), md)

    def test_other_sites_keep_a_trailing_contact_block(self):
        md = 'Last.\nJane Doe  \nAcme Corp  \n555-123-4567  \njane.doe@agency.gov'
        self.assertEqual(A._site_post_process('https://spacenews.com/some-article/', md), md)

    def test_a_line_that_merely_contains_end_stays(self):
        md = 'The burn will end-to-end test the engine.\n\nNext paragraph.'
        self.assertEqual(A._site_post_process(NASA_RELEASE_URL, md), md)


class PlanetaryTests(unittest.TestCase):
    def test_article_footer_is_cut(self):
        md = ('Stack Morgan said the rover will attempt two more samples.\n\n### You Power the Next Great Discovery!\n'
              'Fund future scientific research today.\n[Donate](https://connect.planetary.org/73?s_cid=step2026)\n'
              'Read more: [ Worlds](https://www.planetary.org/articles?keywords=space-places)\n'
              'You are here: [Home](https://www.planetary.org/) > [Articles](https://www.planetary.org/articles/)\n'
              '### [Asa Stahl](https://www.planetary.org/profiles/asa-stahl)\n### Related Articles\n'
              '### [A guide to sidewalk astronomy](https://www.planetary.org/articles/a-guide-to-sidewalk-astronomy)\n'
              'Asa Stahl')
        self.assertEqual(A._site_post_process(PLANETARY_URL, md),
                         'Stack Morgan said the rover will attempt two more samples.')

    def test_downlink_footer_is_cut(self):
        md = ('We love to feature space artwork in the Downlink.\n'
              '__Read more:[ The Downlink](https://www.planetary.org/articles?keywords=the-downlink)\n'
              '### More Downlink\n### [A drive to remember](https://www.planetary.org/the-downlink/a-drive-to-remember)')
        self.assertEqual(A._site_post_process(DOWNLINK_URL, md), 'We love to feature space artwork in the Downlink.')

    def test_membership_appeal_and_chrome_go(self):
        md = ('Intro.\n\n## Help save space missions.\nJoin today.\n[Join](https://www.planetary.org/join)\n\n'
              '## Next section\n![](https://planetary.s3.amazonaws.com/sites/planetary/images/explore_worlds.svg)\n'
              'Text ![](https://www.planetary.org/x/bullet-illustration.png) more [ ](https://www.planetary.org/y).')
        self.assertEqual(A._site_post_process(DOWNLINK_URL, md), 'Intro.\n\n## Next section\nText more .')

    def test_a_picture_with_no_alt_text_stays(self):
        md = ('Before.\n![](' + PL_SIZES.format('1200x699') + ')\nAfter.\n'
              '[![](https://planetary.s3.amazonaws.com/sites/planetary/images/find_life.svg)](https://www.planetary.org/)')
        self.assertEqual(A._site_post_process(PLANETARY_URL, md),
                         'Before.\n![](' + PL_SIZES.format('1200x699') + ')\nAfter.\n')

    def test_srcset_gives_the_widest_picture(self):
        srcset = ', '.join(PL_SIZES.format(s) + ' ' + w for s, w in
                           (('576x335', '576w'), ('768x447', '768w'), ('1200x699', '1200w')))
        res = _result(PLANETARY_URL, images=[{'src': srcset, 'alt': 'Cats Head Nebula'}])
        self.assertEqual(_urls(A.collect_images(res)), [PL_SIZES.format('1200x699')])

    def test_size_folders_are_one_picture(self):
        res = _result(PLANETARY_URL, images=[
            {'src': PL_SIZES.format('576x335') + '.webp', 'alt': 'Cats Head Nebula'},
            {'src': PL_SIZES.format('1200x699'), 'alt': ''}])
        self.assertEqual(A.collect_images(res), [{'url': PL_SIZES.format('1200x699'), 'alt': 'Cats Head Nebula'}])

    def test_srcset_parsing(self):
        self.assertEqual(A._widest_srcset_url('a.jpg 200w, b.jpg 1200w, c.jpg 800w'), 'b.jpg')
        self.assertEqual(A._widest_srcset_url('a.jpg 1x, b.jpg 2x'), 'b.jpg')
        # A comma inside a URL is not a separator; only the comma after a descriptor is.
        self.assertEqual(A._widest_srcset_url('https://e.org/w_400,h_300/p.jpg 400w, https://e.org/w_800,h_600/p.jpg 800w'),
                         'https://e.org/w_800,h_600/p.jpg')
        self.assertEqual(A._widest_srcset_url('data:image/gif;base64,R0lGODlh 4000w'), '')

    def test_a_url_with_a_space_is_not_a_srcset(self):
        res = _result('https://example.com/x', images=[
            {'src': 'https://example.com/photo of rocket.jpg', 'alt': 'Rocket', 'width': 800, 'height': 600}])
        self.assertEqual(_urls(A.collect_images(res)), ['https://example.com/photo of rocket.jpg'])

    def test_chrome_and_headshots_leave_the_list(self):
        keep = {'url': PL_SIZES.format('1200x699'), 'alt': 'Cats Head Nebula'}
        images = [
            {'url': 'https://planetary.s3.amazonaws.com/sites/planetary/images/find_life.svg', 'alt': ''},
            {'url': 'https://planetary.s3.amazonaws.com/web/assets/pictures/downlink-illustration.png', 'alt': ''},
            {'url': 'https://planetary.s3.amazonaws.com/web/assets/pictures/asa-stahl-portrait.jpg', 'alt': 'Asa Stahl'},
            keep]
        self.assertEqual(A._filter_cruft_images(PLANETARY_URL, images), [keep])


class DataUriTests(unittest.TestCase):
    def test_placeholders_never_reach_the_image_list(self):
        real = PL_SIZES.format('1200x699')
        res = _result(PLANETARY_URL, images=[
            {'src': 'data:image/svg+xml;base64,PHN2Zz4=', 'alt': 'Lazy'},
            {'src': 'data:image/gif;base64,R0lGODlh 1x, ' + real + ' 2x', 'alt': 'Cats Head Nebula'},
            {'src': real + ' 1200w, data:image/svg+xml;base64,PHN2Zz4= 2000w', 'alt': 'Placeholder widest'}])
        urls = _urls(A.collect_images(res))
        self.assertEqual(urls, [real])
        self.assertFalse([u for u in urls if u.startswith('data:')])

    def test_lazy_placeholders_leave_the_body_on_every_site(self):
        md = ('A ![lazy](data:image/svg+xml;base64,PHN2Zz4=) B\n'
              '[![x](data:image/gif;base64,R0lGODlh)](https://example.org/photo) C\n'
              '![real](https://example.org/real.jpg)')
        out = A._site_post_process('https://spacenews.com/some-article/', md)
        self.assertNotIn('data:', out)
        self.assertNotIn('[](', out)
        self.assertIn('![real](https://example.org/real.jpg)', out)
        self.assertIn('A ', out)
        self.assertIn(' C', out)


class SelectorCountTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_counts(self):
        html = '<main><div class="entry-content">a</div><div class="entry-content">b</div></main>'
        self.assertEqual(A._selector_match_count(html, 'div.entry-content'), 2)
        self.assertEqual(A._selector_match_count(html, 'main, div.section_main'), 1)
        self.assertEqual(A._selector_match_count(html, 'article.BlogItem'), 0)

    def test_cant_check(self):
        self.assertIsNone(A._selector_match_count('', 'main'))
        self.assertIsNone(A._selector_match_count('<main></main>', ''))

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_a_selector_bs4_cant_read_is_no_flag(self):
        self.assertIsNone(A._selector_match_count('<main></main>', 'main >>> ::nonsense('))


class ScrapeOneTests(unittest.TestCase):
    """scrape_one with the crawler replaced: each call to _fetch_with_retry hands back the next result."""

    def _scrape(self, url, *results, title='T'):
        queue = list(results)
        calls = []

        async def fake_fetch(crawler, u, cfg):
            calls.append(cfg.kwargs)
            return queue.pop(0), None, 1

        saved = A._fetch_with_retry
        A._fetch_with_retry = fake_fetch
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                item = asyncio.run(A.scrape_one(None, {'url': url, 'title': title}))
        finally:
            A._fetch_with_retry = saved
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        return item, events, calls

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_article_scope_when_the_selector_matches(self):
        html = '<html><body><main><article><p>Release text.</p></article></main></body></html>'
        item, events, _ = self._scrape(MLS_URL, _result(MLS_URL, html=html, markdown='Release text.'))
        self.assertEqual(item['scope'], 'article')
        self.assertNotIn('scope_reason', item)
        self.assertFalse([e for e in events if e['event'] == 'scrape_whole_page'])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_whole_page_when_the_selector_matches_nothing(self):
        html = '<html><body><div class="old-drupal"><p>Whole page.</p></div></body></html>'
        item, events, _ = self._scrape(MLS_URL, _result(MLS_URL, html=html, markdown='Header. Whole page. Footer.'))
        self.assertEqual((item['scope'], item['scope_reason']), ('whole_page', 'selector_matched_nothing'))
        self.assertTrue(item['has_content'])
        self.assertEqual([e['reason'] for e in events if e['event'] == 'scrape_whole_page'], ['selector_matched_nothing'])

    def test_whole_page_after_the_unscoped_retry(self):
        html = '<html><body><p>Page.</p></body></html>'
        item, events, calls = self._scrape(
            SFN_URL, _result(SFN_URL, html=html, markdown=''), _result(SFN_URL, html=html, markdown='Whole page text.'))
        self.assertEqual(len(calls), 2)
        self.assertNotIn('css_selector', calls[1])
        self.assertEqual((item['scope'], item['scope_reason']), ('whole_page', 'unscoped_retry'))
        self.assertEqual([e['event'] for e in events], ['scrape_recovered', 'scrape_whole_page'])

    def test_strict_site_is_article_scope(self):
        item, events, _ = self._scrape(SFN_URL, _result(SFN_URL, html=SFN_PAGE, markdown='# Fixture headline\n\nText.'))
        self.assertEqual(item['scope'], 'article')
        self.assertEqual(item['date'], '2026-11-07T02:18:04+00:00')
        self.assertEqual(item['thumbnail'], 'https://spaceflightnow.com/wp-content/uploads/2026/11/hero.jpg')

    def test_nasa_teaser_filter_reads_the_footer_before_it_is_cut(self):
        res = _result(NASA_BLOG_URL, html='<time datetime="2026-11-06T12:48:13-05:00">x</time>',
                      markdown=NASA_BLOG_MARKDOWN, images=[
                          {'src': 'https://www.nasa.gov/wp-content/uploads/2026/11/blog-useva101-110526d-1.jpg',
                           'alt': 'NASA astronaut rides the Canadarm2', 'width': 1920, 'height': 1280},
                          {'src': 'https://www.nasa.gov/wp-content/uploads/2026/11/blog-useva101-110526h.jpg'
                                  '?w=200&h=200&crop=1', 'alt': 'Spacewalk', 'width': 200, 'height': 200}])
        item, _, _ = self._scrape(NASA_BLOG_URL, res)
        self.assertEqual(item['content'], 'Last paragraph of the article.')
        self.assertEqual(_urls(item['images']),
                         ['https://www.nasa.gov/wp-content/uploads/2026/11/blog-useva101-110526d-1.jpg'])
        self.assertEqual(item['scope'], 'article')


REL_URL = 'https://www.relativityspace.com/press-release/2026/10/6/september-2026-company-update'
SFN_OG = '<meta property="og:title" content="Falcon 9 launches from California on classified mission for U.S. Space Force" />\n'


class SfnTitleTests(unittest.TestCase):
    """F6, SFN_PAGE_TITLE_V1: Spaceflight Now's page headline over the feed's, which keeps the first one."""

    _scrape = ScrapeOneTests._scrape

    def test_the_pages_current_headline_wins(self):
        page = SFN_PAGE.replace('<head>\n', '<head>\n' + SFN_OG)
        item, _, _ = self._scrape(SFN_URL, _result(SFN_URL, html=page, markdown='# Fixture headline\n\nText.'))
        self.assertEqual(item['title'], 'Falcon 9 launches from California on classified mission for U.S. Space Force')

    def test_without_og_title_the_h1(self):
        item, _, _ = self._scrape(SFN_URL, _result(SFN_URL, html=SFN_PAGE, markdown='# Fixture headline\n\nText.'))
        self.assertEqual(item['title'], 'Fixture headline')

    def test_entities_and_an_apostrophe_inside_the_value(self):
        self.assertEqual(A._sfn_page_title('<meta property="og:title" content="NASA&#8217;s Swift reenters" />'),
                         'NASA’s Swift reenters')
        self.assertEqual(A._sfn_page_title('<meta property="og:title" content="NASA\'s Swift &amp; more" />'),
                         "NASA's Swift & more")
        self.assertEqual(A._sfn_page_title('<h1 id="story-title">Ax-5 <em>crew</em> &#8216;go&#8217;</h1>'),
                         'Ax-5 crew ‘go’')
        self.assertEqual(A._sfn_page_title('<p>No headline.</p>'), '')

    def test_other_sites_keep_the_feed_title(self):
        html = ('<html><head><meta property="og:title" content="Page headline" /></head><body><main><article>'
                '<p>Release text.</p></article></main></body></html>')
        item, _, _ = self._scrape(MLS_URL, _result(MLS_URL, html=html, markdown='Release text.'))
        self.assertEqual(item['title'], 'T')


MLS_HEADLINE_273 = 'Maritime Launch Services Appoints Sarah McLean as Chief Operating Officer'
MLS_OG_286 = '<meta property="og:title" content="' + MLS_HEADLINE_273 + ' | Maritime Launch Services"/>'


class MaritimeLaunchTitleTests(unittest.TestCase):
    """MLS_TITLE_SUFFIX_STRIP (tcs-workflows#286): with no title given, the page's headline without the site name."""

    _scrape = ScrapeOneTests._scrape

    def test_no_title_given_the_og_title_without_the_site_name(self):
        page = MLS_PAGE_273.replace('<head>', '<head>' + MLS_OG_286)
        item, _, _ = self._scrape(MLS_URL_273, _result(MLS_URL_273, html=page, markdown=MLS_MARKDOWN_273), title='')
        self.assertEqual(item['title'], MLS_HEADLINE_273)

    def test_no_og_title_the_h1(self):
        item, _, _ = self._scrape(MLS_URL_273, _result(MLS_URL_273, html=MLS_PAGE_273, markdown=MLS_MARKDOWN_273),
                                  title='')
        self.assertEqual(item['title'], MLS_HEADLINE_273)

    def test_a_given_title_is_kept(self):
        page = MLS_PAGE_273.replace('<head>', '<head>' + MLS_OG_286)
        item, _, _ = self._scrape(MLS_URL_273, _result(MLS_URL_273, html=page, markdown=MLS_MARKDOWN_273))
        self.assertEqual(item['title'], 'T')

    def test_other_sites_keep_the_suffix(self):
        url = 'https://spaceq.ca/maritime-launch-names-a-coo/'
        html = '<html><head>' + MLS_OG_286 + '</head><body><main><article><p>Text.</p></article></main></body></html>'
        item, _, _ = self._scrape(url, _result(url, html=html, markdown='Text.'), title='')
        self.assertEqual(item['title'], MLS_HEADLINE_273 + ' | Maritime Launch Services')


@unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
class ArticlePicturesTests(unittest.TestCase):
    """ARTICLE_PICTURES_V1: the pictures inside the article, for the Healthcheck's no-images warning."""

    _scrape = ScrapeOneTests._scrape

    def _page(self, article):
        return ('<html><body><header><img src="https://images.squarespace-cdn.com/x/Relativity_White_RGB_900px.png" '
                'alt="Relativity Space"></header><article class="BlogItem">' + article + '</article>'
                '<footer><img src="https://www.relativityspace.com/s/Relativity_Logo-02.svg"></footer></body></html>')

    def test_an_article_with_only_a_video_has_none(self):
        html = self._page('<h1>September 2026 Company Update</h1><p>Text.</p>'
                          '<iframe src="https://www.youtube.com/embed/abc"></iframe>')
        item, _, _ = self._scrape(REL_URL, _result(REL_URL, html=html, markdown='Text. ' * 60, images=[
            {'src': 'https://images.squarespace-cdn.com/x/Relativity_White_RGB_900px.png', 'alt': 'Relativity Space'}]))
        self.assertEqual(item['images'], [])
        self.assertEqual(item['article_pictures'], 0)

    def test_pictures_inside_the_article_are_counted_once(self):
        html = self._page('<p>Text.</p><img src="https://images.squarespace-cdn.com/a.jpg">'
                          '<picture><source srcset="https://images.squarespace-cdn.com/b.webp"><img src="https://images.squarespace-cdn.com/b.jpg"></picture>'
                          '<picture><source srcset="https://images.squarespace-cdn.com/c.webp"></picture>')
        item, _, _ = self._scrape(REL_URL, _result(REL_URL, html=html, markdown='Text. ' * 60))
        self.assertEqual(item['article_pictures'], 3)

    def test_unknown_when_the_selector_matches_nothing(self):
        self.assertIsNone(A._article_picture_count('<main><img src="x.jpg"></main>', 'article.BlogItem'))
        self.assertIsNone(A._article_picture_count('<article class="BlogItem"></article>', ''))
        self.assertIsNone(A._article_picture_count('', 'article.BlogItem'))

    def test_a_whole_page_scrape_has_no_count(self):
        html = '<html><body><div class="old-drupal"><p>Whole page.</p></div></body></html>'
        item, _, _ = self._scrape(MLS_URL, _result(MLS_URL, html=html, markdown='Header. Whole page. Footer.'))
        self.assertEqual(item['scope'], 'whole_page')
        self.assertNotIn('article_pictures', item)

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_stand_in_pictures_are_not_counted(self):
        # tcs-workflows#273: Maritime Launch's press tile and the NASA insignia aren't the article's own pictures.
        self.assertEqual(A._article_picture_count(MLS_PAGE_273, 'main > article'), 0)
        photo = ('<img alt="TSX" src="/_next/image?url=%2Fapi%2Fmedia%2Ffile%2FTSX.jpg%3Fprefix%3Duploads'
                 '&amp;w=1920&amp;q=75">')
        self.assertEqual(A._article_picture_count(MLS_PAGE_273.replace(MLS_TILE_IMG, MLS_TILE_IMG + photo),
                                                  'main > article'), 1)
        nasa = ('<div class="entry-content"><img alt="NASA insignia." src="https://www.nasa.gov/wp-content/uploads/'
                '2022/02/nasa_meatball_large.jpg?w=1920"><p>Text.</p></div>')
        self.assertEqual(A._article_picture_count(nasa, 'div.entry-content'), 0)
        lazy = ('<div class="entry-content"><img src="data:image/gif;base64,R0lGOD" '
                'data-src="https://www.nasa.gov/wp-content/uploads/2026/11/a.jpg"></div>')
        self.assertEqual(A._article_picture_count(lazy, 'div.entry-content'), 1)

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_a_maritime_release_with_only_the_tile_scrapes_clean(self):
        # tcs-workflows#273, end to end: the date from the new header, no images, and nothing for the Healthcheck.
        item, _, _ = self._scrape(MLS_URL_273, _result(MLS_URL_273, html=MLS_PAGE_273,
                                                       markdown=MLS_MARKDOWN_273 + 'The release goes on. ' * 40))
        self.assertEqual(item['scope'], 'article')
        self.assertEqual(item['date'], '2026-11-12')
        self.assertEqual(item['images'], [])
        self.assertEqual(item['thumbnail'], '')
        self.assertEqual(item['article_pictures'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
