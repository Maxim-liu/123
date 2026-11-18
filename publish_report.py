#!/usr/bin/env python3
"""在本地用新日报替换 App 首页；需另外将生成文件上传至 Cloudflare。"""
from pathlib import Path
from bs4 import BeautifulSoup
import sys,re,json
if len(sys.argv)!=2:raise SystemExit('用法: python publish_report.py <次日日报.html>')
app=Path(__file__).resolve().parent; src=Path(sys.argv[1]);m=re.search(r'20\d{2}-\d{2}-\d{2}',src.name)
if not m:raise SystemExit('日报文件名必须包含 YYYY-MM-DD')
date=m.group(0);new=BeautifulSoup(src.read_text(encoding='utf8'),'html.parser');old=BeautifulSoup((app/'index.html').read_text(encoding='utf8'),'html.parser')
for t in old.head.select('link[rel=manifest],link[rel=apple-touch-icon],meta[name=theme-color],meta[name=apple-mobile-web-app-capable],meta[name=apple-mobile-web-app-status-bar-style],meta[name=apple-mobile-web-app-title],meta[name=description],style#pwa-mobile-style'):new.head.append(t.extract())
bar=old.select_one('#app-update-banner');script=old.select_one('#pwa-app-script')
if bar:new.body.append(bar.extract())
if script:script.string=(script.string or '').replace("const BUILD='2026-11-18'",f"const BUILD='{date}'") if "const BUILD='2026-11-18'" in (script.string or '') else re.sub(r"const BUILD='20\d{2}-\d{2}-\d{2}'",f"const BUILD='{date}'",script.string or '');new.body.append(script.extract())
(app/'index.html').write_text(str(new),encoding='utf8');(app/'latest.json').write_text(json.dumps({'date':date},ensure_ascii=False,indent=2),encoding='utf8');print('已准备发布',date)
