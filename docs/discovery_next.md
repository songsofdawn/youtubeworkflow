# Discovery Next：审计与设计

## 修改前审计

1. 两层词库：`config/discovery_keywords.json` 的 `query`（以 `|` 分隔）和 `keywords`；`src/control_panel/youtube.py` 的内置目录、加载/保存函数。
2. 轮换：`src/discovery/store.py::choose_queries` 按 runs 升序；`pipeline.py` 选择补充词并写回绩效。
3. 主词多排序：`pipeline.py` 的 primary query、recall orders、hot lane、adaptive page2 调度。
4. 停用范围：面板不再调用上述目录、调度、旧评分/补量管线。旧文件仅用于历史契约及传统流程，不迁移关键词到新配置。
5. 可复用：`fetch_daily_candidates.py` 的 YouTubeClient、详情/时长/缩略图工具；`discovery/ollama_client.py` 的结构化 HTTP 通信。复用结果展示 groups/results 协议及现有作业资源槽。
6. 旧数据库：`work/discovery/discovery.sqlite3` 的 evaluation_cache、embedding_cache、feedback、query_performance 均为 legacy；Next 不读写这些表。不删除用户历史数据。
7. 前端：`discovery_upgrade.js` 编辑 query/keywords、按领域估算 6–8 次调用；`app.js` 展示旧召回/补量指标。改为 taxonomy 编辑与实际执行绩效。

## 实施设计

- `src/learning`：严格分析 Schema、白名单公开元数据、下载事件收件箱、分析、画像重建。下载是中等正反馈；显式反馈独立记录。
- `src/discovery_next`：共享形式目录、taxonomy、按画像/近期兴趣/历史产出构建策略、临时组合查询、逐次绩效、独立发现服务。
- 下载成功后立即落盘幂等事件并排入 `learning` 作业；模型处理占用现有 gpu_heavy 槽。失败保留待重试事件，不能改写已有有效分析。
- 单视频 JSON 是分析事实源；SQLite 保存事件状态、查询执行和归因，画像/策略可重建。跨进程写入由 SQLite 事务串行化，JSON 使用原子替换。
- 分类来自内容匹配/结构化分析，不从搜索来源继承；热门与潜力排序分开。搜索记录保留来源用于绩效归因。
- 使用现有本地 Ollama Qwen，不隐式启用收费供应商。分析只发送允许的公开元数据，不读取本地字幕正文；可接受上游提供的公开字幕摘要。
- 领域建议只写建议产物，不自动修改 taxonomy。

## 已实现的数据协议

分析 Schema 在 `src/learning/schemas.py::VideoAnalysis`，完整机器可读定义见
`discovery_next_analysis.schema.json`。所有对象禁止额外字段，视频 ID 必须是 11 位安全字符，
分数为有限的 0–1 数值，列表及字符串有长度上限。字段包括 domains(name/score)、topics、
entities、formats、六种 content_traits、positive_signals、negative_signals、search_concepts
及可选 taxonomy_suggestions。建议的 evidence_count 从已校验的不同视频聚合，不能由模型编造。

单视频文档外层包含 version、analyzed_at、metadata、analysis。事件独立保存 event_id、
video_id、kind、created_at、metadata、status、error；download ID 稳定，每视频只记一次。
显式反馈为追加事件，画像和反馈绩效只使用同视频的最新显式反馈，历史审计记录保留。

查询执行有独立 run_id，记录 query/domain/entity/format/order/time_window/run_at、
returned_count/new_unique_count/eligible_count/ai_high_quality_count/shown_count，及状态。
下载与正负反馈通过独立 attributions 关联最近一次真实展示，`QueryPerformance.history()`
返回合并后的 download_count/positive_feedback_count/negative_feedback_count。
搜索为空或失败也保留；在详情调用前保存返回 ID，未展示视频不会产生下载归因。
频道搜索的 query 为空，以 channel_id 标识实际请求；候选归类始终与查询所属领域分离。

事务锁保护并发写入，各 JSON 原子替换。跨多个 JSON 的发布不是整体原子提交；
它们都是派生产物，中断后可通过 `python -m src.learning --rebuild` 恢复一致性。
重新分析先校验完整结果，再替换旧文件；无效分析保留旧文件和旧画像。

## 新旧边界与后续功能

面板入口只实例化 DiscoveryNextService；旧目录函数迁至 legacy_catalog.py，写入口拒绝操作。
旧调度、缓存和数据库保留供历史代码检查，但不进入 Next 的 import/执行链。
传统日报独立保留，不把其 core/rotating queries 误作面板发现。未批量迁移历史下载。

首期完成模板组合、学习概念种子、候选频道及事件闭环。后续可增加：人工确认领域建议的界面、
历史下载的显式批量导入、查询级 AI 改写与更细的绩效报表、更多趋势来源、视觉分析。
不实现本地推荐模型训练、向量数据库或强化学习。没有真实下载、付费 API 或模型联机测试。

## 验证记录（2026-09-15）

- 206 项聚焦测试通过：Discovery Next、面板、旧发现隔离契约、查询与下载。
  包含真实学习 worker 路径（mock AI）、作业日志、幂等事件、错误分析保留、
  分类与来源分离、绩效归因、反馈覆盖、Extreme Survival 无查询配置及取消前不调用搜索。
- 前端 taxonomy 编辑/保存、错误保留、历史结果和 HTML 转义离线交互测试通过；
  两份 JS 的 node --check、Python compileall、git diff --check 通过。
- 独立进程导入 Discovery Next 后，旧 pipeline/query_plan/store 均未加载。
- 全量测试运行 615 项，其中 612 项通过；传统日报 test_fetch_daily_candidates 的 3 项失败：
  固定与轮换组超过 search_core_query_groups_per_day、计划数量实际 28 而旧测试期望 24。
  对应配置、源码和测试与 HEAD 一致，本次未修改这些传统日报文件。随后新增的 2 项
  worker/取消测试已包含在上述 206 项聚焦回归中。
