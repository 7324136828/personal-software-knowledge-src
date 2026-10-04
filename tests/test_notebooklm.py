from __future__ import annotations

import copy
import base64
import csv
import html
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from errors import ApplicationError, GenerationCancelled, InputDocumentError
from notebooklm_converter import (
    artifact_from_metadata,
    convert_notebooklm_artifact,
    discover_notebooklm_artifacts,
)
from pipeline.schemas import get_schema, schema_errors
import study_set


SOURCE_UUID = "11111111-2222-3333-4444-555555555555"
TIMESTAMP = "20260908194812"
PNG_IMAGE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl9l9sAAAAASUVORK5CYII=")


def canonical_podcast() -> dict:
    return {"episode_title": "Imported audio", "podcast_show": "Notebook study",
            "cast": [
                {"speaker_id": "first", "host_id": "HOST_A", "name": "First speaker", "voice_file": "speaker1", "style": "Imported speaker"},
                {"speaker_id": "second", "host_id": "HOST_B", "name": "Second speaker", "voice_file": "speaker2", "style": "Imported speaker"},
            ],
            "script": [{"segment_name": "Overview", "scenes": [
                {"speaker_id": "first", "dialogue": "What can we learn?"},
                {"speaker_id": "second", "dialogue": "The exported lesson explains it."},
            ]}]}


def canonical_slides() -> dict:
    return {"title": "Imported deck", "slides": [{
        "title": "Overview", "subtitle": "Lesson", "bullets": ["First point", "Second point", "Third point"],
        "speaker_notes": "Existing speaker notes.", "image_query": "lesson diagram", "source_ids": ["source.txt"],
    }]}


def content_sidecar(metadata: Path, payload: dict) -> Path:
    path = metadata.with_name(metadata.name.removesuffix(" metadata.json") + ".content.json")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def app_html(payload: dict) -> str:
    """Match NotebookLM's exported, HTML-escaped Angular app payload."""
    value = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    return f'<!doctype html><html><body><app-root data-app-data="{value}"></app-root></body></html>'


def quiz_payload() -> dict:
    return {"quiz": [{
        "question": "Which label contains an ampersand & quotation mark?",
        "answerOptions": [
            {"text": "First", "rationale": "The first is incorrect.", "isCorrect": False},
            {"text": 'Second & "right"', "rationale": "The second is correct.", "isCorrect": True},
            {"text": "Third", "rationale": "The third is incorrect.", "isCorrect": False},
            {"text": "Fourth", "rationale": "The fourth is incorrect.", "isCorrect": False},
        ],
        "hint": "Read the punctuation.",
    }]}


def source_metadata_map(root: Path, entries: dict[str, str], *, merge: bool = False) -> Path:
    path = root / "input" / "metadata" / "sources.metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping = json.loads(path.read_text(encoding="utf-8")) if merge and path.is_file() else {}
    mapping.update({filename: {"id": source_id} for filename, source_id in entries.items()})
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def exported_artifact(root: Path, name: str, kind: str, content: str | bytes | dict,
                      *, extension: str = "", difficulty: str | None = None) -> tuple[Path, Path]:
    artifacts = root / "input" / "Artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if not (root / "input" / "metadata" / "sources.metadata.json").is_file():
        source_metadata_map(root, {"source.txt metadata.json": SOURCE_UUID})
    metadata = {
        "title": name,
        "type": "ARTIFACT_TYPE_APP" if kind.startswith("APP_TYPE_") else kind,
        "status": "ARTIFACT_STATUS_READY",
        "sources": [{"sourceId": {"id": SOURCE_UUID}, "mimeType": "text/plain"}],
    }
    if kind.startswith("APP_TYPE_"):
        metadata["app"] = {"generationOptions": {"appType": kind}}
        if difficulty is not None:
            metadata["app"]["generationOptions"]["quizGenerationOptions"] = {
                "quizDifficulty": difficulty,
            }
    metadata_path = artifacts / f"{name} metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    source = artifacts / f"{name}{extension}"
    if isinstance(content, dict):
        source.write_text(app_html(content), encoding="utf-8")
    elif isinstance(content, bytes):
        source.write_bytes(content)
    else:
        source.write_text(content, encoding="utf-8")
    return metadata_path, source


def config_file(root: Path, types: list[str], **overrides) -> Path:
    entry = {"type": "notebooklm", "input": "input", "output": "output", "types": types, "formats": ["json"]}
    entry.update(overrides)
    path = root / "study-set-config.json"
    path.write_text(json.dumps({"files": [entry]}), encoding="utf-8")
    return path


