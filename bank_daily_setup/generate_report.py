#!/usr/bin/env python3
"""Build and publish the current Beijing evening edition from actual source data."""
import argparse
import datetime as dt
import json
import hashlib
import re
from pathlib import Path
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
SHANGHAI = ZoneInfo('Asia/Shanghai')
REGIONS = {'cn': '中国大陆', 'hk': '中国香港', 'us': '美国'}
ARCHIVE_DATE = '2026-11-18'


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
            or not offer['title'].strip() or url.scheme not in ('http', 'https') or not url.netloc):
        return False
    review_expires = valid_date(offer.get('review_expires_on'))
    if offer.get('verified') is not True and review_expires and review_expires < day:
        return False
    start, end = valid_date(offer.get('start_date')), valid_date(offer.get('end_date'))
    if (start and start > day) or (end and end < day) or (start and end and start > end):
        return False
    inferred = {'weekdays': [], 'month_days': []}
    title = offer['title']
    if not offer.get('weekdays'):
        english_days = {'monday': 1, 'tuesday': 2, 'wednesday': 3, 'thursday': 4,
                        'friday': 5, 'saturday': 6, 'sunday': 7}
        day_names = '|'.join(english_days)
        pattern = rf'\b(?:every|each)\s+((?:{day_names})(?:\s*(?:,|&|and|/)\s*(?:{day_names}))*)\b'
        for match in re.finditer(pattern, title, re.I):
            # Do not mistake a range such as Monday-Friday for Monday only.
            if re.match(r'\s*(?:through\b|to\b|[-–—])', title[match.end():], re.I):
                continue
            inferred['weekdays'].extend(english_days[value.casefold()]
                                         for value in re.findall(day_names, match[1], re.I))
        chinese_days = {value: number for number, value in enumerate('一二三四五六日', 1)}
        chinese_days['天'] = 7
        for match in re.finditer(r'每(?:周|星期)\s*([一二三四五六日天](?:\s*[、,，/和及]\s*[一二三四五六日天])*)', title):
            if re.match(r'\s*(?:至|到|[-–—])', title[match.end():]):
                continue
            inferred['weekdays'].extend(chinese_days[value] for value in match[1] if value in chinese_days)
    if not offer.get('month_days'):
        for match in re.finditer(r'每月\s*(\d{1,2})\s*[日号]', title):
            # A lone calendar date or a multi-day range is not a single-day recurrence.
            if re.match(r'\s*(?:至|到|[-–—]|[、,，/和及]\s*\d)', title[match.end():]):
                continue
            if 1 <= int(match[1]) <= 31:
                inferred['month_days'].append(int(match[1]))
    for field, number in [('weekdays', day.isoweekday()), ('month_days', day.day)]:
        values = offer.get(field) or inferred[field]
        if values and number not in [int(x) for x in values if str(x).isdigit()]:
            return False
    return True


def verified_for(offer, day):
    """An official current quote is distinct from a date-confirmed campaign."""
    if offer.get('verified') is not True or offer.get('verification_scope') == 'community':
        return False
    start, end = valid_date(offer.get('start_date')), valid_date(offer.get('end_date'))
    current_page = offer.get('verification_scope') == 'current_page'
    if current_page and (not start or not end):
        observed = timestamp(offer.get('verified_at'))
        current_page = bool(observed and 0 <= (day - observed.astimezone(SHANGHAI).date()).days <= 2)
    return bool(current_page
                or (start and end and start <= day <= end)
                or (offer.get('verification_scope') == 'official_period' and (start or end)))


def offer_sort_key(offer):
    # Source publication dates are never campaign dates or offer ordering evidence.
    return (str(offer.get('bank') or '').casefold(), offer.get('end_date') or '9999-12-31', offer['title'])


def text_value(value):
    if value is None:
        return ''
    if isinstance(value, (tuple, list)):
        return '；'.join(text_value(item) for item in value)
    if isinstance(value, dict):
        return '；'.join(f'{key}：{text_value(item)}' for key, item in value.items() if item is not None)
    return str(value).strip()


def h(value):
    return escape(text_value(value), quote=True)


