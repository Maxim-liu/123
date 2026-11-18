"""Regression checks for source classification (no network requests)."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "collect_offers", Path(__file__).resolve().parents[1] / "bank_daily_setup" / "collect_offers.py"
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CountingClient:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return "<html><body>No publication date</body></html>", 200, url


class CollectionClassificationTests(unittest.TestCase):
    def test_bochk_generic_detail_link_uses_its_own_offer_heading(self):
        page = collector.Page('''<ul><li><h1>特選客戶限時首次簽賬賞100%回贈</h1>
          <a href="/offer-one"><span>詳情</span></a></li>
          <li><h1>另一活動</h1><a href="/offer-two">詳情</a></li></ul>''')
        self.assertEqual(page.links[0]['title'], '特選客戶限時首次簽賬賞100%回贈')
        self.assertEqual(page.links[1]['title'], '另一活動')
        self.assertEqual(collector.Page('<a href="/img"> <img alt="银行信用卡立减优惠"></a>').links[0]['title'], '银行信用卡立减优惠')

    def test_citic_parenthesized_index_date_is_a_publication_date(self):
        page = collector.Page('''<li><a href="/gonggao/news_261104.shtml">信用卡消费活动公告</a>
           <span class="gg_date"><dd>(2026-11-04)</dd></span></li>''')
        self.assertEqual(page.links[0]['index_date'], '2026-11-04')

    def test_verification_page_with_http_200_is_not_readable_content(self):
        class VerificationClient(collector.PublicClient):
            def policy(self, origin):
                return [], 0
            def raw(self, url, robots=False):
                return '<html>captcha</html>', 200, 'https://post.m.smzdm.com/geetest-middle/?redirect_url=example'
        with self.assertRaises(collector.FetchError) as raised:
            VerificationClient().get('https://post.smzdm.com/p/example/')
        self.assertEqual(raised.exception.status, 'blocked')
        self.assertEqual(str(raised.exception), 'verification_or_login_required')

    def test_roundup_preserves_only_bounded_bank_excerpts_from_article_body(self):
        text = '''<nav>导航银行优惠999元</nav><div id="article_content">
           <p>工商银行信用卡消费优惠最高减20元，详情以原文为准。</p>
           <p>建设银行信用卡返现活动5元。</p><script>导航信用卡红包888元</script>
           </div><footer>页脚银行奖励100元</footer>'''
        source = {'source_type': 'community', 'article_containers': ['article_content']}
        result = collector.article_evidence(text, source)
        self.assertEqual([x['bank'] for x in result['reported_fragments']], ['工商银行', '建设银行'])
        self.assertTrue(all(not x['verified'] for x in result['reported_fragments']))
        self.assertNotIn('999', str(result))
        self.assertNotIn('888', str(result))
        self.assertNotIn('100', str(result))
        self.assertLessEqual(sum(len(x['reported_text']) for x in result['reported_fragments']), 320)

    def test_rss_excerpt_is_preserved_without_verifying_the_offer(self):
        feed = '''<rss><channel><item><title>Chase $400 checking bonus</title>
          <link>https://example.com/bonus</link><pubDate>Wed, 18 Nov 2026 12:00:00 +0000</pubDate>
          <description>Reported bonus requires qualifying direct deposits.</description></item></channel></rss>'''
        source = {'id': 'test', 'name': 'Public RSS', 'region': 'us', 'timezone': 'America/New_York'}
        record = collector.normalize(next(collector.feed_entries(feed, source)), source,
            dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertIn('qualifying direct deposits', record['source_excerpt'])
        self.assertNotIn('qualifying direct deposits', record['details'])
        self.assertIn('$400', record['details'])
        self.assertFalse(record['verified'])
        self.assertIsNone(record['start_date'])
        self.assertIsNone(record['end_date'])

    def test_official_lookback_does_not_extend_community_news_age(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'bank_daily_setup/sources.json').read_text())
        sources = {s['id']: s for s in config['sources']}
        now = dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc)
        entry = {'title': '邮储信用卡双十一双十二满减优惠', 'source_url': 'https://example.com/activity', 'published_raw': '2026-10-20'}
        official = collector.normalize(entry, sources['cn_psbc_debit'], now, dt.date(2026, 11, 19))
        self.assertIsNotNone(official)
        self.assertFalse(official['verified'])
        self.assertIsNone(official['start_date'])
        self.assertIsNone(official['end_date'])
        self.assertIsNone(collector.normalize(entry, sources['cn_smzdm_yangmaocun'], now, dt.date(2026, 11, 19)))
        self.assertTrue(all(s['max_age_days'] == 14 for s in sources.values() if s['source_type'] == 'community'))

    def test_ctrip_explicit_weburl_links_keep_the_bank_and_visible_terms(self):
        listing = '''<ul><li class="link_area" weburl="https://contents.ctrip.com/activitysetupapp/mkt/index/boche">
          <h3>中国银行</h3><span>分期最高减200元</span></li>
          <li><p>优惠满减</p><h3>工商银行 信用卡</h3><span>首单减10元</span>
          <span weburl="https://contents.ctrip.com/activitysetupapp/mkt/index/icbcsdc10">查看详情</span></li></ul>'''
        source = {'id': 'ctrip', 'name': '携程银行优惠', 'region': 'cn', 'timezone': 'Asia/Shanghai',
                  'url': 'https://pages.c-ctrip.com/index.html', 'source_type': 'official',
                  'link_attributes': ['weburl'], 'link_domains': ['contents.ctrip.com'],
                  'link_pattern': '/activitysetupapp/', 'allow_undated': True, 'max_detail_pages': 0}
        entries, seen, errors = collector.html_entries(listing, source, CountingClient())
        self.assertEqual(seen, 2)
        self.assertEqual(errors, [])
        self.assertIn('中国银行', entries[0]['title'])
        self.assertIn('200元', entries[0]['title'])
        self.assertIn('首单减10元', entries[1]['title'])
        self.assertEqual(collector.Page(listing).links, [])

    def test_explicit_list_deadline_is_checked_before_display_truncation(self):
        source = {'id': 'timeline', 'name': 'Timeline', 'region': 'hk', 'timezone': 'Asia/Hong_Kong',
                  'allow_undated': True, 'title_deadline_label': True}
        entry = {'title': '銀行信用卡回贈優惠' + '條件詳見原文' * 50 + '至 2026年11月16日查看詳情',
                 'source_url': 'https://example.com/deal'}
        result = collector.normalize(entry, source, dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertIsNone(result)

    def test_timeline_uses_heading_but_keeps_deadline_context(self):
        listing = '''<a href="/deals/a"><span>信用卡省50%</span>
          <h2>恒生enJoy信用卡加碼迎新優惠</h2><p>簽賬滿HK$100可獲HK$900禮券。</p>
          <span>至 2026年11月16日</span><span>查看詳情</span></a>'''
        source = {'id': 'timeline', 'name': 'Timeline', 'region': 'hk', 'timezone': 'Asia/Hong_Kong',
                  'url': 'https://example.com/', 'link_pattern': '/deals/', 'allow_undated': True,
                  'max_detail_pages': 0, 'use_link_headings': True, 'title_deadline_label': True}
        entries, _, _ = collector.html_entries(listing, source, CountingClient())
        self.assertEqual(entries[0]['title'], '恒生enJoy信用卡加碼迎新優惠')
        self.assertIsNone(collector.normalize(entries[0], source,
            dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19)))

    def test_psbc_explicit_activity_rules_range_filters_old_half_month(self):
        source = {'id': 'psbc', 'name': '邮储全国特惠', 'region': 'cn', 'timezone': 'Asia/Shanghai',
                  'title_activity_range': True, 'max_age_days': 90}
        now = dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc)
        for period, end in [('11月1日-11月15日', None), ('11月16日-11月30日', '2026-11-30')]:
            entry = {'title': f'2026年11月邮储银行信用卡支付宝积分抵现活动（{period}活动规则）',
                     'source_url': 'https://example.com/psbc', 'published_raw': '2026-11-01'}
            result = collector.normalize(entry, source, now, dt.date(2026, 11, 19))
            if end:
                self.assertEqual(result['end_date'], end)
                self.assertFalse(result['verified'])
            else:
                self.assertIsNone(result)

    def test_isolated_run_carries_known_history_by_id(self):
        old = [{'id': 'known', 'source_url': 'https://example.com/old',
                'first_collected_at': '2026-11-18T13:18:14+00:00', 'collected_at': '2026-11-18T13:20:00+00:00'}]
        new = [{'id': 'known', 'source_url': 'https://example.com/new', 'collected_at': '2026-11-18T14:00:00+00:00'},
               {'id': 'new', 'source_url': 'https://example.com/other', 'collected_at': '2026-11-18T14:00:00+00:00'}]
        self.assertEqual(collector.carry_history(new, old), 1)
        self.assertEqual(new[0]['first_collected_at'], '2026-11-18T13:18:14+00:00')
        self.assertEqual(new[1]['first_collected_at'], new[1]['collected_at'])

    def test_generic_card_review_is_not_an_activity(self):
        source = {'id': 'awardwallet', 'name': 'AwardWallet', 'region': 'us', 'timezone': 'America/New_York'}
        entry = {'title': 'Amex Business Gold Review: Rewards, Credits, and Whether It Deserves a Spot in Your Wallet',
                 'source_url': 'https://example.com/review', 'published_raw': '2026-11-16'}
        self.assertIsNone(collector.normalize(entry, source,
            dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19)))

    def test_explicit_summary_expiry_filters_old_offer_and_preserves_extension(self):
        source = {'id': 'rss', 'name': 'RSS', 'region': 'us', 'timezone': 'America/New_York', 'use_excerpt_activity_deadlines': True}
        entry = {'title': 'Hyatt Amex Offer $100 back', 'source_url': 'https://example.com/offer', 'published_raw': '2026-11-17',
                 'source_excerpt': 'Update 7/15/26: Offer valid through 11/15/26.'}
        now = dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc)
        self.assertIsNone(collector.normalize(entry, source, now, dt.date(2026, 11, 19)))
        entry['source_excerpt'] = 'Update 11/17/26: Offer extended through November 30, 2026.'
        record = collector.normalize(entry, source, now, dt.date(2026, 11, 19))
        self.assertEqual(record['end_date'], '2026-11-30')
        self.assertFalse(record['verified'])

    def test_conflicting_bonus_amounts_are_not_republished_as_current_rewards(self):
        source = {'id': 'doc', 'name': 'DoC', 'region': 'us', 'timezone': 'America/New_York'}
        entry = {'title': 'Alliant Credit Union $200 Checking Bonus', 'source_url': 'https://example.com/offer',
                 'published_raw': '2026-11-17', 'source_excerpt': 'Rakuten is offering a bonus of $100 or 10,000 points.'}
        record = collector.normalize(entry, source, dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertTrue(record['source_claim_conflict'])
        self.assertEqual(record['reported_amounts'], [])
        self.assertEqual(record['bank'], 'Alliant Credit Union')

    def test_card_directory_names_do_not_count_as_specific_offers(self):
        source = {'require_specific_offer_title': True}
        self.assertFalse(collector.relevant_title('Mox Credit Card (CashBack)', source))
        self.assertTrue(collector.relevant_title('Mox Credit Card welcome bonus HK$500', source))

    def test_timeline_marketing_badges_are_not_reported_as_reward_percentages(self):
        source = {'id': 'timeline', 'name': 'Timeline', 'region': 'hk', 'timezone': 'Asia/Hong_Kong',
                  'allow_undated': True, 'facts_from_title_only': True, 'mixed_periods_title_pattern': 'PCLO'}
        entry = {'title': '滙豐EveryMile信用卡迎新高達$1,250獎賞錢',
                 'index_context': '信用卡省50%滙豐EveryMile信用卡迎新高達$1,250獎賞錢', 'source_url': 'https://example.com/deal'}
        record = collector.normalize(entry, source, dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertNotIn('50%', record['details'])
        self.assertEqual(record['reported_amounts'], ['$1,250'])
        entry['title'] = '滙豐信用卡 PCLO 精選商戶回贈10%'
        record = collector.normalize(entry, source, dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertEqual(record['reported_amounts'], [])

    def test_failed_source_retains_recent_records_without_refreshing_timestamps(self):
        source = {'id': 'fm', 'name': 'FM', 'region': 'us', 'timezone': 'America/New_York', 'max_age_days': 14}
        previous = [{'id': 'known', 'source_id': 'fm', 'region': 'us', 'title': 'Chase $400 checking bonus',
                     'source_url': 'https://example.com/bonus', 'published_at': '2026-11-17',
                     'collected_at': '2026-11-18T13:18:14+00:00', 'first_collected_at': '2026-11-17T12:00:00+00:00',
                     'verified': False}]
        now = dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc)
        offers = []
        self.assertEqual(collector.retain_failed_sources(offers, previous,
            [{'id': 'fm', 'status': 'error', 'reason': 'network_error'}], [source], now, dt.date(2026, 11, 19)), 1)
        self.assertTrue(offers[0]['retained_after_source_failure'])
        self.assertEqual(offers[0]['collected_at'], previous[0]['collected_at'])
        self.assertEqual(offers[0]['first_collected_at'], previous[0]['first_collected_at'])
        self.assertEqual(collector.carry_history(offers, previous), 0)
        self.assertEqual(collector.retain_failed_sources([], previous,
            [{'id': 'fm', 'status': 'empty'}], [source], now, dt.date(2026, 11, 19)), 0)

    def test_failed_source_does_not_retain_expired_or_stale_records(self):
        source = {'id': 'fm', 'name': 'FM', 'region': 'us', 'timezone': 'America/New_York', 'max_age_days': 14}
        base = {'source_id': 'fm', 'region': 'us', 'title': 'Chase $400 checking bonus', 'source_url': 'https://example.com/bonus',
                'published_at': '2026-11-17', 'collected_at': '2026-11-18T13:18:14+00:00'}
        previous = [{**base, 'id': 'expired', 'end_date': '2026-11-18'},
                    {**base, 'id': 'stale', 'published_at': '2026-10-01'}]
        self.assertEqual(collector.retain_failed_sources([], previous, [{'id': 'fm', 'status': 'blocked'}], [source],
            dt.datetime(2026, 11, 18, 14, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19)), 0)

    def test_psbc_index_date_is_preserved_without_using_url_or_activity_dates(self):
        source = {
            'id': 'psbc', 'name': '邮储全国特惠', 'region': 'cn', 'bank': '邮储银行',
            'source_type': 'official', 'timezone': 'Asia/Shanghai', 'method': 'html',
            'url': 'https://www.psbc.com/cn/grfw/xyk/tyhd/qgthhd/',
            'link_pattern': r'/qgthhd/\d{6}/t\d{8}_\d+\.html$',
            'activity_index': True, 'use_index_dates': True, 'max_detail_pages': 1,
        }
        # Real PSBC listing structure: its URL says 11/12, publication says 11/16,
        # and the title contains a separate activity date range.
        listing = '''<ul><li><span>2026-11-16</span>
        <a href="./202611/t20261112_484071.html">2026年11月邮储银行信用卡支付宝积分抵现活动（11月16日-11月30日活动规则）</a>
        </li><li><span>2026-11-17</span><a href="./202611/t20261117_484072.html">周六5折，限时优惠</a></li></ul>'''
        client = CountingClient()
        entries, seen, errors = collector.html_entries(listing, source, client)
        self.assertEqual(seen, 2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(client.calls, [])
        self.assertEqual(errors, [])
        self.assertEqual(entries[0]['published_raw'], '2026-11-16')
        self.assertEqual(entries[0]['date_evidence'], 'public_index_date')
        record = collector.normalize(entries[0], source,
            dt.datetime(2026, 11, 18, 13, tzinfo=dt.timezone.utc), dt.date(2026, 11, 19))
        self.assertEqual(record['published_at'], '2026-11-16')
        self.assertEqual(record['published_at_precision'], 'date_only')
        self.assertIsNone(record['start_date'])
        self.assertIsNone(record['end_date'])
        self.assertFalse(record['verified'])

    def test_index_date_does_not_leak_across_rows_or_from_activity_title(self):
        source = {'id': 'index', 'name': '公开活动', 'region': 'cn',
                  'url': 'https://example.com/', 'timezone': 'Asia/Shanghai',
                  'link_pattern': r'/offer-', 'use_index_dates': True, 'max_detail_pages': 1}
        listing = '''<li><span>2026-11-16</span><a href="/navigation">帮助</a></li>
        <li><a href="/offer-1">银行信用卡优惠 2026-11-18至2026-11-30</a></li>
        <li><span>活动截止：2026-11-30</span><a href="/offer-2">银行信用卡优惠</a></li>
        <footer>2026-11-18</footer>'''
        client = CountingClient()
        entries, seen, errors = collector.html_entries(listing, source, client)
        self.assertEqual(entries, [])
        self.assertEqual(seen, 2)
        self.assertEqual(len(client.calls), 1)

    def test_bank_index_context_does_not_relax_community_financial_filter(self):
        title = '周六5折，限时优惠'
        official = {'source_type': 'official', 'bank': '邮储银行', 'activity_index': True}
        self.assertTrue(collector.relevant_title(title, official))
        self.assertFalse(collector.relevant_title(title, {'source_type': 'community'}))
        self.assertFalse(collector.relevant_title(title, {'source_type': 'official', 'bank': '邮储银行'}))

    def test_configured_bank_paths_match_observed_article_urls(self):
        path = Path(__file__).resolve().parents[1] / 'bank_daily_setup' / 'sources.json'
        sources = {s['id']: s for s in json.loads(path.read_text())['sources']}
        self.assertEqual(sources['cn_abc']['url'], 'https://www.abchina.com.cn/cn/CreditCard/Special0ffers/All/')
        self.assertRegex('https://www.abchina.com.cn/cn/CreditCard/Special0ffers/All/gwj20261112.htm', sources['cn_abc']['link_pattern'])
        self.assertRegex('https://www.psbc.com/cn/grfw/xyk/tyhd/qgthhd/202611/t20261112_484071.html', sources['cn_psbc_national']['link_pattern'])

    def test_publication_date_is_never_promoted_to_activity_dates(self):
        source = {"id": "test", "name": "Public feed", "region": "us", "timezone": "America/New_York"}
        now = dt.datetime(2026, 11, 18, 13, 30, tzinfo=dt.timezone.utc)
        result = collector.normalize(
            {"title": "Chase $400 checking bonus", "source_url": "https://example.com/article",
             "published_raw": "Wed, 18 Nov 2026 12:00:00 +0000"},
            source, now, dt.date(2026, 11, 19),
        )
        self.assertIsNotNone(result)
        self.assertFalse(result['verified'])
        self.assertIsNone(result['start_date'])
        self.assertIsNone(result['end_date'])
        self.assertEqual(result['published_at'], '2026-11-18T12:00:00+00:00')

    def test_explicit_activity_deadline_filters_the_target_edition(self):
        source = {"id": "test", "name": "Public feed", "region": "cn", "timezone": "Asia/Shanghai"}
        now = dt.datetime(2026, 11, 18, 13, 30, tzinfo=dt.timezone.utc)
        for deadline, accepted in [('2026-11-18', False), ('2026-11-19', True)]:
            with self.subTest(deadline=deadline):
                result = collector.normalize(
                    {"title": f"银行信用卡立减优惠 {deadline}截止", "source_url": "https://example.com/article",
                     "published_raw": "Wed, 18 Nov 2026 12:00:00 +0000"},
                    source, now, dt.date(2026, 11, 19),
                )
                if accepted:
                    self.assertEqual(result['end_date'], deadline)
                    self.assertFalse(result['verified'])
                else:
                    self.assertIsNone(result)

    def test_purchases_does_not_mean_chase_bank(self):
        source = {"id": "test", "name": "Public feed", "region": "us", "timezone": "America/New_York"}
        now = dt.datetime(2026, 11, 18, 13, 30, tzinfo=dt.timezone.utc)
        for title in (
            "Instacart: Free Turkey Up To $40, Requires $10 In Other Purchases [11/18 – 11/22]",
            "United MPX App Restricts Gift Card Purchases To Premier Members & Cardholders",
        ):
            with self.subTest(title=title):
                self.assertNotEqual(collector.bank_name(title, source), "Chase")
                result = collector.normalize(
                    {"title": title, "source_url": "https://example.com/article",
                     "published_raw": "Wed, 18 Nov 2026 12:00:00 +0000"},
                    source, now, dt.date(2026, 11, 19),
                )
                self.assertIsNone(result)
        self.assertEqual(collector.bank_name("Chase $400 checking bonus", source), "Chase")
        self.assertEqual(collector.bank_name("Citi credit card offer", source), "Citi")
        self.assertNotEqual(collector.bank_name("Rewards in participating cities", source), "Citi")


if __name__ == "__main__":
    unittest.main()
