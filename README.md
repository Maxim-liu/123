# 银行每日线报

中国大陆、中国香港、美国三区的银行、信用卡与里程活动日报，保留原有分类、搜索筛选、稀缺活动及高价值活动板块。网站入口为仓库根目录的 `index.html`，可作为 PWA 添加到手机主屏幕。

## 每日更新

实际启用的任务是 `.github/workflows/daily-report.yml`，每天 13:00 UTC（北京时间 21:00）依次执行：

1. 从 `bank_daily_setup/sources.json` 中配置的公开来源采集线索。
2. 合并 `verified_offers.json` 中经来源核对的活动，生成目标日期的日报。
3. 将本期日报直接发布到根 `index.html` 和 `latest.json`，提交变更供现有网站托管流程发布。

任务没有邮件步骤。GitHub 定时任务可能延迟；需要补跑时可在 Actions 中运行 **Daily Report**，按需填写 `report_date`。留空会采用北京时间最近一次 21:00 发布周期：21:00 后生成次日，21:00 前补跑当前日。日报覆盖目标日期北京时间 00:00—23:59，不通过目录内“最大文件日期”选择报告。

## 本地生成与发布

在仓库根目录运行：

```bash
python -m pip install 'beautifulsoup4>=4.12,<5'
python -m unittest discover -s tests -v
python bank_daily_setup/collect_offers.py --date 2026-11-19
python bank_daily_setup/generate_report.py --date 2026-11-19
```

最后一条命令同时生成本期存档并发布根目录页面，无需再复制文件。检查根 `latest.json` 的 `report_date`、`revision` 与页面中 `report-metadata` 是否一致。更改实际内容会产生新 `revision`，同日更正可被客户端识别；相同内容重跑不刷新版本与生成时间。

只预览而不修改网站入口：

```bash
python bank_daily_setup/generate_report.py --date 2026-11-19 --no-publish --output-dir /tmp/bank-report-preview
```

发布一个明确选定的已有存档：

```bash
python publish_report.py 'bank_daily_setup/银行每日线报（2026-11-19）.html'
```

根 `publish_report.py` 是同一个已验证发布器的兼容入口。`bank_daily_setup/index.html` 和该目录的 `latest.json` 是旧安装包中的文件，不再作为每日发布来源；不要将它们复制覆盖根文件。`bank_daily_setup/daily-report.yml` 仅是工作流参考副本，GitHub 实际读取 `.github/workflows/daily-report.yml`。

## 数据的含义

- `verified_offers.json` 是经来源核对的人工维护记录，格式为 JSON 数组。只有 `verified: true`、有效来源 URL、明确的 `start_date` 与 `end_date`，且目标日报日期落在该区间内，才进入“已核实活动”。是否仍有名额、个人是否合资格仍以来源为准。
- 已核对当前官方页面但没有公开完整起止日期的活动，可填写 `verification_scope: "current_page"`、`verified: true` 和实际 `verified_at`。这类记录显示“本期活动时间及个人资格待确认”，不会被当作已证明目标日有效的活动。
- `collected_offers.json` 保存实际取得的公开线索及 `collected_at`。自动采集不等于独立核验，未核验记录明确标记并附原始来源。
- `collection_status.json` 单独保存本次采集结果。空结果或失败不会把旧线索的采集时间改成现在，也不会用生成页面的时间充当数据更新时间。
- 原有 `2026-11-18` 记录保留为历史快照。日报日期变化不表示这些历史活动重新核验或仍可参加。

完整字段说明见 `bank_daily_setup/README.txt`。来源名称、地址或站点结构变化时，需要维护采集配置；来源检查细节保留在 JSON 和任务日志中。