def offer_identity(item):
    # Duplicate official/feed records and repeated placement never inflate counts.
    return (item['region'], str(item.get('bank') or '').casefold(),
            re.sub(r'\s+', '', item['title']).casefold())


def source_identity(item):
    parsed = urlparse(str(item.get('source_url', '')))
    return (item.get('region'), parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query)


def activity_time(item):
    value = text_value(item.get('time') or item.get('start_time'))
    match = re.match(r'^(\d{1,2}):(\d{2})(?:\b|[—–-])', value)
    if match and int(match[1]) < 24 and int(match[2]) < 60:
        return f'{int(match[1]):02d}:{match[2]}'
    return None


def applies_today(item, day):
    for field, number in [('weekdays', day.isoweekday()), ('month_days', day.day)]:
        values = item.get(field)
        if values and number not in [int(x) for x in values if str(x).isdigit()]:
            return False
    release = valid_date(item.get('release_date'))
    if release and release != day:
        return False
    return True


def highlight_kind(item, day):
    kind = item.get('highlight')
    until = valid_date(item.get('highlight_until'))
    if kind not in ('rare', 'value') or (item.get('highlight_until') and (not until or day > until)):
        return None
    return kind


def today_items(items, day):
    chosen = []
    for item in items:
        end = valid_date(item.get('end_date'))
        if applies_today(item, day) and (activity_time(item) or highlight_kind(item, day)
                                        or valid_date(item.get('start_date')) == day
                                        or (end and 0 <= (end - day).days <= 2)
                                        or item.get('today') is True):
            chosen.append(item)
    return sorted(chosen, key=lambda item: (activity_time(item) or '99:99', offer_sort_key(item)))


def category(item):
    value = text_value(item.get('category') or '其他活动')
    labels = {'welcome': '迎新奖励', 'welcome_bonus': '迎新奖励', 'signup': '开卡奖励',
              'business': '商业卡', 'business_card': '商业卡', 'transfer': '转点加赠',
              'transfer_bonus': '转点加赠', 'payment': '支付优惠', 'deposit': '存款与开户',
              'benefit': '卡片权益', 'points': '积分活动', 'miles': '里程活动', 'travel': '出行优惠',
              'cashback': '返现优惠', 'bank_bonus': '银行开户', 'other': '其他活动',
              'rare': '稀缺活动', 'value': '高价值活动', 'credit_card': '卡片优惠',
              'bank_payment': '支付优惠', 'roundup': '优惠汇总'}
    return labels.get(value.casefold(), '其他活动' if re.fullmatch(r'[A-Za-z][A-Za-z0-9_ -]*', value) else value)


def record_status_label(value):
    value = text_value(value)
    labels = {'recent_high': '近期高位', 'not_confirmed': '未证实史高',
              'all_time_high': '历史最高', 'historical_high': '历史最高',
              'historic_high': '历史最高', 'confirmed_high': '已确认史高',
              'public_high': '公开渠道高位', 'at_high': '持平历史高位',
              'below_high': '低于历史高位', 'not_high': '非历史最高',
              'unknown': '历史对比待确认', 'unverified': '未证实史高'}
    return labels.get(value.casefold(), '历史对比待确认' if re.fullmatch(r'[A-Za-z][A-Za-z0-9_ -]*', value) else value)


def is_credit_card_offer(item):
    raw_category = text_value(item.get('category')).casefold()
    label = category(item)
    title = item['title']
    if (raw_category in ('deposit', 'bank_bonus', 'checking', 'savings', 'transfer', 'transfer_bonus')
            or re.search(r'存款|储蓄|借记卡|银行开户|银行账户|转点|里程兑换', label)
            or re.search(r'\b(?:checking|savings|deposit)\b', title, re.I)):
        return False
    return bool(raw_category in ('welcome', 'welcome_bonus', 'signup', 'business', 'business_card', 'credit_card')
                or re.search(r'信用卡|商业卡|开卡奖励|迎新奖励', label)
                or re.search(r'信用卡|商业卡|credit.?card|Ink Business|Business (?:Platinum|Gold|Green)', title, re.I))


