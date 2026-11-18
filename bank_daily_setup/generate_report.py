#!/usr/bin/env python3
"""Create a dated report without misrepresenting historical offers as fresh."""
import argparse
import datetime as dt
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='YYYY-MM-DD (default: tomorrow in Asia/Shanghai)')
    ap.add_argument('--offers', default='verified_offers.json', help='curated verified offers JSON')
    args = ap.parse_args()
    if args.date:
        day = dt.date.fromisoformat(args.date)
    else:
        from zoneinfo import ZoneInfo
        day = dt.datetime.now(ZoneInfo('Asia/Shanghai')).date() + dt.timedelta(days=1)
    source = BASE / 'report_template.html'
    soup = BeautifulSoup(source.read_text(encoding='utf-8'), 'html.parser')
    original_date = '2026-11-18'
    iso = day.isoformat()
    soup.title.string = f'银行每日线报 · {iso}'
    header = soup.select_one('header.top')
    header.select_one('h1').string = f'银行每日线报 · {iso} · 全球三区'
    sub = header.select_one('.sub')
    if sub: sub.string = f'{iso} · 中国大陆 / 中国香港 / 美国 · 历史快照与已核实新活动分开展示'
    for el in header.select('.chips, .notice-strong'):
        el.decompose()
    notice = soup.new_tag('p', attrs={'class':'notice-strong'})
    notice.string = (f'本期日期：{iso}（北京时间）。以下原有活动均来自 {original_date} 历史快照，'
                     '并非今日实时确认；不得据此认定仍可参加。新增已核实活动见各地区顶部。')
    header.append(notice)
    offers_path = BASE / args.offers
    offers = json.loads(offers_path.read_text(encoding='utf-8')) if offers_path.exists() else []
    if not isinstance(offers, list): raise ValueError('offers must be a JSON array')
    for region, name in [('cn','中国大陆'),('hk','中国香港'),('us','美国')]:
        panel = soup.select_one(f'#region-{region}')
        if panel is None: raise RuntimeError(f'missing panel {region}')
        verified = [x for x in offers if x.get('region') == region and x.get('verified') is True
                    and x.get('source_url','').startswith(('https://','http://'))
                    and x.get('start_date','0000-01-01') <= iso <= x.get('end_date','9999-12-31')]
        section = soup.new_tag('section', attrs={'class':'card','id':f'{region}-verified'})
        h2=soup.new_tag('h2');h2.string=f'📌 {name} · {iso} 已核实活动';section.append(h2)
        if not verified:
            p=soup.new_tag('p', attrs={'class':'region-note'})
            p.string='暂无完成来源核验的当日新活动。历史记录仅供检索，不代表活动仍有效。'
            section.append(p)
        else:
            for offer in verified:
                article=soup.new_tag('article',attrs={'class':'offer-item'})
                h3=soup.new_tag('h3');h3.string=f"{offer.get('bank','')} · {offer.get('title','')}";article.append(h3)
                p=soup.new_tag('p');p.string=offer.get('details','');article.append(p)
                a=soup.new_tag('a',href=offer['source_url'],target='_blank',rel='noopener noreferrer')
                a.string='查看核验来源';article.append(a)
                section.append(article)
        panel.insert(0,section)
        # Old spotlight, "today", deadline tables and counts are retained but visibly historical.
        warning=soup.new_tag('div',attrs={'class':'snapshot-notice'})
        warning.string=f'以下为 {original_date} 历史存档，不是 {iso} 的可参加活动清单。'
        section.insert_after(warning)
    # Remove misleading time-of-day indicators tied to the original date.
    for span in soup.select('.time-status'):
        span.decompose()
    # Keep search/filter scripts functional, but use the new report date.
    for script in soup.select('script'):
        text=script.string or script.get_text()
        if "reportDate=new Date('2026-11-18" in text:
            script.string=text.replace("reportDate=new Date('2026-11-18T00:00:00+08:00')", f"reportDate=new Date('{iso}T00:00:00+08:00')")
        elif "const BUILD='2026-11-18'" in text:
            script.string=text.replace("const BUILD='2026-11-18'",f"const BUILD='{iso}'")
    filename=f'银行每日线报（{iso}）.html'
    output=BASE / filename
    output.write_text(str(soup),encoding='utf-8')
    print(output.name)

if __name__=='__main__': main()
