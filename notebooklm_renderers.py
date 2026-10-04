"""Deterministic companion formats for canonical NotebookLM study artifacts."""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import textwrap
import xml.etree.ElementTree as ET
from html import escape

from errors import InvalidArgumentsError
from pipeline.schemas import canonical_action


_JSON_KEYS = {
    "create_datatables": ("title", "fields", "data"),
    "create_flashcards": ("title", "description", "cards"),
    "create_infographics": ("title", "subtitle", "sections"),
    "create_mindmaps": ("name", "children"),
    "create_podcasts": ("episode_title", "podcast_show", "cast", "script"),
    "create_qandas": ("title", "description", "questions"),
    "create_quizzes": ("title", "description", "questions"),
    "create_reports": ("title", "executive_summary", "sections", "conclusions"),
    "create_slides": ("title", "slides"),
}
_FORMATS = {
    "create_datatables": {".json", ".csv", ".md"},
    "create_flashcards": {".json", ".txt"},
    "create_infographics": {".json", ".md", ".html", ".svg", ".wireframe.txt"},
    "create_mindmaps": {".json", ".md", ".mmd"},
    "create_podcasts": {".json", ".md"},
    "create_qandas": {".json"}, "create_quizzes": {".json"},
    "create_reports": {".json", ".md", ".html"},
    "create_slides": {".json", ".md"},
}
_SVG_NS = "http://www.w3.org/2000/svg"
_SVG_TAGS = {
    "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "title", "desc", "image", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "pattern", "use", "symbol", "marker",
}


def _text(value) -> str:
    return "" if value is None else str(value)


def _line(value) -> str:
    return re.sub(r"[\t\r\n]+", " ", _text(value))


