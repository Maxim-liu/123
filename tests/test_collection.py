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
