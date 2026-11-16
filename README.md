# 炭素材料证据与产能台账

本地 FastAPI 服务记录生产批次、实验室检验、发运、产线产能、客户订单与交付证据。所有示例记录均为合成数据，不连接真实工厂。

## 运行

在已准备好锁定依赖的 Python 环境中执行 uvicorn carbon_trace.api:app，通过 CARBON_TRACE_DB 指定 SQLite 文件。接口说明见 /docs。执行 python -m carbon_trace.cli demo --fixture fixtures/demo.json 可运行隔离的合成数据演示，输出日报、版本与对账结果。

## 数据约定

日期为 YYYY-MM-DD，事件时间必须带时区。工厂时区决定生产与发运所属日期，数量采用十进制吨。来源与外部事件编号组成不可变身份；完全相同的重试视为重复，不同内容的重试产生冲突拒绝。批次编号也具备幂等性，错误行不会回滚同批有效行。

登记工厂、产线和等级标准后提交生产批次。等级标准生效区间不得重叠；质量分析使用生产当日标准和该批次最新检验，区分未检验和检验不合格。准备发布时固定数据修订号与发布头；如其间新增事件或竞争版本，原候选失效，已发布数据保持不变。

订单预约只接受检验合格批次，且不得超过订单需求或批次余量。取消订单或释放预约后可重新使用余量。事故标记显示在日报；交付核收与物理发运量分别统计。

## 跨厂产能调剂

一条产线停工时，计划员可以在不改动任何生产、检验、发运、订单与预约事实的前提下，用调剂方案比较本地履约与跨厂调剂后的交期风险。方案是独立的计划层：POST /reallocations 按订单条目登记他厂在其本地日期承担的数量，POST /reallocations/{plan_id}/withdraw 可撤回；创建与撤回都受 expected_revision 保护。

方案创建时固化三类事实摘要（方案引用的订单、窗口内各厂事故、各厂各本地日名义产能与已记录产量）。查询时若订单取消或预约变动、事故开闭、产线或产量变化使摘要不再匹配，方案显露为 stale 并给出具体原因（orders_changed、incidents_changed、capacity_basis_changed、order_cancelled、order_requirement_changed、spare_capacity_no_longer_available）。失效或撤回的方案不再占用余量，因此同一名义余量可被新方案重新承诺；有效方案之间对同一厂、同一本地日的余量承诺互斥，超额创建会被拒绝。

所有日期均为各厂时区下的本地日期。GET /reallocations/{plan_id}/evaluation 同时给出每个本地日的 UTC 起点和按期判定口径（供应日本地零点的 UTC 时刻不晚于订单到期日本地零点的 UTC 时刻即视为按期），并输出本地履约基线、各供应厂承担数量与日期、按期/逾期吨位以及无法兑现的需求。GET /plants/{plant_id}/spare-capacity 按厂本地日历列出名义余量、已承诺余量与可用余量。日报与已发布版本完全不引用调剂表，统计含义保持不变。

## 核心接口

使用 POST /plants、POST /lines、POST /specifications 和 POST /batches 建立数据。日报通过 GET /plants/{plant_id}/report 查询。先从 GET /status 取得修订号与发布头，再调用 POST /candidates 和 POST /candidates/{candidate_id}/publish。历史版本通过 GET /versions/{version} 查询。其他接口提供库存、质量、溯源、对账、产能预测、订单、预约、事故和交付核收。跨厂调剂通过 POST /reallocations、GET /reallocations、GET /reallocations/{plan_id}、GET /reallocations/{plan_id}/evaluation、POST /reallocations/{plan_id}/withdraw 和 GET /plants/{plant_id}/spare-capacity 使用。错误响应包含稳定代码与原因。