def is_business_card_offer(item):
    return is_credit_card_offer(item) and bool(re.search(r'商业|business', category(item) + ' ' + item['title'], re.I))


def comparison_html(item):
    comparison = item.get('comparison')
    history_url = item.get('history_source_url')
    if isinstance(comparison, dict):
        history_url = history_url or comparison.get('history_source_url') or comparison.get('source_url')
        labels = {'current': '当前奖励', 'historical': '历史奖励', 'previous': '此前奖励',
                  'current_offer': '当前报价', 'historical_high': '历史最高',
                  'summary': '奖励对比', 'details': '对比说明', 'note': '备注'}
        lines = []
        for key, value in comparison.items():
            if key in ('history_source_url', 'source_url', 'url') or value is None:
                continue
            label = labels.get(key, key if re.search(r'[\u4e00-\u9fff]', key) else '')
            lines.append((label + '：' if label else '') + text_value(value))
        content = h('；'.join(lines) or '暂无可靠历史奖励对比。')
    else:
        content = h(comparison or '暂无可靠历史奖励对比。')
    if item.get('record_status'):
        content += '<span class="comparison-note">' + h(record_status_label(item['record_status'])) + '</span>'
    try:
        parsed = urlparse(text_value(history_url))
    except ValueError:
        parsed = None
    if parsed and parsed.scheme in ('http', 'https') and parsed.netloc:
        content += f'<div class="offer-extra"><a class="source-link" href="{h(history_url)}" target="_blank" rel="noopener noreferrer">历史奖励来源 ↗</a></div>'
    return content


def period_label(item, official=True):
    start, end = valid_date(item.get('start_date')), valid_date(item.get('end_date'))
    if start and end:
        label = f'{start.isoformat()} — {end.isoformat()}'
    elif end:
        label = '截至 ' + end.isoformat()
    elif item.get('verification_scope') == 'current_page' and official:
        label = '官网当前报价，截止未公布'
    elif start:
        label = start.isoformat() + ' 起，截止未公布'
    else:
        label = '活动日期待确认' if not official else '截止未公布'
    weekdays = item.get('weekdays') or []
    month_days = item.get('month_days') or []
    if weekdays:
        names = ['一', '二', '三', '四', '五', '六', '日']
        labels = [names[int(value) - 1] for value in weekdays if str(value).isdigit() and 1 <= int(value) <= 7]
        if labels:
            label += ' · 每周' + '、'.join(labels)
    if month_days:
        label += ' · 每月' + '、'.join(map(str, month_days)) + '日'
    return label


def source_link(item, community=False):
    output = f'<a class="source-link" href="{h(item["source_url"])}" target="_blank" rel="noopener noreferrer">{"查看原文" if community else "官方详情"} ↗</a>'
    for related in item.get('related_sources', []):
        url = related.get('url', '')
        parsed = urlparse(url)
        if parsed.scheme in ('http', 'https') and parsed.netloc and url != item['source_url']:
            output += f'<div class="offer-extra"><a href="{h(url)}" target="_blank" rel="noopener noreferrer">{h(related.get("label") or "相关讨论")} ↗</a></div>'
    return output


def combine_reviewed_leads(items):
    """Only combine articles explicitly reviewed as the same offer and headline.

    A changed headline is kept for a fresh review, including a changed bonus on
    an otherwise stable URL. Unrelated bank/channel offers are never collapsed.
    """
    items = [dict(item) for item in items]
    by_id = {item.get('id'): item for item in items if item.get('id')}
    combined = set()
    for item in items:
        related = list(item.get('related_sources', []))
        urls = {item['source_url']} | {value.get('url') for value in related}
        for reference in item.get('related_leads', []):
            candidate = by_id.get(reference.get('id'))
            if (not candidate or candidate is item or candidate.get('verified') is True
                    or candidate['region'] != item['region']
                    or candidate['title'] != reference.get('title')):
                continue
            combined.add(candidate['id'])
            if reference.get('include_source', True) and candidate['source_url'] not in urls:
                related.append({'url': candidate['source_url'], 'label': candidate.get('origin') or '相关讨论'})
                urls.add(candidate['source_url'])
        item['related_sources'] = related
    return [item for item in items if item.get('id') not in combined], len(combined)


