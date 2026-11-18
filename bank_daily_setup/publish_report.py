#!/usr/bin/env python3
"""Publish one explicit report and its content revision to the existing PWA."""
import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
REVISION_PLACEHOLDER = '__REPORT_REVISION__'
PWA_HEAD_SELECTORS = (
    'link[rel=manifest]', 'link[rel=apple-touch-icon]', 'meta[name=theme-color]',
    'meta[name=apple-mobile-web-app-capable]', 'meta[name=apple-mobile-web-app-status-bar-style]',
    'meta[name=apple-mobile-web-app-title]', 'meta[name=description]', 'style#pwa-mobile-style',
)


def set_metadata(soup, metadata):
    element = soup.select_one('#report-metadata')
    if not element:
        element = soup.new_tag('script', attrs={'id': 'report-metadata', 'type': 'application/json'})
        soup.head.append(element)
    element.string = json.dumps(metadata, ensure_ascii=False, sort_keys=True).replace('</', '<\\/')


def set_revision(soup, revision):
    element = soup.select_one('meta[name="report-revision"]')
    if not element:
        element = soup.new_tag('meta', attrs={'name': 'report-revision'})
        soup.head.append(element)
    element['content'] = revision
    script = soup.select_one('#pwa-app-script')
    if script:
        content = script.string or script.get_text()
        script.string = re.sub(r"\bconst REV\s*=\s*(['\"])[^'\"]*\1", f"const REV='{revision}'", content)


def publish_report(source, site_dir=None, now=None):
    source = Path(source).resolve()
    site_dir = Path(site_dir or BASE.parent).resolve()
    current_path = site_dir / 'index.html'
    latest_path = site_dir / 'latest.json'
    if not source.is_file() or not current_path.is_file():
        raise ValueError('Report and current index.html must both exist; no files changed')
    new = BeautifulSoup(source.read_text(encoding='utf-8'), 'html.parser')
    old = BeautifulSoup(current_path.read_text(encoding='utf-8'), 'html.parser')
    if not new.head or not new.body:
        raise ValueError('Report must have a head and body; no files changed')
    if not old.head or not old.body:
        raise ValueError('Current index.html is invalid; no files changed')
    meta_tag = new.select_one('#report-metadata')
    metadata = json.loads(meta_tag.get_text()) if meta_tag else {}
    if not isinstance(metadata, dict):
        raise ValueError('Invalid report metadata; no files changed')
    match = re.search(r'20\d{2}-\d{2}-\d{2}', source.name)
    date = metadata.get('report_date') or metadata.get('date') or (match.group(0) if match else None)
    if not date:
        raise ValueError('Report must declare its date; no files changed')
    dt.date.fromisoformat(date)
    if match and match.group(0) != date:
        raise ValueError('Report date disagrees with filename; no files changed')
    metadata.update({'date': date, 'report_date': date})
    metadata.setdefault('generated_at', (now or dt.datetime.now(ZoneInfo('Asia/Shanghai'))).isoformat(timespec='seconds'))
    metadata.setdefault('collected_at', None)

    # Preserve current app additions. The canonical template supplies the newest PWA logic.
    for selector in PWA_HEAD_SELECTORS:
        if selector == 'style#pwa-mobile-style' and new.head.select_one('style#report-style'):
            continue
        if not new.head.select_one(selector):
            existing = old.head.select_one(selector)
            if existing:
                new.head.append(existing.extract())
    app_template = BASE / 'report_template.html'
    template = BeautifulSoup(app_template.read_text(encoding='utf-8'), 'html.parser') if app_template.exists() else old
    for selector in ('#app-update-banner', '#pwa-app-script'):
        current = template.select_one(selector)
        if current:
            for obsolete in new.select(selector):
                obsolete.decompose()
            new.body.append(current.extract())
        elif not new.select_one(selector):
            existing = old.select_one(selector)
            if existing:
                new.body.append(existing.extract())
    script = new.select_one('#pwa-app-script')
    if script:
        script.string = re.sub(r"\bconst BUILD\s*=\s*(['\"])[^'\"]*\1", f"const BUILD='{date}'",
                               script.string or script.get_text())

    # Exclude the clock and revision itself: re-running identical content is idempotent.
    hash_metadata = {key: value for key, value in metadata.items() if key not in ('generated_at', 'revision')}
    set_metadata(new, hash_metadata)
    set_revision(new, REVISION_PLACEHOLDER)
    revision = hashlib.sha256(str(new).encode('utf-8')).hexdigest()[:20]
    try:
        previous = json.loads(latest_path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    if isinstance(previous, dict) and previous.get('revision') == revision and previous.get('generated_at'):
        metadata['generated_at'] = previous['generated_at']
    metadata['revision'] = revision
    set_metadata(new, metadata)
    set_revision(new, revision)

    output = str(new)
    test = BeautifulSoup(output, 'html.parser')
    for selector in ('#region-cn', '#region-hk', '#region-us', '#pwa-app-script', '#report-metadata',
                     'meta[name="report-revision"]'):
        if len(test.select(selector)) != 1:
            raise ValueError(f'Expected exactly one {selector}; no files changed')
    if json.loads(test.select_one('#report-metadata').get_text()) != metadata:
        raise ValueError('Report metadata failed validation; no files changed')
    manifest = json.dumps(metadata, ensure_ascii=False, indent=2) + '\n'
    # Validate both outputs before replacing either; generation failures preserve the live page.
    page_tmp = current_path.with_name('index.html.tmp')
    manifest_tmp = latest_path.with_name('latest.json.tmp')
    try:
        page_tmp.write_text(output, encoding='utf-8')
        manifest_tmp.write_text(manifest, encoding='utf-8')
        page_tmp.replace(current_path)
        manifest_tmp.replace(latest_path)
        # The dated archive carries exactly the same revision as the live page.
        if source != current_path:
            source.write_text(output, encoding='utf-8')
    finally:
        page_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
    return metadata


def main(argv=None, default_site_dir=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='explicit dated report to publish')
    parser.add_argument('--site-dir', type=Path, default=default_site_dir or BASE.parent)
    args = parser.parse_args(argv)
    metadata = publish_report(args.report, args.site_dir)
    print(f"Ready to publish: {metadata['report_date']} revision {metadata['revision']}")


if __name__ == '__main__':
    main()
