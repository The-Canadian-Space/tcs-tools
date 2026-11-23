"""Tests for the article dates and ESA alt text restored in tcs-workflows#176.

Run:
    python working-files/python-scripts/test_article_scraper_dates.py

Crawl4AI and lib_index (which lives only on the VPS) are stubbed, so this runs anywhere. The HTML below is
the markup each site served on 2026-10-14, measured on the VPS with the scraper's own crawl settings:

- ESA has no date meta, JSON-LD or <time>. The article's own date is the first <span> of
  <div class="meta article__item"> under the <h1>; the related-article cards use a plain <div class="meta">.
- NASA news releases have only Parse.ly's parsely-pub-date meta. NASA blogs also have a <time datetime>.
- Axiom Space shows the date in <div class="press-release-date"> above the <h1>.

Added for tcs-workflows#232, from the pages as served on 2026-11-05:

- ESA Multimedia pages show the date as text beside an sr-only "publication date" label, after an empty copy.
- SpacePolicyOnline's byline and meta description say "Posted: November 1, 2026 5:31 pm ET".
- Reaction Dynamics uses <time> with no datetime attribute: "20 OCTOBER, 2026".

Added for tcs-workflows#234, from the pages as served on 2026-11-05:

- ESA's main site has one date bar on every page, in the same markup #232 found on the media pages.
- Crawl4AI applies a css_selector twice: on the page, one comma-separated part at a time, then on the joined result.
  excluded_selector only runs on the second pass. Unit tests can't see any of that, so real pages have to be checked too.
- Each ESA picture arrives as several variants on media.esa.int, sharing an asset number.
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

ESA_URL = 'https://www.esa.int/Space_Safety/Planetary_Defence/First_switch_on_for_ESA-JAXA_Ramses_asteroid_mission'
NASA_RELEASE_URL = 'https://www.nasa.gov/news-release/nasa-names-astronaut-christina-koch-acting-deputy-chief-engineer/'
NASA_BLOG_URL = 'https://www.nasa.gov/blogs/spacestation/2026/10/13/dragon-launches-new-cargo/'
AXIOM_URL = 'https://www.axiomspace.com/release/axiom-space-kepler-ready-orbital-computing-for-quantum-era'

# What Crawl4AI 0.9.4 returns for ESA's css_selector: each match's outerHTML, in one div.
ESA_ARTICLE_META = ('<div class="meta article__item"> <span>23/09/2026</span> <span><span id="viewcount">2652</span>'
                    '<small> views</small></span> <span><span id="ezsr_total_27537267">40</span><small> likes</small>'
                    '</span> </div>')
ESA_SIDEBAR_CARD = ('<header class="entry"> <span class="pillar">Science &amp; Exploration</span> <h3 class="heading">'
                    'Welcome home, Sophie Adenot</h3> <div class="meta"> <span>12/10/2026</span> <span>681 '
                    '<small>views</small></span> </div></header>')
ESA_SCOPED = ("<div class='crawl4ai-result'>"
              '<div class="abstract article__block article__item"><p>The Ramses spacecraft has been turned on.</p></div>'
              + ESA_ARTICLE_META + '</div>')


class EsaDateTests(unittest.TestCase):
    def test_own_date_from_the_scoped_html(self):
        self.assertEqual(A.extract_date(ESA_SCOPED, url=ESA_URL), '2026-09-23')

    def test_a_related_card_earlier_on_the_page_is_not_the_date(self):
        full = '<html><body>' + ESA_SIDEBAR_CARD * 3 + '<h1 class="heading heading--main article__item">Ramses</h1> ' \
               + ESA_ARTICLE_META + '</body></html>'
        self.assertEqual(A.extract_date(full, url=ESA_URL), '2026-09-23')

    def test_only_related_cards_gives_no_date(self):
        self.assertEqual(A.extract_date('<html><body>' + ESA_SIDEBAR_CARD + '</body></html>', url=ESA_URL), '')

    def test_esa_selects_its_date_line(self):
        sel = [s.strip() for s in A.ESA_SISTER_CONFIG['selector'].split(',')]
        self.assertIn('div.meta.article__item', sel)
        self.assertNotIn('div.meta', sel)  # the bare class would pull in every related card

    def test_date_line_text_is_dropped_from_the_content(self):
        content = 'The Ramses spacecraft has been turned on.\n\n23/09/2026 2652 views 40 likes'
        self.assertEqual(A._site_post_process(ESA_URL, content), 'The Ramses spacecraft has been turned on.')

    def test_a_date_inside_a_sentence_stays(self):
        content = 'Ramses was switched on.\n\nThe review on 23/09/2026 cleared it for testing.'
        self.assertEqual(A._site_post_process(ESA_URL, content), content)

    def test_other_sites_keep_a_bare_date_line(self):
        content = 'Body.\n\n23/09/2026'
        self.assertEqual(A._site_post_process('https://spacenews.com/x/', content), content)


class NasaDateTests(unittest.TestCase):
    PARSELY = '<meta name="parsely-pub-date" content="2026-10-13T20:41:11Z">'

    def test_parsely_is_one_of_the_selected_head_tags(self):
        self.assertIn("meta[name='parsely-pub-date']", A.STRICT_SCOPE_HEAD_META)
        self.assertIn("meta[name='parsely-pub-date']", A.run_config_for(NASA_RELEASE_URL).kwargs['css_selector'])

    def test_news_release_gets_the_parsely_date(self):
        html = "<div class='crawl4ai-result'><div class=\"entry-content\"><p>Body.</p></div>" + self.PARSELY + '</div>'
        self.assertEqual(A.extract_date(html, url=NASA_RELEASE_URL), '2026-10-13T20:41:11Z')

    def test_a_time_tag_still_wins_so_dated_pages_are_unchanged(self):
        html = ("<div class='crawl4ai-result'><div class=\"entry-content\"><time datetime=\"2026-10-13T11:45:12-04:00\">"
                'Oct 13</time></div>' + self.PARSELY.replace('20:41:11Z', '15:45:12Z') + '</div>')
        self.assertEqual(A.extract_date(html, url=NASA_BLOG_URL), '2026-10-13T11:45:12-04:00')


class AxiomDateTests(unittest.TestCase):
    def test_press_release_date(self):
        html = ('<div class="post-title-div"><div class="press-release-date">October 6, 2026</div>'
                '<h1 class="post-h1">Axiom Space, Kepler Ready Orbital Computing for Quantum Era</h1></div>')
        self.assertEqual(A.extract_date(html, url=AXIOM_URL), 'October 6, 2026')

    def test_that_div_without_a_date_gives_nothing(self):
        html = '<div class="press-release-date">Press release</div><h1>Title</h1>'
        self.assertEqual(A.extract_date(html, url=AXIOM_URL), '')


class MoreSiteDateTests(unittest.TestCase):
    ESA_MEDIA_URL = 'https://www.esa.int/ESA_Multimedia/Images/2026/10/Swarming_robots_to_tackle_space_debris'
    SPO_URL = 'https://spacepolicyonline.com/news/whats-happening-in-space-policy-november-1-8-2026/'
    RD_URL = 'https://www.reactiondynamics.space/en/news/reaction-dynamics-unveils-borealis'
    ESA_EMPTY = ('<span class="favorites-views__item" style="display: none;">\n'
                 '  <span class="favorites-views__count"></span>\n'
                 '  <span class="sr-only">publication date</span>\n</span>')
    ESA_FILLED = ('<span class="favorites-views__item">\n'
                  '  <span class="favorites-views__count">15 OCT. 2026</span>\n'
                  '  <span class="sr-only">publication date</span>\n</span>')

    def test_esa_multimedia_date_skips_the_empty_copy(self):
        self.assertEqual(A.extract_date(self.ESA_EMPTY + self.ESA_FILLED, url=self.ESA_MEDIA_URL), '2026-10-15')

    def test_esa_multimedia_with_only_the_empty_copy_gives_nothing(self):
        self.assertEqual(A.extract_date(self.ESA_EMPTY, url=self.ESA_MEDIA_URL), '')

    def test_the_date_bar_works_on_every_esa_page(self):
        for url in (ESA_URL, 'https://www.esa.int/articles/news-media/news/x', 'https://new.esa.int/articles/news-media/news/x',
                    'https://www.esa.int/media/about-us/x'):
            self.assertEqual(A.extract_date(self.ESA_FILLED, url=url), '2026-10-15')

    def test_the_date_bar_only_counts_on_esas_main_site(self):
        for url in ('https://example.com/a', 'https://sci.esa.int/s/x'):
            self.assertEqual(A.extract_date(self.ESA_FILLED, url=url), '')

    def test_the_date_bar_beats_a_related_cards_date(self):
        html = '<time datetime="2026-11-04">04 NOV. 2026</time>' + self.ESA_FILLED
        self.assertEqual(A.extract_date(html, url='https://new.esa.int/articles/x'), '2026-10-15')

    def test_spacepolicyonline_takes_the_posted_date_not_the_update(self):
        html = ('<span class="entry-date">By Marcia Smith | Posted: November 1, 2026 5:31 pm ET | '
                'Last Updated: Nov 02, 2026 1:26 pm ET</span>')
        self.assertEqual(A.extract_date(html, url=self.SPO_URL), 'November 1, 2026')

    def test_posted_only_counts_on_spacepolicyonline(self):
        html = '<p>Posted: November 1, 2026 in the forum.</p>'
        self.assertEqual(A.extract_date(html, url='https://spacenews.com/x/'), '')

    def test_reaction_dynamics_takes_the_first_time_tag(self):
        html = ('<time class="mb-8 block">20 OCTOBER, 2026</time><h1>Borealis</h1>'
                '<a href="/en/news/x"><time class="font-space">8 OCTOBER, 2026</time></a>')
        self.assertEqual(A.extract_date(html, url=self.RD_URL), '2026-10-20')

    def test_a_time_tag_with_a_datetime_still_wins(self):
        html = '<time datetime="2026-10-20">20 OCTOBER, 2026</time>'
        self.assertEqual(A.extract_date(html, url=self.RD_URL), '2026-10-20')

    def test_a_time_tag_with_no_real_month_gives_nothing(self):
        self.assertEqual(A.extract_date('<time>20 SOMETHING, 2026</time>', url=self.RD_URL), '')


class EsaNewSiteTests(unittest.TestCase):
    NEW_URL = 'https://new.esa.int/articles/news-media/news/x'
    SISTERS = ('https://sci.esa.int/s/x', 'https://blogs.esa.int/rocketscience/x', 'https://earth.esa.int/eogateway/x',
               'https://technology.esa.int/lab/x', 'https://kelvins.esa.int/x', 'https://hera.esa.int/x')

    class Result:
        def __init__(self, *srcs, alt='Alba Mons'):
            self.url = 'https://new.esa.int/articles/x'
            self.media = {'images': [{'src': s, 'alt': alt} for s in srcs]}

    def test_every_main_site_host_and_old_url_shape_gets_the_same_rules(self):
        want = A.config_for(ESA_URL)
        for url in (self.NEW_URL, 'https://esa.int/x', 'https://www.esa.int/media/about-us/x',
                    'https://new.esa.int/missions/about-us/x', 'https://www.esa.int/ESA_Multimedia/Images/2026/10/Swarming_robots',
                    'https://www.esa.int/ESA_Multimedia/Videos/2026/10/Finding_Our_Way'):
            self.assertIs(A.config_for(url), want)

    def test_the_body_is_all_of_main_in_one_part(self):
        cfg = A.config_for(ESA_URL)
        self.assertEqual(cfg['selector'], 'main')
        self.assertTrue(cfg['strict_scope'])
        self.assertTrue(A.run_config_for(ESA_URL).kwargs['css_selector'].startswith('main, '))

    def test_the_chrome_goes_through_excluded_selector(self):
        got = A.run_config_for(ESA_URL).kwargs['excluded_selector']
        self.assertEqual(got, A.config_for(ESA_URL)['exclude'])
        parts = [s.strip() for s in got.split(',')]
        for chrome in ('h1', '.favorites-views__wrapper', '.grid-layout', '.articles-modules__share'):
            self.assertIn(chrome, parts)

    def test_no_article_module_is_excluded(self):
        exclude = A.config_for(ESA_URL)['exclude']
        for module in ('articles-modules__text', 'full-text', 'highlight-text-image', 'articles-modules__full-image'):
            self.assertNotIn(module, exclude)

    def test_sister_sites_keep_the_rules_they_had(self):
        for url in self.SISTERS:
            self.assertEqual(A.config_for(url), {'source': 'ESA', 'keep_overlays': True, 'strict_scope': True,
                                                 'selector': 'div.article__block, div.abstract, div.meta.article__item'})
            self.assertNotIn('excluded_selector', A.run_config_for(url).kwargs)

    def test_main_site_rules_leave_sister_pages_alone(self):
        content = 'Caption.\n__\n![fallback image 5](https://media.esa.int/a.webp)\n  * [ tag ](https://sci.esa.int/search?k=1)'
        self.assertEqual(A._site_post_process(self.SISTERS[0], content), content)

    def test_video_icon_lines_are_dropped(self):
        self.assertEqual(A._site_post_process(self.NEW_URL, 'Caption.\n__\n__ __\n__ __\nGas on Mars'), 'Caption.\nGas on Mars')

    def test_placeholder_pictures_are_dropped_from_the_text(self):
        content = 'Before.\n![fallback image 5](https://media.esa.int/global/missing-4.a1394e2f.webp)\nAfter.'
        self.assertEqual(A._site_post_process(self.NEW_URL, content), 'Before.\n\nAfter.')

    def test_tag_links_are_dropped_and_other_list_links_stay(self):
        content = ('License\nESA Standard Licence\n  * [ history ](https://new.esa.int/search?category=multimedia&keywords=6908)\n'
                   '  * [ agency history ](https://www.esa.int/search?category=multimedia&keywords=74574)\n'
                   '  * [The mission](https://www.esa.int/articles/hera)')
        self.assertEqual(A._site_post_process(self.NEW_URL, content),
                         'License\nESA Standard Licence\n  * [The mission](https://www.esa.int/articles/hera)')

    def test_sentences_that_start_like_a_widget_line_stay(self):
        for line in ('Like Earth, Mars has volcanoes.', 'You have already seen the first image of the mission.',
                     'Thank you for liking the mission on social media.'):
            content = 'First.\n\n' + line
            self.assertEqual(A._site_post_process(self.NEW_URL, content), content)

    def test_the_old_widget_lines_still_go(self):
        content = 'Text.\n\nLike\n\nThank you for liking\n\nYou have already liked this page, you can only like it once!'
        self.assertEqual(A._site_post_process(ESA_URL, content), 'Text.')

    def test_the_old_breadcrumb_goes_and_an_image_credit_stays(self):
        self.assertEqual(A._site_post_process(ESA_URL, 'Text.\n\nESA / Applications / Satellite navigation / Galileo'), 'Text.')
        content = 'Text.\n\nESA / Hubble & NASA, J. Doe'
        self.assertEqual(A._site_post_process(ESA_URL, content), content)

    def test_one_image_per_esa_asset(self):
        base = 'https://media.esa.int/global/public/common/image/2026/11/402323/image-thumb__402323__'
        other = 'https://media.esa.int/global/public/on-demand/video/2026/11/402352/image-thumb__402352__fit_large/gas.96ad6aef.webp'
        res = self.Result(base + 'fit_large/a.b0ac4d68.webp', base + 'fit_large/a@2x.b0ac4d68.webp',
                          base + 'fit_large/a~-~media--92aa2fb2--query.ef5c984e.webp', base + 'fit_medium/a.ff6f13e7.webp', other)
        self.assertEqual([i['url'] for i in A.collect_images(res)], [base + 'fit_large/a.b0ac4d68.webp', other])

    def test_esa_thumbnails_and_placeholders_are_skipped(self):
        thumb = 'https://media.esa.int/global/public/common/image/2026/01/274347/image-thumb__274347__fill_small/c.4fa1b7eb.webp'
        self.assertEqual(A.collect_images(self.Result(thumb)), [])
        self.assertEqual(A.collect_images(self.Result('https://media.esa.int/x/missing-4.a1394e2f.webp', alt='fallback image 5')), [])

    def test_images_elsewhere_are_not_grouped_or_skipped(self):
        res = self.Result('https://example.com/402323/image-thumb__402323__a.webp', 'https://example.com/402323/image-thumb__402323__b.webp',
                          'https://example.com/__fill_small/c.webp')
        self.assertEqual(len(A.collect_images(res)), 3)

    def test_a_doubled_host_in_the_thumbnail_is_repaired(self):
        html = '<meta name="twitter:image" content="https://www.esa.inthttps://media.esa.int/global/a.jpg">'
        self.assertEqual(A.extract_og_image(html), 'https://media.esa.int/global/a.jpg')

    def test_a_url_inside_a_query_string_stays(self):
        html = '<meta name="twitter:image" content="https://cdn.example.com/img?src=https://other.example.com/a.jpg">'
        self.assertEqual(A.extract_og_image(html), 'https://cdn.example.com/img?src=https://other.example.com/a.jpg')


class ImageUrlTests(unittest.TestCase):
    def result(self, src):
        return types.SimpleNamespace(url='https://www.relativityspace.com/press-release/x', media={'images': [{'src': src, 'alt': 'Logo'}]})

    def test_a_protocol_relative_url_gets_https(self):
        urls = [i['url'] if isinstance(i, dict) else i for i in A.collect_images(self.result('//images.squarespace-cdn.com/a/b.png'))]
        self.assertEqual(urls, ['https://images.squarespace-cdn.com/a/b.png'])

    def test_a_full_url_is_unchanged(self):
        urls = [i['url'] if isinstance(i, dict) else i for i in A.collect_images(self.result('https://images.squarespace-cdn.com/a/b.png'))]
        self.assertEqual(urls, ['https://images.squarespace-cdn.com/a/b.png'])


class EsaTemplateAltTests(unittest.TestCase):
    def test_unrendered_template_alt_is_dropped(self):
        self.assertEqual(A._clean_alt('$video.data_map.short_description.content'), '')
        self.assertEqual(A._clean_alt('$image.caption'), '')

    def test_dollar_amounts_in_captions_stay(self):
        # SpaceQ writes US dollars as "$US"; the 2026-08-27 audit flagged that as a false positive.
        for alt in ('$US 120M contract for the lunar rover', 'A $5.2 billion budget line for exploration'):
            with self.subTest(alt=alt):
                self.assertEqual(A._clean_alt(alt), alt)


    def test_placeholder_alt_in_the_body_is_dropped(self):
        # The Ramses article's linked video thumbnail, as the deployed scraper wrote it on 2026-10-15 (URLs shortened).
        line = ('[ ![Play](https://www.esa.int/extension/pillars/design/pillars/images/play-button.svg) '
                '![$video.data_map.short_description.content](https://www.esa.int/var/esa/storage/images/Ramses_pillars.png)'
                ' ](https://www.esa.int/ESA_Multimedia/Videos/2025/10/Ramses)')
        out = A._site_post_process(ESA_URL, 'The Ramses spacecraft has been turned on.\n\n' + line)
        self.assertNotIn('$video', out)
        self.assertIn('![](https://www.esa.int/var/esa/storage/images/Ramses_pillars.png)', out)
        self.assertIn('![Play](https://www.esa.int/extension/pillars/design/pillars/images/play-button.svg)', out)
        self.assertIn(' ](https://www.esa.int/ESA_Multimedia/Videos/2025/10/Ramses)', out)

    def test_a_bare_placeholder_alt_in_the_body_is_dropped(self):
        self.assertEqual(A._site_post_process(ESA_URL, 'Text.\n\n![$video](https://www.esa.int/a.png)'),
                         'Text.\n\n![](https://www.esa.int/a.png)')

    def test_body_alts_that_only_start_with_a_dollar_stay(self):
        for alt in ('$US 120M contract for the lunar rover', '$5.2 billion budget line'):
            with self.subTest(alt=alt):
                content = 'Text.\n\n![' + alt + '](https://www.esa.int/a.png)'
                self.assertEqual(A._site_post_process(ESA_URL, content), content)

    def test_other_sites_bodies_are_untouched(self):
        content = 'Text.\n\n![$video.data_map.short_description.content](https://spacenews.com/a.png)'
        self.assertEqual(A._site_post_process('https://spacenews.com/x/', content), content)


if __name__ == '__main__':
    unittest.main(verbosity=2)
