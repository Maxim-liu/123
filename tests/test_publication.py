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

    def test_current_page_confirmation_is_not_confirmation_of_the_target_day(self):
        self.write(self.curated, [self.sample(title='Current official page', verified=True,
                                             collected_at=None, verification_scope='current_page',
                                             verified_at='2026-11-18T22:15:00+08:00')])
        report = self.build()
        metadata = publish_report(report, self.site)
        page = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        self.assertNotIn('Current official page', page.select_one('#us-verified').get_text())
        self.assertIn('本期活动时间及个人资格待确认', page.select_one('#us-collected').get_text())
        self.assertEqual(metadata['collected_at'], '2026-11-18T22:15:00+08:00')

    def test_new_items_are_sorted_by_bank_and_date(self):
        self.seed([self.sample(bank='B Bank', title='B'),
                   self.sample(bank='A Bank', title='A later', end_date='2026-12-31'),
                   self.sample(bank='A Bank', title='A earlier', end_date='2026-11-30')])
        page = BeautifulSoup(self.build().read_text(encoding='utf-8'), 'html.parser')
        titles = [item.get_text() for item in page.select('#us-collected h3')]
        self.assertEqual(titles, ['A Bank · A earlier', 'A Bank · A later', 'B Bank · B'])

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

    def test_all_historical_regions_and_sections_are_preserved(self):
        report = self.build()
        publish_report(report, self.site)
        original = BeautifulSoup(TEMPLATE.read_text(encoding='utf-8'), 'html.parser')
        published = BeautifulSoup(report.read_text(encoding='utf-8'), 'html.parser')
        for region in ('cn', 'hk', 'us'):
            selector = '#region-' + region
            self.assertEqual(len(published.select(selector)), 1)
            self.assertEqual(len(original.select(selector + ' tr')), len(published.select(selector + ' tr')))
        for selector in ('#today', '#all', '#miles', '#deadlines', '#hk-all', '#us-all',
                         '#pwa-app-script', '#app-update-banner'):
            self.assertEqual(len(published.select(selector)), 1, selector)
        self.assertEqual(report.read_bytes(), (self.site / 'index.html').read_bytes())

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
