#!/usr/bin/env python3
"""Build and publish the current Beijing evening edition from actual source data."""
import argparse
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo('Asia/Shanghai')
REGIONS = {'cn': '中国大陆', 'hk': '中国香港', 'us': '美国'}
SNAPSHOT_DATE = '2026-11-18'


def report_date_for(now=None):
    """Use the last 21:00 publication cycle, including delayed/morning retries."""
    local = (now or dt.datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return local.date() + dt.timedelta(days=int(local.hour >= 21))


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def timestamp_label(value):
    parsed = timestamp(value)
    if parsed:
        return parsed.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M 北京时间')
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return value
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.strftime('%Y-%m-%d %H:%M') + '（来源时区未注明）'
    return None


def valid_date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def offer_items(data, label):
    values = data.get('offers', []) if isinstance(data, dict) else data
    if not isinstance(values, list):
        raise ValueError(f'{label} must be an array or an object with an offers array')
    if any(not isinstance(item, dict) for item in values):
        raise ValueError(f'{label} contains an invalid offer')
    return values


def usable_offer(offer, day):
    try:
        url = urlparse(str(offer.get('source_url', '')))
    except ValueError:
        return False
    if (offer.get('region') not in REGIONS or not isinstance(offer.get('title'), str)
            or not offer['title'].strip()
            or url.scheme not in ('http', 'https') or not url.netloc):
        return False
    start, end = valid_date(offer.get('start_date')), valid_date(offer.get('end_date'))
    if (start and start > day) or (end and end < day) or (start and end and start > end):
        return False
    return True


def verified_for(offer, day):
    start, end = valid_date(offer.get('start_date')), valid_date(offer.get('end_date'))
    return bool(offer.get('verified') is True and start and end and start <= day <= end)


def offer_sort_key(offer):
    date = (offer.get('end_date') or offer.get('start_date')
            or offer.get('published_at') or '9999-12-31')
    return (offer['region'], str(offer.get('bank') or '').casefold(), date, offer['title'])


def append_offer(soup, section, offer, verified):
    article = soup.new_tag('article', attrs={'class': 'offer-item collected-offer'})
    title = soup.new_tag('h3')
    title.string = ' · '.join(str(offer.get(key, '')).strip() for key in ('bank', 'title')
                            if str(offer.get(key, '')).strip())
    article.append(title)
    description = soup.new_tag('p')
    description.string = str(offer.get('details') or offer.get('summary') or '完整奖励和参加条件请查看来源。')
    article.append(description)
    facts = []
    if verified:
        facts.append(f"活动日期：{offer['start_date']} 至 {offer['end_date']}")
        if timestamp_label(offer.get('verified_at')):
            facts.append('核验时间：' + timestamp_label(offer['verified_at']))
    else:
        if offer.get('verification_scope') == 'current_page' and offer.get('verified') is True:
            facts.append('已核对当前来源页面；本期活动时间及个人资格待确认')
            if timestamp_label(offer.get('verified_at')):
                facts.append('来源核对时间：' + timestamp_label(offer['verified_at']))
        else:
            facts.append('待核验：活动时间、资格及名额须查看原文确认')
        if valid_date(offer.get('start_date')):
            facts.append('来源列示开始日期：' + offer['start_date'])
        if valid_date(offer.get('end_date')):
            facts.append('来源列示截止日期：' + offer['end_date'])
    published_label = '来源更新时间' if offer.get('publication_kind') == 'updated' else '来源发布日期'
    for key, name in [('published_at', published_label), ('collected_at', '采集时间')]:
        label = timestamp_label(offer.get(key))
        if label:
            facts.append(f'{name}：{label}')
    if offer.get('publication_note'):
        facts.append(str(offer['publication_note']))
    note = soup.new_tag('p', attrs={'class': 'region-note'})
    note.string = '；'.join(facts)
    article.append(note)
    link = soup.new_tag('a', href=offer['source_url'], target='_blank', rel='noopener noreferrer')
    link.string = '查看核验来源 ↗' if verified else '查看原始线索 ↗'
    article.append(link)
    section.append(article)


def build_report(day, *, offers_path=None, collected_path=None, status_path=None,
                 template_path=None, output_dir=None, now=None):
    now = (now or dt.datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    iso = day.isoformat()
    curated_data = read_json(Path(offers_path or BASE / 'verified_offers.json'), [])
    collected_data = read_json(Path(collected_path or BASE / 'collected_offers.json'), {})
    collection_status = read_json(Path(status_path or BASE / 'collection_status.json'), {})
    curated = offer_items(curated_data, 'verified_offers')
    collected = offer_items(collected_data, 'collected_offers')
    envelope = collected_data if isinstance(collected_data, dict) else {}
    if not isinstance(collection_status, dict):
        raise ValueError('collection_status must be an object')

    # Curated records win over duplicate feed entries. Unknown dates never imply verification.
    offers, seen = [], set()
    for item in curated + collected:
        if not usable_offer(item, day):
            continue
        key = (item['region'], item['source_url'], item['title'])
        if key in seen:
            continue
        seen.add(key)
        offers.append(item)
    offers.sort(key=offer_sort_key)
    verified = [item for item in offers if verified_for(item, day)]
    leads = [item for item in offers if not verified_for(item, day)]

    source_status = collection_status.get('status', envelope.get('status', 'missing'))
    if source_status not in ('ok', 'partial', 'failed', 'empty', 'stale', 'missing'):
        source_status = 'missing'
    observation_times = [timestamp(item.get('collected_at')) for item in offers]
    observation_times += [timestamp(item.get('verified_at')) for item in offers if item.get('verified') is True]
    if any(usable_offer(item, day) for item in collected):
        observation_times.append(timestamp(envelope.get('collected_at')))
    observations = [value for value in observation_times if value]
    collected_at = max(observations).isoformat(timespec='seconds') if observations else None
    if source_status in ('ok', 'partial') and envelope.get('report_date') not in (None, iso):
        source_status = 'stale'

    soup = BeautifulSoup(Path(template_path or BASE / 'report_template.html').read_text(encoding='utf-8'), 'html.parser')
    header = soup.select_one('header.top')
    if not soup.head or not soup.body or not soup.title or not header or not header.select_one('h1'):
        raise ValueError('report template is missing its head, body, title or report header')
    soup.title.string = f'银行每日线报 · {iso}'
    header.select_one('h1').string = f'银行每日线报 · {iso} · 全球三区'
    sub = header.select_one('.sub')
    if sub:
        sub.string = f'{iso} 00:00—23:59（北京时间） · 中国大陆 / 中国香港 / 美国'
    for element in header.select('.chips, .notice-strong, #data-freshness'):
        element.decompose()
    notice = soup.new_tag('p', attrs={'class': 'notice-strong', 'id': 'data-freshness'})
    if not offers:
        notice.string = f'暂无可确认的新活动。历史记录截至 {SNAPSHOT_DATE}，参加前请核对来源。'
    else:
        note = ''
        if collected_at:
            note += '更新于 ' + timestamp_label(collected_at) + '。'
        note += f'历史记录截至 {SNAPSHOT_DATE}，参加前请核对来源。'
        notice.string = note
    header.append(notice)

    for region, name in REGIONS.items():
        panel = soup.select_one(f'#region-{region}')
        if panel is None:
            raise ValueError(f'missing panel {region}')
        for previous in panel.select(f'#{region}-verified, #{region}-collected, .generated-snapshot-notice'):
            previous.decompose()
        sections = []
        for suffix, title, items, checked in [
            ('verified', f'📌 {name} · {iso} 已核实活动', verified, True),
            ('collected', f'📰 {name} · 最近采集线索（待核验）', leads, False),
        ]:
            section = soup.new_tag('section', attrs={'class': 'card', 'id': f'{region}-{suffix}'})
            heading = soup.new_tag('h2')
            heading.string = title
            section.append(heading)
            region_items = [item for item in items if item['region'] == region]
            if not region_items:
                empty = soup.new_tag('p', attrs={'class': 'region-note'})
                empty.string = ('暂无完成来源和活动日期核验的本期活动。' if checked
                                else '暂无取得可展示的新线索；原有内容仍按历史快照保留。')
                section.append(empty)
            for offer in region_items:
                append_offer(soup, section, offer, checked)
            sections.append(section)
        warning = soup.new_tag('div', attrs={'class': 'snapshot-notice generated-snapshot-notice'})
        warning.string = f'以下为 {SNAPSHOT_DATE} 历史存档，不是 {iso} 的可参加活动清单。'
        for element in reversed(sections + [warning]):
            panel.insert(0, element)

    for span in soup.select('.time-status'):
        span.string = '历史时点'
        span['class'] = ['time-status', 'closed']
    for script in soup.select('script:not([type="application/json"])'):
        content = script.string or script.get_text()
        content = re.sub(r"const reportDate\s*=\s*new Date\(['\"]20\d{2}-\d{2}-\d{2}T00:00:00\+08:00['\"]\)",
                         f"const reportDate=new Date('{iso}T00:00:00+08:00')", content)
        # Historical table times remain historical, including same-day rebuilds.
        content = re.sub(r'const snapshotNow=new Date\(\);.*?(?=\n\}\)\(\);)',
                         "document.querySelectorAll('#today .time-status').forEach(el=>{el.textContent='历史时点';el.classList.add('closed')});",
                         content, flags=re.S)
        script.string = content
    footer = soup.select_one('.footer')
    if footer:
        footer.string = f'银行与里程活动日报 · 本期 {iso} · 历史快照 {SNAPSHOT_DATE}'

    metadata = {
        'schema_version': 2,
        'date': iso,
        'report_date': iso,
        'timezone': 'Asia/Shanghai',
        'window_start': iso + 'T00:00:00+08:00',
        'window_end': iso + 'T23:59:59+08:00',
        'generated_at': now.isoformat(timespec='seconds'),
        'collected_at': collected_at,
        'source_status': source_status,
        'snapshot_date': SNAPSHOT_DATE,
        'verified_count': len(verified),
        'collected_count': len(leads),
        'regions': {region: {'verified_count': sum(item['region'] == region for item in verified),
                             'collected_count': sum(item['region'] == region for item in leads)}
                    for region in REGIONS},
    }
    for previous in soup.select('#report-metadata'):
        previous.decompose()
    data = soup.new_tag('script', attrs={'id': 'report-metadata', 'type': 'application/json'})
    data.string = json.dumps(metadata, ensure_ascii=False, sort_keys=True).replace('</', '<\\/')
    soup.head.append(data)
    target_dir = Path(output_dir or BASE)
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / f'银行每日线报（{iso}）.html'
    output.write_text(str(soup), encoding='utf-8')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='YYYY-MM-DD; default: current Beijing 21:00 publication cycle')
    parser.add_argument('--offers', type=Path, default=BASE / 'verified_offers.json')
    parser.add_argument('--collected', type=Path, default=BASE / 'collected_offers.json')
    parser.add_argument('--status', type=Path, default=BASE / 'collection_status.json')
    parser.add_argument('--output-dir', type=Path, default=BASE)
    parser.add_argument('--site-dir', type=Path, default=BASE.parent)
    parser.add_argument('--no-publish', action='store_true', help='only write a dated preview')
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date) if args.date else report_date_for()
    output = build_report(day, offers_path=args.offers, collected_path=args.collected,
                          status_path=args.status, output_dir=args.output_dir)
    if args.no_publish:
        print(output)
        return
    if __package__:
        from .publish_report import publish_report
    else:
        from publish_report import publish_report
    metadata = publish_report(output, args.site_dir)
    print(f"Published {metadata['report_date']} revision {metadata['revision']} "
          f"({metadata['verified_count']} verified, {metadata['collected_count']} unverified leads)")


if __name__ == '__main__':
    main()