def convert(metadata: Path, output: Path):
    result = convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
    return result, json.loads(output.read_text(encoding="utf-8"))


class NotebookLMConversionTests(unittest.TestCase):
    def test_native_json_quiz_and_flashcard_exports_use_same_canonical_mapping_as_html(self) -> None:
        cards = {"flashcards": [{
            "f": {"flashcardContentBlock": [{"type": "text", "content": "Native question"}]},
            "b": {"flashcardContentBlock": [{"type": "text", "content": "Native answer"}]}, "c": 1,
        }]}
        cases = [("Native Quiz", "APP_TYPE_QUIZ", quiz_payload(), "create_quizzes"),
                 ("Native Cards", "APP_TYPE_FLASHCARDS", cards, "create_flashcards")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind, payload, action in cases:
                with self.subTest(action=action):
                    metadata, source = exported_artifact(root, name, kind, json.dumps(payload), extension=".json")
                    output = root / "output" / name / "artifact.json"
                    _, actual = convert(metadata, output)
                    self.assertEqual(schema_errors(actual, get_schema(action)), [])
                    self.assertNotIn("provenance", actual)
                    if action == "create_quizzes":
                        self.assertEqual(actual["questions"][0]["correct"], 1)
                        self.assertEqual(actual["questions"][0]["sources"], [source.name])
                    else:
                        self.assertEqual(actual["cards"][0]["front"], "Native question")
                        self.assertEqual(actual["cards"][0]["back"], "Native answer")

    def test_quiz_preserves_correct_index_rationales_and_html_entities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, source = exported_artifact(root, "Punctuation Quiz", "APP_TYPE_QUIZ", quiz_payload(),
                                                 difficulty="QUIZ_DIFFICULTY_HARD")
            output = root / "output" / "quizzes.json"
            result, payload = convert(metadata, output)
            self.assertEqual(schema_errors(payload, get_schema("create_quizzes")), [])
            question = payload["questions"][0]
            self.assertEqual(question["correct"], 1)
            self.assertEqual(question["options"][1], 'Second & "right"')
            self.assertEqual(question["question"], quiz_payload()["quiz"][0]["question"])
            self.assertEqual(question["difficulty"], "analysis")
            self.assertEqual(question["sources"], [source.name])
            for answer in quiz_payload()["quiz"][0]["answerOptions"]:
                self.assertIn(answer["rationale"], question["explanation"])
            self.assertNotIn("provenance", payload)
            self.assertNotIn("hint", question)
            self.assertNotIn("answer_rationales", question)
            sidecar = json.loads(output.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            self.assertIn(SOURCE_UUID, json.dumps(sidecar))
            self.assertEqual(json.loads(result.text), payload)

    def test_flashcard_content_blocks_become_canonical_cards(self) -> None:
        card = {"flashcards": [{
            "f": {"flashcardContentBlock": [{"type": "text", "content": "What is wisdom?"}]},
            "b": {"flashcardContentBlock": [{"type": "text", "content": "Clear understanding & action."}]},
            "c": 1,
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, source = exported_artifact(root, "Wisdom Flashcards", "APP_TYPE_FLASHCARDS", card)
            _, payload = convert(metadata, root / "output" / "flashcards.json")
            self.assertEqual(schema_errors(payload, get_schema("create_flashcards")), [])
            self.assertEqual(list(payload), ["title", "description", "cards"])
            self.assertEqual(list(payload["cards"][0]), ["type", "front", "back", "tags", "source_ids"])
            self.assertEqual(payload["cards"][0]["front"], "What is wisdom?")
            self.assertEqual(payload["cards"][0]["back"], "Clear understanding & action.")
            self.assertEqual(payload["cards"][0]["source_ids"], [source.name])
            self.assertEqual(payload["cards"][0]["type"], "basic")

    def test_flashcard_txt_keeps_one_tab_separated_record_per_card(self) -> None:
        card = {"flashcards": [{
            "f": {"flashcardContentBlock": [{"type": "text", "content": "Question\twith\na line break"}]},
            "b": {"flashcardContentBlock": [{"type": "text", "content": "Answer\r\nwith\ta tab"}]},
            "c": 1,
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Cards", "APP_TYPE_FLASHCARDS", card)
            output = root / "output" / "flashcards.txt"
            convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
            records = [line for line in output.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
            self.assertEqual(records, ["Question with a line break\tAnswer with a tab"])

    def test_mindmap_normalizes_leaf_children_without_losing_tree(self) -> None:
        tree = {"name": "Wisdom", "children": [{"name": "Knowledge", "children": [{"name": "Practice"}]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Wisdom Map", "APP_TYPE_MINDMAP", tree)
            _, payload = convert(metadata, root / "output" / "mindmaps.json")
            self.assertEqual(schema_errors(payload, get_schema("create_mindmaps")), [])
            self.assertEqual(list(payload), ["name", "children"])
            self.assertEqual(payload["children"][0]["children"][0], {"name": "Practice", "children": []})
            self.assertEqual(payload["name"], "Wisdom")

    def test_markdown_table_maps_columns_rows_and_citation_without_changing_values(self) -> None:
        table = "| Concept Name | Meaning |\n| --- | --- |\n| Wisdom | Seeing clearly [1] |\n| Practice | Daily action |\n\n[1] wisdom.txt\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Concepts", "ARTIFACT_TYPE_TABLE", table, extension=".md")
            _, payload = convert(metadata, root / "output" / "datatables.json")
            self.assertEqual(schema_errors(payload, get_schema("create_datatables")), [])
            self.assertEqual(list(payload), ["title", "fields", "data"])
            self.assertEqual(list(payload["fields"][0]), ["name", "description", "example"])
            self.assertEqual(list(payload["data"][0]), ["concept_name", "meaning", "source_id", "page"])
            self.assertEqual([field["name"] for field in payload["fields"]], ["concept_name", "meaning"])
            self.assertEqual(payload["data"][0]["concept_name"], "Wisdom")
            self.assertIn("Seeing clearly", payload["data"][0]["meaning"])
            self.assertEqual(payload["data"][0]["source_id"], "wisdom.txt")
            self.assertEqual(payload["data"][0]["page"], "N/A")
            output_csv = root / "output" / "datatables.csv"
            convert_notebooklm_artifact(artifact_from_metadata(metadata), output_csv)
            with output_csv.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["concept_name"], "Wisdom")
            self.assertEqual(rows[1]["meaning"], "Daily action")

    def test_markdown_table_preserves_empty_cells_and_escaped_pipes(self) -> None:
        table = "| Concept | Meaning |\n| --- | --- |\n| | Left \\| right |\n| Wisdom | |\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, source = exported_artifact(root, "Concepts", "ARTIFACT_TYPE_TABLE", table, extension=".md")
            _, payload = convert(metadata, root / "output" / "datatables.json")
            self.assertEqual(payload["data"][0]["concept"], "")
            self.assertEqual(payload["data"][0]["meaning"], "Left | right")
            self.assertEqual(payload["data"][1]["meaning"], "")
            self.assertEqual(payload["data"][0]["source_id"], source.name)

    def test_podcast_content_is_canonical_with_metadata_and_original_audio_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = b"RIFF\x10\x00\x00\x00WAVEoriginal audio"
            metadata, source = exported_artifact(root, "Overview", "ARTIFACT_TYPE_AUDIO_OVERVIEW", audio, extension=".wav")
            content = content_sidecar(metadata, canonical_podcast())
            output = root / "output" / "podcasts.json"
            _, payload = convert(metadata, output)
            self.assertEqual(payload, canonical_podcast())
            self.assertEqual(schema_errors(payload, get_schema("create_podcasts")), [])
            files = [path for path in output.parent.rglob("*") if path.is_file()]
            self.assertIn(audio, [path.read_bytes() for path in files])
            artifact = artifact_from_metadata(metadata)
            self.assertTrue({source, metadata, content}.issubset(artifact.dependencies))
            sidecar = json.loads(output.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            self.assertIn(SOURCE_UUID, json.dumps(sidecar))
            self.assertEqual(sidecar["original_metadata"], artifact.metadata)

    def test_infographic_wraps_original_png_as_self_contained_canonical_svg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Diagram", "ARTIFACT_TYPE_INFOGRAPHIC", PNG_IMAGE, extension=".png")
            output = root / "output" / "infographic.json"
            _, payload = convert(metadata, output)
            self.assertEqual(schema_errors(payload, get_schema("create_infographics")), [])
            self.assertEqual(list(payload), ["title", "subtitle", "sections"])
            self.assertEqual(payload["sections"][0]["type"], "svg")
            asset = output.parent / payload["sections"][0]["value"]
            self.assertTrue(asset.is_file())
            self.assertIn("data:image/png;base64," + base64.b64encode(PNG_IMAGE).decode("ascii"), asset.read_text(encoding="utf-8"))
            self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_podcast_labelled_transcript_preserves_observed_speakers_and_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Audio", "ARTIFACT_TYPE_AUDIO_OVERVIEW", b"RIFFaudio", extension=".wav")
            transcript = metadata.with_name("Audio.transcript.json")
            transcript.write_text(json.dumps({"segments": [
                {"speaker": "Narrator A", "text": "First actual sentence."},
                {"speaker": "Narrator B", "text": "Second actual sentence."},
                {"speaker": "Narrator A", "text": "Third actual sentence."},
            ]}), encoding="utf-8")
            _, payload = convert(metadata, root / "output" / "episode.json")
            self.assertEqual(schema_errors(payload, get_schema("create_podcasts")), [])
            self.assertEqual({speaker["name"] for speaker in payload["cast"]}, {"Narrator A", "Narrator B"})
            dialogue = [scene["dialogue"] for segment in payload["script"] for scene in segment["scenes"]]
            self.assertEqual(dialogue, ["First actual sentence.", "Second actual sentence.", "Third actual sentence."])

    def test_slide_folder_preserves_documents_and_extensionless_slide_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, placeholder = exported_artifact(root, "Slide Deck", "ARTIFACT_TYPE_SLIDES", "placeholder")
            placeholder.unlink()
            placeholder.mkdir()
            assets = {
                "Deck.pdf": b"%PDF-1.7 original deck",
                "Deck.pptx": b"PK\x03\x04 original presentation",
                "Deck_-_Slide_1": b"\x89PNG\r\n\x1a\n original slide image",
            }
            for name, content in assets.items():
                (placeholder / name).write_bytes(content)
            content = content_sidecar(metadata, canonical_slides())
            artifact = artifact_from_metadata(metadata)
            self.assertTrue({metadata, content, *(placeholder / name for name in assets)}.issubset(artifact.dependencies))
            output = root / "output" / "slides.json"
            _, payload = convert(metadata, output)
            self.assertEqual(payload, canonical_slides())
            copied = {path.name: path.read_bytes() for path in output.parent.rglob("*") if path.is_file()}
            for name, content in assets.items():
                self.assertEqual(copied[name], content)
            self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_media_rejects_unsupported_wav_output_and_cannot_overwrite_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Audio", "ARTIFACT_TYPE_AUDIO_OVERVIEW", b"RIFFaudio", extension=".wav")
            artifact = artifact_from_metadata(metadata)
            original_metadata = metadata.read_bytes()
            for output in (root / "output" / "podcasts.wav", metadata):
                with self.subTest(output=output), self.assertRaises(ApplicationError):
                    convert_notebooklm_artifact(artifact, output)
            self.assertEqual(metadata.read_bytes(), original_metadata)
            self.assertFalse((root / "output").exists())

    def test_malformed_quiz_fails_instead_of_inventing_an_answer(self) -> None:
        invalid_payloads = [
            {"quiz": []},
            {"quiz": [{"question": "Missing options"}]},
            {"quiz": [{**quiz_payload()["quiz"][0], "answerOptions": quiz_payload()["quiz"][0]["answerOptions"][:3]}]},
        ]
        ambiguous = copy.deepcopy(quiz_payload())
        ambiguous["quiz"][0]["answerOptions"][0]["isCorrect"] = True
        invalid_payloads.append(ambiguous)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, payload in enumerate(invalid_payloads):
                with self.subTest(index=index):
                    metadata, _ = exported_artifact(root, f"Invalid Quiz {index}", "APP_TYPE_QUIZ", payload)
                    output = root / "output" / str(index) / "quizzes.json"
                    with self.assertRaises(ApplicationError):
                        convert_notebooklm_artifact(artifact_from_metadata(metadata), output)
                    self.assertFalse(output.exists())

    def test_malformed_metadata_type_fields_report_application_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            original = json.loads(metadata.read_text(encoding="utf-8"))
            for invalid in ([], {}, 123, None):
                for field in ("type", "appType"):
                    with self.subTest(invalid=invalid, field=field):
                        payload = copy.deepcopy(original)
                        if field == "type":
                            payload["type"] = invalid
                        else:
                            payload["app"]["generationOptions"]["appType"] = invalid
                        metadata.write_text(json.dumps(payload), encoding="utf-8")
                        with self.assertRaises(ApplicationError):
                            artifact_from_metadata(metadata)

    def test_cancellation_leaves_no_converted_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            output = root / "output" / "quizzes.json"
            with self.assertRaises(GenerationCancelled):
                convert_notebooklm_artifact(artifact_from_metadata(metadata), output, cancel_check=lambda: True)
            self.assertFalse(output.exists())


class NotebookLMStudySetTests(unittest.TestCase):
    def test_unambiguous_table_reference_resolves_source_filename_for_other_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quiz_metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            table_metadata, table_source = exported_artifact(root, "References", "ARTIFACT_TYPE_TABLE",
                "| Term | Meaning |\n| --- | --- |\n| Wisdom | Understanding [1] |\n\n[1] wisdom.txt\n", extension=".md")
            artifact = artifact_from_metadata(quiz_metadata)
            self.assertEqual(artifact.source_names, ("wisdom.txt",))
            self.assertTrue({table_metadata, table_source}.issubset(artifact.dependencies))
            output = root / "output" / "quiz.json"
            convert_notebooklm_artifact(artifact, output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["questions"][0]["sources"], ["wisdom.txt"])
            config_file(root, ["quiz"])
            with patch("study_set._timestamp", return_value=TIMESTAMP):
                jobs = study_set.plan_study_sets(root)
            self.assertEqual(jobs[0].output, root / "output" / SOURCE_UUID / "quizzes" / f"quiz_{TIMESTAMP}.json")

    def test_ambiguous_table_source_references_do_not_guess_a_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata, source = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            for index, name in enumerate(("first.txt", "second.txt")):
                exported_artifact(root, f"References {index}", "ARTIFACT_TYPE_TABLE",
                    f"| Term | Meaning |\n| --- | --- |\n| A | B [1] |\n\n[1] {name}\n", extension=".md")
            artifact = artifact_from_metadata(metadata)
            self.assertEqual(artifact.source_names, ())
            output = root / "output" / "quiz.json"
            convert_notebooklm_artifact(artifact, output)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["questions"][0]["sources"], [source.name])

    def test_default_formats_and_timestamp_filenames_follow_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            exported_artifact(root, "Cards", "APP_TYPE_FLASHCARDS", {"flashcards": [{
                "f": {"flashcardContentBlock": [{"type": "text", "content": "Question"}]},
                "b": {"flashcardContentBlock": [{"type": "text", "content": "Answer"}]}, "c": 1,
            }]})
            exported_artifact(root, "Map", "APP_TYPE_MINDMAP", {"name": "Root", "children": [{"name": "Leaf"}]})
            exported_artifact(root, "Table", "ARTIFACT_TYPE_TABLE", "| Term | Meaning |\n| --- | --- |\n| A | B |\n", extension=".md")
            path = config_file(root, ["quiz", "flashcard", "mindmap", "datatable"])
            config = json.loads(path.read_text(encoding="utf-8"))
            del config["files"][0]["formats"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("study_set._timestamp", return_value=TIMESTAMP) as timestamp:
                jobs = study_set.plan_study_sets(root)
            timestamp.assert_called_once()
            outputs = {job.output.relative_to(root / "output").as_posix() for job in jobs}
            self.assertEqual(outputs, {
                f"{SOURCE_UUID}/quizzes/quiz_{TIMESTAMP}.json",
                f"{SOURCE_UUID}/flashcards/flashcards_{TIMESTAMP}.json", f"{SOURCE_UUID}/flashcards/flashcards_{TIMESTAMP}.txt",
                f"{SOURCE_UUID}/mindmaps/mindmap_{TIMESTAMP}.json", f"{SOURCE_UUID}/mindmaps/mindmap_{TIMESTAMP}.md", f"{SOURCE_UUID}/mindmaps/mindmap_{TIMESTAMP}.mmd",
                f"{SOURCE_UUID}/datatables/{TIMESTAMP}.json", f"{SOURCE_UUID}/datatables/{TIMESTAMP}.csv",
            })

    def test_notebooklm_ignores_document_glob_and_needs_no_model_or_connector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            config_file(root, ["quiz"], inputPattern="*.txt")
            with patch("study_set._timestamp", return_value=TIMESTAMP), \
                    patch("cli_runtime.create_connector") as connector, \
                    patch("study_set.run_cli_generation") as generation, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                jobs = study_set.plan_study_sets(root)
                self.assertEqual(study_set.generate_study_sets(root), 0)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].source, source)
            self.assertEqual(jobs[0].input_type, "notebooklm")
            self.assertEqual(jobs[0].output, root / "output" / SOURCE_UUID / "quizzes" / f"quiz_{TIMESTAMP}.json")
            self.assertTrue(jobs[0].output.is_file())
            self.assertEqual(json.loads(jobs[0].output.read_text(encoding="utf-8"))["questions"][0]["sources"], ["source.txt"])
            self.assertIn(root / "input" / "metadata" / "sources.metadata.json", jobs[0].notebooklm_task.source.dependencies)
            connector.assert_not_called()
            generation.assert_not_called()

    def test_global_notebooklm_type_can_omit_input_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            path = config_file(root, ["quiz"])
            config = json.loads(path.read_text(encoding="utf-8"))
            config["type"] = config["files"][0].pop("type")
            path.write_text(json.dumps(config), encoding="utf-8")
            jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 1)
            self.assertIsNone(jobs[0].args.model)

    def test_mixed_entries_generate_documents_and_convert_only_matching_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            documents = root / "documents"
            documents.mkdir()
            (documents / "chapter.txt").write_text("Source chapter", encoding="utf-8")
            path = config_file(root, ["quiz"])
            config = json.loads(path.read_text(encoding="utf-8"))
            config["files"].append({
                "type": "document", "input": "documents", "output": "output",
                "inputPattern": "*.txt", "types": ["report"], "model": "document-model",
            })
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("study_set._timestamp", return_value=TIMESTAMP), \
                    patch("study_set.run_cli_generation", return_value=0) as generation, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                jobs = study_set.plan_study_sets(root)
                self.assertEqual(study_set.generate_study_sets(root), 0)
            self.assertEqual([job.input_type for job in jobs], ["notebooklm", "document"])
            self.assertEqual(jobs[1].args.model, "document-model")
            generation.assert_called_once()
            self.assertTrue(jobs[0].output.exists())

    def test_missing_requested_export_kinds_are_skipped_but_empty_selection_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            with self.assertLogs("content_generator.notebooklm", level="WARNING") as logs:
                artifacts = discover_notebooklm_artifacts(root / "input", ["create_quizzes", "create_reports"])
            self.assertEqual([artifact.action for artifact in artifacts], ["create_quizzes"])
            self.assertIn("create_reports skipped", logs.output[0])
            config_file(root, ["report", "podcast"])
            with self.assertRaises(InputDocumentError):
                study_set.plan_study_sets(root)

    def test_document_entries_still_require_a_model_in_a_mixed_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            documents = root / "documents"
            documents.mkdir()
            (documents / "chapter.txt").write_text("Source chapter", encoding="utf-8")
            path = config_file(root, ["quiz"])
            config = json.loads(path.read_text(encoding="utf-8"))
            config["files"].append({
                "input": "documents", "output": "output", "inputPattern": "*.txt", "types": ["report"],
            })
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ApplicationError, "model"):
                study_set.plan_study_sets(root)

    def test_notebooklm_table_csv_is_supported_but_invalid_quiz_format_rejects_entire_plan(self) -> None:
        table = "| Concept | Meaning |\n| --- | --- |\n| Wisdom | Understanding |\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Table", "ARTIFACT_TYPE_TABLE", table, extension=".md")
            path = config_file(root, ["datatable"], formats=["csv"])
            jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].output.suffix, ".csv")
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            config = json.loads(path.read_text(encoding="utf-8"))
            config["files"].append({
                "type": "notebooklm", "input": "input", "output": "output",
                "types": ["quiz"], "formats": ["csv"],
            })
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ApplicationError, "Unsupported.*format"):
                study_set.plan_study_sets(root)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertNotEqual(study_set.generate_study_sets(root), 0)
            self.assertFalse((root / "output").exists())

    def test_malformed_export_never_calls_generation_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", "<app-root data-app-data='{bad json}'></app-root>")
            config_file(root, ["quiz"])
            with patch("cli_runtime.create_connector") as connector, \
                    patch("study_set.run_cli_generation") as generation, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertNotEqual(study_set.generate_study_sets(root), 0)
            connector.assert_not_called()
            generation.assert_not_called()
            self.assertFalse(any((root / "output").rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
