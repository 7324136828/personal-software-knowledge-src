"""Local text extraction for NotebookLM slide and audio exports.

The optional commands run without a shell. OCR uses installed Tesseract; a
configured local transcription command must return speaker-labelled JSON. No
models or tools are downloaded, and no remote service is contacted.
"""

from __future__ import annotations

import json
import copy
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from urllib.parse import unquote

from errors import GenerationCancelled, InputDocumentError

_OCR_CACHE = {}
_TRANSCRIPT_CACHE = {}


def _archive_target(base: str, target: str) -> str:
    target = unquote(target)
    return posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))


def _cancel(check):
    if check is not None and check():
        raise GenerationCancelled("NotebookLM conversion was cancelled.")


def _run(argv: list[str], check, *, timeout: int = 3600) -> str:
    _cancel(check)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, creationflags=flags)
        except OSError as exc:
            raise InputDocumentError(f"Could not start the configured local extraction tool: {exc}") from exc
        started = time.monotonic()
        try:
            while process.poll() is None:
                _cancel(check)
                if time.monotonic() - started > timeout:
                    raise InputDocumentError("Local NotebookLM extraction tool exceeded its time limit.")
                time.sleep(0.1)
            _cancel(check)
            if process.returncode:
                raise InputDocumentError(f"Local NotebookLM extraction tool failed (exit {process.returncode}).")
            stdout.seek(0)
            return stdout.read().decode("utf-8-sig")
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise


def _ocr(image: Path, check) -> str:
    _cancel(check)
    command = shutil.which("tesseract")
    if command is None:
        raise InputDocumentError("NotebookLM slides contain only images. No local OCR tool is available. "
                                 "Install Tesseract or supply '<artifact>.content.json' with the canonical slide content.")
    try:
        status = image.stat()
        key = (str(image.resolve()), status.st_mtime_ns, status.st_size, command)
    except OSError:
        key = None
    if key is not None and key in _OCR_CACHE:
        return _OCR_CACHE[key]
    text = _run([command, str(image), "stdout"], check, timeout=120)
    if key is not None:
        _OCR_CACHE[key] = text
    return text


def extract_image_text(source: Path, check) -> tuple[str, dict]:
    """Optionally recover literal image text while retaining the source visual."""
    _cancel(check)
    if shutil.which("tesseract") is None:
        return "", {"ocr_status": "unavailable", "ocr_engine": "tesseract",
                    "warning": "No local OCR tool is available; original infographic visual preserved without editable text."}
    try:
        text = _ocr(source, check).strip()
    except InputDocumentError as exc:
        return "", {"ocr_status": "failed", "ocr_engine": "tesseract", "warning": str(exc)}
    if not text:
        return "", {"ocr_status": "no_text", "ocr_engine": "tesseract",
                    "warning": "Local OCR returned no text; original infographic visual preserved."}
    return text, {"ocr_status": "extracted", "ocr_engine": "tesseract",
                  "ocr_policy": "Verbatim local OCR output; no semantic categories inferred."}


def _paragraphs(element) -> list[str]:
    paragraphs = []
    for paragraph in element.iter():
        if paragraph.tag.endswith("}p"):
            text = "".join((node.text or "") if node.tag.endswith("}t") else "\n"
                           for node in paragraph.iter() if node.tag.endswith(("}t", "}br"))).strip()
            if text:
                paragraphs.append(text)
    return paragraphs


def _slide(title: str, lines: list[str], sources: list[str], *, subtitle="", notes="") -> dict:
    lines = [line.strip() for line in lines if line.strip()]
    if len(lines) < 3:
        raise InputDocumentError(f"NotebookLM slide '{title}' has fewer than three extractable content lines. "
                                 "Supply canonical '.content.json' to identify its bullet content without fabricating it.")
    # Group long paragraphs into six bullets, preserving every source line.
    if len(lines) > 6:
        width = (len(lines) + 5) // 6
        lines = ["\n".join(lines[index:index + width]) for index in range(0, len(lines), width)]
    return {"title": title, "subtitle": subtitle, "bullets": lines, "speaker_notes": notes or "\n".join(lines),
            "image_query": title, "source_ids": sources}


