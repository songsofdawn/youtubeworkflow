from pathlib import Path
from typing import Annotated

from pydantic import Field

from src.learning.schemas import StrictModel, Terms, Text
from src.learning.storage import atomic_json, read_json


class Domain(StrictModel):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_]{2,64}$")]
    label: Text
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    entities: Annotated[list[Text], Field(min_length=1, max_length=32)]
    formats: Annotated[list[Text], Field(min_length=1, max_length=24)]
    positive_traits: Terms
    negative_intents: Terms
    enabled: bool = True
    default_selected: bool = False


class Taxonomy:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "config/discovery_taxonomy.json"
        bundled = Path(__file__).resolve().parents[2] / "config"
        self.formats = read_json(self.root / "config/discovery_formats.json",
                                 read_json(bundled / "discovery_formats.json", {}))

    def validate(self, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise ValueError("taxonomy 需要 1–100 个领域")
        domains = [Domain.model_validate(value).model_dump() for value in values]
        if len({d["id"] for d in domains}) != len(domains):
            raise ValueError("领域 ID 重复")
        for domain in domains:
            unknown = set(domain["formats"]) - self.formats.keys()
            if unknown:
                raise ValueError("未知共享形式：" + ", ".join(sorted(unknown)))
        return domains

    def load(self):
        bundled = Path(__file__).resolve().parents[2] / "config/discovery_taxonomy.json"
        return self.validate(read_json(self.path, read_json(bundled, {})).get("domains", []))

    def save(self, values):
        domains = self.validate(values)
        atomic_json(self.path, {"version": 1, "domains": domains})
        return domains
