# Bilibili 历史画像基线

## 范围
- 来源：Bilibili UID 104407846 导出文件
- 截止条件：2026-08-25 00:00 (+08:00) 之后
- 纳入视频：721
- 实际最早纳入：2026/8/25 18:11:32
- 实际最晚纳入：2026/9/9 23:51:59
- 事件权重：全部按当前项目 `download = 0.35` 处理
- 不注入频道偏好：Bilibili 导出文件没有可靠的原 YouTube channel_id，避免错误把 B 站账号当作 YouTube 频道

## 一级领域分布
| domain | count | share |
|---|---:|---:|
| ai_technology | 177 | 24.5% |
| minecraft | 155 | 21.5% |
| food_cooking | 80 | 11.1% |
| engineering_manufacturing | 49 | 6.8% |
| agriculture_gardening | 43 | 6.0% |
| challenges_experiments | 40 | 5.5% |
| gaming | 25 | 3.5% |
| tutorials_skills | 23 | 3.2% |
| art_creativity | 21 | 2.9% |
| science | 18 | 2.5% |
| chemistry | 16 | 2.2% |
| business_industry | 13 | 1.8% |
| nature_animals | 11 | 1.5% |
| restoration_crafts | 10 | 1.4% |
| history_archaeology | 10 | 1.4% |
| everyday_science | 10 | 1.4% |
| cleaning_transformation | 7 | 1.0% |
| outdoor_travel | 5 | 0.7% |
| extreme_survival | 4 | 0.6% |
| space_astronomy | 2 | 0.3% |
| social_experiments | 2 | 0.3% |

## 内容形式
| format | occurrences |
|---|---:|
| comparison | 232 |
| challenge | 190 |
| experiment | 164 |
| full_process | 158 |
| build | 147 |
| extreme_survival | 62 |
| investigation | 53 |
| documentary | 52 |
| mod_showcase | 35 |
| transformation | 15 |
| 100_days | 15 |
| restoration | 14 |
| simulation | 6 |

## 核心画像
当前基线最强的两个领域是：
1. `ai_technology`：177 条，主要围绕 OpenAI / ChatGPT / GPT-6 / Astra / Claude / Fable 5.1 / AI coding / agents / video generation。
2. `minecraft`：155 条，主要围绕 Minecraft hardcore / mods / building / redstone / 100 days。

第二梯队：
- `food_cooking`：80 条
- `engineering_manufacturing`：49 条
- `agriculture_gardening`：43 条
- `challenges_experiments`：40 条

## 当前内容偏好特征
```json
{
  "novelty": 0.6356,
  "visual_payoff": 0.7646,
  "story_strength": 0.6115,
  "knowledge_value": 0.6182,
  "localization_value": 0.8631,
  "result_payoff": 0.7395
}
```

这些数值说明该历史内容明显偏向：
- localization_value 高
- visual_payoff / result_payoff 较高
- novelty 中高
- story_strength 与 knowledge_value 居中

## 主要实体
### AI
claude, astra, gpt-6, openai, chatgpt, fable 5.1, ai coding, ai technology, llm, ai agents, ai video generation, glm

### Minecraft
minecraft, mods, hardcore, building, redstone, 100 days, bedrock, shaders, wynncraft, civilization, physics

### Food
food, noodles, baking, steak, fast food, high protein, street food, traditional food, pizza, air fryer

## Taxonomy 缺口建议
- `software_programming`：77 条证据
- `computer_hardware`：141 条证据
- `entertainment_pop_culture`：8 条证据
- `personal_development_psychology`：8 条证据

这些 suggestion 只写入 `taxonomy_suggestions.json`，不会自动修改 taxonomy。

## 重要说明
当前 `DiscoveryNextService.run()` 会先执行 `LearningService.rebuild()`，因此**只替换 `user_content_profile.json` 不足以形成持久基线**。
本包同时包含：
- `events.sqlite3`
- `videos/*.json`
- `user_content_profile.json`
- `taxonomy_suggestions.json`

这样下一次 rebuild 会从 721 条 seed event 和 721 份 video analysis 重新得到同一基线，然后再叠加未来真实下载/反馈。

本包使用 synthetic learning id（`bseed......`），只是为了满足当前 `VideoId` 的 11 字符约束并持久化 Bilibili 历史画像；它们不代表真实 YouTube ID。

## 安装方式
### 推荐：合并到现有 learning 数据
在项目根目录运行：

```powershell
python merge_into_project.py .
```

脚本会：
1. 备份当前 `data/learning`；
2. 合并 721 个 seed video JSON；
3. 向现有 `events.sqlite3` 插入 seed download events（`INSERT OR IGNORE`）；
4. 保留已有 query_runs/query_hits/attributions；
5. 尝试调用项目自己的 `LearningService.rebuild()`。

### 直接替换
如果你就是想把现有 learning 全部重置成这份基线：

1. 先备份项目中的 `data/learning`
2. 用 `replace_data/data/learning` 整个覆盖项目里的 `data/learning`
3. 启动项目或执行一次 Discovery，系统会重新生成 `discovery_strategy.json`

直接替换会清空已有 query performance 历史，因为 seed 数据库的 query 表为空。
