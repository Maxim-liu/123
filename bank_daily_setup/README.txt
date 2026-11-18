银行每日线报自动化补丁（安全历史快照版）

本补丁不包含真实的自动互联网采集与独立核验；verified_offers.json 需要经人工或另行接入的可信采集服务维护。
禁止将旧活动仅更改日期后冒充实时优惠。生成日报顶部仅显示带有 verified=true、来源 URL 和有效日期的条目。

部署：
1. 将 report_template.html、generate_report.py、verified_offers.json 放到 GitHub 仓库根目录。
2. 将 daily-report.yml 的内容替换 .github/workflows/daily-report.yml。
3. 保留原 publish_report.py、index.html、latest.json、manifest.webmanifest、sw.js 等文件。
4. 本地测试：pip install beautifulsoup4；python generate_report.py --date 2026-11-19。
5. 注意：publish_report.py 是否兼容生成报告需要在仓库中进一步验证，不能保证发布步骤直接成功。

verified_offers.json 例子：
[{"region":"us","bank":"Example Bank","title":"示例活动","details":"奖励、门槛及资格","source_url":"https://example.com/offer","start_date":"2026-11-19","end_date":"2026-11-30","verified":true}]
只允许填写实际核验过的活动，不要使用示例内容作为真实数据。

GitHub 定时任务可能延迟；13:00 UTC 对应北京时间 21:00，但不保证准点运行。