def _finish(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def _cards(data: dict) -> str:
    lines = ["# " + _line(data["title"]), "# " + _line(data["description"])]
    lines.extend(_line(card["front"]) + "\t" + _line(card["back"]) for card in data["cards"])
    return _finish(lines)


def _table(data: dict, extension: str) -> str:
    names = [field["name"] for field in data["fields"]]
    names += [name for name in ("source_id", "page") if name not in names]
    if extension == ".csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=names, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data["data"])
        return stream.getvalue()
    def cell(value):
        return _text(value).replace("|", r"\|").replace("\n", "<br>")
    lines = ["| " + " | ".join(names) + " |", "| " + " | ".join("---" for _ in names) + " |"]
    lines.extend("| " + " | ".join(cell(row.get(name)) for name in names) + " |" for row in data["data"])
    return _finish(lines)


def _mindmap(data: dict, extension: str) -> str:
    lines = []
    count = 0
    def label(value):
        return _line(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    if extension == ".mmd":
        lines = ["mindmap", '  root(("' + label(data["name"]) + '"))']
    def walk(node, depth):
        nonlocal count
        if extension == ".md":
            # Markdown defines six heading levels. Deeper nodes remain in a
            # nested list instead of producing invalid seven-level headings.
            if depth < 6:
                lines.append("#" * (depth + 1) + " " + _line(node["name"]))
            else:
                lines.append("  " * (depth - 6) + "- " + _line(node["name"]))
        elif depth:
            count += 1
            lines.append("  " * depth + f'n{count}["' + label(node["name"]) + '"]')
        for child in node.get("children", []):
            walk(child, depth + 1)
    walk(data, 0)
    return _finish(lines)


def _podcast(data: dict) -> str:
    names = {host["speaker_id"]: host["name"] for host in data["cast"]}
    lines = ["# " + data["episode_title"], "_" + data["podcast_show"] + "_"]
    for segment in data["script"]:
        lines.extend(["", "## " + segment["segment_name"]])
        for scene in segment["scenes"]:
            if scene["dialogue"].strip():
                lines.extend(["", "**" + names[scene["speaker_id"]] + ":** " + scene["dialogue"]])
    return _finish(lines)


def _slides(data: dict) -> str:
    lines = ["# " + data["title"]]
    for slide in data["slides"]:
        lines.extend(["", "---", "## " + slide["title"]])
        if slide["subtitle"]:
            lines.append("### " + slide["subtitle"])
        lines.append("")
        lines.extend("- " + bullet for bullet in slide["bullets"])
        lines.extend(["", "**Speaker Notes:** " + slide["speaker_notes"], "",
                      "**Sources:** " + ", ".join(slide["source_ids"])])
    return _finish(lines)


def _citation(citation: dict) -> str:
    label = _text(citation["source_id"])
    if citation.get("chunk_id") is not None:
        label += ":" + _text(citation["chunk_id"])
    if citation.get("page") is not None:
        label += ":p" + _text(citation["page"])
    return "[" + label + "]"


def _report_md(data: dict) -> str:
    lines = ["# " + data["title"], "", "## Executive Summary", data["executive_summary"]]
    for section in data["sections"]:
        lines.extend(["", "## " + section["title"], section["content"], "", "### Key Claims"])
        for claim in section["claims"]:
            lines.extend(["- " + claim["claim"], "  Sources: " + ", ".join(map(_citation, claim["citations"]))])
    lines.extend(["", "## Conclusions", data["conclusions"]])
    return _finish(lines)


_REPORT_CSS = """:root { --bg:#fbfbf9; --fg:#1b1b1b; --muted:#666; --border:#7a4b9e; --cardline:#ddd; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161619; --fg:#ececec; --muted:#a0a0a0; --border:#b98fd6; --cardline:#333; }
}
body { margin:0; background:var(--bg); color:var(--fg);
  font-family:"Segoe UI",system-ui,-apple-system,sans-serif; line-height:1.6; }
main { max-width:820px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
h1 { font-size:1.9rem; }
h2 { font-size:1.25rem; margin-top:2rem; border-bottom:1px solid var(--cardline); padding-bottom:.3rem; }
.claim { border-left:3px solid var(--border); padding:.4rem .8rem; margin:.5rem 0;
  background:rgba(122,75,158,.06); border-radius:0 6px 6px 0; }
.citation { color:var(--muted); font-size:.85rem; font-variant-numeric:tabular-nums; }"""


def _html(title: str, css: str, body: str) -> str:
    return ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>' + escape(title) + '</title>\n<style>\n' + css + '\n</style>\n</head>\n'
            '<body>\n<main>\n' + body + '\n</main>\n</body>\n</html>\n')


def _paragraphs(value: str) -> str:
    return "\n".join("<p>" + escape(paragraph).replace("\n", "<br>") + "</p>"
                     for paragraph in re.split(r"\n\s*\n", value))


def _report_html(data: dict) -> str:
    parts = ["<h1>" + escape(data["title"]) + "</h1>", "<h2>Executive Summary</h2>", _paragraphs(data["executive_summary"])]
    for section in data["sections"]:
        parts.extend(["<h2>" + escape(section["title"]) + "</h2>", _paragraphs(section["content"])])
        claims = []
        for claim in section["claims"]:
            citations = " ".join('<span class="citation">' + escape(_citation(citation)) + '</span>'
                                 for citation in claim["citations"])
            claims.append('<div class="claim">' + escape(claim["claim"]) + " " + citations + "</div>")
        parts.append("".join(claims))
    parts.extend(["<h2>Conclusions</h2>", _paragraphs(data["conclusions"])])
    return _html(data["title"], _REPORT_CSS, "\n".join(parts))


def _panels(data: dict) -> list[tuple[str | None, list[dict]]]:
    groups = []
    for section in data["sections"]:
        panel = section.get("panel")
        if not groups or panel != groups[-1][0]:
            groups.append((panel, []))
        groups[-1][1].append(section)
    return groups


def _infographic_md(data: dict) -> str:
    lines = ["# " + data["title"], "", "*" + data["subtitle"] + "*"]
    panel_number = 0
    for panel, sections in _panels(data):
        if panel is not None:
            panel_number += 1
            lines.extend(["", f"## Panel {panel_number}: {panel}"])
        for section in sections:
            lines.extend(["", "### " + section["title"]])
            kind = section["type"]
            if kind == "quote":
                lines.extend("> " + line for line in _text(section["value"]).splitlines())
                if section["label"]:
                    lines.extend([">", "> — " + section["label"]])
            elif kind == "stat":
                lines.append("**" + _text(section["value"]) + "**")
                if section["label"]:
                    lines.extend(["", "  " + section["label"]])
            elif kind == "svg":
                lines.append("![" + _text(section["label"]) + "](" + _asset_reference(section["value"]) + ")")
            else:
                lines.extend(f"{index}. {item}" for index, item in enumerate(section["items"], 1))
    return _finish(lines)


def _asset_reference(value) -> str:
    """Allow package-relative diagram assets only."""
    value = _text(value).replace("\\", "/")
    if (not value.startswith("assets/") or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(char in value for char in ':\x00\r\n<>"')):
        raise InvalidArgumentsError("Infographic SVG references must be relative files under assets/.")
    return value


def _safe_svg(text: str) -> str:
    """Keep declarative SVG drawing content and embedded raster images."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise InvalidArgumentsError("Infographic asset contains invalid SVG.") from exc
    if root.tag.split("}")[-1] != "svg":
        raise InvalidArgumentsError("Infographic diagram asset must be an SVG.")
    for parent in root.iter():
        for child in list(parent):
            if child.tag.split("}")[-1] not in _SVG_TAGS:
                parent.remove(child)
        for name, value in list(parent.attrib.items()):
            local = name.split("}")[-1].lower()
            if local.startswith("on") or local == "style":
                del parent.attrib[name]
            elif local == "href" and not (value.startswith("#") or re.match(r"^data:image/(?:png|jpeg|gif|webp);base64,", value)):
                del parent.attrib[name]
            elif "url(" in value.lower() and not re.fullmatch(r"url\(#[A-Za-z_][\w:.-]*\)", value):
                del parent.attrib[name]
    ET.register_namespace("", _SVG_NS)
    return ET.tostring(root, encoding="unicode")


def _assets(data: dict) -> dict[str, str]:
    return {_asset_reference(asset["id"]): _safe_svg(asset["svg"]) for asset in data.get("assets", [])}


def _chart_items(items: list[str]) -> list[tuple[str, float, str]]:
    parsed = []
    for item in items:
        label, separator, raw = item.rpartition(":")
        try:
            value = float(raw.strip().replace(",", ""))
        except ValueError as exc:
            raise InvalidArgumentsError("Infographic chart items must contain 'label: number'.") from exc
        if not separator or not math.isfinite(value):
            raise InvalidArgumentsError("Infographic chart items must contain finite numbers.")
        parsed.append((label.strip(), value, raw.strip()))
    return parsed


_INFOGRAPHIC_CSS = """:root { --bg:#f7f7f5; --fg:#1a1a1a; --muted:#5a5a5a; --card:#fff; --border:#d8d8d2; --accent:#7a4b9e; --track:#ececec; }
@media (prefers-color-scheme: dark) { :root { --bg:#16161a; --fg:#ececec; --muted:#a0a0a0; --card:#1f1f25; --border:#33333a; --accent:#b98fd6; --track:#2a2a30; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font-family:"Segoe UI",system-ui,sans-serif; line-height:1.55; }
main { max-width:1040px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }
h1 { font-size:2rem; margin:0 0 .3rem; }
.subtitle { color:var(--muted); margin-bottom:2rem; }
.panel { position:relative; background:var(--card); border:1px solid var(--border); border-radius:14px; padding:1.5rem; margin-bottom:1.5rem; }
.panel > h2 { color:var(--accent); margin:0 0 1rem; font-size:1.25rem; }
.pnum { position:absolute; top:-14px; left:-14px; width:30px; height:30px; border-radius:50%; background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; }
.sec { margin:1rem 0; }
.sec h3 { font-size:1.02rem; margin:0 0 .4rem; }
.halfrow { display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }
@media (max-width:640px) { .halfrow { grid-template-columns:1fr; } }
blockquote { margin:0; padding-left:1rem; border-left:3px solid var(--accent); font-style:italic; }
blockquote footer,.statlbl { color:var(--muted); }
blockquote footer { margin-top:.4rem; font-style:normal; }
.statval { color:var(--accent); font-size:1.7rem; font-weight:700; margin:0; }
.statlbl { margin:.2rem 0 0; }
.chart { display:flex; flex-direction:column; gap:.5rem; }
.bar { display:grid; grid-template-columns:minmax(80px,160px) 1fr 48px; align-items:center; gap:.6rem; }
.blbl { font-size:.88rem; text-align:right; color:var(--muted); }
.btrack { background:var(--track); border-radius:6px; height:16px; overflow:hidden; }
.bfill { display:block; height:100%; background:var(--accent); }
.bnum { font-size:.85rem; font-variant-numeric:tabular-nums; }
img,svg { max-width:100%; height:auto; }"""


def _infographic_html(data: dict) -> str:
    assets = _assets(data)
    def section_html(section):
        kind = section["type"]
        title = "<h3>" + escape(section["title"]) + "</h3>"
        value, label = escape(_text(section["value"])), escape(_text(section["label"]))
        if kind == "quote":
            body = "<blockquote>" + value.replace("\n", "<br>") + ("<footer>" + label + "</footer>" if label else "") + "</blockquote>"
        elif kind == "stat":
            body = '<p class="statval">' + value + '</p><p class="statlbl">' + label + "</p>"
        elif kind == "svg":
            reference = _asset_reference(section["value"])
            body = assets.get(reference) or ('<img src="' + escape(reference, quote=True) + '" alt="' + label + '">')
            if label:
                body += '<p class="statlbl">' + label + "</p>"
        elif kind == "chart":
            bars = _chart_items(section["items"])
            maximum = max((abs(number) for _, number, _ in bars), default=1) or 1
            body = '<div class="chart">' + "".join(
                '<div class="bar"><span class="blbl">' + escape(name) + '</span><div class="btrack">'
                '<span class="bfill" style="width:' + f"{abs(number) / maximum * 100:.6g}" + '%"></span></div>'
                '<span class="bnum">' + escape(raw) + '</span></div>' for name, number, raw in bars) + "</div>"
            if label:
                body += '<p class="statlbl">' + label + '</p>'
        else:
            body = "<ol>" + "".join("<li>" + escape(item) + "</li>" for item in section["items"]) + "</ol>"
        return '<section class="sec ' + escape(kind, quote=True) + '">' + title + body + "</section>"
    parts = ["<h1>" + escape(data["title"]) + "</h1>", '<p class="subtitle">' + escape(data["subtitle"]) + "</p>"]
    number = 0
    for panel, sections in _panels(data):
        parts.append('<div class="panel">')
        if panel is not None:
            number += 1
            parts.append(f'<div class="pnum">{number}</div><h2>' + escape(panel) + "</h2>")
        index = 0
        while index < len(sections):
            if sections[index]["span"] == "half" and index + 1 < len(sections) and sections[index + 1]["span"] == "half":
                parts.append('<div class="halfrow">' + section_html(sections[index]) + section_html(sections[index + 1]) + "</div>")
                index += 2
            else:
                parts.append(section_html(sections[index]))
                index += 1
        parts.append("</div>")
    return _html(data["title"], _INFOGRAPHIC_CSS, "\n".join(parts))


def _infographic_svg(data: dict) -> str:
    assets = _assets(data)
    content = []
    y = 64
    def text(value, size=13, color="#333", weight=None, x=60, width=88):
        nonlocal y
        for paragraph in _text(value).splitlines() or [""]:
            for line in textwrap.wrap(paragraph, width=width, break_long_words=True, break_on_hyphens=False) or [""]:
                attributes = f'x="{x}" y="{y}" font-size="{size}" fill="{color}"'
                if weight:
                    attributes += f' font-weight="{weight}"'
                content.append("<text " + attributes + ">" + escape(line) + "</text>")
                y += size + 6
    text(data["title"], 34, "#1a1a1a", "700", width=43)
    text(data["subtitle"], 18, "#5a5a5a", width=76)
    y += 24
    number = 0
    for panel, sections in _panels(data):
        if panel is not None:
            number += 1
            content.append(f'<circle cx="70" cy="{y - 6}" r="13" fill="#7a4b9e"/>')
            content.append(f'<text x="70" y="{y - 1}" font-size="13" fill="#fff" text-anchor="middle" font-weight="700">{number}</text>')
            text(panel, 19, "#7a4b9e", "700", x=94, width=70)
            y += 8
        for section in sections:
            text(section["title"], 15, "#1a1a1a", "600", width=85)
            kind = section["type"]
            if kind == "svg":
                reference = _asset_reference(section["value"])
                svg = assets.get(reference)
                if svg:
                    root = ET.fromstring(svg)
                    viewbox = root.attrib.get("viewBox", "").split()
                    try:
                        width, height = float(viewbox[2]), float(viewbox[3])
                        aspect = min(max(height / width, .15), 2)
                    except (ValueError, IndexError, ZeroDivisionError):
                        aspect = .6
                    height = int(780 * aspect)
                    source = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
                else:
                    height, source = 400, reference
                content.append(f'<image x="60" y="{y}" width="780" height="{height}" href="' + escape(source, quote=True) + '"/>')
                y += height + 18
            elif kind == "chart":
                bars = _chart_items(section["items"])
                maximum = max((abs(number) for _, number, _ in bars), default=1) or 1
                for name, number_value, raw in bars:
                    text(name + ": " + raw, 13, width=88)
                    content.append(f'<rect x="60" y="{y - 8}" width="780" height="12" rx="3" fill="#ececec"/>')
                    content.append(f'<rect x="60" y="{y - 8}" width="{abs(number_value) / maximum * 780:.6g}" height="12" rx="3" fill="#7a4b9e"/>')
                    y += 22
            elif kind in {"flow", "comparison"}:
                for index, item in enumerate(section["items"], 1):
                    text(f"{index}. {item}", x=76, width=85)
            else:
                text(section["value"], 22 if kind == "stat" else 13, weight="700" if kind == "stat" else None)
            if section["label"]:
                text("— " + section["label"], 12, "#777", width=92)
            y += 24
    height = y + 40
    return (f'<svg xmlns="{_SVG_NS}" width="900" height="{height}" viewBox="0 0 900 {height}" '
            'font-family="Segoe UI, system-ui, sans-serif" role="img" aria-label="' + escape(data["title"], quote=True) + '">\n'
            '<title>' + escape(data["title"]) + '</title>\n'
            f'<rect x="0" y="0" width="900" height="{height}" fill="#f7f7f5"/>\n' + "\n".join(content) + "\n</svg>\n")


def _infographic_wireframe(data: dict) -> str:
    width = 78
    def box(values, rule="-"):
        border = "+" + rule * (width - 2) + "+"
        rows = [border]
        for value in values:
            rows.extend("| " + line.ljust(width - 4) + " |"
                        for line in textwrap.wrap(_line(value), width - 4, break_on_hyphens=False) or [""])
        return [*rows, border]
    def summary(section):
        value = _text(section["value"]) if section["value"] is not None else "; ".join(section["items"])
        suffix = " — " + section["label"] if section["label"] else ""
        return f"[{section['type']}] {section['title']} :: {value}{suffix}"
    lines = box([data["title"], data["subtitle"]], "=")
    for panel, sections in _panels(data):
        if panel is not None:
            lines.extend(box(["PANEL: " + panel]))
        index = 0
        while index < len(sections):
            first = sections[index]
            if first["span"] == "half" and index + 1 < len(sections) and sections[index + 1]["span"] == "half":
                second = sections[index + 1]
                left = textwrap.wrap(summary(first), 35, break_on_hyphens=False) + ["span=half"]
                right = textwrap.wrap(summary(second), 35, break_on_hyphens=False) + ["span=half"]
                lines.append("+" + "-" * 37 + "+" + "-" * 38 + "+")
                for row in range(max(len(left), len(right))):
                    lines.append("| " + (left[row] if row < len(left) else "").ljust(35) + " | " +
                                 (right[row] if row < len(right) else "").ljust(35) + "  |")
                lines.append("+" + "-" * 37 + "+" + "-" * 38 + "+")
                index += 2
            else:
                lines.extend(box([summary(first), "span=" + first["span"]]))
                index += 1
    return _finish(lines)


def render_artifact(data: dict, action: str, extension: str) -> str:
    """Render a validated schema without generation or added semantic content."""
    action = canonical_action(action)
    extension = "." + extension.lower().lstrip(".")
    if extension not in _FORMATS.get(action, ()):
        raise InvalidArgumentsError(f"Unsupported NotebookLM format '{extension}' for {action}.")
    if extension == ".json":
        clean = {key: data[key] for key in _JSON_KEYS[action] if key in data}
        return json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
    if action == "create_flashcards":
        return _cards(data)
    if action == "create_datatables":
        return _table(data, extension)
    if action == "create_mindmaps":
        return _mindmap(data, extension)
    if action == "create_podcasts":
        return _podcast(data)
    if action == "create_slides":
        return _slides(data)
    if action == "create_reports":
        return _report_html(data) if extension == ".html" else _report_md(data)
    if action == "create_infographics":
        return {".md": _infographic_md, ".html": _infographic_html, ".svg": _infographic_svg,
                ".wireframe.txt": _infographic_wireframe}[extension](data)
    raise InvalidArgumentsError(f"Unsupported NotebookLM output action '{action}'.")
