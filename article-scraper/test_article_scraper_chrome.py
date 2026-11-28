"""Tests for the third batch of tcs-workflows#244: page chrome left in SpaceQ, NASA, NASA Science and
SpacePolicyOnline bodies, two kinds of non-description alt, logo thumbnails, and NASA pages with no listed picture.

Run:
    python working-files/python-scripts/test_article_scraper_chrome.py

Crawl4AI and lib_index are stubbed, so this runs anywhere. The fixtures copy text from the 2026-11-08 audit's saved
files and from the pages as served on 2026-11-11.
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
except ImportError:
    HAVE_BS4 = False

SQ_URL = 'https://spaceq.ca/csa-abb-mars-methane-detector/'
NASA_BLOG_URL = 'https://www.nasa.gov/blogs/spacestation/2026/11/06/spacewalk-cleanup/'
SCI_BLOG_URL = 'https://science.nasa.gov/blog/rubies-on-mars/'
APOD_URL = 'https://science.nasa.gov/image-article/apod-2026-november-7-de-gerlache-kocher-massif/'
SPO_URL = 'https://spacepolicyonline.com/news/whats-happening-in-space-policy-november-1-7-2026/'


def _result(url, html='', markdown='', images=None):
    return types.SimpleNamespace(url=url, html=html, markdown=markdown, success=True,
                                 media={'images': list(images or [])})


def _scrape(url, res, title='T'):
    async def fake_fetch(crawler, u, cfg):
        return res, None, 1

    saved = A._fetch_with_retry, A._plain_get
    A._fetch_with_retry = fake_fetch
    A._plain_get = lambda u: ('', 'no network in tests')   # #317: a plain_fetch site takes the browser path here
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(A.scrape_one(None, {'url': url, 'title': title}))
    finally:
        A._fetch_with_retry, A._plain_get = saved


class SpaceQTests(unittest.TestCase):
    def test_related_posts_block_is_excluded(self):
        self.assertEqual(A.run_config_for(SQ_URL).kwargs['excluded_selector'], 'div.jp-relatedposts')

    def test_empty_related_heading_is_cut(self):
        body = 'Last paragraph of the article.\n\n### _Related_\n'
        self.assertEqual(A._site_post_process(SQ_URL, body), 'Last paragraph of the article.')

    def test_logo_thumbnail_gives_way_to_the_first_picture(self):
        photo = 'https://spaceq.ca/wp-content/uploads/2026/11/mars-methane-detector.jpg'
        html = '<meta property="og:image" content="https://spaceq.ca/wp-content/uploads/2020/01/SpaceQ-Logo-Square.png">'
        item = _scrape(SQ_URL, _result(SQ_URL, html=html, markdown='Article text.',
                                       images=[{'src': photo, 'alt': 'The detector', 'width': 800, 'height': 450}]))
        self.assertEqual(item['thumbnail'], photo)
        self.assertEqual(item['images'], [{'url': photo, 'alt': 'The detector'}])

    def test_logo_thumbnail_with_no_picture_is_dropped(self):
        html = '<meta property="og:image" content="https://spaceq.ca/wp-content/uploads/2020/01/SpaceQ-Logo-Square.png">'
        item = _scrape(SQ_URL, _result(SQ_URL, html=html, markdown='Article text.'))
        self.assertEqual((item['thumbnail'], item['images']), ('', []))


class AltTextTests(unittest.TestCase):
    def test_stray_leading_period(self):
        self.assertEqual(A._clean_alt('.Illustration of the gold-coloured GOSAT-GW satellite'),
                         'Illustration of the gold-coloured GOSAT-GW satellite')

    def test_real_leading_periods_stay(self):
        self.assertEqual(A._clean_alt('.NET logo on a screen'), '.NET logo on a screen')
        self.assertEqual(A._clean_alt('...and the rocket lifts off'), '...and the rocket lifts off')

    def test_a_bare_timestamp_is_no_description(self):
        self.assertEqual(A._clean_alt('2026-10-21 14:03:11'), '')
        self.assertEqual(A._clean_alt('Launch on 2026-10-21 14:03:11 UTC'), 'Launch on 2026-10-21 14:03:11 UTC')


class NasaBodyTests(unittest.TestCase):
    def test_headline_repeated_above_the_hero_is_cut(self):
        body = ('# Spacewalk Cleanup, Biomedical Science, and Maintenance Wrap Up Week\n'
                '[![NASA astronaut rides the Canadarm2](https://www.nasa.gov/a.jpg)](https://www.nasa.gov/a.jpg)\n'
                'Caption.\n\nThe seven crew members cleaned up after a spacewalk.')
        out = A._site_post_process(NASA_BLOG_URL, body)
        self.assertTrue(out.startswith('[![NASA astronaut rides the Canadarm2]'))

    def test_a_heading_that_is_not_above_a_picture_stays(self):
        body = '# A real heading\n\nText.'
        self.assertEqual(A._site_post_process(NASA_BLOG_URL, body), body)

    def test_nasa_plus_label_line(self):
        body = 'Caption of the hero.\nNASA+\nThe seven Expedition 75 crew members cleaned up.'
        self.assertEqual(A._site_post_process(NASA_BLOG_URL, body),
                         'Caption of the hero.\n\nThe seven Expedition 75 crew members cleaned up.')

    def test_nasa_plus_inside_a_sentence_stays(self):
        body = 'Watch it live on NASA+ tonight.\n\nMore text.'
        self.assertEqual(A._site_post_process(NASA_BLOG_URL, body), body)

    def test_media_contact_heading_with_a_space_before_the_colon(self):
        body = 'Last paragraph.\n\n**Media Contact :**\nPat Quinn\nNASA’s Goddard Space Flight Center\n301-555-0168'
        self.assertEqual(A._site_post_process('https://science.nasa.gov/missions/hubble/fixture/', body), 'Last paragraph.')

    def test_social_follow_list_is_cut(self):
        body = 'Last line of the story.\n\nFacebook logo\n\n[@NASA](https://www.facebook.com/NASA)\n\nInstagram logo\n\nX logo'
        self.assertEqual(A._site_post_process('https://science.nasa.gov/solar-system/fixture/', body), 'Last line of the story.')

    def test_blog_promo_cards_are_cut(self):
        body = ('An unexpected tool on board as we delve further into Mars’ deep past.\n##  \n'
                '  * ### Want to read more posts from the Perseverance team?\n'
                '[ Visit Mission Updates  ](https://science.nasa.gov/mission/mars-2020-perseverance/science-updates/)\n'
                '  * ### Want to learn more about Perseverance’s science instruments?\n'
                '[ Visit the Science Instruments page  ](https://science.nasa.gov/mission/mars-2020-perseverance/science-instruments/)\n\n\n'
                '![](https://assets.science.nasa.gov/dynamicimage/assets/science/psd/mars/resources/detail_files/2/7/27795_PIA26202-web.jpg)')
        self.assertEqual(A._site_post_process(SCI_BLOG_URL, body),
                         'An unexpected tool on board as we delve further into Mars’ deep past.')

    def test_apod_teaser_and_credits_row(self):
        body = ('The explanation of the picture.\n'
                '**Tomorrow\'s picture:** meteoric   \n\n'
                '| A service of:  |  [ASD](https://science.gsfc.nasa.gov/astrophysics/) at [NASA](https://www.nasa.gov/),   \n'
                '[NASA Science Activation](https://science.nasa.gov/learn/about-science-activation/) |')
        out = A._site_post_process(APOD_URL, body)
        self.assertNotIn('Tomorrow', out)
        self.assertIn('[NASA](https://www.nasa.gov/), [NASA Science Activation]', out)
        self.assertIn('The explanation of the picture.', out)


class NasaImageTests(unittest.TestCase):
    def test_promo_card_picture_leaves_the_list(self):
        own = 'https://assets.science.nasa.gov/dynamicimage/assets/science/psd/mars/downloadable_items/5/1/Mars_Perseverance_SIF.jpg'
        promo = 'https://assets.science.nasa.gov/dynamicimage/assets/science/psd/mars/resources/detail_files/2/7/27795_PIA26202-web.jpg'
        images = [{'url': own, 'alt': ''}, {'url': promo, 'alt': ''}]
        seen = {own: '', promo: 'Want to read more posts from the Perseverance team? Visit Mission Updates'}
        self.assertEqual(A._filter_cruft_images(SCI_BLOG_URL, images, seen), [{'url': own, 'alt': ''}])

    def test_og_image_is_listed_when_the_list_is_empty(self):
        og = 'https://assets.science.nasa.gov/dynamicimage/assets/science/missions/hubble/galaxies/hubble-galaxy.jpg?w=1280'
        html = f'<meta property="og:image" content="{og}">'
        item = _scrape('https://science.nasa.gov/missions/hubble/fixture/', _result(
            'https://science.nasa.gov/missions/hubble/fixture/', html=html, markdown='Hubble text.'))
        self.assertEqual(item['images'], [{'url': og.split('?')[0], 'alt': ''}])
        self.assertEqual(item['thumbnail'], og)

    def test_no_og_image_fallback_on_other_sites(self):
        html = '<meta property="og:image" content="https://spacepolicyonline.com/wp-content/uploads/2026/11/photo.jpg">'
        item = _scrape(SPO_URL, _result(SPO_URL, html=html, markdown='Policy text.'))
        self.assertEqual(item['images'], [])


class SpacePolicyOnlineTests(unittest.TestCase):
    def test_update_note_above_last_updated_is_cut(self):
        body = ('Last paragraph of the week.\n\n'
                '_This article has been updated to add the information about Swift’s impending reentry._\n\n'
                '##### Last Updated: Nov 07, 2026 10:00 pm ET')
        self.assertEqual(A._site_post_process(SPO_URL, body), 'Last paragraph of the week.')

    def test_a_note_mid_body_stays(self):
        body = 'First.\n\n_This article has been updated to add a quote._\n\nThe body goes on.'
        self.assertIn('This article has been updated', A._site_post_process(SPO_URL, body))


ESA_URL = 'https://www.esa.int/Applications/Satellite_navigation/Galileo_fixture'
NSF_URL = 'https://www.nasaspaceflight.com/2026/11/launch-preview-11092026/'
ARS_URL = 'https://arstechnica.com/space/2026/11/rocket-report-fixture/'


class EsaTests(unittest.TestCase):
    def test_third_party_warning_is_excluded(self):
        self.assertIn('.interactive-media-modal__third-party-warning', A.run_config_for(ESA_URL).kwargs['excluded_selector'])

    def test_about_galileo_is_cut(self):
        body = 'The last paragraph.\n\n## About Galileo\n\nGalileo is Europe’s own global navigation system.'
        self.assertEqual(A._site_post_process(ESA_URL, body), 'The last paragraph.')

    def test_rule_lines_go_and_text_stays(self):
        body = 'First paragraph.\n* * *\nSecond paragraph.\n--\nThird -- with a dash inside.'
        self.assertEqual(A._site_post_process(ESA_URL, body), 'First paragraph.\nSecond paragraph.\nThird -- with a dash inside.')


class NsfTests(unittest.TestCase):
    def test_counter_and_see_also_are_excluded(self):
        kw = A.run_config_for(NSF_URL).kwargs
        self.assertEqual(kw['excluded_selector'], 'i.penci-post-countview-number-check, h4.widget-title, h4.widget-title + ul')
        self.assertNotIn('"', kw['excluded_selector'])

    def test_body_chrome(self):
        body = ('_6_\nSeven rockets from four countries are set to launch this week.\n'
                '#### See Also\n  * [Starship Flight 15 awaits FAA](https://www.nasaspaceflight.com/2026/11/starship/)\n'
                '  * [Launch Roundup](https://www.nasaspaceflight.com/2026/11/roundup/)\n'
                'Falcon 9 will land downrange.\n'
                '[**Mir | Demo Flight**](https://forum.nasaspaceflight.com/index.php?topic=64752.0)\n'
                'Discover more\nTrack Satellites\nRead Tech News\nGo Stargazing\n'
                'On Tuesday, Nov. 10, South Korea is set to launch.\n**\n'
                '[![](https://www.nasaspaceflight.com/wp-content/uploads/2026/06/L2_Membership_ShopBanner_c3a.jpg)]'
                '(https://shop.nasaspaceflight.com/)')
        out = A._site_post_process(NSF_URL, body)
        for gone in ('_6_', 'See Also', 'Starship Flight 15 awaits', 'Discover more', 'Track Satellites', 'L2_Membership', '\n**\n'):
            self.assertNotIn(gone, out)
        for kept in ('Seven rockets from four countries', 'Falcon 9 will land downrange.', 'On Tuesday, Nov. 10, South Korea',
                     'Mir | Demo Flight'):
            self.assertIn(kept, out)

    def test_sidebar_and_ad_pictures_leave_the_list(self):
        photo = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/11/IMG_9624-1920x1284.jpeg'
        towel = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/06/Towel-e1782335989998.png'
        live = 'https://www.nasaspaceflight.com/wp-content/uploads/2026/03/live-thumb.jpg'
        images = [{'url': photo, 'alt': ''}, {'url': towel, 'alt': ''}, {'url': live, 'alt': ''}]
        seen = {photo: '', towel: '', live: 'NSF Starbase Live SEN ISS Live NSF Spacecoast Live Active Discussions'}
        self.assertEqual(A._filter_cruft_images(NSF_URL, images, seen), [{'url': photo, 'alt': ''}])

    def test_raw_camera_file_name_is_no_alt(self):
        self.assertEqual(A._clean_alt('DSC_4471.NEF'), '')
        self.assertEqual(A._clean_alt('IMG_0012.CR3'), '')


class ArsTests(unittest.TestCase):
    def test_lightbox_captions_and_newsletter_box_are_excluded(self):
        self.assertEqual(A.run_config_for(ARS_URL).kwargs['excluded_selector'],
                         'div.pswp-caption-content, div.ars-newsletter-shortcode')

    def test_junk_pictures_leave_the_list(self):
        lead = 'https://cdn.arstechnica.net/wp-content/uploads/2026/11/launch-1152x648.jpg'
        junk = ['https://cdn.arstechnica.net/wp-content/uploads/2016/05/e.berger-11.jpg',
                'https://cdn.arstechnica.net/wp-content/uploads/2026/11/other-story-thumb.jpg',
                'https://arstechnica.com/wp-content/themes/ars/assets/img/loader.gif',
                'https://cdn.arstechnica.net/wp-content/uploads/2018/05/heavyl.png']
        images = [{'url': lead, 'alt': 'A Falcon 9 lifts off'}] + [{'url': u, 'alt': ''} for u in junk]
        seen = {lead: 'A Falcon 9 lifts off', junk[0]: 'Photo of Eric Berger',
                junk[1]: 'Listing image for first story in Most Read: Some other story', junk[2]: 'Loading', junk[3]: ''}
        self.assertEqual(A._filter_cruft_images(ARS_URL, images, seen), [{'url': lead, 'alt': 'A Falcon 9 lifts off'}])

    def test_a_photo_described_in_a_full_sentence_stays(self):
        img = {'url': 'https://cdn.arstechnica.net/wp-content/uploads/2026/11/isaacman.jpg',
               'alt': 'Photo of Jared Isaacman speaking at the Kennedy Space Center'}
        self.assertEqual(A._filter_cruft_images(ARS_URL, [img], {}), [img])

    def test_rocket_report_graphics_and_newsletter_box_leave_the_body(self):
        body = ('Welcome to the Rocket Report!\n'
                '![](https://cdn.arstechnica.net/wp-content/uploads/2018/05/smalll.png)\n'
                'A small rocket news item.\n'
                '##### Rocket Report\n\nKeep up with the latest in space exploration and rocket launches.\n\n'
                '[Sign Me Up!](https://arstechnica.com/newsletters/)\n'
                'The next item.')
        out = A._site_post_process(ARS_URL, body)
        self.assertEqual(out, 'Welcome to the Rocket Report!\nA small rocket news item.\nThe next item.')


AXM_URL = 'https://www.axiomspace.com/release/redwire-follow-on-contract-second-module'
SPN_URL = 'https://spacenews.com/esi-motion-fixture/'
WF = 'https://cdn.prod.website-files.com/671fb31e0ed79c76c93e1019/6720aa_hero'


class AxiomTests(unittest.TestCase):
    def test_body_selector_only(self):
        self.assertEqual(A.run_config_for(AXM_URL).kwargs['target_elements'], ['.post-rich-text'])

    def test_webflow_sizes_are_one_picture(self):
        res = _result(AXM_URL, images=[
            {'src': WF + '-p-500.jpg', 'alt': '', 'width': 500, 'height': 300},
            {'src': WF + '-p-1080.jpg', 'alt': '', 'width': 1080, 'height': 600},
            {'src': WF + '.jpg', 'alt': '', 'width': 1200, 'height': 675}])
        self.assertEqual([i['url'] for i in A.collect_images(res)], [WF + '.jpg'])

    def test_site_asset_bucket_is_no_picture(self):
        res = _result(AXM_URL, images=[
            {'src': 'https://cdn.prod.website-files.com/671fb31e0ed79c76c93e0fee/672b_privacyoptions.svg', 'alt': '',
             'width': 300, 'height': 150},
            {'src': WF + '.jpg', 'alt': '', 'width': 1200, 'height': 675}])
        self.assertEqual([i['url'] for i in A.collect_images(res)], [WF + '.jpg'])

    def test_spacers_and_paragraph_breaks(self):
        body = '‍HOUSTON, Nov. 6, 2026 – Axiom Space announced a contract.\nThe module is the second.\n‍\n**About Redwire**'
        self.assertEqual(A._site_post_process(AXM_URL, body),
                         'HOUSTON, Nov. 6, 2026 – Axiom Space announced a contract.\n\nThe module is the second.\n\n**About Redwire**')


class EuropeanSpaceflightTests(unittest.TestCase):
    def test_image_list_is_the_articles_own(self):
        self.assertEqual(A.config_for('https://europeanspaceflight.com/fixture/')['image_scope'], 'div.td-post-content')


class SpaceNewsTests(unittest.TestCase):
    def test_podcast_page_ends_at_the_description(self):
        body = ('This week on Space Minds, the guest talks about lunar logistics.\n\n### About Space Minds\n\n'
                'Space Minds is a new audio podcast...\n\n"*" indicates required fields\n\nNote: By registering...')
        self.assertEqual(A._site_post_process(SPN_URL, body), 'This week on Space Minds, the guest talks about lunar logistics.')

    def test_press_release_media_contact_is_cut(self):
        body = 'Please visit our website; [www.esimotion.com](http://www.esimotion.com)\n\nMedia Contact:\nJane Doe\njane@example.com'
        self.assertEqual(A._site_post_process(SPN_URL, body), 'Please visit our website; [www.esimotion.com](http://www.esimotion.com)')

    def test_press_release_is_named(self):
        page = ('<meta name="parsely-section" content="Press Release" />'
                '<meta name="parsely-author" content="ESI Motion" />')
        self.assertEqual(A._press_release_by(SPN_URL, page), 'ESI Motion')
        self.assertEqual(A._press_release_by(SPN_URL, '<meta name="parsely-section" content="Press Release" />'), 'a company')
        self.assertEqual(A._press_release_by(SPN_URL, '<meta name="parsely-section" content="Launch" />'), '')
        self.assertEqual(A._press_release_by('https://spaceq.ca/fixture/', page), '')

    def test_press_release_note_in_the_file(self):
        import tempfile
        item = {'title': 'ESI Motion wins a contract', 'url': SPN_URL, 'source': 'SpaceNews', 'date': '2026-11-05',
                'content': 'Release text. ' * 30, 'has_content': True, 'images': [], 'press_release_by': 'ESI Motion'}
        with tempfile.TemporaryDirectory() as tmp:
            saved = A.SCRAPER_RUNS_ROOT
            A.SCRAPER_RUNS_ROOT = tmp
            try:
                A._write_file_output([item], 'test-run')
            finally:
                A.SCRAPER_RUNS_ROOT = saved
            with open(os.path.join(tmp, 'test-run', 'article-0.md'), encoding='utf-8') as f:
                text = f.read()
        self.assertIn(f'**URL:** {SPN_URL}\n**Note:** Press release from ESI Motion, carried by SpaceNews. '
                      'Not SpaceNews reporting.\n\n---\n', text)

    def test_og_image_listed_when_the_list_is_empty(self):
        og = 'https://spacenews.com/wp-content/uploads/2026/11/gomspace-satellite.jpg'
        item = _scrape(SPN_URL, _result(SPN_URL, html=f'<meta property="og:image" content="{og}">', markdown='Text.'))
        self.assertEqual(item['images'], [{'url': og, 'alt': ''}])
        self.assertNotIn('press_release_by', item)


FFY_URL = 'https://fireflyspace.com/news/firefly-aerospace-completes-preliminary-design-review-for-nasa-moonfall-mission/'
RD_URL = 'https://www.reactiondynamics.space/en/news/reaction-dynamics-unveils-borealis'
REL_URL = 'https://www.relativityspace.com/press-release/2026/10/6/september-2026-company-update'
STOKE_URL = 'https://www.stokespace.com/news/stoke-and-planet-announce-multi-launch-agreement'


class PressReleaseBoilerplateTests(unittest.TestCase):
    def test_firefly_bold_about_heading(self):
        body = ('The mission will pave the way for future human missions.\n\n**About Firefly Aerospace**\n\n'
                'Firefly Aerospace is a market leading space and defense technology company.\n\n'
                '**Forward-Looking Statements**\n\nThis press release contains forward-looking statements.\n\n'
                '**Media Contact******\n\nmedia@fireflyspace.com')
        self.assertEqual(A._site_post_process(FFY_URL, body), 'The mission will pave the way for future human missions.')

    def test_firefly_scitec_and_the_old_h3(self):
        self.assertEqual(A._site_post_process(FFY_URL, 'Operational edge.\n\nAbout SciTec\n\nSciTec is a ...'), 'Operational edge.')
        self.assertEqual(A._site_post_process(FFY_URL, 'Text.\n\n### ABOUT FIREFLY AEROSPACE\n\nFirefly is ...'), 'Text.')

    def test_firefly_named_in_a_sentence_stays(self):
        body = 'Analysts wrote about Firefly Aerospace and its rockets this week.\n\nMore text.'
        self.assertEqual(A._site_post_process(FFY_URL, body), body)

    def test_reaction_dynamics(self):
        self.assertEqual(A.run_config_for(RD_URL).kwargs['excluded_selector'], 'div.max-w-3xl.mx-auto')
        body = 'Path to sovereign launch at scale.\n\n**About Reaction Dynamics**\n\nReaction Dynamics is ...\n\nMedia contact\nJane'
        self.assertEqual(A._site_post_process(RD_URL, body), 'Path to sovereign launch at scale.')

    def test_stoke_about_heading_after_the_redesign(self):
        body = 'Text of the release.\n\n**About Stoke**  \n  \nStoke is building fully and rapidly reusable launch vehicles.'
        self.assertEqual(A._site_post_process(STOKE_URL, body), 'Text of the release.')

    def test_relativity_byline_only_post(self):
        body = ('Scaled operations.\n\n[Tim Ellis](https://www.relativityspace.com/press-release?author=5f2a)'
                'October 6, 2026')
        self.assertEqual(A._site_post_process(REL_URL, body), 'Scaled operations.')


class SmallSourceImageTests(unittest.TestCase):
    def test_relativity_header_logo(self):
        logo = 'https://images.squarespace-cdn.com/content/v1/abc/Relativity_White_RGB.png'
        photo = 'https://images.squarespace-cdn.com/content/v1/abc/Landscape+RSxSES+.png'
        images = [{'url': logo, 'alt': 'Relativity Space'}, {'url': photo, 'alt': ''}]
        self.assertEqual(A._filter_cruft_images(REL_URL, images, {}), [{'url': photo, 'alt': ''}])

    def test_og_image_entities(self):
        html = '<meta property="og:image" content="https://cdn.sanity.io/images/x/y.jpg?auto=format&amp;fit=max&amp;w=1200">'
        self.assertEqual(A.extract_og_image(html), 'https://cdn.sanity.io/images/x/y.jpg?auto=format&fit=max&w=1200')

    def test_stoke_title_from_the_h1(self):
        html = ('<meta property="og:title" content="Stoke Space announces Nova Pathfinder Flight 1 manifest">'
                '<h1>Stoke and Planet announce a multi-launch agreement</h1>')
        item = _scrape(STOKE_URL, _result(STOKE_URL, html=html, markdown='Release text.'), title='')
        self.assertEqual(item['title'], 'Stoke and Planet announce a multi-launch agreement')

    def test_nasa_science_photo_once_and_never_the_rendition(self):
        dam = ('https://assets.science.nasa.gov/content/dam/science/psd/photojournal/pia/pia26/pia26814/PIA26814.png'
               '/jcr:content/renditions/cq5dam.thumbnail.319.319.png')
        dyn = 'https://assets.science.nasa.gov/dynamicimage/assets/science/psd/photojournal/pia/pia26/pia26814/PIA26814.png'
        res = _result('https://science.nasa.gov/photojournal/fixture/', images=[
            {'src': dam, 'alt': '', 'width': 319, 'height': 319}, {'src': dyn, 'alt': '', 'width': 1600, 'height': 900}])
        self.assertEqual([i['url'] for i in A.collect_images(res)], [dyn])

    def test_nasa_science_lrg_copy_wins(self):
        base = 'https://assets.science.nasa.gov/dynamicimage/assets/science/missions/hubble/galaxies/'
        res = _result('https://science.nasa.gov/missions/hubble/fixture/', images=[
            {'src': base + 'Hubble_Galaxy.jpg', 'alt': 'A galaxy', 'width': 1200, 'height': 800},
            {'src': base + 'Hubble_Galaxy_lrg.jpg', 'alt': '', 'width': 3000, 'height': 2000}])
        self.assertEqual(A.collect_images(res), [{'url': base + 'Hubble_Galaxy_lrg.jpg', 'alt': 'A galaxy'}])


AA_URL = 'https://aerospaceamerica.aiaa.org/canadian-rocket-startup-plans-engine-component-hot-fire-tests-in-december/'
AA_OG = 'https://aerospaceamerica.aiaa.org/wp-content/uploads/2026/09/R-2-illustration-scaled.webp'
AA_CAPTION = "An illustration of Canada Rocket Company's R-2. Credit: CRC"
# The hero's caption as the scoped page holds it on 2026-11-11. Crawl4AI has already removed the hero <img>.
AA_FEATURED = '<figcaption class="featured-img-caption wp-caption-text">' + AA_CAPTION + '</figcaption>'
AA_BODY = ('<div class="post-content cf entry-content content-normal"><p>Canada Rocket Company plans to hot-fire '
           'engine components in December.</p></div>')


class AerospaceAmericaTests(unittest.TestCase):
    def test_hero_is_listed_with_its_caption_and_the_caption_leaves_the_body(self):
        html = '<meta property="og:image" content="' + AA_OG + '">' + AA_FEATURED + AA_BODY
        md = AA_CAPTION + '\n\nCanada Rocket Company plans to hot-fire engine components in December.\n'
        item = _scrape(AA_URL, _result(AA_URL, html=html, markdown=md))
        self.assertEqual(item['images'], [{'url': AA_OG, 'alt': AA_CAPTION}])
        self.assertNotIn('Credit: CRC', item['content'])
        self.assertTrue(item['content'].startswith('Canada Rocket Company plans'))

    def test_hero_without_a_caption_is_listed_with_no_alt(self):
        html = '<meta property="og:image" content="' + AA_OG + '">' + AA_BODY
        item = _scrape(AA_URL, _result(AA_URL, html=html, markdown='Body text.'))
        self.assertEqual(item['images'], [{'url': AA_OG, 'alt': ''}])
        self.assertEqual(item['content'], 'Body text.')

    def test_selector_takes_in_the_hero_frame(self):
        self.assertEqual(A.config_for(AA_URL)['selector'], 'div.featured figcaption, div.post-content.entry-content')

    def test_related_reading_link_leaves_the_body(self):
        text = ('First paragraph.\n'
                '  * **[RELATED READING: Launch vehicle availability poses challenge for government and industry, '
                'panelists say](https://aerospaceamerica.aiaa.org/launch-vehicle-availability-poses-challenge-for-'
                'government-and-industry-panelists-say/)  \n'
                '**\n'
                'Second paragraph.')
        self.assertEqual(A._site_post_process(AA_URL, text), 'First paragraph.\nSecond paragraph.')

    def test_nameplate_and_cover_promo_leave_the_list(self):
        real = {'url': AA_OG, 'alt': AA_CAPTION}
        images = [{'url': 'https://aerospaceamerica.aiaa.org/wp-content/uploads/2023/01/nameplate.png', 'alt': 'Aerospace America'},
                  {'url': 'https://aerospaceamerica.aiaa.org/wp-content/uploads/2025/04/AA-AprJune-2025-cover-scaled.jpg', 'alt': 'Demo'},
                  real]
        self.assertEqual(A._filter_cruft_images(AA_URL, images, {}), [real])

    def test_empty_frame_link_and_its_caption_leave_the_body(self):
        html = '<meta property="og:image" content="' + AA_OG + '">' + AA_FEATURED + AA_BODY
        md = ('[ ](' + AA_URL + ' "Canadian rocket startup plans engine component hot-fire tests in December") '
              + AA_CAPTION + '\nToronto-based Canada Rocket Company is readying this year for hot-fire testing.')
        item = _scrape(AA_URL, _result(AA_URL, html=html, markdown=md))
        self.assertEqual(item['content'], 'Toronto-based Canada Rocket Company is readying this year for hot-fire testing.')
        self.assertEqual(item['images'], [{'url': AA_OG, 'alt': AA_CAPTION}])

    def test_featured_caption_reads_entities(self):
        self.assertEqual(A._featured_caption('<figcaption class="featured-img-caption">R-2 &amp; GS3. Credit: CRC</figcaption>'),
                         'R-2 & GS3. Credit: CRC')
        self.assertEqual(A._featured_caption('<figcaption class="image-caption"></figcaption>'), '')

    def test_drop_caption_line_leaves_other_text(self):
        self.assertEqual(A._drop_caption_line('A line.\n\n_' + AA_CAPTION + '_\n\nB line.', AA_CAPTION),
                         'A line.\n\nB line.')
        self.assertEqual(A._drop_caption_line('Nothing to drop.', AA_CAPTION), 'Nothing to drop.')


AXM_CDN = 'https://cdn.prod.website-files.com/671fb31e0ed79c76c93e1019/'
AXM_HERO = AXM_CDN + '6abd0e0200_Web_16x9-Redwire%20Contract%20Award%20Ceremonial%20Signing.avif'
AXM_GOLD = AXM_CDN + '6abeb8cf87064910a4b6f518_mike-gold-redwire.avif'
AXM_CIRTAIN = AXM_CDN + '6abea0ce476be38a025b1e1e_Axiom-Jonathan.avif'
AXM_BODY = AXM_CDN + '6abf0000aaaa_Joan-Kranz.avif'


def _axm_page(hero_caption='', body_img=True):
    """An Axiom release as served on 2026-11-11: the hero and its caption div above .post-rich-text, a body photo, and
    the quote blocks with their headshots after the body."""
    return ('<div class="press-release-hero"><a href="#"><img src="' + AXM_HERO + '" srcset="'
            + AXM_HERO.replace('.avif', '-p-500.avif') + ' 500w" class="press-release-post-image"></a>'
            '<div class="figure-caption' + ('' if hero_caption else ' w-dyn-bind-empty') + '">' + hero_caption + '</div>'
            '<div class="u-24-height"></div></div>'
            '<div class="post-rich-text w-richtext"><p>Redwire text.</p>'
            + ('<figure><img src="' + AXM_BODY + '" alt=""></figure>' if body_img else '') + '</div>'
            '<div class="quote"><img alt="" class="press-release-quote-headshot" loading="lazy" width="120" src="' + AXM_GOLD + '"></div>'
            '<div class="quote"><img alt="" class="press-release-quote-headshot" loading="lazy" width="120" src="' + AXM_CIRTAIN + '"></div>')


class AxiomImageTests(unittest.TestCase):
    def test_quote_headshots_leave_the_list(self):
        images = [{'url': AXM_HERO, 'alt': ''}, {'url': AXM_GOLD, 'alt': ''}, {'url': AXM_CIRTAIN, 'alt': ''}]
        out = A._axiom_images(images, _axm_page(body_img=False))
        self.assertEqual(out, [{'url': AXM_HERO, 'alt': ''}])

    def test_headshot_matches_through_a_size_variant(self):
        images = [{'url': AXM_GOLD.replace('.avif', '-p-500.avif'), 'alt': ''}]
        self.assertEqual(A._axiom_images(images, _axm_page(body_img=False)), [])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_a_body_photo_the_list_missed_is_added(self):
        out = A._axiom_images([{'url': AXM_HERO, 'alt': ''}, {'url': AXM_GOLD, 'alt': ''}], _axm_page())
        self.assertEqual(out, [{'url': AXM_HERO, 'alt': ''}, {'url': AXM_BODY, 'alt': ''}])

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_hero_takes_its_figure_caption(self):
        caps = A._html_captions(_axm_page(hero_caption='Redwire and Axiom Space sign the follow-on contract.'))
        self.assertEqual(caps.get(A._image_key(AXM_HERO)), 'Redwire and Axiom Space sign the follow-on contract.')

    @unittest.skipUnless(HAVE_BS4, 'needs BeautifulSoup')
    def test_an_empty_figure_caption_gives_no_alt(self):
        self.assertNotIn(A._image_key(AXM_HERO), A._html_captions(_axm_page()))

    def test_other_sites_are_untouched(self):
        images = [{'url': AXM_GOLD, 'alt': ''}]
        item = _scrape(SQ_URL, _result(SQ_URL, html=_axm_page(), markdown='Text.', images=[{'src': AXM_GOLD, 'alt': '', 'score': 5}]))
        self.assertEqual([i['url'] for i in item['images']], [AXM_GOLD])


ESA_NEW_URL = 'https://www.esa.int/articles/news-media/news/european-orbiter-detects-hints-of-volcanic-activity-on-mars'
ESA_POSTER = ('https://media.esa.int/global/public/on-demand/video/2026/11/402352/image-thumb__402352__fit_large/'
              'mars-vulcanic-final~-~time-18.96ad6aef.webp')
ESA_PHOTO = ('https://media.esa.int/global/public/common/image/2026/11/402322/image-thumb__402322__fit_large/'
             'hcl-context.c1b3b112.webp')


def _esa_page(title_attrs='data-video-title="Magmatic gas release on Mars" data-media-id="402352"'):
    """ESA's video block as served on 2026-11-07 (trimmed): the title and media id on the block, the poster inside."""
    return ('<main><section class="articles-modules"><div class="video video--compact video--has-modal" ' + title_attrs
            + ' role="button"><div class="video__poster" data-video-poster><div class="image video__poster-img"><figure>'
            '<picture><img src="' + ESA_POSTER + '" alt="" loading="lazy"></picture></figure></div></div></div>'
            '</section></main>')


