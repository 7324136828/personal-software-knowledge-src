"""Podcast-script generation action (text only)."""

from __future__ import annotations

from pathlib import Path

from action_base import generate_artifact
from cli_runtime import run_action_cli
from connectors.base import LLMConnector
from errors import ProviderError
from pipeline.validator import validate_output


def generate(
    source_text: str,
    source_path: Path,
    skill_text: str,
    connector: LLMConnector,
    output_path: Path | None = None,
) -> str:
    """Generate a source-grounded podcast script artifact."""

    result = generate_artifact(
        action="create_podcasts",
        artifact_name="text-only podcast script",
        source_text=source_text,
        source_path=source_path,
        skill_text=skill_text,
        connector=connector,
        output_path=output_path,
    )
    extension = ".md" if output_path is not None and output_path.suffix.lower() == ".md" else ".json"
    checked = validate_output(result, "create_podcasts", extension)
    if not checked["valid"]:
        raise ProviderError("Invalid podcast script: " + "; ".join(checked["errors"][:3]))
    return checked["text"]


if __name__ == "__main__":
    raise SystemExit(run_action_cli("create_podcasts"))
