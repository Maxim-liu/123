#!/usr/bin/env python3
"""Collect public bank/payment leads; never promote a lead to a verified offer.

Python 3.11+, standard library only. Sources are deliberately data, not scraper
code. RSS publication dates, HTML article metadata and forum post timestamps
are retained separately from the actual collection time. A fetched page without
a publication date is accepted only for explicitly opted-in discovery sources,
and is labelled undated. No logins, cookies, captcha solving or proxy rotation.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
UTC = dt.timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
USER_AGENT = "BankDailyPublicFeed/1.0 (+https://github.com/Maxim-liu/123)"
FINANCIAL = re.compile(r"银行|銀行|信用卡|立减|立減|刷卡|还款|還款|云闪付|雲閃付|银联|銀聯|支付|返现|返現|回贈|獎賞|里數|积分|積分|开卡|開卡|办卡|迎新|工行|农行|建行|交行|招行|中行|邮储|浦发|广发|中信|光大|民生|华夏|滙豐|匯豐|恒生|渣打|中銀|東亞|大新|星展|\b(?:Chase|Amex|American Express|Citi|Wells Fargo|Bank(?:ing)?|credit.?cards?|checking|cash.?back|Membership Rewards|statement credit)\b", re.I)
BENEFIT = re.compile(r"优惠|優惠|立减|立減|刷卡金|红包|紅包|返现|返現|回贈|獎賞|奖励|獎勵|里數|积分|積分|福利|迎新|活动|活動|羊毛|满减|滿減|兑换|兌換|领券|領券|大促|特惠|天天减|(?:最高|首单|分期)减|\d(?:\.\d)?折|gift|bonus|offer|cash.?back|credit|rebate|reward|\$|%|％", re.I)
EXPIRED = re.compile(r"\b(?:dead|expired|ended)\b|已结束|已結束|已过期|已過期|已截止|失效", re.I)
EDITORIAL = re.compile(r"^Best credit card combinations|^How (?:to|do)|^What (?:is|are)|^Weekly (?:review|roundup)|\bReview:\s*(?:Rewards|Benefits|Is |Our )", re.I)


def now_iso() -> str:
    return dt.datetime.now(UTC).isoformat(timespec="seconds")


def report_day(now: dt.datetime) -> dt.date:
    local = now.astimezone(SHANGHAI)
    return local.date() + dt.timedelta(days=local.hour >= 21)


def clean_text(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", value or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def relevant_title(title, source):
    # A configured bank promotion index supplies the institution context;
    # community articles must still name a financial product in their title.
    bank_index = source.get("source_type") == "official" and source.get("bank") and source.get("activity_index", False)
    if source.get("require_specific_offer_title") and not re.search(r"[$¥￥%％]|\d[\d,]*\s*(?:元|积分|積分|points|miles|bonus)", title, re.I):
        return False
    return bool((FINANCIAL.search(title) or bank_index) and BENEFIT.search(title))


def date_value(value: str | None, timezone: str) -> tuple[str | None, str]:
    """Return ISO timestamp and the precision/basis, without inventing a time."""
    if not value:
        return None, "unknown"
    value = clean_text(str(value)).strip()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed:
            if parsed.tzinfo is None:
                return parsed.isoformat(timespec="seconds"), "timezone_unconfirmed"
            return parsed.isoformat(timespec="seconds"), "source_timezone"
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return dt.date.fromisoformat(value).isoformat(), "date_only"
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Preserve the uncertainty; the regional zone is only used to filter.
            return parsed.isoformat(timespec="seconds"), "timezone_unconfirmed"
        return parsed.isoformat(timespec="seconds"), "source_timezone"
    except ValueError:
        pass
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", value)
    if m:
        y, mo, day, hour, minute, second = m.groups()
        try:
            if hour is None:
                return dt.date(int(y), int(mo), int(day)).isoformat(), "date_only"
            parsed = dt.datetime(int(y), int(mo), int(day), int(hour), int(minute), int(second or 0))
            return parsed.isoformat(timespec="seconds"), "timezone_unconfirmed"
        except ValueError:
            pass
    return None, "unknown"


def date_for_comparison(value: str, timezone: str) -> dt.datetime:
    value_dt = dt.datetime.fromisoformat(value)
    return value_dt if value_dt.tzinfo else value_dt.replace(tzinfo=ZoneInfo(timezone))


class Page(HTMLParser):
    """Read public HTML links, document metadata and JSON-LD, without JS."""
    def __init__(self, text: str, link_attributes=()):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: list[dict] = []
        self.structured: list = []
        self.times: list[str] = []
        self._anchor = None
        self._json = None
        self._rows = []
        self._date_span = None
        self._heading = None
        self._link_heading = None
        self.link_attributes = link_attributes
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            self._rows.append({"links": [], "dates": [], "heading": "", "parts": []})
        if tag in ("h1", "h2", "h3", "h4") and self._rows:
            self._heading = {"tag": tag, "row": self._rows[-1], "parts": []}
        if tag in ("h1", "h2", "h3", "h4") and self._anchor is not None:
            self._link_heading = {"tag": tag, "anchor": self._anchor, "parts": []}
        if tag == "span" and self._rows and self._anchor is None:
            self._date_span = []
        if tag == "meta":
            self.meta[(a.get("property") or a.get("name") or a.get("itemprop") or "").lower()] = a.get("content", "")
        if tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        custom_url = next((a[key] for key in self.link_attributes if a.get(key)), None)
        if (tag == "a" and a.get("href")) or custom_url:
            self._anchor = {"url": a.get("href") if tag == "a" else custom_url,
                            "title": "", "hint": a.get("title", ""), "_tag": tag, "_custom": bool(custom_url)}
        if tag == "img" and self._anchor is not None and not self._anchor["hint"]:
            self._anchor["hint"] = a.get("alt", "")
        if tag == "script" and "ld+json" in a.get("type", ""):
            self._json = []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["title"] += data
        if self._json is not None:
            self._json.append(data)
        if self._date_span is not None:
            self._date_span.append(data)
        if self._heading is not None:
            self._heading["parts"].append(data)
        if self._link_heading is not None:
            self._link_heading["parts"].append(data)
        if self._rows and self._json is None:
            self._rows[-1]["parts"].append(data)

    def handle_endtag(self, tag):
        if self._anchor is not None and tag == self._anchor["_tag"]:
            self._anchor["title"] = clean_text(self._anchor["title"]) or clean_text(self._anchor["hint"])
            self.links.append(self._anchor)
            if self._rows:
                self._rows[-1]["links"].append(self._anchor)
            self._anchor = None
        if tag == "span" and self._date_span is not None:
            value = clean_text("".join(self._date_span)).strip("()（）[]【】 ")
            if self._rows and re.fullmatch(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", value):
                self._rows[-1]["dates"].append(value)
            self._date_span = None
        if tag == "li" and self._rows:
            row = self._rows.pop()
            for link in row["links"]:
                if link.get("_custom"):
                    link["title"] = clean_text(" ".join(row["parts"])) or link["title"]
            if row["heading"]:
                for link in row["links"]:
                    if clean_text(link["title"]).lower() in ("", "详情", "詳情", "查看详情", "查看詳情", "learn more", "view offer"):
                        link["title"] = row["heading"]
            # Only a single link paired with one standalone date. Dates in
            # anchor titles, activity prose, footer text or other rows do not count.
            if len(row["links"]) == len(row["dates"]) == 1:
                row["links"][0]["index_date"] = row["dates"][0]
        if self._heading is not None and tag == self._heading["tag"]:
            self._heading["row"]["heading"] = clean_text("".join(self._heading["parts"]))
            self._heading = None
        if self._link_heading is not None and tag == self._link_heading["tag"]:
            self._link_heading["anchor"]["heading_title"] = clean_text("".join(self._link_heading["parts"]))
            self._link_heading = None
        if tag == "script" and self._json is not None:
            try:
                self.structured.append(json.loads("".join(self._json)))
            except json.JSONDecodeError:
                pass
            self._json = None


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


class ArticleBody(HTMLParser):
    """Extract only explicitly configured article containers, never page chrome."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text, containers):
        super().__init__(convert_charrefs=True)
        self.containers, self.depth, self.skip, self.parts = set(containers), 0, 0, []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tokens = {attrs.get("id", ""), *attrs.get("class", "").split()}
        if not self.depth and self.containers.intersection(tokens):
            self.depth = 1
        elif self.depth and tag not in self.VOID:
            self.depth += 1
        if self.depth and tag in ("script", "style", "iframe"):
            self.skip += 1
        if self.depth and tag in ("p", "li", "br", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth:
            if tag in ("script", "style", "iframe") and self.skip:
                self.skip -= 1
            if tag not in self.VOID:
                self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts.append(data)


def article_evidence(text, source):
    """Keep a short source excerpt plus bank-labelled excerpts; not verification."""
    page = Page(text)
    body = "".join(ArticleBody(text, source.get("article_containers", [])).parts)
    excerpt = clean_text(body or page.meta.get("og:description") or page.meta.get("description", ""))[:320]
    fragments = []
    budget = 320
    if body:
        for line in body.splitlines():
            line = clean_text(line)
            if not line or not relevant_title(line, source):
                continue
            bank = bank_name(line, source)
            if bank == "多家机构 / 原文为准":
                continue
            snippet = line[:min(110, budget)]
            fragments.append({"bank": bank, "reported_text": snippet, "verified": False})
            budget -= len(snippet)
            if budget <= 0 or len(fragments) >= 4:
                break
    # Do not duplicate excerpts when a roundup supplied bank-specific segments.
    return {"source_excerpt": "" if fragments else excerpt, "reported_fragments": fragments}


class FetchError(Exception):
    def __init__(self, reason, status="error", http_status=None):
        super().__init__(reason)
        self.status, self.http_status = status, http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self, timeout=12):
        self.timeout = timeout
        self.policies = {}
        self.locks = {}
        self.last_request = {}
        self.guard = threading.Lock()

    def host_lock(self, host):
        with self.guard:
            return self.locks.setdefault(host, threading.RLock())

    def raw(self, url, robots=False):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/html, application/xml;q=0.9, */*;q=0.5"})
        opener = urllib.request.build_opener() if robots else urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as response:
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise FetchError("response_size_limit")
                charset = response.headers.get_content_charset() or "utf-8"
                if charset.lower() in ("gb2312", "gbk"):
                    charset = "gb18030"
                text = body.decode(charset, errors="replace")
                return text, response.status, response.geturl()
        except urllib.error.HTTPError as exc:
            if not robots and exc.code in (301, 302, 303, 307, 308):
                return "", exc.code, urllib.parse.urljoin(url, exc.headers.get("Location", ""))
            raise FetchError(f"http_{exc.code}", "blocked" if exc.code in (401, 403, 429) else "error", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"network_error: {type(exc).__name__}") from exc

    def policy(self, origin):
        if origin in self.policies:
            return self.policies[origin]
        try:
            text, _, _ = self.raw(origin + "/robots.txt", robots=True)
        except FetchError as exc:
            if exc.http_status in (404, 410):
                text = ""
            else:
                raise FetchError("robots_unavailable: " + str(exc), exc.status, exc.http_status) from exc
        if re.search(r"<\s*(?:html|!doctype\s+html)\b", text[:1500], re.I):
            raise FetchError("robots_returned_html_or_challenge", "blocked")
        # Parse wildcard and end-anchor rules (urllib.robotparser omits these).
        groups, agents, rules, delay = [], [], [], 0.0
        for rawline in text.splitlines() + ["User-agent: __end__"]:
            line = rawline.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules or delay:
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], 0.0
                agents.append(value.lower())
            elif key in ("allow", "disallow") and agents:
                if value:
                    rules.append((key == "allow", value))
            elif key == "crawl-delay" and agents:
                try:
                    delay = float(value)
                except ValueError:
                    pass
        user_agent = USER_AGENT.lower()
        matches = [(max((len(a) for a in a_list if a != "*" and a in user_agent), default=0), r, d)
                   for a_list, r, d in groups if "*" in a_list or any(a in user_agent for a in a_list)]
        specificity = max((m[0] for m in matches), default=-1)
        chosen_rules = [rule for spec, rule_list, _ in matches if spec == specificity for rule in rule_list]
        chosen_delay = max((delay for spec, _, delay in matches if spec == specificity), default=0)
        self.policies[origin] = (chosen_rules, chosen_delay)
        return self.policies[origin]

    def get(self, url):
        for _ in range(5):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.netloc or parts.username or parts.password:
                raise FetchError("unsafe_or_unsupported_url", "blocked")
            origin = f"{parts.scheme}://{parts.netloc}"
            with self.host_lock(origin):
                rules, delay = self.policy(origin)
                path = urllib.parse.unquote(parts.path or "/") + ("?" + urllib.parse.unquote(parts.query) if parts.query else "")
                matches = []
                for allow, rule in rules:
                    rule = urllib.parse.unquote(rule)
                    end = rule.endswith("$")
                    pattern = re.escape(rule[:-1] if end else rule).replace(r"\*", ".*")
                    if re.match(pattern + ("$" if end else ""), path):
                        matches.append((len(rule.replace("*", "").rstrip("$")), allow))
                if matches and not max(matches)[1]:
                    raise FetchError("robots_disallow", "blocked")
                remaining = max(0.75, delay) - (time.monotonic() - self.last_request.get(origin, 0))
                if remaining > 15:
                    raise FetchError(f"crawl_delay_deferred_{delay:g}s", "blocked")
                if remaining > 0:
                    time.sleep(remaining)
                self.last_request[origin] = time.monotonic()
                text, status, final_url = self.raw(url)
            if status in (301, 302, 303, 307, 308):
                if final_url == url:
                    raise FetchError("invalid_redirect")
                url = final_url
                continue
            final_parts = urllib.parse.urlsplit(final_url)
            if (final_parts.hostname in ("verify.meituan.com", "passport.meituan.com")
                    or re.search(r"/(?:geetest-middle|captcha|challenge)(?:/|$)", final_parts.path, re.I)):
                raise FetchError("verification_or_login_required", "blocked", status)
            return text, status, final_url
        raise FetchError("too_many_redirects")


def feed_entries(text, source):
    root = ET.fromstring(text)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        values = {}
        for child in element:
            key = child.tag.rsplit("}", 1)[-1]
            if key == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
                values["link"] = child.get("href")
            elif child.text and key not in values:
                values[key] = child.text
        yield {"title": clean_text(values.get("title", "")), "source_url": values.get("link", ""),
               "source_excerpt": clean_text(values.get("description") or values.get("summary") or values.get("encoded") or "")[:320],
               "published_raw": values.get("pubDate") or values.get("published") or values.get("date") or values.get("updated"),
               "publication_kind": "updated" if "updated" in values and not any(x in values for x in ("pubDate", "published", "date")) else "published",
               "date_evidence": "public_feed"}


def page_date(page: Page, text: str):
    for key in ("article:published_time", "datepublished", "pubdate", "publishdate", "publish_date", "date"):
        if page.meta.get(key):
            return page.meta[key], "article_metadata"
    for value in page.structured:
        for item in walk_json(value):
            if item.get("datePublished"):
                return item["datePublished"], "json_ld_datePublished"
    # Only explicit post labels; never any date found somewhere in a page.
    visible = clean_text(text)
    match = re.search(r"(?:发表于|發表於|发布时间|發佈時間|发布于)\s*[:：]?\s*(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)", visible)
    if match:
        return match.group(1), "labelled_post_timestamp"
    return None, "not_disclosed"


def html_entries(text, source, client):
    page = Page(text, source.get("link_attributes", ()))
    candidates = list(page.links)
    for structured in page.structured:
        for item in walk_json(structured):
            if item.get("url") and (item.get("name") or item.get("headline")):
                candidates.append({"url": item["url"], "title": item.get("headline") or item.get("name")})
    seen = set()
    pattern = re.compile(source.get("link_pattern", r"."))
    detail_errors = []
    accepted = []
    detail_requests = 0
    for entry in candidates:
        context = clean_text(entry.get("title", ""))
        title = clean_text(entry.get("heading_title") or context) if source.get("use_link_headings") else context
        url = urllib.parse.urljoin(source["url"], entry.get("url", ""))
        if url in seen or not pattern.search(url) or not relevant_title(title, source):
            continue
        allowed = source.get("link_domains", [urllib.parse.urlsplit(source["url"]).hostname])
        if urllib.parse.urlsplit(url).hostname not in allowed:
            continue
        seen.add(url)
        published = entry.get("index_date") if source.get("use_index_dates", False) else None
        evidence = "public_index_date" if published else "not_disclosed"
        excerpt = {}
        detail_state = "not_requested"
        if not published and detail_requests < source.get("max_detail_pages", 3):
            detail_requests += 1
            try:
                article, _, _ = client.get(url)
                published, evidence = page_date(Page(article), article)
                excerpt = article_evidence(article, source)
                detail_state = "readable"
            except FetchError as exc:
                detail_errors.append({"url": url, "reason": str(exc), "status": exc.status})
                detail_state = exc.status
        if published or source.get("allow_undated", False):
            accepted.append({"title": title, "source_url": url, "published_raw": published,
                             "index_context": context,
                             "date_evidence": evidence, "publication_kind": "published",
                             "detail_access_status": detail_state, **excerpt})
        if len(seen) >= source.get("max_index_candidates", 100):
            break
    return accepted, len(seen), detail_errors


def bank_name(title, source):
    names = [(r"\b(?:American Express|Amex|AE)\b|美國運通|美国运通", "American Express"), (r"\bChase\b", "Chase"), (r"\bCiti\b", "Citi"),
             (r"\bMox\b", "Mox"), (r"\bAlliant\b", "Alliant Credit Union"), (r"\bOnePay\b", "OnePay"),
             (r"\bCapital One\b", "Capital One"), (r"工銀亞洲|工银亚洲|\bICBC Asia\b", "工银亚洲"),
             (r"\bDBS\b|星展", "DBS / 星展"), (r"\bBEA\b|東亞|东亚", "东亚银行"),
             (r"Wells Fargo", "Wells Fargo"), (r"U\.?S\.?\s*Bank|USBank", "U.S. Bank"),
             (r"Bank of America|\bBoA\b", "Bank of America"), (r"HSBC|滙豐|匯豐|汇丰", "HSBC / 汇丰"),
             (r"工商|工行", "工商银行"), (r"建设|建行", "建设银行"), (r"农业|农行", "农业银行"),
             (r"交通|交行", "交通银行"), (r"招商|招行", "招商银行"), (r"中国银行|中行", "中国银行"),
             (r"邮储|邮政储蓄", "邮储银行"), (r"中信", "中信银行"), (r"浦发|浦發", "浦发银行"),
             (r"广发|廣發", "广发银行"), (r"兴业|興業", "兴业银行"), (r"民生", "民生银行"),
             (r"光大", "光大银行"), (r"平安", "平安银行"), (r"华夏|華夏", "华夏银行"), (r"渣打", "渣打银行"),
             (r"恒生", "恒生银行"), (r"中銀", "中银香港"), (r"银联|銀聯|云闪付|雲閃付", "银联 / 云闪付")]
    found = [name for pattern, name in names if re.search(pattern, title, re.I)]
    return " / ".join(found[:3]) if found else source.get("bank", "多家机构 / 原文为准")


def excerpt_deadline(text):
    """Read only explicit, fully dated activity expiry labels, never post dates."""
    dates = []
    label = r"(?:valid through|expires?(?: on)?|ends?(?: on)?|extended through)\s*"
    for m in re.finditer(label + r"(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})(?!\d)", text, re.I):
        try:
            year = int(m[3]); dates.append(dt.date(year if year >= 2000 else 2000 + year, int(m[1]), int(m[2])).isoformat())
        except ValueError:
            pass
    months = 'January February March April May June July August September October November December'.split()
    for m in re.finditer(label + r"(" + '|'.join(months) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", text, re.I):
        try:
            dates.append(dt.date(int(m[3]), [x.lower() for x in months].index(m[1].lower()) + 1, int(m[2])).isoformat())
        except ValueError:
            pass
    # Multiple different deadlines can refer to different merchants/offers.
    return dates[0] if len(set(dates)) == 1 else None


def normalize(entry, source, now, target_day):
    full_title = clean_text(entry.get("index_context") or entry.get("title", ""))
    title = clean_text(entry.get("title", ""))[:220]
    url = entry.get("source_url", "").strip()
    if not title or not url.startswith(("https://", "http://")) or EXPIRED.search(title) or EDITORIAL.search(title):
        return None
    if not relevant_title(title, source):
        return None
    published, precision = date_value(entry.get("published_raw"), source["timezone"])
    if published:
        when = date_for_comparison(published, source["timezone"])
        if when > now + dt.timedelta(hours=1) or now - when > dt.timedelta(days=source.get("max_age_days", 14)):
            return None
    elif not source.get("allow_undated", False):
        return None
    end_date = None
    deadline = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s*(?:截止|结束|結束)", full_title)
    if not deadline and source.get("title_deadline_label", False):
        deadline = re.search(r"至\s*(20\d{2})年(\d{1,2})月(\d{1,2})日", full_title)
    if deadline:
        try:
            end_date = dt.date(*(int(x) for x in deadline.groups())).isoformat()
        except ValueError:
            pass
    if not end_date and source.get("title_activity_range", False):
        year = re.search(r"(20\d{2})年", full_title)
        period = re.search(r"[（(](\d{1,2})月(\d{1,2})日\s*[-至—–~～]\s*(\d{1,2})月(\d{1,2})日活动规则[）)]", full_title)
        if year and period and int(period[3]) >= int(period[1]):
            try:
                end_date = dt.date(int(year[1]), int(period[3]), int(period[4])).isoformat()
            except ValueError:
                pass
    if not end_date and source.get("use_excerpt_activity_deadlines"):
        end_date = excerpt_deadline(clean_text(entry.get("source_excerpt", "")))
    if end_date and end_date < target_day.isoformat():
        return None
    note = "原文发布日期未确认；仅表示本次在公开列表发现，不代表新帖或活动仍有效。" if not published else "保留原文日期；该日期不等于活动开始日或有效期。"
    if precision == "timezone_unconfirmed":
        note += "原文时间未标注时区。"
    if entry.get("publication_kind") == "updated":
        note = "来源仅提供更新时间，不视为首次发布日期。" + note
    excerpt = clean_text(entry.get("source_excerpt", ""))[:320]
    fragments = entry.get("reported_fragments", [])
    reported = title if source.get("facts_from_title_only") else " ".join([full_title, excerpt, *(x["reported_text"] for x in fragments)])
    # Publish a factual digest rather than copying source prose. Amounts may be
    # spending thresholds or rewards, so do not label them as verified rewards.
    amounts = list(dict.fromkeys(re.findall(r"(?:HK\$|US\$|\$|¥|￥)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:%|％|元|港元|美元|积分|積分|里數|points|miles)|(?:最高减|首单减)\s*\d+(?:\.\d+)?", reported, re.I)))[:5]
    institutions = list(dict.fromkeys(x["bank"] for x in fragments))
    detail_parts = []
    if institutions:
        detail_parts.append("报道涉及" + "、".join(institutions))
    if amounts:
        detail_parts.append("原文出现的金额或比例：" + "、".join(amounts))
    details = "；".join(detail_parts) + "。具体门槛、奖励对应关系及有效期待核。" if detail_parts else "公开列表已发现该优惠；原文未提取到可独立表述的金额或条件，请核对详情。"
    mixed_periods = source.get("mixed_periods_title_pattern") and re.search(source["mixed_periods_title_pattern"], title, re.I)
    if mixed_periods:
        amounts = []
        details = "原文汇集多个商户且截止日不同；当前适用商户与回赠须逐项重新核对。"
    title_bonus = re.search(r"\$([\d,]+)\s+(?:(?:checking|savings|account|cash)\s+)*bonus", title, re.I)
    excerpt_bonus = re.search(r"bonus(?:\s+of)?\s+\$([\d,]+)", excerpt, re.I)
    claim_conflict = bool(title_bonus and excerpt_bonus and title_bonus[1].replace(',', '') != excerpt_bonus[1].replace(',', ''))
    if claim_conflict:
        amounts = []
        details = "标题与来源摘要的奖励数字不一致，需重新核对原文更新及官方条款；暂不采用摘要金额。"
    category = "credit_card" if re.search(r"信用卡|credit.?card|Amex|Chase", title, re.I) else "bank_payment"
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:20], "region": source["region"],
            "bank": bank_name(title, source), "title": title,
            "details": details, "source_excerpt": excerpt, "reported_fragments": fragments, "reported_amounts": amounts,
            "source_claim_conflict": claim_conflict,
            "detail_access_status": entry.get("detail_access_status"),
            "source_url": url, "start_date": None, "end_date": end_date, "verified": False,
            "published_at": published, "published_at_precision": precision, "publication_note": note,
            "publication_kind": entry.get("publication_kind", "published"), "date_evidence": entry.get("date_evidence"),
            "collected_at": now.isoformat(timespec="seconds"), "status": "unverified", "category": category,
            "origin": source["name"], "source_id": source["id"], "source_type": source.get("source_type", "community"),
            "channel_category": source.get("channel_category", "banking")}


def collect_source(source, client, now, target_day):
    status = {"id": source["id"], "name": source["name"], "region": source["region"], "url": source["url"],
              "source_type": source.get("source_type", "community"), "channel_category": source.get("channel_category", "banking"),
              "checked_at": now_iso(), "status": "empty", "http_status": None, "items_seen": 0, "items_accepted": 0}
    if not source.get("enabled", True):
        status.update(status="disabled", reason=source.get("disabled_reason", "disabled"))
        return [], status
    try:
        text, http_status, final_url = client.get(source["url"])
        status.update(http_status=http_status, final_url=final_url)
        if source["method"] == "rss":
            entries = list(feed_entries(text, source))
            status["items_seen"] = len(entries)
        else:
            entries, count, detail_errors = html_entries(text, source, client)
            status.update(items_seen=count)
            if not Page(text, source.get("link_attributes", ())).links and re.search(r'<script\b[^>]*\bsrc=', text, re.I):
                status["discovery_note"] = "javascript_only_index_no_static_links"
            if detail_errors:
                status["detail_errors"] = detail_errors
        offers = []
        for entry in entries:
            record = normalize(entry, source, now, target_day)
            if record:
                offers.append(record)
            if len(offers) >= source.get("max_items", 6):
                break
        status.update(status="ok" if offers else "empty", items_accepted=len(offers),
                      reason="public_leads_collected" if offers else "no_recent_dated_financial_leads")
        if not offers and not status["items_seen"]:
            status["reason"] = "no_matching_activity_links_in_public_html" if source["method"] == "html" else "feed_contains_no_entries"
            if source.get("require_specific_offer_title"):
                status["reason"] = "no_specific_offer_terms_in_parsed_titles"
        if not offers and status.get("discovery_note"):
            status["reason"] = status["discovery_note"]
        return offers, status
    except FetchError as exc:
        status.update(status=exc.status, http_status=exc.http_status, reason=str(exc))
    except (ET.ParseError, ValueError, LookupError) as exc:
        status.update(status="error", reason="parse_error: " + type(exc).__name__)
    return [], status


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def carry_history(offers, previous):
    by_id = {o.get("id"): o for o in previous if o.get("id")}
    by_url = {o.get("source_url"): o for o in previous if o.get("source_url")}
    new_count = 0
    for offer in offers:
        prior = by_id.get(offer["id"]) or by_url.get(offer["source_url"])
        offer["first_collected_at"] = prior.get("first_collected_at", prior.get("collected_at")) if prior else offer["collected_at"]
        new_count += prior is None
    return new_count


def retain_failed_sources(offers, previous, statuses, sources, now, target_day):
    """Keep age-bounded old leads only when their whole source failed this run."""
    failed = {s["id"]: s for s in statuses if s["status"] in ("blocked", "error")}
    configured = {s["id"]: s for s in sources}
    known = {o["id"] for o in offers}
    retained = 0
    for prior in previous:
        sid = prior.get("source_id")
        source = configured.get(sid)
        if sid not in failed or not source or prior.get("id") in known:
            continue
        if prior.get("end_date") and prior["end_date"] < target_day.isoformat():
            continue
        freshness = prior.get("published_at") or prior.get("collected_at")
        parsed, _ = date_value(freshness, source["timezone"])
        if not parsed or now - date_for_comparison(parsed, source["timezone"]) > dt.timedelta(days=source.get("max_age_days", 14)):
            continue
        entry = {"title": prior.get("title", ""), "source_url": prior.get("source_url", ""),
                 "published_raw": prior.get("published_at"), "source_excerpt": prior.get("source_excerpt", "")}
        if normalize(entry, source, now, target_day) is None:
            continue
        old = dict(prior)
        old["retained_after_source_failure"] = True
        old["retained_source_failure_reason"] = failed[sid].get("reason", failed[sid]["status"])
        failed[sid]["retained_items"] = failed[sid].get("retained_items", 0) + 1
        offers.append(old)
        known.add(old["id"])
        retained += 1
    return retained


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD; default rolls to tomorrow at 21:00 Beijing")
    parser.add_argument("--sources", type=Path, default=BASE / "sources.json")
    parser.add_argument("--output", type=Path, default=BASE / "collected_offers.json")
    parser.add_argument("--status", type=Path, default=BASE / "collection_status.json")
    parser.add_argument("--previous", type=Path, help="history baseline for an isolated run; defaults to --output")
    parser.add_argument("--source", action="append", help="limit to one or more configured source IDs")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    now = dt.datetime.now(UTC)
    target_day = dt.date.fromisoformat(args.date) if args.date else report_day(now)
    config = json.loads(args.sources.read_text(encoding="utf-8"))
    sources = config["sources"]
    if args.source:
        sources = [s for s in sources if s["id"] in args.source]
    client = PublicClient(timeout=config.get("request_timeout_seconds", 12))
    offers, statuses = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {pool.submit(collect_source, s, client, now, target_day): s for s in sources}
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                records, status = future.result()
            except Exception as exc:
                records, status = [], {"id": source["id"], "name": source["name"], "region": source["region"], "url": source["url"],
                                       "checked_at": now_iso(), "status": "error", "reason": "collector_error: " + type(exc).__name__,
                                       "http_status": None, "items_seen": 0, "items_accepted": 0}
            offers.extend(records)
            statuses.append(status)
            print(f"{source['id']}: {status['status']} ({len(records)} leads)", flush=True)
    unique = {offer["source_url"]: offer for offer in offers}
    offers = sorted(unique.values(), key=lambda o: (o.get("published_at") or "", o["source_url"]), reverse=True)
    previous = []
    history_path = args.previous or args.output
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            previous = data.get("offers", [])
        except (ValueError, KeyError, AttributeError):
            pass
    retained_count = retain_failed_sources(offers, previous, statuses, sources, now, target_day)
    new_count = carry_history(offers, previous)
    successes = sum(s["status"] in ("ok", "empty") for s in statuses)
    failures = sum(s["status"] in ("blocked", "error") for s in statuses)
    overall = ("partial" if failures else "ok") if offers else ("empty" if successes else "failed")
    finished = now_iso()
    summary = {"schema_version": 1, "report_date": target_day.isoformat(), "attempted_at": now.isoformat(timespec="seconds"),
               "finished_at": finished, "status": overall, "source_count": len(sources), "success_count": successes,
               "offer_count": len(offers), "new_count": new_count, "previous_data_preserved": not bool(len(offers) - retained_count) and args.output.exists(),
               "sources": sorted(statuses, key=lambda s: (s["region"], s["id"]))}
    summary["sources_with_leads"] = sum(s.get("items_accepted", 0) > 0 for s in statuses)
    summary["retained_count"] = retained_count
    summary["fresh_offer_count"] = len(offers) - retained_count
    summary["fresh_region_leads"] = {region: sum(o["region"] == region and not o.get("retained_after_source_failure") for o in offers) for region in ("cn", "hk", "us")}
    summary["retained_region_leads"] = {region: sum(o["region"] == region and o.get("retained_after_source_failure", False) for o in offers) for region in ("cn", "hk", "us")}
    summary["source_results"] = {"attempted": sum(s["status"] != "disabled" for s in statuses),
                                 "producing": summary["sources_with_leads"],
                                 **{state: sum(s["status"] == state for s in statuses) for state in ("empty", "blocked", "error", "disabled")}}
    summary["region_leads"] = {region: sum(o["region"] == region for o in offers) for region in ("cn", "hk", "us")}
    summary["coverage_gaps"] = config.get("coverage_gaps", [])
    # A failed/empty run must not relabel old records or advance their timestamp.
    if offers:
        collected_time = now.isoformat(timespec="seconds") if len(offers) > retained_count else max(o["collected_at"] for o in offers)
        write_json(args.output, {"schema_version": 1, "report_date": target_day.isoformat(), "generated_at": finished,
                               "collected_at": collected_time, "status": overall,
                               "new_count": new_count, "retained_count": retained_count, "offers": offers})
    write_json(args.status, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "sources"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
