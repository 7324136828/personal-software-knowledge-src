"""Mind-map generation action."""

from __future__ import annotations

from pathlib import Path

from action_base import generate_artifact
from cli_runtime import run_action_cli
from connectors.base import LLMConnector


def generate(
    source_text: str,
    source_path: Path,
    skill_text: str,
    connector: LLMConnector,
    output_path: Path | None = None,
) -> str:
    """Generate a source-grounded mind-map artifact."""

    return generate_artifact(
        action="create_mindmaps",
        artifact_name="mind map",
        source_text=source_text,
        source_path=source_path,
        skill_text=skill_text,
        connector=connector,
        output_path=output_path,
    )


if __name__ == "__main__":
    raise SystemExit(run_action_cli("create_mindmaps"))
