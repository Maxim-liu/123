import datetime as dt
import unittest
from bs4 import BeautifulSoup
from bank_daily_setup.generate_report import table_html, all_section, display_activity_time

class TimeViewsTests(unittest.TestCase):
    def setUp(self):
        self.day = dt.date(2026, 11, 19)
        self.items = [
            {'region':'cn','bank':'测试银行','title':'晚间活动','source_url':'https://example.org/a','time':'20:00-21:00','time_note':'每天限量，需报名','end_date':'2026-11-25'},
            {'region':'cn','bank':'测试银行','title':'上午活动','source_url':'https://example.org/b','time':'09:30','end_date':'2026-11-24'},
            {'region':'cn','bank':'其他银行','title':'未注明时间','source_url':'https://example.org/c','end_date':'2026-11-26'},
        ]
    def test_time_only(self):
        self.assertEqual(display_activity_time(self.items[0]), '20:00–21:00')
        self.assertEqual(display_activity_time(self.items[2]), '未注明')
        soup=BeautifulSoup(table_html(self.items,mode='today',region='cn'),'html.parser')
        self.assertNotIn('每天限量',soup.get_text())
        self.assertEqual(soup.select_one('tbody tr td').get_text(strip=True),'20:00–21:00')
    def test_both_summaries_and_grouping(self):
        soup=BeautifulSoup(all_section('cn','中国大陆',self.items,self.day),'html.parser')
        self.assertIsNotNone(soup.select_one('#all'))
        self.assertIsNotNone(soup.select_one('#all-by-time'))
        self.assertEqual(len(soup.select('#all thead th')),7)
        labels=[x.get_text(strip=True) for x in soup.select('#all-by-time .time-group-heading')]
        self.assertEqual(labels,['上午（00:00–11:59）','晚上（18:00–23:59）','未注明具体时间'])
        self.assertEqual(len(soup.select('#all-by-time tbody tr[data-offer-id]')),3)
    def test_hk_us_time_column(self):
        for region in ('hk','us'):
            item=dict(self.items[0],region=region)
            soup=BeautifulSoup(table_html([item],region=region),'html.parser')
            self.assertEqual(len(soup.select('thead th')),7)
            self.assertEqual(soup.select('thead th')[4].get_text(strip=True),'时间')

if __name__=='__main__': unittest.main()


class InternationalLayoutTests(unittest.TestCase):
    def test_region_columns_match_mainland_order(self):
        for region in ('cn','hk','us'):
            item={'region':region,'bank':'银行','title':'活动','reward':'奖励','time':'10:00','end_date':'2026-11-30','source_url':'https://example.org'}
            soup=BeautifulSoup(table_html([item],region=region),'html.parser')
            labels=[x.get_text(strip=True) for x in soup.select('thead th')]
            self.assertEqual(labels.index('时间'),4)
            self.assertEqual(len(labels),7)
            self.assertEqual(labels.index('类型'),2)


class ActivityEntryLinkTests(unittest.TestCase):
    def test_direct_entry_and_official_source_are_distinct(self):
        item={'region':'cn','bank':'银行','title':'测试活动','source_url':'https://example.org/terms','entry_url':'https://example.org/join','time':'10:00'}
        soup=BeautifulSoup(table_html([item],region='cn'),'html.parser')
        self.assertEqual(soup.select_one('.activity-entry')['href'],'https://example.org/join')
        self.assertEqual(soup.select_one('.source-link')['href'],'https://example.org/terms')
    def test_source_only_is_not_misrepresented_as_registration(self):
        item={'region':'us','bank':'银行','title':'测试活动','source_url':'https://example.org/terms'}
        soup=BeautifulSoup(table_html([item],region='us'),'html.parser')
        self.assertFalse(soup.select('.activity-entry'))
        self.assertEqual(soup.select_one('.source-link').get_text(strip=True),'查看活动详情 ↗')
    def test_unsafe_entry_url_is_rejected(self):
        item={'region':'hk','bank':'银行','title':'测试活动','source_url':'https://example.org/terms','entry_url':'javascript:alert(1)'}
        soup=BeautifulSoup(table_html([item],region='hk'),'html.parser')
        self.assertFalse(soup.select('.activity-entry'))