class EsaVideoTests(unittest.TestCase):
    def test_a_poster_takes_its_videos_title(self):
        out = A._esa_video_alts([{'url': ESA_POSTER, 'alt': ''}], _esa_page())
        self.assertEqual(out, [{'url': ESA_POSTER, 'alt': 'Magmatic gas release on Mars'}])

    def test_own_alts_and_photos_are_kept(self):
        images = [{'url': ESA_POSTER, 'alt': 'Its own words'}, {'url': ESA_PHOTO, 'alt': ''}]
        self.assertEqual(A._esa_video_alts(images, _esa_page()), images)

    def test_attribute_order_and_entities(self):
        page = _esa_page('data-media-id="402352" data-video-title="Mars &amp; its gas"')
        self.assertEqual(A._esa_video_alts([{'url': ESA_POSTER, 'alt': ''}], page)[0]['alt'], 'Mars & its gas')

    def test_another_videos_title_is_not_used(self):
        page = _esa_page('data-video-title="Another video" data-media-id="400001"')
        self.assertEqual(A._esa_video_alts([{'url': ESA_POSTER, 'alt': ''}], page)[0]['alt'], '')

    def test_wired_in_for_esa_only(self):
        media = [{'src': ESA_POSTER, 'alt': '', 'score': 5}]
        item = _scrape(ESA_NEW_URL, _result(ESA_NEW_URL, html=_esa_page(), markdown='Text.', images=media))
        self.assertEqual([i['alt'] for i in item['images']], ['Magmatic gas release on Mars'])
        item = _scrape(SQ_URL, _result(SQ_URL, html=_esa_page(), markdown='Text.', images=media))
        self.assertEqual([i['alt'] for i in item['images']], [''])


if __name__ == '__main__':
    unittest.main(verbosity=2)