def offer_anchor(item):
    key = '\x1f'.join(offer_identity(item))
    return 'offer-' + item['region'] + '-' + hashlib.sha256(key.encode()).hexdigest()[:12]


def brief_text(item, limit=115):
    value = text_value(item.get('summary'))
    if not value:
        value = text_value(item.get('details') or item.get('reward'))
        value = re.split(r'[。\n]', value, maxsplit=1)[0]
    return value if len(value) <= limit else value[:limit].rstrip('，；、 ') + '…'


def reward_html(item, compact=False):
    reward = text_value(item.get('reward'))
    if compact:
        brief = brief_text(item, 90)
        if reward and reward not in brief:
            return '<b class="offer-reward">' + h(reward) + '</b>' + ('<div class="offer-extra">' + h(brief) + '</div>' if brief else '')
        return h(brief or reward or '查看完整活动条件。')
    detail = text_value(item.get('details') or item.get('summary') or item.get('reward'))
    output = '<b class="offer-reward">' + h(reward) + '</b>' if reward and reward not in detail else ''
    output += '<div class="offer-detail">' + h(detail or '详见活动页面。') + '</div>'
    eligibility = text_value(item.get('eligibility'))
    if eligibility and eligibility not in detail:
        output += '<div class="offer-extra">资格：' + h(eligibility) + '</div>'
    return output


def deadline_label(item, official=True):
    end = valid_date(item.get('end_date'))
    if end:
        return end.isoformat()
    return period_label(item, official)


def table_html(items, *, mode='all', caption='活动列表', community=False, region=None, compact=False):
    region = region or (items[0]['region'] if items else 'cn')
    international = [('bank', '银行 / 平台', '14%'), ('title', '活动', '20%'),
                     ('reward', '奖励及条件', ''), ('category', '类型', '10%'),
                     ('deadline', '截止日期', '12%'), ('source', '入口', '8%')]
    if mode == 'comparison':
        columns = [('bank', '银行', '12%'), ('title', '卡种', '19%'), ('reward', '奖励及条件', '25%'),
                   ('comparison', '史高判定', ''), ('applicability', '适用性', '12%'), ('source', '详情', '8%')]
    elif region == 'cn' and mode == 'today':
        columns = [('time', '时间', '64px'), ('bank', '银行', '88px'), ('title', '活动', '200px'), ('reward', '要点', '')]
    elif region == 'cn' and mode == 'miles':
        columns = [('bank', '主体', '96px'), ('title', '动态', '210px'), ('reward', '要点', ''), ('deadline', '日期', '110px')]
    elif region == 'cn' and not community:
        columns = [('bank', '银行', '88px'), ('title', '活动', '190px'), ('category', '类型', '80px'),
                   ('reward', '要点（门槛 · 频次）', ''), ('entry', '入口', '150px'), ('deadline', '截止', '110px')]
    else:
        columns = international
    output = '<div class="table-scroll"><table><caption>' + h(caption) + '</caption><thead><tr>'
    output += ''.join(f'<th scope="col"' + (f' style="width:{width}"' if width else '') + f'>{label}</th>' for _, label, width in columns)
    output += '</tr></thead><tbody>'
    for item in items:
        anchor = offer_anchor(item)
        row_id = f' id="{anchor}"' if mode == 'all' and not community and not compact else ''
        output += f'<tr{row_id} data-bank="{h(item.get("bank"))}" data-category="{h(category(item))}" data-deadline="{h(item.get("end_date"))}" data-offer-id="{h(item.get("id") or item["title"])}">'
        for key, label, _ in columns:
            content, klass = '', ''
            if key == 'bank':
                content, klass = h(item.get('bank') or '银行 / 平台'), 'bank'
            elif key == 'title':
                if compact and not community:
                    content = f'<a class="offer-title" href="#{anchor}">{h(item["title"])}</a>'
                else:
                    content = f'<b class="offer-title">{h(item["title"])}</b>'
                if mode == 'today' and region != 'cn' and activity_time(item):
                    content += '<div class="time-cell">' + h(activity_time(item)) + '</div>'
            elif key == 'category':
                content = h(category(item))
            elif key == 'reward':
                content = reward_html(item, compact=compact)
                if community and item.get('publication_note'):
                    content += '<div class="offer-extra">' + h(item['publication_note']) + '</div>'
            elif key == 'deadline':
                content, klass = h(deadline_label(item, not community)), 'date-cell'
                if item.get('end_time'):
                    content += '<div class="offer-extra">' + h(item['end_time']) + '</div>'
                if community and timestamp_label(item.get('published_at')):
                    label = '原文更新：' if item.get('publication_kind') == 'updated' else '源站标注日期：'
                    content += '<div class="offer-extra">' + label + h(timestamp_label(item['published_at'])) + '</div>'
            elif key == 'time':
                content, klass = h(activity_time(item) or '全天'), 'time-cell'
                if item.get('time_note'):
                    content += '<div class="offer-extra">' + h(item['time_note']) + '</div>'
            elif key == 'comparison':
                content = comparison_html(item)
            elif key == 'applicability':
                content = '商业信用卡' if is_business_card_offer(item) else '个人信用卡'
            elif key in ('entry', 'source'):
                if key == 'entry' and item.get('channel'):
                    content = '<span class="entry-channel">' + h(item['channel']) + '</span>'
                content += source_link(item, community)
            output += f'<td data-label="{label}" class="{klass}">{content}</td>'
        output += '</tr>'
    return output + '</tbody></table></div>'


