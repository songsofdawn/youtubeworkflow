# Discovery Next：自学习内容发现架构

控制面板的“智能发现”使用 `src/discovery_next/`，学习与画像使用 `src/learning/`。旧
`src/discovery/` 调度器和 `config/discovery_keywords.json` 只保留为 legacy，面板不导入
执行，也不把旧主词/补充词、固定轮换或按执行次数排序迁回 Next。

## 一次发现的数据流

1. 读取 `data/learning/user_content_profile.json` 和 `data/learning/events.sqlite3` 中的查询历史。
2. `DomainAllocator` 自动分配本轮领域预算；Auto Discovery 不要求勾选领域，Manual Focus 只影响本轮。
3. `QueryPlanner` 从 intent 开始生成自然英文查询；优先使用 Qwen，不可用时回退学习证据。
4. `RecallExecutor` 按 exploitation / learned concept / emerging / preferred channel / AI query /
   coverage / exploration / cross-domain 多路召回，并统一进入候选池。
5. 候选池按 video_id 去重，保留每个视频命中过的 query、domain、intent、recall source、
   semantic cluster、first_seen_at 和 last_seen_at；近似标题获得重复/多样性惩罚而不是被
   简单按热度截断。
6. 硬过滤（已知视频、时长、窗口、嵌入状态、语言、风险短语）后，`CandidatePreselector`
   按热门、相关、用户偏好、新颖、emerging、exploration、低热高相关、稀有领域等分桶。
7. 启用本地 Qwen 时，对分桶后的候选做结构化内容分析；未启用时使用公开元数据特征。
8. `CandidateRanker` 分别计算 `rank_hot()` 和 `rank_potential()`，再限制频道、语义簇和
   领域重复。下载与反馈通过最近真实展示归因到单个查询，其他查询只保留召回记录。

## 模块职责

| 模块 | 只负责 |
| --- | --- |
| `domain_allocator.py` | 从历史 seed、近期下载、显式/强兴趣、emerging、查询绩效、覆盖和负反馈计算领域预算与动态探索比例 |
| `query_planner.py` | 生成 intent → query 候选池，做语义去重、cooldown、绩效先验、领域与 recall source 预算控制 |
| `recall.py` | 执行查询/频道召回，保留每个候选的完整归因 |
| `candidate_preselector.py` | 在 Qwen 预算前做分层选择，避免 hottest/单频道/单查询垄断 |
| `candidate_ranker.py` | 独立 Hot/Potential 评分及最终多样性控制 |
| `query_performance.py` | 查询执行、命中、展示与归因历史，计算质量 KPI 和 cooldown |
| `strategy_builder.py` | 组合整轮 domain allocation、recall budget 与查询计划 |
| `service.py` | 只做 orchestration，不承载业务算法 |

## 关键算法

### 领域分配

输入同时使用长期领域偏好、近期衰减偏好、真实下载、显式兴趣、强兴趣、emerging interest、
历史查询成功率、覆盖缺口和负反馈。权重关系按“历史 seed < 新下载 < interested <
strong_interest”建模，并设置 floor/ceiling。单一强偏好不会得到 100% 预算；动态探索
比例受画像集中度、近期领域多样性、探索成功率和 emerging 信号影响。

### 查询规划

查询先确定 intent，再由 Qwen 生成自然查询。学习概念、领域实体、emerging interest、
偏好频道、coverage、exploration 与 cross-domain 进入候选池；候选保存 normalized query、
domain、intent、source、semantic cluster、performance prior、last used time 和 use count。
选择阶段依次做 token 归一化、语义相似去重、cooldown、领域预算和 recall source 预算过滤，
再按绩效与来源先验选择最终查询。没有固定每领域 query 数，也没有每 5 条插入 exploration 的规则。

### 查询绩效

`QueryPerformance` 记录返回数、新增唯一数、重复数、novel/unique channel 数、合格数、
AI 选择/接受/高质量数、展示数、下载数和正负反馈数，并派生 duplicate/new unique/novel/
unique channel/eligible/AI accept/high quality/shown/download/positive/negative 等比率。
质量分不以 returned_count 为核心：低新增、低合格、低下载的查询会进入 cooldown；
高产出查询获得更高候选优先级。下载与反馈只归给最近一次真实展示的查询。

### 候选选择与排序

Qwen 预算按多个 bucket 分配，`low_pop_high_relevance` 保证低播放、高相关候选有真实机会
进入 AI。Potential 排序以 AI 机会/潜力、用户匹配、相关性、novelty、emerging、本地化价值、
内容特质和多样性为主，热度仅作弱信号；Hot 排序强调速度、播放量、新鲜度、互动率，并设
AI 质量底线。最终输出限制频道、语义簇和领域重复。

## 可检查数据

- `data/learning/videos/<video_id>.json`：严格校验的单视频分析。
- `data/learning/user_content_profile.json`：可重建的用户画像。
- `data/learning/discovery_strategy.json`：整轮策略，含 domain allocation、allocation reason、
  recall budget、query plan 和 AI budget。
- `data/learning/taxonomy_suggestions.json`：领域建议及真实证据，不自动修改 taxonomy。
- `data/learning/events.sqlite3`：事件、query_runs、query_hits、attributions。

这些文件均可通过 `python -m src.learning --rebuild` 重建；SQLite 事务串行化跨进程写入，
JSON 使用原子替换。旧数据不会被删除或迁移到 Next。

## 验证

修改 Discovery Next 或学习逻辑后运行：

```bat
.venv\Scripts\python.exe -m unittest tests.test_discovery_next tests.test_discovery_next_phase2 tests.test_discovery_next_acceptance
.venv\Scripts\python.exe -m unittest tests.test_control_panel
.venv\Scripts\python.exe -m compileall -q src tests
node --check src\control_panel\static\app.js
node --check src\control_panel\static\discovery_upgrade.js
git diff --check
```

所有测试必须离线，mock Ollama、YouTube 与投稿调用。
