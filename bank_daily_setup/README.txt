银行每日线报：采集、生成与发布

一、实际使用的位置

保留本目录结构，不要把脚本逐个搬到仓库根目录。
实际工作流：.github/workflows/daily-report.yml。
本目录 daily-report.yml 是参考副本，GitHub 不执行它。
网站入口：仓库根 index.html；版本信息：仓库根 latest.json。
本目录的旧 index.html/latest.json 不参与每日发布，不要再复制它们覆盖网站。

二、执行

在仓库根目录：
  python -m pip install 'beautifulsoup4>=4.12,<5'
  python -m unittest discover -s tests -v
  python bank_daily_setup/collect_offers.py --date 2026-11-19
  python bank_daily_setup/generate_report.py --date 2026-11-19

生成器会直接调用统一发布器，写入根 index.html/latest.json 和带日期的存档。
只生成预览时加 --no-publish；可用 --output-dir 指定预览目录。
根 publish_report.py 与本目录 publish_report.py 均使用同一发布逻辑。

每天13:00 UTC即北京时间21:00运行，生成次日00:00—23:59的日报。
不传 --date 时按北京时间21点发布周期决定日期：21点后次日、21点前当日；
因此任务延迟至凌晨或早晨补跑不会再无故向后多推一天。
GitHub定时任务可能延迟，手动补跑可明确指定 report_date。没有邮件发送步骤。

三、verified_offers.json

格式为JSON数组。字段：
  region：cn / hk / us
  bank：银行或平台名称
  title：活动名称
  details：奖励、门槛、资格和限制
  source_url：实际活动来源的完整 http(s) URL
  verified：是否核对过来源，布尔值
  start_date / end_date：来源明确给出的 YYYY-MM-DD；未知时不填写或填null
  verified_at：实际核对时间，带时区的ISO8601时间
  collected_at：实际取得来源数据的时间，可选；不能填页面生成时间
  category / origin：活动分类与来源性质，可选
  verification_scope：仅核对当前页面而无法确认完整有效期时填 current_page

只有 verified=true 且有明确、有效、覆盖目标日期的起止日期，才显示为已核实活动。
缺少完整日期的 current_page 记录仍显示“本期活动时间及个人资格待确认”。
不要为未知有效期补造日期。不得把示例条目当作真实活动。

四、采集输出

collected_offers.json 顶层为：
  schema_version、report_date、status、collected_at、offers
status支持 ok / partial / failed / empty。
offers为上述字段组成的数组；自动采集条目通常 verified=false，并可包含：
  id、published_at、status、category、origin。
其中 published_at 是来源发布时间，collected_at 是实际采集时间；未知字段不编造。

collection_status.json 单独记录 report_date、attempted_at、finished_at、status、
source_count、success_count、offer_count 和 sources 检查结果。
本次零可用数据时保留之前的采集文件，仅更新采集状态；生成器保留真实旧时间。
无数据时页面显示空态，不用新的日报日期冒充新的数据。

五、发布一致性

latest.json 包含 date/report_date、revision、generated_at、collected_at、
source_status、snapshot_date 和各地区数量。采集诊断留在JSON中。
revision由实际页面内容生成，同日内容改变也会产生新版本；相同内容重跑保持版本。
发布前验证HTML三区与PWA结构；验证失败不覆盖现有网站。
新页面使用report_template.html中的PWA更新脚本，原有三区、分类、筛选与历史表保留。