def section_heading(section_id, title, tag=None):
    paths = {
        'today': ('#c2610b', '<path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/>'),
        'all': ('#2a639c', '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/>'),
        'miles': ('#1d6b45', '<path d="M2 16l20-8-4 10-6-3-4 4v-5z"/>'),
        'deadlines': ('#c0392b', '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/>'),
    }
    icon = ''
    if section_id in paths:
        color, shape = paths[section_id]
        icon = f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2">{shape}</svg>'
    return '<h2>' + icon + h(title) + (f'<span class="tag">{h(tag)}</span>' if tag else '') + '</h2>'


def section_html(section_id, title, items, *, mode='all', community=False, note=None, tag=None, region=None, compact=True):
    content = f'<section class="card" id="{section_id}">' + section_heading(section_id, title, tag)
    if note:
        content += f'<p class="note">{h(note)}</p>'
    content += table_html(items, mode=mode, caption=title, community=community, region=region, compact=compact) if items else '<p class="empty">本期暂无合适活动。</p>'
    return content + '</section>'


def spotlight_html(region, name, items, kind):
    title = '✨ 稀缺活动' if kind == 'rare' else '💎 高价值活动'
    content = f'<section class="spotlight {kind}" id="{region}-{kind}"><div class="spotlight-head"><h2>{title} · {name}</h2></div>'
    if not items:
        empty = '本期暂无已确认的罕见回归或首次推出活动。' if kind == 'rare' else '本期暂无合适的高价值活动。'
        return content + '<div class="empty">' + empty + '</div></section>'
    content += '<div class="spotlight-grid">'
    for item in items:
        content += f'<article class="spot"><div class="tagline">{h(category(item))}</div>'
        if item.get('reward'):
            content += f'<div class="reward">{h(item["reward"])}</div>'
        content += f'<h3>{h(item["title"])}</h3><p>{h(brief_text(item, 100))}</p><a href="#{offer_anchor(item)}">查看活动 ↗</a></article>'
    return content + '</div></section>'


