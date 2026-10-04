from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from errors import GenerationCancelled, InputDocumentError
import notebooklm_media


_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def shape(lines: list[str], placeholder: str = "") -> str:
    marker = f'<p:ph type="{placeholder}"/>' if placeholder else ""
    paragraphs = "".join('<a:p><a:r><a:t>' + escape(line) + '</a:t></a:r></a:p>' for line in lines)
    return '<p:sp><p:nvSpPr><p:nvPr>' + marker + '</p:nvPr></p:nvSpPr><p:txBody>' + paragraphs + '</p:txBody></p:sp>'


def presentation(path: Path, *, body: list[str] | None = None, title: str | None = "Recorded slide title",
                 notes: str = "Actual recorded speaker notes", notes_number: int = 1, image: bool = False,
                 subtitle: str = "Original subtitle") -> None:
    content = (shape([title], "title") if title is not None else "")
    if subtitle and title is not None:
        content += shape([subtitle], "subTitle")
    content += shape(body or [])
    if image:
        content += '<p:pic><p:blipFill><a:blip r:embed="imageRelation"/></p:blipFill></p:pic>'
    root = f'<p:sld xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}" xmlns:r="{_RELATIONSHIP_NS}"><p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>'
    relation_type = _RELATIONSHIP_NS
    relations = (f'<Relationship Id="notesRelation" Type="{relation_type}/notesSlide" Target="../notesSlides/notesSlide{notes_number}.xml"/>'
                 + (f'<Relationship Id="imageRelation" Type="{relation_type}/image" Target="../media/slide.png"/>' if image else ""))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", root)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + relations + '</Relationships>')
        notes_shape = shape([notes], "body") + shape(["1"], "sldNum")
        archive.writestr(f"ppt/notesSlides/notesSlide{notes_number}.xml", f'<p:notes xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}"><p:cSld><p:spTree>{notes_shape}</p:spTree></p:cSld></p:notes>')
        if image:
            archive.writestr("ppt/media/slide.png", b"\x89PNG\r\n\x1a\noriginal image bytes")


def audio(path: Path) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 16000)


def pdf(path: Path, *, textful: bool) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if textful:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 16 Tf 40 740 Td (PDF slide title) Tj "
                         b"0 -28 Td (First preserved fact) Tj "
                         b"0 -28 Td (Second preserved fact) Tj "
                         b"0 -28 Td (Third preserved fact) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


