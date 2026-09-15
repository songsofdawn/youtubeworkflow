"""Read-only legacy catalog for inspecting historical configuration."""
import json
import re
from pathlib import Path
from typing import Any

DISCOVERY_PACK_ID_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")
DISCOVERY_PACKS: tuple[dict[str, Any], ...] = (
    {
        "id": "ai_technology",
        "label": "AI 与新科技",
        "description": "新模型、AI 工具、机器人和新硬件实测",
        "query": "new AI model test|AI tool comparison|robotics experiment|new technology test",
        "keywords": ["ai", "model", "chatgpt", "claude", "gemini", "robot", "technology"],
    },
    {
        "id": "software_programming",
        "label": "软件与编程",
        "description": "编程项目、自动化、开发工具和软件工作流",
        "query": "programming project|Python automation|coding challenge|new software workflow",
        "keywords": ["programming", "python", "coding", "software", "developer", "automation"],
    },
    {
        "id": "science",
        "label": "科学",
        "description": "科学发现、工程原理和可视化科普",
        "query": "science experiment|new scientific discovery|engineering explained|physics experiment",
        "keywords": ["science", "scientific", "physics", "engineering", "discovery", "experiment"],
    },
    {
        "id": "gaming",
        "label": "游戏",
        "description": "新游戏、更新、玩法实验和高质量挑战",
        "query": "new game gameplay|game update|gaming challenge|game experiment",
        "keywords": ["game", "gameplay", "gaming", "update", "challenge"],
    },
    {
        "id": "minecraft",
        "label": "Minecraft",
        "description": "Minecraft 生存、But、模组、建造与实验",
        "query": "Minecraft but|Minecraft challenge|Minecraft experiment|Minecraft survival",
        "keywords": ["minecraft", "hardcore", "survival", "mod", "build", "but"],
    },
    {
        "id": "chemistry",
        "label": "化学",
        "description": "化学实验、材料反应和实验室演示",
        "query": "chemistry experiment|chemical reaction|laboratory experiment|materials science test",
        "keywords": ["chemistry", "chemical", "reaction", "laboratory", "material", "molecule"],
    },
    {
        "id": "challenges_experiments",
        "label": "挑战与实验",
        "description": "30/100 天挑战、测试和意外结果",
        "query": "I tried for 30 days|100 days challenge|I tested|what happens if",
        "keywords": ["challenge", "experiment", "i tried", "i tested", "100 days", "30 days"],
    },
    {
        "id": "entertainment",
        "label": "娱乐",
        "description": "高参与度娱乐内容、喜剧和有解说反应",
        "query": "funny challenge|comedy experiment|entertainment reaction|unexpected moments",
        "keywords": ["funny", "comedy", "entertainment", "reaction", "unexpected"],
    },
    {
        "id": "agriculture_gardening",
        "label": "农业与园艺",
        "description": "种植、收获、农场技术和园艺实验",
        "query": "garden harvest|growing experiment|farm technology|vegetable garden update",
        "keywords": ["garden", "growing", "harvest", "farm", "plant", "vegetable"],
    },
    {
        "id": "food_cooking",
        "label": "美食与烹饪",
        "description": "烹饪实验、食谱测试和特色美食",
        "query": "food experiment|recipe test|cooking challenge|street food discovery",
        "keywords": ["food", "recipe", "cooking", "chef", "kitchen", "street food"],
    },
    {
        "id": "outdoor_travel",
        "label": "户外与旅行",
        "description": "露营、生存挑战、远途旅行和地点探索",
        "query": "outdoor adventure|survival challenge|camping experiment|remote travel discovery",
        "keywords": ["outdoor", "survival", "camping", "travel", "adventure", "remote"],
    },
    {
        "id": "tutorials_skills",
        "label": "教程与技能",
        "description": "完整教程、技能学习和实用工作流",
        "query": "complete tutorial|beginner guide|how to build|new skill challenge",
        "keywords": ["tutorial", "guide", "how to", "beginner", "skill", "build"],
    },
    {
        "id": "art_creativity",
        "label": "艺术与创意",
        "description": "绘画、动画、3D 制作和创意挑战",
        "query": "art challenge|animation process|creative project|3D printing project",
        "keywords": ["art", "animation", "creative", "drawing", "3d", "design"],
    },
    {
        "id": "social_experiments",
        "label": "娱乐与社会实验",
        "description": "社会实验、真人挑战和有叙事的互动内容",
        "query": "social experiment|public challenge|I asked strangers|human behavior experiment",
        "keywords": ["social experiment", "public", "strangers", "human behavior", "challenge"],
    },
)
def load_discovery_packs(path: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Load and validate user-editable discovery packs, with built-ins as fallback."""
    if path is None or not path.is_file():
        return DISCOVERY_PACKS
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"智能发现关键词文件无法读取：{path}（{exc}）") from exc
    raw_packs = payload.get("packs") if isinstance(payload, dict) else None
    if not isinstance(raw_packs, list):
        raise ValueError(f"智能发现关键词文件格式错误：{path} 中缺少 packs 列表")

    packs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_pack in enumerate(raw_packs, 1):
        if not isinstance(raw_pack, dict):
            raise ValueError(f"智能发现关键词文件第 {index} 个领域必须是对象")
        if not bool(raw_pack.get("enabled", True)):
            continue
        pack_id = str(raw_pack.get("id") or "").strip()
        label = str(raw_pack.get("label") or "").strip()
        description = str(raw_pack.get("description") or "").strip()
        from src.discovery.query_plan import normalize_queries  # legacy catalog only
        query = "|".join(normalize_queries(raw_pack.get("query")))
        keywords_value = raw_pack.get("keywords")
        keywords = (
            [" ".join(str(value).casefold().split()) for value in keywords_value]
            if isinstance(keywords_value, list)
            else []
        )
        keywords = [value for value in keywords if value]
        if not DISCOVERY_PACK_ID_PATTERN.fullmatch(pack_id):
            raise ValueError(
                f"智能发现关键词文件第 {index} 个领域 id 无效；只能使用小写字母、数字和下划线"
            )
        if pack_id in seen_ids:
            raise ValueError(f"智能发现关键词文件存在重复领域 id：{pack_id}")
        if not label or not description or not query or not keywords:
            raise ValueError(
                f"智能发现领域 {pack_id} 必须填写 label、description、query 和 keywords"
            )
        seen_ids.add(pack_id)
        packs.append(
            {
                "id": pack_id,
                "label": label,
                "description": description,
                "query": query,
                "keywords": keywords,
                "default_selected": bool(raw_pack.get("default_selected", True)),
            }
        )
    if not packs:
        raise ValueError("智能发现关键词文件没有任何已启用领域")
    return tuple(packs)



def save_discovery_packs(path: Path, raw_packs: list[dict[str, Any]]):
    raise ValueError("Legacy discovery catalog is read-only; edit Discovery Next taxonomy instead")


def public_discovery_catalog(
    packs: tuple[dict[str, Any], ...] | None = None,
    *,
    include_details: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for pack in (packs or DISCOVERY_PACKS):
        item = {
            "id": pack["id"],
            "label": pack["label"],
            "description": pack["description"],
            "examples": str(pack["query"]).split("|")[:3],
            "default_selected": bool(pack.get("default_selected", True)),
        }
        if include_details:
            item.update(
                {
                    "query": str(pack["query"]),
                    "keywords": [str(value) for value in pack["keywords"]],
                    "enabled": True,
                }
            )
        output.append(item)
    return output
