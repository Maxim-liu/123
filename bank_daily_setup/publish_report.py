#!/usr/bin/env python3
"""Merge a dated report into the PWA while preserving PWA assets and scripts."""
import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python publish_report.py <dated-report.html>')
    base = Path(__file__).resolve().parent
    source = Path(sys.argv[1])
    match = re.search(r'20\d{2}-\d{2}-\d{2}', source.name)
    if not match or not source.is_file():
        raise SystemExit('Report must exist and filename must contain YYYY-MM-DD')
    date = match.group(0)
    current_path = base / 'index.html'
    if not current_path.is_file():
        raise SystemExit('Missing index.html; aborting without changing anything')
    new = BeautifulSoup(source.read_text(encoding='utf-8'), 'html.parser')
    old = BeautifulSoup(current_path.read_text(encoding='utf-8'), 'html.parser')
    if not new.head or not new.body or not new.select_one('#region-cn') or not new.select_one('#region-hk') or not new.select_one('#region-us'):
        raise SystemExit('Invalid report: missing head/body or region panels')
    # Copy only missing PWA head elements; never duplicate existing ones.
    selectors = ('link[rel=manifest]', 'link[rel=apple-touch-icon]', 'meta[name=theme-color]',
                 'meta[name=apple-mobile-web-app-capable]', 'meta[name=apple-mobile-web-app-status-bar-style]',
                 'meta[name=apple-mobile-web-app-title]', 'meta[name=description]', 'style#pwa-mobile-style')
    for selector in selectors:
        if not new.head.select_one(selector):
            existing = old.head.select_one(selector)
            if existing:
                new.head.append(existing.extract())
    for selector in ('#app-update-banner', '#pwa-app-script'):
        if not new.select_one(selector):
            existing = old.select_one(selector)
            if existing:
                new.body.append(existing.extract())
    script = new.select_one('#pwa-app-script')
    if script:
        script.string = re.sub(r"const BUILD\s*=\s*['\"]20\d{2}-\d{2}-\d{2}['\"]", f"const BUILD='{date}'", script.string or script.get_text())
    # Validate output before replacing live files.
    output = str(new)
    test = BeautifulSoup(output, 'html.parser')
    for selector in ('#region-cn', '#region-hk', '#region-us', '#pwa-app-script'):
        if len(test.select(selector)) != 1:
            raise SystemExit(f'Expected exactly one {selector}; no files changed')
    temp = current_path.with_suffix('.html.tmp')
    temp.write_text(output, encoding='utf-8')
    temp.replace(current_path)
    (base / 'latest.json').write_text(json.dumps({'date': date}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Ready to publish:', date)


if __name__ == '__main__':
    main()