class NotebookLMLocalSlideTests(unittest.TestCase):
    def test_powerpoint_text_and_speaker_notes_are_preserved_without_local_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "slides.pptx"
            body = ["First original fact", "Second original fact", "Third original fact"]
            presentation(path, body=body)
            with patch("notebooklm_media._run", side_effect=AssertionError("Text layer needs no process")):
                result = notebooklm_media.extract_slides(path, "Imported deck", ["chapter.txt"], None)
        slide = result["slides"][0]
        self.assertEqual(result["title"], "Imported deck")
        self.assertEqual(slide["title"], "Recorded slide title")
        self.assertEqual(slide["subtitle"], "Original subtitle")
        self.assertEqual(slide["bullets"], body)
        self.assertEqual(slide["speaker_notes"], "Actual recorded speaker notes")
        self.assertEqual(slide["source_ids"], ["chapter.txt"])

    def test_powerpoint_notes_follow_relationship_target_after_slide_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reordered.pptx"
            presentation(path, body=["First fact", "Second fact", "Third fact"], notes_number=7,
                         notes="Notes belonging to this slide through its relationship")
            result = notebooklm_media.extract_slides(path, "Deck", ["chapter.txt"], None)
        self.assertEqual(result["slides"][0]["speaker_notes"], "Notes belonging to this slide through its relationship")

    def test_powerpoint_slide_order_follows_presentation_relationships(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reordered.pptx"
            presentation(path, body=["First fact", "Second fact", "Third fact"], title="Original first slide")
            with zipfile.ZipFile(path, "a") as archive:
                content = shape(["Actual opening slide"], "title") + shape(["Opening fact one", "Opening fact two", "Opening fact three"])
                archive.writestr("ppt/slides/slide2.xml", f'<p:sld xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}"><p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>')
                archive.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{_PRESENTATION_NS}" xmlns:r="{_RELATIONSHIP_NS}"><p:sldIdLst><p:sldId id="256" r:id="second"/><p:sldId id="257" r:id="first"/></p:sldIdLst></p:presentation>')
                archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                 f'<Relationship Id="first" Type="{_RELATIONSHIP_NS}/slide" Target="slides/slide1.xml"/>'
                                 f'<Relationship Id="second" Type="{_RELATIONSHIP_NS}/slide" Target="slides/slide2.xml"/></Relationships>')
            result = notebooklm_media.extract_slides(path, "Deck", ["chapter.txt"], None)
        self.assertEqual([slide["title"] for slide in result["slides"]], ["Actual opening slide", "Original first slide"])

    def test_powerpoint_explicit_line_break_preserves_word_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "linebreak.pptx"
            paragraph = '<a:p><a:r><a:t>First actual word</a:t></a:r><a:br/><a:r><a:t>continued without loss</a:t></a:r></a:p>'
            body = '<p:sp><p:txBody>' + paragraph + '</p:txBody></p:sp>' + shape(["Second actual fact", "Third actual fact"])
            content = shape(["Original title"], "title") + body
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/slides/slide1.xml", f'<p:sld xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}"><p:cSld><p:spTree>{content}</p:spTree></p:cSld></p:sld>')
            result = notebooklm_media.extract_slides(path, "Deck", ["chapter.txt"], None)
        self.assertEqual(result["slides"][0]["bullets"][0], "First actual word\ncontinued without loss")

    def test_image_only_powerpoint_uses_installed_tesseract_and_exact_recognized_words(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "images.pptx"
            presentation(path, title=None, subtitle="", image=True, notes="")
            with patch("notebooklm_media.shutil.which", return_value="local-tesseract.exe"), \
                    patch("notebooklm_media._run", return_value="Recognized title\nFirst recognized fact\nSecond recognized fact\nThird recognized fact\n") as run:
                result = notebooklm_media.extract_slides(path, "Deck", ["source.txt"], None)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "local-tesseract.exe")
            self.assertEqual(argv[2], "stdout")
            self.assertEqual(run.call_args.kwargs["timeout"], 120)
        slide = result["slides"][0]
        self.assertEqual(slide["title"], "Recognized title")
        self.assertEqual(slide["bullets"], ["First recognized fact", "Second recognized fact", "Third recognized fact"])
        self.assertEqual(slide["speaker_notes"], "First recognized fact\nSecond recognized fact\nThird recognized fact")
        self.assertEqual(slide["image_query"], "Recognized title")

    def test_text_title_and_image_body_still_extracts_image_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed.pptx"
            presentation(path, image=True)
            with patch("notebooklm_media.shutil.which", return_value="tesseract"), \
                    patch("notebooklm_media._run", return_value="Recorded slide title\nFirst recognized fact\nSecond recognized fact\nThird recognized fact\n"):
                result = notebooklm_media.extract_slides(path, "Deck", ["source.txt"], None)
        slide = result["slides"][0]
        self.assertEqual(slide["title"], "Recorded slide title")
        self.assertEqual(slide["subtitle"], "Original subtitle")
        self.assertEqual(slide["bullets"], ["First recognized fact", "Second recognized fact", "Third recognized fact"])

    def test_missing_tesseract_for_powerpoint_images_is_actionable_without_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "images.pptx"
            presentation(path, title=None, subtitle="", image=True)
            with patch("notebooklm_media.shutil.which", return_value=None), patch("notebooklm_media._run") as run:
                with self.assertRaisesRegex(InputDocumentError, "Tesseract.*content.json"):
                    notebooklm_media.extract_slides(path, "Deck", ["source.txt"], None)
                run.assert_not_called()

    def test_pdf_text_layer_preserves_lines_and_defaults_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "text.pdf"
            pdf(path, textful=True)
            with patch("notebooklm_media._run", side_effect=AssertionError("No OCR needed")):
                result = notebooklm_media.extract_slides(path, "PDF deck", ["chapter.txt"], None)
        slide = result["slides"][0]
        self.assertEqual(slide["title"], "PDF slide title")
        self.assertEqual(slide["bullets"], ["First preserved fact", "Second preserved fact", "Third preserved fact"])
        self.assertEqual(slide["speaker_notes"], "First preserved fact\nSecond preserved fact\nThird preserved fact")
        self.assertEqual(slide["source_ids"], ["chapter.txt"])

    def test_pdf_without_text_and_poppler_has_actionable_error_without_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.pdf"
            pdf(path, textful=False)
            with patch("notebooklm_media.shutil.which", return_value=None), patch("notebooklm_media._run") as run:
                with self.assertRaisesRegex(InputDocumentError, "pdftoppm and Tesseract.*content.json"):
                    notebooklm_media.extract_slides(path, "Deck", ["source.txt"], None)
                run.assert_not_called()

    def test_pdf_images_run_local_page_rasterizer_then_ocr_without_changing_recognized_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.pdf"
            pdf(path, textful=False)
            installed = {"pdftoppm": "local-pdftoppm", "tesseract": "local-tesseract"}
            with patch("notebooklm_media.shutil.which", side_effect=installed.get), \
                    patch("notebooklm_media._run", side_effect=["", "Actual PDF title\nActual first fact\nActual second fact\nActual third fact\n"]) as run:
                result = notebooklm_media.extract_slides(path, "Deck", ["chapter.txt"], None)
            rasterize, recognize = run.call_args_list
            self.assertEqual(rasterize.args[0][:8], ["local-pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-png", str(path)])
            self.assertEqual(recognize.args[0], ["local-tesseract", rasterize.args[0][-1] + ".png", "stdout"])
        self.assertEqual(result["slides"][0]["bullets"], ["Actual first fact", "Actual second fact", "Actual third fact"])
        self.assertEqual(result["slides"][0]["speaker_notes"], "Actual first fact\nActual second fact\nActual third fact")

    def test_ocr_with_too_few_content_lines_fails_instead_of_inventing_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sparse.pptx"
            presentation(path, title=None, subtitle="", image=True)
            with patch("notebooklm_media.shutil.which", return_value="tesseract"), \
                    patch("notebooklm_media._run", return_value="Actual title\nOne actual line\n"):
                with self.assertRaisesRegex(InputDocumentError, "fewer than three.*content.json"):
                    notebooklm_media.extract_slides(path, "Deck", ["source.txt"], None)


