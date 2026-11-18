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
BENEFIT = re.compile(r"优惠|優惠|立减|立減|刷卡金|红包|紅包|返现|返現|回贈|獎賞|奖励|獎勵|里數|积分|積分|福利|迎新|活动|活動|羊毛|满减|滿減|兑换|兌換|领券|領券|\d(?:\.\d)?折|gift|bonus|offer|cash.?back|credit|rebate|reward|\$|%|％", re.I)
EXPIRED = re.compile(r"\b(?:dead|expired|ended)\b|已结束|已結束|已过期|已過期|已截止|失效", re.I)
EDITORIAL = re.compile(r"^Best credit card combinations|^How (?:to|do)|^What (?:is|are)|^Weekly (?:review|roundup)", re.I)


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
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: list[dict] = []
        self.structured: list = []
        self.times: list[str] = []
        self._anchor = None
        self._json = None
        self._rows = []
        self._date_span = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            self._rows.append({"links": [], "dates": []})
        if tag == "span" and self._rows and self._anchor is None:
            self._date_span = []
        if tag == "meta":
            self.meta[(a.get("property") or a.get("name") or a.get("itemprop") or "").lower()] = a.get("content", "")
        if tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        if tag == "a" and a.get("href"):
            self._anchor = {"url": a["href"], "title": "", "hint": a.get("title", "")}
        if tag == "script" and "ld+json" in a.get("type", ""):
            self._json = []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["title"] += data
        if self._json is not None:
            self._json.append(data)
        if self._date_span is not None:
            self._date_span.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self._anchor["title"] = clean_text(self._anchor["title"] or self._anchor["hint"])
            self.links.append(self._anchor)
            if self._rows:
                self._rows[-1]["links"].append(self._anchor)
            self._anchor = None
        if tag == "span" and self._date_span is not None:
            value = clean_text("".join(self._date_span))
            if self._rows and re.fullmatch(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", value):
                self._rows[-1]["dates"].append(value)
            self._date_span = None
        if tag == "li" and self._rows:
            row = self._rows.pop()
            # Only a single link paired with one standalone date. Dates in
            # anchor titles, activity prose, footer text or other rows do not count.
            if len(row["links"]) == len(row["dates"]) == 1:
                row["links"][0]["index_date"] = row["dates"][0]
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
                body = response.read(2_500_001)
                if len(body) > 2_500_000:
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
    page = Page(text)
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
        title = clean_text(entry.get("title", ""))
        url = urllib.parse.urljoin(source["url"], entry.get("url", ""))
        if url in seen or not pattern.search(url) or not relevant_title(title, source):
            continue
        allowed = source.get("link_domains", [urllib.parse.urlsplit(source["url"]).hostname])
        if urllib.parse.urlsplit(url).hostname not in allowed:
            continue
        seen.add(url)
        published = entry.get("index_date") if source.get("use_index_dates", False) else None
        evidence = "public_index_date" if published else "not_disclosed"
        if not published and detail_requests < source.get("max_detail_pages", 3):
            detail_requests += 1
            try:
                article, _, _ = client.get(url)
                published, evidence = page_date(Page(article), article)
            except FetchError as exc:
                detail_errors.append({"url": url, "reason": str(exc), "status": exc.status})
        if published or source.get("allow_undated", False):
            accepted.append({"title": title, "source_url": url, "published_raw": published,
                             "date_evidence": evidence, "publication_kind": "published"})
        if len(seen) >= source.get("max_index_candidates", 100):
            break
    return accepted, len(seen), detail_errors


def bank_name(title, source):
    names = [(r"\b(?:American Express|Amex)\b", "American Express"), (r"\bChase\b", "Chase"), (r"\bCiti\b", "Citi"),
             (r"Wells Fargo", "Wells Fargo"), (r"U\.?S\.?\s*Bank|USBank", "U.S. Bank"),
             (r"Bank of America|\bBoA\b", "Bank of America"), (r"HSBC|滙豐|匯豐|汇丰", "HSBC / 汇丰"),
             (r"工商|工行", "工商银行"), (r"建设|建行", "建设银行"), (r"农业|农行", "农业银行"),
             (r"交通|交行", "交通银行"), (r"招商|招行", "招商银行"), (r"中国银行|中行", "中国银行"),
             (r"邮储|邮政储蓄", "邮储银行"), (r"中信", "中信银行"), (r"渣打", "渣打银行"),
             (r"恒生", "恒生银行"), (r"中銀", "中银香港"), (r"银联|銀聯|云闪付|雲閃付", "银联 / 云闪付")]
    found = [name for pattern, name in names if re.search(pattern, title, re.I)]
    return " / ".join(found[:3]) if found else source.get("bank", "多家机构 / 原文为准")


def normalize(entry, source, now, target_day):
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
    deadline = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\s*(?:截止|结束|結束)", title)
    if deadline:
        try:
            end_date = dt.date(*(int(x) for x in deadline.groups())).isoformat()
        except ValueError:
            pass
    if end_date and end_date < target_day.isoformat():
        return None
    note = "原文发布日期未确认；仅表示本次在公开列表发现，不代表新帖或活动仍有效。" if not published else "保留原文日期；该日期不等于活动开始日或有效期。"
    if precision == "timezone_unconfirmed":
        note += "原文时间未标注时区。"
    if entry.get("publication_kind") == "updated":
        note = "来源仅提供更新时间，不视为首次发布日期。" + note
    category = "credit_card" if re.search(r"信用卡|credit.?card|Amex|Chase", title, re.I) else "bank_payment"
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:20], "region": source["region"],
            "bank": bank_name(title, source), "title": title,
            "details": "公开来源线索，尚未完成活动条件与有效期核验。请查看原文，并以银行或支付机构的官方条款、实时名额为准。",
            "source_url": url, "start_date": None, "end_date": end_date, "verified": False,
            "published_at": published, "published_at_precision": precision, "publication_note": note,
            "publication_kind": entry.get("publication_kind", "published"), "date_evidence": entry.get("date_evidence"),
            "collected_at": now.isoformat(timespec="seconds"), "status": "unverified", "category": category,
            "origin": source["name"], "source_id": source["id"], "source_type": source.get("source_type", "community")}


