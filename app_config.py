"""Static action configuration for the content-generation application."""

from __future__ import annotations

from typing import Final, TypedDict


class ActionConfig(TypedDict):
    """Import and skill information for one CLI action."""

    module: str
    skill: str


ACTION_CONFIG: Final[dict[str, ActionConfig]] = {
    "create_datatables": {"module": "create_datatables", "skill": "datatables"},
    "create_flashcards": {"module": "create_flashcards", "skill": "flashcards"},
    "create_infographics": {"module": "create_infographics", "skill": "infographics"},
    "create_mindmaps": {"module": "create_mindmaps", "skill": "mindmaps"},
    "create_podcasts": {"module": "create_podcasts", "skill": "podcasts"},
    "create_qandas": {"module": "create_qandas", "skill": "qandas"},
    "create_quizzes": {"module": "create_quizzes", "skill": "quizzes"},
    "create_reports": {"module": "create_reports", "skill": "reports"},
    "create_slides": {"module": "create_slides", "skill": "slides"},
}