class NotebookLMLocalAudioTests(unittest.TestCase):
    def test_configured_local_process_uses_argument_array_and_preserves_observed_speakers(self) -> None:
        transcript = {"segments": [{"speaker": "Recorded host A", "text": "Actual first spoken words."},
                                    {"speaker": "Recorded host B", "text": "Actual reply, preserved exactly."},
                                    {"speaker": "Recorded host A", "text": "Actual final turn."}]}
        captured = []
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0

        def launch(argv, *, stdout, stderr, creationflags):
            captured.append(argv)
            stdout.write(json.dumps(transcript).encode("utf-8"))
            return process

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recording & sample.wav"
            audio(path)
            command = json.dumps(["installed-diarizer", "--input", "{input}"])
            with patch.dict(os.environ, {"NOTEBOOKLM_TRANSCRIBER_COMMAND": command}), \
                    patch("notebooklm_media.subprocess.Popen", side_effect=launch):
                data, metadata = notebooklm_media.extract_podcast(path, "recording", "Recorded episode", None)
            self.assertEqual(captured, [["installed-diarizer", "--input", str(path)]])
        self.assertEqual([host["speaker_id"] for host in data["cast"]], ["Recorded host A", "Recorded host B"])
        self.assertEqual([host["host_id"] for host in data["cast"]], ["HOST_A", "HOST_B"])
        self.assertEqual([scene["dialogue"] for scene in data["script"][0]["scenes"]],
                         [segment["text"] for segment in transcript["segments"]])
        self.assertEqual(metadata["transcript"], transcript)
        self.assertAlmostEqual(metadata["original_duration_minutes"], 2 / 60)

    def test_missing_local_transcriber_explains_sidecar_and_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audio.wav"
            audio(path)
            with patch.dict(os.environ, {"NOTEBOOKLM_TRANSCRIBER_COMMAND": ""}), patch("notebooklm_media._run") as run:
                with self.assertRaisesRegex(InputDocumentError, "transcript.json.*NOTEBOOKLM_TRANSCRIBER_COMMAND"):
                    notebooklm_media.extract_podcast(path, "audio", "Episode", None)
                run.assert_not_called()

    def test_one_or_three_speakers_are_rejected_without_fabricating_cast(self) -> None:
        for names in (["one"], ["one", "two", "three"]):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "audio.wav"
                audio(path)
                transcript = {"segments": [{"speaker": name, "text": "Original dialogue"} for name in names]}
                with patch("notebooklm_media._transcript", return_value=transcript):
                    with self.assertRaisesRegex(InputDocumentError, "exactly two observed speakers"):
                        notebooklm_media.extract_podcast(path, "audio", "Episode", None)

    def test_provided_cast_cannot_duplicate_observed_speakers(self) -> None:
        transcript = {"segments": [{"speaker": "A", "text": "First"}, {"speaker": "B", "text": "Second"}],
                      "cast": [{"speaker_id": "A"}, {"speaker_id": "A"}, {"speaker_id": "B"}]}
        with patch("notebooklm_media._transcript", return_value=transcript):
            with self.assertRaisesRegex(InputDocumentError, "cast.*exactly"):
                notebooklm_media.extract_podcast(Path("audio.wav"), "audio", "Episode", None)

    def test_non_json_local_transcriber_output_is_actionable(self) -> None:
        with patch.dict(os.environ, {"NOTEBOOKLM_TRANSCRIBER_COMMAND": '["local-transcriber", "{input}"]'}), \
                patch("notebooklm_media._run", return_value="unlabelled plain transcript"):
            with self.assertRaisesRegex(InputDocumentError, "speaker-labelled JSON"):
                notebooklm_media.extract_podcast(Path("audio.wav"), "audio", "Episode", None)

    def test_cancellation_terminates_running_local_tool(self) -> None:
        process = Mock()
        process.poll.return_value = None
        checks = iter([False, True])
        with patch("notebooklm_media.subprocess.Popen", return_value=process):
            with self.assertRaises(GenerationCancelled):
                notebooklm_media._run(["installed-local-tool", "audio.wav"], lambda: next(checks))
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_missing_configured_executable_reports_start_failure(self) -> None:
        with patch("notebooklm_media.subprocess.Popen", side_effect=FileNotFoundError("Local executable missing")):
            with self.assertRaisesRegex(InputDocumentError, "Could not start.*local extraction tool"):
                notebooklm_media._run(["unavailable-local-tool"], None)


if __name__ == "__main__":
    unittest.main()