def deadline_calendar(items, day):
    groups = {}
    for item in items:
        groups.setdefault(item['end_date'], []).append(item)
    dates = sorted(groups)
    midpoint = (len(dates) + 1) // 2
    content = '<section class="card" id="deadlines">' + section_heading('deadlines', '截止日历', '红色为一周内到期，尽快处理')
    if not dates:
        return content + '<p class="empty">本期暂无合适活动。</p></section>'
    content += '<div class="grid2">'
    for part in [dates[:midpoint], dates[midpoint:]]:
        if not part:
            continue
        content += '<div class="table-scroll"><table><thead><tr><th scope="col" style="width:110px">截止日期</th><th scope="col">涉及活动</th></tr></thead><tbody>'
        for date in part:
            urgent = 0 <= (valid_date(date) - day).days <= 7
            content += '<tr><td data-label="截止日期" class="date-cell' + (' dead' if urgent else '') + '">' + date + '</td><td data-label="涉及活动">'
            content += '；'.join(f'{h(item.get("bank"))} <a class="offer-title" href="#{offer_anchor(item)}">{h(item["title"])}</a>' for item in groups[date])
            content += '</td></tr>'
        content += '</tbody></table></div>'
    return content + '</div></section>'


def all_section(region, name, items, day):
    prefix = '' if region == 'cn' else region + '-'
    tag = '权益类型：💰刷卡金/返现 · 🎫立减金/满减 · 🧧红包 · ⭐积分 · ✈️里程 · 🎟️权益' if region == 'cn' else f'{len(items)} 条 · 按银行排序'
    content = f'<section class="card" id="{prefix}all" data-offer-filter data-report-date="{day.isoformat()}">' + section_heading(prefix + 'all', '全部活动汇总（按银行分组）', tag)
    banks = sorted({text_value(item.get('bank')) for item in items}, key=str.casefold)
    cats = sorted({category(item) for item in items})
    if region == 'cn':
        content += '<div class="toolbar"><div class="search-wrap"><label for="search">搜索活动</label><input id="search" type="search" autocomplete="off" aria-label="搜索中国大陆活动" placeholder="搜索银行、活动、门槛、入口…"></div>'
        for kind, field_id, label, choices in [('bank', 'bankFilter', '银行 / 渠道', banks), ('category', 'typeFilter', '权益类型', cats)]:
            content += f'<div><label for="{field_id}">{label}</label><select id="{field_id}" data-filter="{kind}"><option value="">全部{"银行" if kind == "bank" else "类型"}</option>'
            content += ''.join(f'<option value="{h(value)}">{h(value)}</option>' for value in choices) + '</select></div>'
        content += f'<div><label for="deadlineFilter">截止时间</label><select id="deadlineFilter" data-filter="deadline"><option value="">全部活动</option><option value="soon">7天内截止</option><option value="month">{day.month}月内截止</option><option value="dated">有明确日期</option></select></div><button id="resetFilters" data-filter-reset type="button">重置</button></div>'
    else:
        content += f'<div class="region-tools"><input id="{region}-search" type="search" aria-label="搜索{name}活动" placeholder="搜索银行、活动、奖励条件">'
        for kind, suffix, label, choices in [('bank', 'bank', '银行', banks), ('category', 'type', '类型', cats)]:
            content += f'<select id="{region}-{suffix}" data-filter="{kind}" aria-label="{label}筛选"><option value="">全部{label}</option>'
            content += ''.join(f'<option value="{h(value)}">{h(value)}</option>' for value in choices) + '</select>'
        content += '</div>'
    content += '<div class="filter-meta"><span class="filter-count"></span></div><div id="' + region + '-verified">'
    content += table_html(items, caption=name + '全部活动', region=region) if items else '<p class="empty">本期暂无合适活动。</p>'
    return content + '</div><div class="no-results filter-empty" hidden>没有符合筛选条件的活动。</div></section>'