def collect_source(source, client, now, target_day):
    status = {"id": source["id"], "name": source["name"], "region": source["region"], "url": source["url"],
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
            if not Page(text).links and re.search(r'<script\b[^>]*\bsrc=', text, re.I):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD; default rolls to tomorrow at 21:00 Beijing")
    parser.add_argument("--sources", type=Path, default=BASE / "sources.json")
    parser.add_argument("--output", type=Path, default=BASE / "collected_offers.json")
    parser.add_argument("--status", type=Path, default=BASE / "collection_status.json")
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
    previous = {}
    if args.output.exists():
        try:
            data = json.loads(args.output.read_text(encoding="utf-8"))
            previous = {o["source_url"]: o for o in data.get("offers", [])}
        except (ValueError, KeyError, AttributeError):
            pass
    new_count = 0
    for offer in offers:
        prior = previous.get(offer["source_url"])
        offer["first_collected_at"] = prior.get("first_collected_at", prior.get("collected_at")) if prior else offer["collected_at"]
        new_count += prior is None
    successes = sum(s["status"] in ("ok", "empty") for s in statuses)
    failures = sum(s["status"] in ("blocked", "error") for s in statuses)
    overall = ("partial" if failures else "ok") if offers else ("empty" if successes else "failed")
    finished = now_iso()
    summary = {"schema_version": 1, "report_date": target_day.isoformat(), "attempted_at": now.isoformat(timespec="seconds"),
               "finished_at": finished, "status": overall, "source_count": len(sources), "success_count": successes,
               "offer_count": len(offers), "new_count": new_count, "previous_data_preserved": not bool(offers) and args.output.exists(),
               "sources": sorted(statuses, key=lambda s: (s["region"], s["id"]))}
    # A failed/empty run must not relabel old records or advance their timestamp.
    if offers:
        write_json(args.output, {"schema_version": 1, "report_date": target_day.isoformat(), "generated_at": finished,
                               "collected_at": now.isoformat(timespec="seconds"), "status": overall,
                               "new_count": new_count, "offers": offers})
    write_json(args.status, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "sources"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