def _pptx(path: Path, sources: list[str], check, *, raw_text: bool = False) -> list:
    try:
        with ZipFile(path) as archive:
            slide_paths = sorted((name for name in archive.namelist()
                                  if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                                 key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
            if "ppt/presentation.xml" in archive.namelist() and "ppt/_rels/presentation.xml.rels" in archive.namelist():
                relations = {}
                for relation in ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels")):
                    if relation.get("Type", "").endswith("/slide") and relation.get("TargetMode") != "External":
                        target = relation.get("Target", "")
                        relations[relation.get("Id")] = _archive_target("ppt", target)
                ordered = []
                for node in ET.fromstring(archive.read("ppt/presentation.xml")).iter():
                    if node.tag.endswith("}sldId"):
                        relation_id = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                        target = relations.get(relation_id)
                        if target is None or target not in archive.namelist():
                            raise InputDocumentError("NotebookLM PowerPoint slide order contains a missing relationship target.")
                        ordered.append(target)
                if ordered:
                    slide_paths = ordered
            slides = []
            for index, name in enumerate(slide_paths, 1):
                _cancel(check)
                root = ET.fromstring(archive.read(name))
                relationships = posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")
                targets, notes_path = {}, None
                if relationships in archive.namelist():
                    for relation in ET.fromstring(archive.read(relationships)):
                        if relation.get("TargetMode") == "External":
                            continue
                        target = _archive_target(posixpath.dirname(name), relation.get("Target", ""))
                        if relation.get("Type", "").endswith("/image"):
                            targets[relation.get("Id")] = target
                        elif relation.get("Type", "").endswith("/notesSlide"):
                            notes_path = target
                title, subtitle, lines = "", "", []
                for shape in root.iter():
                    if not shape.tag.endswith("}sp"):
                        continue
                    texts = _paragraphs(shape)
                    if not texts:
                        continue
                    placeholder = next((node for node in shape.iter() if node.tag.endswith("}ph")), None)
                    kind = placeholder.get("type", "") if placeholder is not None else ""
                    if kind in {"title", "ctrTitle"}:
                        title = " ".join(texts)
                    elif kind == "subTitle":
                        subtitle = " ".join(texts)
                    else:
                        lines.extend(texts)
                if not title and lines:
                    title = lines.pop(0)
                if len(lines) < 3 and targets:
                    blocks = []
                    with tempfile.TemporaryDirectory() as temporary:
                        for number, node in enumerate(root.iter()):
                            if not node.tag.endswith("}blip"):
                                continue
                            relation = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
                            image_path = targets.get(relation)
                            if image_path is None or image_path not in archive.namelist():
                                continue
                            image = Path(temporary) / f"image-{number}{Path(image_path).suffix}"
                            image.write_bytes(archive.read(image_path))
                            blocks.append(_ocr(image, check))
                    extracted = [line.strip() for line in "\n".join(blocks).splitlines() if line.strip()]
                    if extracted:
                        if not title:
                            title, extracted = extracted[0], extracted[1:]
                        lines.extend(line for line in extracted if line not in {title, subtitle, *lines})
                notes = []
                if notes_path is not None and notes_path in archive.namelist():
                    note_root = ET.fromstring(archive.read(notes_path))
                    for shape in note_root.iter():
                        if shape.tag.endswith("}sp"):
                            placeholder = next((node for node in shape.iter() if node.tag.endswith("}ph")), None)
                            if placeholder is None or placeholder.get("type") != "sldNum":
                                notes.extend(_paragraphs(shape))
                if raw_text:
                    text = "\n".join(value for value in [title, subtitle, *lines, *notes] if value.strip())
                    if not text.strip():
                        raise InputDocumentError(f"No text could be extracted from NotebookLM slide {index}.")
                    slides.append(f"--- Slide {index} ---\n{text}")
                else:
                    slides.append(_slide(title or f"Slide {index}", lines, sources, subtitle=subtitle, notes="\n".join(notes)))
            return slides
    except (BadZipFile, ET.ParseError, KeyError, OSError) as exc:
        raise InputDocumentError(f"Could not extract NotebookLM PowerPoint '{path.name}': {exc}") from exc


def extract_slides(source: Path, title: str, sources: list[str], check) -> dict:
    candidates = list(source.rglob("*.pptx")) if source.is_dir() else ([source] if source.suffix.lower() == ".pptx" else [])
    if candidates:
        return {"title": title, "slides": _pptx(sorted(candidates)[0], sources, check)}
    pdfs = list(source.rglob("*.pdf")) if source.is_dir() else ([source] if source.suffix.lower() == ".pdf" else [])
    if not pdfs:
        raise InputDocumentError("NotebookLM slide export needs a PowerPoint, PDF, or canonical '.content.json'.")
    try:
        from pypdf import PdfReader
        pages = list(PdfReader(pdfs[0]).pages)
        slides = []
        for index, page in enumerate(pages, 1):
            _cancel(check)
            text = page.extract_text() or ""
            if not text.strip():
                command = shutil.which("pdftoppm")
                if command is None:
                    raise InputDocumentError("NotebookLM PDF slides have no text layer. Local OCR requires installed "
                                             "pdftoppm and Tesseract, or supply canonical '<artifact>.content.json'.")
                with tempfile.TemporaryDirectory() as temporary:
                    prefix = Path(temporary) / "slide"
                    _run([command, "-f", str(index), "-l", str(index), "-singlefile", "-png", str(pdfs[0]), str(prefix)], check, timeout=120)
                    text = _ocr(prefix.with_suffix(".png"), check)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            slides.append(_slide(lines[0] if lines else f"Slide {index}", lines[1:], sources))
        return {"title": title, "slides": slides}
    except (OSError, ValueError, ImportError) as exc:
        raise InputDocumentError(f"Could not extract NotebookLM PDF '{pdfs[0].name}': {exc}") from exc


def extract_ocr_text(source: Path, action: str, *, cancel_check=None) -> str:
    """Extract visual evidence for model structuring, with no bullet constraints."""
    source = Path(source)
    _cancel(cancel_check)
    if action == "create_infographics":
        return _ocr(source, cancel_check).strip()
    if action != "create_slides":
        raise InputDocumentError("NotebookLM visual text extraction supports slides and infographics only.")
    powerpoints = sorted(source.rglob("*.pptx")) if source.is_dir() else ([source] if source.suffix.lower() == ".pptx" else [])
    if powerpoints:
        return "\n\n".join(_pptx(powerpoints[0], [], cancel_check, raw_text=True))
    pdfs = sorted(source.rglob("*.pdf")) if source.is_dir() else ([source] if source.suffix.lower() == ".pdf" else [])
    if not pdfs:
        if source.is_dir():
            images = []
            raster_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
            for path in source.rglob("*"):
                if not path.is_file() or (path.suffix and path.suffix.lower() not in raster_extensions):
                    continue
                with path.open("rb") as stream:
                    signature = stream.read(12)
                is_raster = (signature.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\0", b"MM\0*"))
                             or signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
                if is_raster:
                    images.append(path)
            natural = lambda path: [int(part) if part.isdigit() else part.lower()
                                    for part in re.split(r"(\d+)", path.relative_to(source).as_posix())]
            parts = []
            for index, image in enumerate(sorted(images, key=natural), 1):
                _cancel(cancel_check)
                text = _ocr(image, cancel_check).strip()
                if not text:
                    raise InputDocumentError(f"No text could be extracted from NotebookLM slide image '{image.name}'.")
                parts.append(f"--- Slide {index}: {image.name} ---\n{text}")
            if parts:
                return "\n\n".join(parts)
        raise InputDocumentError("NotebookLM slide OCR requires an exported PowerPoint, PDF, or ordered raster images.")
    try:
        from pypdf import PdfReader
        parts = []
        for index, page in enumerate(PdfReader(pdfs[0]).pages, 1):
            _cancel(cancel_check)
            text = page.extract_text() or ""
            if not text.strip():
                rasterizer = shutil.which("pdftoppm")
                if rasterizer is None or shutil.which("tesseract") is None:
                    raise InputDocumentError("NotebookLM PDF slides contain only images. Local extraction requires "
                                             "installed pdftoppm and Tesseract; no tools are downloaded automatically.")
                with tempfile.TemporaryDirectory() as temporary:
                    prefix = Path(temporary) / "slide"
                    _run([rasterizer, "-f", str(index), "-l", str(index), "-singlefile", "-png", str(pdfs[0]), str(prefix)], cancel_check, timeout=120)
                    text = _ocr(prefix.with_suffix(".png"), cancel_check)
            if not text.strip():
                raise InputDocumentError(f"No text could be extracted from NotebookLM PDF slide {index}.")
            parts.append(f"--- Slide {index} ---\n{text.strip()}")
        return "\n\n".join(parts)
    except (OSError, ValueError, ImportError) as exc:
        raise InputDocumentError(f"Could not extract NotebookLM slide PDF '{pdfs[0].name}': {exc}") from exc


def _transcript(source: Path, name: str, check) -> dict:
    transcript = source.parent / (name + ".transcript.json")
    if transcript.is_file():
        try:
            return json.loads(transcript.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise InputDocumentError(f"Invalid NotebookLM transcript '{transcript.name}': {exc}") from exc
    configured = os.environ.get("NOTEBOOKLM_TRANSCRIBER_COMMAND")
    if configured:
        try:
            argv = json.loads(configured)
        except ValueError as exc:
            raise InputDocumentError("NOTEBOOKLM_TRANSCRIBER_COMMAND must be a JSON array of local command arguments.") from exc
        if not isinstance(argv, list) or not argv or any(not isinstance(value, str) for value in argv):
            raise InputDocumentError("NOTEBOOKLM_TRANSCRIBER_COMMAND must be a non-empty JSON argument array.")
        try:
            _cancel(check)
            try:
                status = source.stat()
                key = (str(source.resolve()), status.st_mtime_ns, status.st_size, configured)
            except OSError:
                key = None
            if key is not None and key in _TRANSCRIPT_CACHE:
                return copy.deepcopy(_TRANSCRIPT_CACHE[key])
            transcript = json.loads(_run([value.replace("{input}", str(source)) for value in argv], check))
            if key is not None:
                _TRANSCRIPT_CACHE[key] = transcript
            return copy.deepcopy(transcript)
        except ValueError as exc:
            raise InputDocumentError("Local transcriber must output speaker-labelled JSON, not plain text.") from exc
    raise InputDocumentError("NotebookLM audio has no speaker-labelled transcript. No configured local transcription "
                             "tool is available. Supply '<artifact>.transcript.json' or canonical '.content.json', or "
                             "configure NOTEBOOKLM_TRANSCRIBER_COMMAND with an installed local diarizing transcriber. "
                             "Models are never downloaded automatically.")


def extract_podcast(source: Path, name: str, title: str, check) -> tuple[dict, dict]:
    transcript = _transcript(source, name, check)
    if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list) or not transcript["segments"]:
        raise InputDocumentError("NotebookLM transcript needs a non-empty segments array with speaker and text.")
    speakers = []
    for segment in transcript["segments"]:
        if not isinstance(segment, dict) or not isinstance(segment.get("speaker"), str) or not segment["speaker"].strip() or not isinstance(segment.get("text"), str) or not segment["text"].strip():
            raise InputDocumentError("Every NotebookLM transcript segment needs a real speaker label and non-empty text.")
        if segment["speaker"] not in speakers:
            speakers.append(segment["speaker"])
    if len(speakers) != 2:
        raise InputDocumentError("Canonical podcast import requires exactly two observed speakers. "
                                 "Supply a diarized transcript or structured '.content.json'; speaker turns are not fabricated.")
    cast = transcript.get("cast")
    if cast is None:
        cast = [{"speaker_id": speaker, "host_id": "HOST_A" if index == 0 else "HOST_B", "name": speaker,
                 "voice_file": source.name, "style": "Original recorded speaker"} for index, speaker in enumerate(speakers)]
    if not isinstance(cast, list) or len(cast) != 2 or any(not isinstance(member, dict) or not isinstance(member.get("speaker_id"), str)
                                         for member in cast) or {member["speaker_id"] for member in cast} != set(speakers):
        raise InputDocumentError("NotebookLM transcript cast must identify exactly its observed speaker labels.")
    scenes = [{"speaker_id": segment["speaker"], "directions": "", "dialogue": segment["text"]}
              for segment in transcript["segments"]]
    data = {"episode_title": transcript.get("episode_title", title), "podcast_show": transcript.get("podcast_show", "NotebookLM"),
            "cast": cast, "script": [{"segment_name": transcript.get("segment_name", "Imported recording"), "scenes": scenes}]}
    metadata = {"transcript": transcript, "speaker_assignment": "observed transcript labels",
                "duration_policy": "original imported dialogue preserved without generation runtime truncation"}
    try:
        with wave.open(str(source), "rb") as audio:
            metadata["original_duration_minutes"] = audio.getnframes() / audio.getframerate() / 60
    except (wave.Error, OSError):
        pass
    return data, metadata
