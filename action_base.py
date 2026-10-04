"""Shared prompt construction for provider-independent action modules."""

from __future__ import annotations

from pathlib import Path

from connectors.base import LLMConnector
from podcast_policy import PODCAST_PROMPT_CONSTRAINT
from result_processor import process_result


def build_generation_prompts(
    *,
    action: str,
    artifact_name: str,
    source_text: str,
    source_path: Path,
    skill_text: str,
    output_path: Path | None = None,
) -> tuple[str, str]:
    """Build a source-grounded request containing the full skill specification."""

    system_prompt = f"""You are generating a {artifact_name} according to an authoritative skill specification.

--- SKILL SPECIFICATION ---

{skill_text}

--- END SKILL SPECIFICATION ---

Follow the specification exactly, including its schema, required sections, formatting,
naming conventions, output syntax, completeness checks, and grounding rules. The source
document is authoritative. Do not invent definitions, exercises, formulas, quotations,
or factual claims absent from it unless the skill explicitly allows external knowledge.
Return only the requested generated artifact, with no preamble or commentary, unless the
skill explicitly requires otherwise."""

    if action == "create_podcasts":
        system_prompt += "\n\n" + PODCAST_PROMPT_CONSTRAINT

    output_requirement = (
        str(output_path)
        if output_path is not None
        else "the caller-provided output destination"
    )
    user_prompt = f"""SOURCE DOCUMENT

Filename: {source_path.name}
File extension: {source_path.suffix.lower() or '(none)'}
Source character count: {len(source_text)}

Source content:

{source_text}

--- END SOURCE DOCUMENT ---

TASK

Execute the action `{action}` and generate the requested {artifact_name} from this source.
Preserve meaningful structure, chapter and exercise numbering, tables, code blocks, and
LaTeX notation exactly where relevant. The caller will write this response exactly to:
{output_requirement}

That explicit destination is authoritative. Produce only the content for that destination;
do not create, rename, or merely list other files. If the skill describes several derived
formats, use the requested destination's extension to select the applicable representation.
If that extension is not named by the skill, emit the skill's canonical/source-of-truth
representation as plain UTF-8 text without an enclosing Markdown code fence.
"""
    return system_prompt, user_prompt


def generate_artifact(
    *,
    action: str,
    artifact_name: str,
    source_text: str,
    source_path: Path,
    skill_text: str,
    connector: LLMConnector,
    output_path: Path | None = None,
) -> str:
    """Generate one artifact through the provider-neutral connector contract."""

    system_prompt, user_prompt = build_generation_prompts(
        action=action,
        artifact_name=artifact_name,
        source_text=source_text,
        source_path=source_path,
        skill_text=skill_text,
        output_path=output_path,
    )
    result = connector.generate(system_prompt=system_prompt, user_prompt=user_prompt)
    return process_result(result, output_path)