def render_region(region, name, items, leads, day):
    prefix = '' if region == 'cn' else region + '-'
    today = today_items(items, day)
    deadlines = sorted([item for item in items if valid_date(item.get('end_date'))],
                       key=lambda item: (item['end_date'], item.get('end_time') or '23:59', offer_sort_key(item)))
    miles = [item for item in items if re.search(r'积分|里程|转点|miles|points|transfer', category(item) + ' ' + item['title'], re.I)]
    month_end = dt.date(day.year + (day.month == 12), day.month % 12 + 1, 1) - dt.timedelta(days=1)
    month_count = sum(valid_date(item['end_date']) <= month_end for item in deadlines)
    total = len(items) + len(leads)
    banks = len({item.get('bank') for item in items + leads})
    if region == 'cn':
        stats = [(total, '本期收录活动总数', False), (banks, '覆盖银行/渠道', False),
                 (len(today), '今日重点活动', True), (month_count, f'{month_end.month}月{month_end.day}日前截止', True)]
        content = '<div class="stats">' + ''.join(f'<div class="stat"><div class="n{" hot" if hot else ""}">{number}</div><div class="l">{label}</div></div>' for number, label, hot in stats) + '</div>'
    else:
        content = f'<section class="card region-hero"><h2>{name} · 信用卡与里程活动</h2><p>活动币种：{"HKD" if region == "hk" else "USD"} · {day.year}年{day.month}月{day.day}日</p><div class="region-kpis">'
        content += ''.join(f'<div><b>{number}</b><span>{label}</span></div>' for number, label in [(total, '活动与线索'), (banks, '银行 / 平台'), (len(deadlines), '有截止日期的活动')]) + '</div>'
        if region == 'us':
            content += '<p><a href="#us-records">查看史高与商业卡开卡奖励 ↓</a></p>'
        content += '</section>'
    for kind in ('rare', 'value'):
        content += spotlight_html(region, name, [item for item in items if highlight_kind(item, day) == kind], kind)
    content += section_html(prefix + 'today', '今日必抢' if region == 'cn' else '今日必看', today, mode='today', region=region,
                            tag='按当天时间点排序，资格与名额以活动页面为准' if region == 'cn' else '优先关注近期截止')
    if region == 'us':
        compare = [item for item in items if is_credit_card_offer(item)
                   and (item.get('comparison') or item.get('record_status') or item.get('history_source_url'))]
        business = [item for item in items if is_business_card_offer(item)]
        content += section_html('us-records', '美国史高开卡奖励 · 个人卡与商业卡', compare, mode='comparison', region=region,
                                note='“史高”仅用于有明确历史对比依据的报价；定向奖励以个人申请页为准。')
        content += section_html('us-business', '美国商业卡', business, region=region)
    content += all_section(region, name, items, day)
    content += section_html(prefix + 'miles', '里程与积分专题', miles, mode='miles', region=region,
                            tag='重点关注：南航 / 东航 / 海航 / 亚洲万里通 / Avios' if region == 'cn' else None)
    content += deadline_calendar(deadlines, day) if region == 'cn' else section_html(prefix + 'deadlines', '截止日历 · 按截止时间排序', deadlines, mode='deadlines', region=region)
    content += section_html(region + '-collected', '社区与活动线索', leads, community=True, region=region, compact=False,
                            note='以下线索的活动日期与个人资格尚待确认，请先查看原文。' if leads else None)
    return content


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
    offers, seen = [], set()
    official_sources = {source_identity(item) for item in curated
                        if usable_offer(item, day) and verified_for(item, day)}
    for item in curated + collected:
        item = dict(item)
        if item.get('region') == 'hk':
            item['bank'] = {'HSBC / 汇丰': '汇丰银行 HSBC', 'American Express': 'American Express 香港',
                            '工银亚洲': '工银亚洲 ICBC Asia', 'Citi': 'Citi Hong Kong',
                            'DBS': '星展银行 DBS', 'BEA': '东亚银行 BEA'}.get(item.get('bank'), item.get('bank'))
        if not usable_offer(item, day):
            continue
        # Prefer the reviewed terms when a feed rediscovers the same official
        # article with its original (different) headline; don't count it twice.
        if (item.get('source_type') == 'official' and item.get('source_id')
                and not verified_for(item, day) and source_identity(item) in official_sources):
            continue
        key = offer_identity(item)
        if key in seen:
            continue
        seen.add(key)
        offers.append(item)
    offers, combined_lead_count = combine_reviewed_leads(offers)
    offers.sort(key=lambda item: (item['region'], offer_sort_key(item)))
    verified = [item for item in offers if verified_for(item, day)]
    leads = [item for item in offers if not verified_for(item, day)]
    source_status = collection_status.get('status', envelope.get('status', 'missing'))
    if source_status not in ('ok', 'partial', 'failed', 'empty', 'stale', 'missing'):
        source_status = 'missing'
    observations = [timestamp(item.get(field)) for item in offers for field in ('collected_at', 'verified_at')]
    if any(usable_offer(item, day) for item in collected):
        observations.append(timestamp(envelope.get('collected_at')))
    observations = [value for value in observations if value]
    collected_at = max(observations).isoformat(timespec='seconds') if observations else None
    if source_status in ('ok', 'partial') and envelope.get('report_date') not in (None, iso):
        source_status = 'stale'
    soup = BeautifulSoup(Path(template_path or BASE / 'report_template.html').read_text(encoding='utf-8'), 'html.parser')
    header = soup.select_one('header.top')
    if not soup.head or not soup.body or not soup.title or not header or not header.select_one('h1'):
        raise ValueError('report template is missing its head, body, title or report header')
    soup.title.string = f'银行每日线报 · {iso}'
    header.select_one('h1').string = '银行 & 里程活动日报 · 全球三区'
    sub = header.select_one('.sub')
    if sub:
        sub.string = f'{day.year}年{day.month}月{day.day}日 · 周' + '一二三四五六日'[day.weekday()] + ' · 中国大陆 / 中国香港 / 美国'
    for element in header.select('.chips, .notice-strong, #data-freshness'):
        element.decompose()
    chips = soup.new_tag('div', attrs={'class': 'chips'})
    chip = soup.new_tag('span', attrs={'class': 'chip today'})
    chip.string = '今天是周' + '一二三四五六日'[day.weekday()] + '：' + ' · '.join(
        name + str(len(today_items([item for item in verified if item['region'] == region], day))) + '项重点'
        for region, name in [('cn', '大陆'), ('hk', '香港'), ('us', '美国')])
    chips.append(chip)
    header.append(chips)
    notice = soup.new_tag('p', attrs={'class': 'notice-strong', 'id': 'data-freshness'})
    notice.string = (('更新于 ' + timestamp_label(collected_at) + ' · ') if collected_at else '') + f'{len(verified)} 项官网活动 / {len(leads)} 条待确认线索' if offers else '暂无可确认的新活动。'
    header.append(notice)
    for region, name in REGIONS.items():
        panel = soup.select_one(f'#region-{region}')
        if panel is None:
            raise ValueError(f'missing panel {region}')
        current = [item for item in verified if item['region'] == region]
        community = [item for item in leads if item['region'] == region]
        panel.clear()  # Never carry previous edition rows or statistics into today's report.
        fragment = BeautifulSoup(render_region(region, name, current, community, day), 'html.parser')
        panel.append(fragment)
        tab = soup.select_one(f'.region-tab[data-region="{region}"] small')
        if tab:
            tab.string = f'{len(current)} 项活动 · {len(community)} 条线索'
    script = soup.select_one('#pwa-app-script')
    if script:
        script.string = re.sub(r"\bconst BUILD\s*=\s*(['\"])[^'\"]*\1", f"const BUILD='{iso}'", script.string or script.get_text())
    footer = soup.select_one('.footer')
    if footer:
        footer.clear()
        footer.append(f'银行与里程活动日报 · 本期 {iso} · ')
        link = soup.new_tag('a', href=f'./archive/{ARCHIVE_DATE}.html')
        link.string = '查看 11月18日存档'
        footer.append(link)
    metadata = {
        'schema_version': 3, 'date': iso, 'report_date': iso, 'timezone': 'Asia/Shanghai',
        'window_start': iso + 'T00:00:00+08:00', 'window_end': iso + 'T23:59:59+08:00',
        'generated_at': now.isoformat(timespec='seconds'), 'collected_at': collected_at,
        'source_status': source_status, 'verified_count': len(verified), 'collected_count': len(leads),
        'combined_lead_count': combined_lead_count,
        'current_page_count': sum(item.get('verification_scope') == 'current_page' for item in verified),
        'regions': {region: {'verified_count': sum(item['region'] == region for item in verified),
                             'collected_count': sum(item['region'] == region for item in leads)} for region in REGIONS},
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
