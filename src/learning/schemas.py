from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=160)]
VideoId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
Terms = Annotated[list[Text], Field(max_length=32)]
EventKind = Literal["strong_interest", "interested", "download", "not_interested",
                    "off_topic", "too_common", "bad_channel"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DomainScore(StrictModel):
    name: Text
    score: Score


class ContentTraits(StrictModel):
    novelty: Score
    visual_payoff: Score
    story_strength: Score
    knowledge_value: Score
    localization_value: Score
    result_payoff: Score


class TaxonomySuggestion(StrictModel):
    name: Text
    label: Text
    confidence: Score
    suggested_entities: Terms
    suggested_formats: Terms


class VideoAnalysis(StrictModel):
    video_id: VideoId
    domains: Annotated[list[DomainScore], Field(max_length=12)]
    topics: Terms
    entities: Terms
    formats: Terms
    content_traits: ContentTraits
    positive_signals: Terms
    negative_signals: Terms
    search_concepts: Terms
    taxonomy_suggestions: Annotated[list[TaxonomySuggestion], Field(max_length=8)] = []


class LearningEvent(StrictModel):
    event_id: Text
    video_id: VideoId
    kind: EventKind
    created_at: str


EVENT_WEIGHTS = {"strong_interest": 1.0, "interested": 0.7, "download": 0.35,
                 "not_interested": -0.7, "off_topic": -0.8, "too_common": -0.4,
                 "bad_channel": -1.0}
