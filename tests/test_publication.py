"""Regression checks for the broken date -> generation -> publication boundary."""
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from bank_daily_setup.generate_report import build_report, report_date_for
from bank_daily_setup.publish_report import publish_report

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'bank_daily_setup' / 'report_template.html'
DAY = dt.date(2026, 11, 19)
NOW = dt.datetime.fromisoformat('2026-11-19T05:00:00+08:00')


class PublicationTests(unittest.TestCase):
    def test_reviewed_duplicate_keeps_sources_but_changed_offer_remains_visible(self):
        canonical = self.sample(id='canonical', title='Reviewed transfer offer',
            source_url='https://example.com/reviewed',
            related_leads=[{'id': 'blog', 'title': '40% transfer bonus'}])
        self.write(self.curated, [canonical])
        self.seed([self.sample(id='blog', title='40% transfer bonus',
            source_url='https://blog.example/bonus', origin='Community blog')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        metadata = json.loads(page.select_one('#report-metadata').get_text())
        self.assertEqual(metadata['collected_count'], 1)
        self.assertEqual(metadata['combined_lead_count'], 1)
        self.assertIsNotNone(page.select_one('#us-collected a[href="https://blog.example/bonus"]'))
        self.seed([self.sample(id='blog', title='50% transfer bonus',
            source_url='https://blog.example/bonus', origin='Community blog')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        metadata = json.loads(page.select_one('#report-metadata').get_text())
        self.assertEqual(metadata['collected_count'], 2)
        self.assertIn('50% transfer bonus', page.select_one('#us-collected').get_text())

    def test_manual_community_leads_expire_without_inventing_campaign_dates(self):
        self.write(self.curated, [self.sample(title='Expired community review', review_expires_on='2026-11-18'),
                                  self.sample(title='Recent community lead', review_expires_on='2026-11-30')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        leads = page.select_one('#us-collected').get_text()
        self.assertNotIn('Expired community review', leads)
        self.assertIn('Recent community lead', leads)
        self.assertIn('活动日期待确认', leads)
        self.assertNotIn('2026-11-30', leads)

    def test_reward_amount_survives_compact_and_full_report_sections(self):
        self.write(self.curated, [self.sample(region='cn', title='京东支付优惠',
            reward='满800减12，每月1次', details='须选择指定银行卡支付，每日限量。',
            verified=True, start_date='2026-11-01', end_date='2026-11-30', time='00:00')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        for section in ('#today', '#cn-verified'):
            with self.subTest(section=section):
                text = page.select_one(section).get_text()
                self.assertIn('满800减12，每月1次', text)
                self.assertIn('指定银行卡', text)

    def test_official_feed_headline_does_not_duplicate_reviewed_article(self):
        self.write(self.curated, [self.sample(title='Reviewed offer terms', verified=True,
                                             start_date='2026-11-16', end_date='2026-11-30')])
        self.seed([self.sample(title='Original bank headline with different wording',
                               source_type='official', source_id='bank_promotion_feed')])
        report = self.build()
        page = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        metadata = json.loads(page.select_one('#report-metadata').get_text())
        self.assertEqual(metadata['verified_count'], 1)
        self.assertEqual(metadata['collected_count'], 0)
        self.assertIn('Reviewed offer terms', page.select_one('#us-verified').get_text())
        self.assertNotIn('Original bank headline', page.select_one('#us-collected').get_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.site = self.base / 'site'
        self.site.mkdir()
        (self.site / 'index.html').write_text(TEMPLATE.read_text(encoding='utf-8'), encoding='utf-8')
        self.curated = self.base / 'verified.json'
        self.collected = self.base / 'collected.json'
        self.status = self.base / 'status.json'
        self.write(self.curated, [])
        self.write(self.collected, {})
        self.write(self.status, {})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def build(self, now=NOW):
        return build_report(DAY, offers_path=self.curated, collected_path=self.collected,
                            status_path=self.status, template_path=TEMPLATE,
                            output_dir=self.base / 'archive', now=now)

    def sample(self, **overrides):
        value = {'id': 'sample', 'region': 'us', 'bank': 'Example Bank', 'title': 'Sample lead',
                 'details': 'Confirm conditions at the source.', 'source_url': 'https://example.com/offer',
                 'verified': False, 'start_date': None, 'end_date': None,
                 'collected_at': '2026-11-18T21:05:00+08:00'}
        value.update(overrides)
        return value

    def seed(self, offers, status='ok'):
        self.write(self.collected, {'report_date': DAY.isoformat(), 'status': status,
                                    'collected_at': '2026-11-18T21:05:00+08:00', 'offers': offers})
        self.write(self.status, {'report_date': DAY.isoformat(), 'status': status,
                                 'attempted_at': '2026-11-18T21:00:00+08:00'})

    def test_beijing_cutoff_and_delayed_retry_use_one_edition(self):
        cases = {
            '2026-11-18T12:59:59+00:00': '2026-11-18',
            '2026-11-18T13:00:00+00:00': '2026-11-19',
            '2026-11-18T21:00:00+00:00': '2026-11-19',
            '2026-11-19T12:59:59+00:00': '2026-11-19',
            '2026-12-31T13:00:00+00:00': '2027-01-01',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(report_date_for(dt.datetime.fromisoformat(value)).isoformat(), expected)

    def test_unverified_and_undated_items_are_not_presented_as_verified(self):
        self.seed([self.sample(publication_note='Original publication date is unknown; not necessarily a new post.'),
                   self.sample(title='No dates but claimed verified', verified=True),
                   self.sample(title='Expired', start_date='2026-10-01', end_date='2026-11-18'),
                   self.sample(title='Unsafe source', source_url='javascript:alert(1)')])
        self.write(self.curated, [self.sample(title='Confirmed current', verified=True,
                                             start_date='2026-11-18', end_date='2026-11-20')])
        soup = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        checked = soup.select_one('#us-verified').get_text()
        leads = soup.select_one('#us-collected').get_text()
        self.assertIn('Confirmed current', checked)
        self.assertNotIn('No dates but claimed verified', checked)
        self.assertIn('No dates but claimed verified', leads)
        self.assertIn('Sample lead', leads)
        self.assertIn('not necessarily a new post', leads)
        self.assertNotIn('Expired', leads)
        self.assertNotIn('Unsafe source', leads)

    def test_empty_collection_cannot_claim_fresh_data(self):
        self.seed([], 'empty')
        report = self.build()
        metadata = publish_report(report, self.site)
        self.assertIsNone(metadata['collected_at'])
        self.assertEqual(metadata['verified_count'], 0)
        self.assertEqual(metadata['collected_count'], 0)
        self.assertEqual(metadata['source_status'], 'empty')
        soup = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('暂无可确认的新活动', soup.select_one('#data-freshness').get_text())
        self.assertNotIn('更新于', soup.select_one('#data-freshness').get_text())

    def test_failed_attempt_preserves_actual_data_timestamp(self):
        self.seed([self.sample()])
        self.write(self.status, {'status': 'failed', 'report_date': DAY.isoformat(),
                                 'attempted_at': '2026-11-19T04:59:00+08:00'})
        metadata = publish_report(self.build(), self.site)
        self.assertEqual(metadata['collected_at'], '2026-11-18T21:05:00+08:00')
        self.assertEqual(metadata['source_status'], 'failed')
        soup = BeautifulSoup((self.site / 'index.html').read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('2026-11-18 21:05', soup.select_one('#data-freshness').get_text())
        self.assertNotIn('2026-11-19 04:59', soup.select_one('#data-freshness').get_text())

    def test_current_page_quotes_have_explicit_unknown_deadline_label(self):
        self.write(self.curated, [self.sample(title='Current official page', verified=True,
                                             collected_at=None, verification_scope='current_page',
                                             verified_at='2026-11-18T22:15:00+08:00')])
        report = self.build()
        metadata = publish_report(report, self.site)
        page = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('Current official page', page.select_one('#us-verified').get_text())
        self.assertIn('官网当前报价，截止未公布', page.select_one('#us-verified').get_text())
        self.assertNotIn('Current official page', page.select_one('#us-collected').get_text())
        self.assertEqual(metadata['current_page_count'], 1)
        self.assertEqual(metadata['collected_at'], '2026-11-18T22:15:00+08:00')

    def test_new_items_are_sorted_by_bank_and_date(self):
        self.seed([self.sample(bank='B Bank', title='B'),
                   self.sample(bank='A Bank', title='A later', end_date='2026-12-31'),
                   self.sample(bank='A Bank', title='A earlier', end_date='2026-11-30')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        rows = [(row.select_one('.bank').get_text(), row.select_one('.offer-title').get_text())
                for row in page.select('#us-collected tbody tr')]
        self.assertEqual(rows, [('A Bank', 'A earlier'), ('A Bank', 'A later'), ('B Bank', 'B')])

    def test_same_day_changed_content_changes_revision_and_identical_retry_does_not(self):
        self.seed([self.sample()])
        first = publish_report(self.build(), self.site)
        first_page = (self.site / 'index.html').read_bytes()
        retry = publish_report(self.build(now=NOW + dt.timedelta(hours=1)), self.site)
        self.assertEqual(first, retry)
        self.assertEqual(first_page, (self.site / 'index.html').read_bytes())
        self.seed([self.sample(details='The source corrected the reward amount.')])
        changed = publish_report(self.build(now=NOW + dt.timedelta(hours=2)), self.site)
        self.assertEqual(first['date'], changed['date'])
        self.assertNotEqual(first['revision'], changed['revision'])
        page = BeautifulSoup((self.site / 'index.html').read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(page.select_one('meta[name="report-revision"]')['content'], changed['revision'])
        self.assertIn("const REV='" + changed['revision'] + "'", page.select_one('#pwa-app-script').get_text())

    def test_regions_are_rebuilt_and_historical_rows_are_removed(self):
        original = BeautifulSoup(TEMPLATE.read_text(encoding='utf-8'), 'html.parser')
        for region in ('cn', 'hk', 'us'):
            original.select_one('#region-' + region).append(BeautifulSoup(
                '<p>OLD_EDITION_18</p><table><tr><td>周三旧活动</td></tr></table>', 'html.parser'))
        legacy_template = self.base / 'legacy-template.html'
        legacy_template.write_text(str(original), encoding='utf-8')
        report = build_report(DAY, offers_path=self.curated, collected_path=self.collected,
                              status_path=self.status, template_path=legacy_template,
                              output_dir=self.base / 'archive', now=NOW)
        publish_report(report, self.site)
        published = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        for region in ('cn', 'hk', 'us'):
            selector = '#region-' + region
            self.assertEqual(len(published.select(selector)), 1)
            self.assertNotIn('OLD_EDITION_18', published.select_one(selector).get_text())
            self.assertNotIn('周三旧活动', published.select_one(selector).get_text())
        for selector in ('#today', '#all', '#miles', '#deadlines', '#hk-all', '#us-all',
                         '#hk-today', '#us-today', '#hk-miles', '#us-miles', '#hk-deadlines',
                         '#us-deadlines', '#us-records', '#us-business', '#pwa-app-script', '#app-update-banner'):
            self.assertEqual(len(published.select(selector)), 1, selector)
        self.assertEqual(report.read_bytes(), (self.site / 'index.html').read_bytes())

    def test_thursday_edition_excludes_wednesday_monthly_18_and_expired_offers(self):
        common = {'verified': True, 'start_date': '2026-11-01', 'end_date': '2026-11-30'}
        items = [self.sample(title='Wednesday only', weekdays=[3], **common),
                 self.sample(title='Monthly 18 only', month_days=[18], **common),
                 self.sample(title='One day 18 only', verified=True, start_date='2026-11-18', end_date='2026-11-18'),
                 self.sample(title='Expired offer', verified=True, start_date='2026-11-01', end_date='2026-11-17'),
                 self.sample(title='Thursday afternoon', weekdays=[4], time='14:00', **common),
                 self.sample(title='Monthly 19 morning', month_days=[19], time='08:30', **common)]
        self.write(self.curated, items)
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        for title in ['Wednesday only', 'Monthly 18 only', 'One day 18 only', 'Expired offer']:
            self.assertNotIn(title, page.get_text())
        self.assertEqual([row.get_text() for row in page.select('#us-today .offer-title')],
                         ['Monthly 19 morning', 'Thursday afternoon'])
        self.assertIn('周四', page.select_one('header .sub').get_text())

    def test_original_onepay_wednesday_community_lead_is_not_a_thursday_offer(self):
        title = ('[Extended 1/15/27] OnePay: $1 Cashback Per Gallon Of Gas Every Wednesday '
                 '(50 Gallons, Two Redemptions Per Day)')
        self.seed([self.sample(title=title, bank='多家机构 / 原文为准', source_type='community',
                               source_url='https://www.doctorofcredit.com/onepay-1-cashback-per-gallon-of-gas-every-wednesday-50-gallons-two-redemptions-per-day/',
                               published_at='2026-11-18T05:01:18+00:00')])
        thursday = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertNotIn(title, thursday.get_text())
        wednesday_report = build_report(dt.date(2026, 11, 18), offers_path=self.curated,
                                        collected_path=self.collected, status_path=self.status,
                                        template_path=TEMPLATE, output_dir=self.base / 'wednesday', now=NOW)
        wednesday = BeautifulSoup(wednesday_report.read_text(encoding='utf-8'), 'html.parser')
        self.assertIn(title, wednesday.select_one('#us-collected').get_text())
        self.assertNotIn(title, wednesday.select_one('#us-all').get_text())

    def test_only_explicit_title_recurrences_filter_community_leads(self):
        excluded = ['Each Monday cashback', '每周三返现活动', '每星期日积分活动', '每月18日银行优惠']
        retained = ['Each Thursday cashback', '每星期四银行优惠', '每月19号积分活动',
                    'Wednesday 11/18 bank offer news', '11月18日公布银行优惠',
                    'Every Monday and Thursday cashback', '每周三、四返现',
                    'Every Monday-Friday cashback', '每月18日至20日返现']
        self.seed([self.sample(title=title) for title in excluded + retained]
                  + [self.sample(title='Every Wednesday explicit Thursday schedule', weekdays=[4]),
                     self.sample(title='每月18日标题已更正为19日', month_days=[19])])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        content = page.select_one('#us-collected').get_text()
        for title in excluded:
            self.assertNotIn(title, content)
        for title in retained + ['Every Wednesday explicit Thursday schedule', '每月18日标题已更正为19日']:
            self.assertIn(title, content)

    def test_previous_one_time_release_is_not_a_new_daily_drop(self):
        self.write(self.curated, [self.sample(title='Remaining quota', verified=True,
                                             start_date='2026-11-18', end_date='2026-11-30',
                                             release_date='2026-11-18', time='10:00', highlight='rare')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('Remaining quota', page.select_one('#us-all').get_text())
        self.assertNotIn('Remaining quota', page.select_one('#us-today').get_text())

    def test_undated_official_quotes_expire_after_two_days(self):
        self.write(self.curated, [self.sample(title='Fresh quote', verified=True,
                                             verification_scope='current_page', verified_at='2026-11-17T23:00:00+08:00'),
                                  self.sample(title='Stale quote', verified=True,
                                             verification_scope='current_page', verified_at='2026-11-16T23:00:00+08:00')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('Fresh quote', page.select_one('#us-all').get_text())
        self.assertNotIn('Stale quote', page.select_one('#us-all').get_text())
        self.assertIn('Stale quote', page.select_one('#us-collected').get_text())

    def test_partial_current_page_dates_need_recent_confirmation(self):
        common = {'verified': True, 'verification_scope': 'current_page',
                  'verified_at': '2026-11-16T23:00:00+08:00'}
        self.write(self.curated, [self.sample(title='Only end date', end_date='2026-11-30', **common),
                                  self.sample(title='Only start date', start_date='2026-11-01', **common),
                                  self.sample(title='Complete official period', start_date='2026-11-01',
                                              end_date='2026-11-30', **common),
                                  self.sample(title='Fresh partial date', end_date='2026-11-30',
                                              verified=True, verification_scope='current_page',
                                              verified_at='2026-11-18T23:00:00+08:00')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        for title in ['Only end date', 'Only start date']:
            self.assertNotIn(title, page.select_one('#us-all').get_text())
            self.assertIn(title, page.select_one('#us-collected').get_text())
        for title in ['Complete official period', 'Fresh partial date']:
            self.assertIn(title, page.select_one('#us-all').get_text())

    def test_display_codes_and_historical_source_are_readable(self):
        labels = {'welcome': '迎新奖励', 'business': '商业卡', 'transfer': '转点加赠',
                  'payment': '支付优惠', 'deposit': '存款与开户', 'benefit': '卡片权益', 'points': '积分活动'}
        items = [self.sample(title='Offer ' + str(index), category=code, verified=True,
                             start_date='2026-11-01', end_date='2026-11-30')
                 for index, code in enumerate(labels)]
        items[0].update(record_status='recent_high', comparison='近期奖励高于常规报价。',
                        history_source_url='https://example.com/reward-history')
        items[1].update(record_status='not_confirmed', comparison={'summary': '尚未证实历史最高。',
                        'history_source_url': 'https://example.com/business-history'})
        self.write(self.curated, items)
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        for code, label in labels.items():
            self.assertIn(label, page.select_one('#us-all').get_text())
            self.assertNotIn(code, page.select_one('#us-all').get_text())
        self.assertIn('近期高位', page.select_one('#us-records').get_text())
        self.assertIn('未证实史高', page.select_one('#us-records').get_text())
        self.assertNotIn('recent_high', page.select_one('#us-records').get_text())
        self.assertNotIn('not_confirmed', page.select_one('#us-records').get_text())
        self.assertEqual(len(page.select('#us-records a[href="https://example.com/reward-history"]')), 1)
        self.assertEqual(len(page.select('#us-records a[href="https://example.com/business-history"]')), 1)
        self.assertIn('Offer 1', page.select_one('#us-business').get_text())
        self.assertIn('Offer 2', page.select_one('#us-miles').get_text())

    def test_highlight_expiry_removes_emphasis_but_keeps_valid_offer(self):
        common = {'verified': True, 'start_date': '2026-11-15', 'end_date': '2026-12-31'}
        self.write(self.curated, [self.sample(title='Old rare spotlight', highlight='rare', highlight_until='2026-11-18', **common),
                                  self.sample(title='Last day rare spotlight', highlight='rare', highlight_until='2026-11-19', **common),
                                  self.sample(title='Active value spotlight', highlight='value', highlight_until='2026-11-22', **common)])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('Old rare spotlight', page.select_one('#us-all').get_text())
        self.assertNotIn('Old rare spotlight', page.select_one('#us-rare').get_text())
        self.assertNotIn('Old rare spotlight', page.select_one('#us-today').get_text())
        self.assertIn('Last day rare spotlight', page.select_one('#us-rare').get_text())
        self.assertIn('Last day rare spotlight', page.select_one('#us-today').get_text())
        self.assertIn('Active value spotlight', page.select_one('#us-value').get_text())

    def test_credit_card_sections_exclude_deposits_and_transfer_bonuses(self):
        common = {'verified': True, 'start_date': '2026-11-01', 'end_date': '2026-12-31',
                  'comparison': '奖励对比说明。', 'record_status': 'not_confirmed'}
        examples = [('Marriott Bevy', 'welcome'), ('Ink Business Unlimited', 'business'),
                    ('Ink Business Preferred', '商业卡'), ('Business Checking', 'deposit'),
                    ('银行开户奖励', '银行开户'), ('LifeMiles加赠', 'transfer'),
                    ('里程转点奖励', '转点加赠')]
        self.write(self.curated, [self.sample(title=title, category=kind, **common) for title, kind in examples])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual([item.get_text() for item in page.select('#us-records .offer-title')],
                         ['Ink Business Preferred', 'Ink Business Unlimited', 'Marriott Bevy'])
        self.assertEqual([item.get_text() for item in page.select('#us-business .offer-title')],
                         ['Ink Business Preferred', 'Ink Business Unlimited'])
        self.assertEqual(len(page.select('#us-all tbody tr')), len(examples))

    def test_stats_deduplicate_and_deadlines_use_activity_dates(self):
        common = {'verified': True, 'start_date': '2026-11-01'}
        late = self.sample(title='Later deadline', end_date='2026-11-30', highlight='value', **common)
        early = self.sample(title='Earlier deadline', end_date='2026-11-20', published_at='2026-12-30', **common)
        self.write(self.curated, [late, early])
        self.seed([dict(late, source_url='https://example.com/duplicate')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        metadata = json.loads(page.select_one('#report-metadata').get_text())
        self.assertEqual(metadata['verified_count'], 2)
        self.assertEqual(metadata['collected_count'], 0)
        self.assertEqual(page.select_one('#region-us .region-kpis b').get_text(), '2')
        self.assertEqual([item.get_text() for item in page.select('#us-deadlines .offer-title')],
                         ['Earlier deadline', 'Later deadline'])
        self.assertEqual(len(page.select('#us-all tbody tr')), 2)

    def test_tables_keep_semantic_headers_and_horizontal_mobile_layout(self):
        self.seed([self.sample()])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertTrue(page.select('#us-collected thead th[scope="col"]'))
        self.assertTrue(all(cell.get('data-label') for cell in page.select('tbody td')))
        css = page.select_one('#report-style').get_text()
        self.assertIn('.table-scroll{overflow-x:auto', css)
        self.assertNotIn('td::before', css)
        self.assertNotIn('tbody tr{display:block', css)
        self.assertIn('data-offer-filter', page.select_one('#report-ui-script').get_text())

    def test_approved_table_columns_and_region_structure_are_restored(self):
        common = {'verified': True, 'start_date': '2026-11-01', 'end_date': '2026-11-30',
                  'time': '10:00', 'category': 'points'}
        self.write(self.curated, [self.sample(region=region, title=region + '积分活动', **common)
                                  for region in ('cn', 'hk', 'us')]
                   + [self.sample(title='US Business Card', verified=True, category='business',
                                  start_date='2026-11-01', end_date='2026-11-30', comparison='历史奖励对比。')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        expected = {
            '#today': ['时间', '银行', '活动', '要点'],
            '#all': ['银行', '活动', '类型', '要点（门槛 · 频次）', '时间', '入口', '截止'],
            '#miles': ['主体', '动态', '要点', '日期'],
            '#deadlines': ['截止日期', '涉及活动'],
            '#hk-all': ['银行 / 平台', '活动', '类型', '奖励及条件', '时间', '入口', '截止日期'],
            '#us-all': ['银行 / 平台', '活动', '类型', '奖励及条件', '时间', '入口', '截止日期'],
            '#us-records': ['银行', '卡种', '奖励及条件', '史高判定', '适用性', '详情'],
        }
        for selector, headers in expected.items():
            with self.subTest(selector=selector):
                table = page.select_one(selector + ' table')
                self.assertEqual([cell.get_text() for cell in table.select('thead th')], headers)
        self.assertEqual(len(page.select('#region-cn > .stats .stat')), 4)
        self.assertEqual(len(page.select('#region-hk > .region-hero .region-kpis > div')), 3)
        self.assertEqual(len(page.select('#region-us > .region-hero .region-kpis > div')), 3)
        self.assertIsNotNone(page.select_one('#all #deadlineFilter'))
        self.assertIsNotNone(page.select_one('#all #resetFilters'))
        sections = [node.get('id') for node in page.select('#region-us > section')]
        self.assertLess(sections.index('us-records'), sections.index('us-all'))
        self.assertLess(sections.index('us-business'), sections.index('us-all'))
        self.assertEqual(len(page.select('.wrap > .quick-nav')), 1)
        self.assertFalse(page.select('.region-panel .quick-nav'))
        approved = BeautifulSoup((ROOT / 'archive' / '2026-11-18.html').read_text(encoding='utf-8'), 'html.parser')
        approved_css = '\n'.join(style.get_text() for style in approved.select('style')[:6])
        self.assertTrue(page.select_one('#report-style').get_text().startswith(approved_css))

    def test_brief_sections_do_not_duplicate_full_activity_terms(self):
        detail = '开户奖励需完成首笔合资格交易。后续完整条件只在全部活动中显示。'
        self.write(self.curated, [self.sample(region='cn', title='精简专题活动', verified=True,
                                             category='points', details=detail, highlight='value', time='10:00',
                                             start_date='2026-11-01', end_date='2026-11-30')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(page.get_text().count('后续完整条件只在全部活动中显示。'), 1)
        row = page.select_one('#all tbody tr')
        self.assertTrue(row.get('id', '').startswith('offer-cn-'))
        self.assertEqual(page.select_one('#today .offer-title')['href'], '#' + row['id'])

    def test_new_report_style_does_not_inherit_old_mobile_overrides(self):
        current = BeautifulSoup((self.site / 'index.html').read_text(), 'html.parser')
        old_style = current.new_tag('style', id='pwa-mobile-style')
        old_style.string = '.table-scroll{overflow-x:auto!important}.app-update-banner{display:block!important}'
        current.head.append(old_style)
        (self.site / 'index.html').write_text(str(current), encoding='utf-8')
        publish_report(self.build(), self.site)
        page = BeautifulSoup((self.site / 'index.html').read_text(), 'html.parser')
        self.assertIsNotNone(page.select_one('style#report-style'))
        self.assertIsNone(page.select_one('style#pwa-mobile-style'))

    def test_legacy_report_still_inherits_mobile_style(self):
        current = BeautifulSoup((self.site / 'index.html').read_text(), 'html.parser')
        old_style = current.new_tag('style', id='pwa-mobile-style')
        old_style.string = '.legacy-mobile{max-width:100%}'
        current.head.append(old_style)
        (self.site / 'index.html').write_text(str(current), encoding='utf-8')
        report = self.build()
        page = BeautifulSoup(report.read_text(), 'html.parser')
        page.select_one('style#report-style').decompose()
        report.write_text(str(page), encoding='utf-8')
        publish_report(report, self.site)
        published = BeautifulSoup((self.site / 'index.html').read_text(), 'html.parser')
        self.assertEqual(published.select_one('style#pwa-mobile-style').get_text(), '.legacy-mobile{max-width:100%}')

    def test_invalid_report_never_replaces_live_files(self):
        self.write(self.site / 'latest.json', {'date': '2026-11-18'})
        before = [(self.site / file).read_bytes() for file in ('index.html', 'latest.json')]
        bad = self.base / 'report-2026-11-19.html'
        bad.write_text('<html><head><title>Bad</title></head><body>Missing all regions</body></html>')
        with self.assertRaises(ValueError):
            publish_report(bad, self.site)
        self.assertEqual(before, [(self.site / file).read_bytes() for file in ('index.html', 'latest.json')])

    def test_generator_cli_publishes_directly_from_any_working_directory(self):
        self.seed([self.sample()])
        result = subprocess.run([sys.executable, str(ROOT / 'bank_daily_setup' / 'generate_report.py'),
                                 '--date', DAY.isoformat(), '--offers', str(self.curated),
                                 '--collected', str(self.collected), '--status', str(self.status),
                                 '--output-dir', str(self.base / 'cli-archive'), '--site-dir', str(self.site)],
                                cwd=self.base, check=True, capture_output=True, text=True)
        self.assertIn('Published 2026-11-19 revision', result.stdout)
        latest = json.loads((self.site / 'latest.json').read_text())
        self.assertEqual(latest['report_date'], '2026-11-19')
        self.assertEqual(latest['collected_count'], 1)


if __name__ == '__main__':
    unittest.main()
