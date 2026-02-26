import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List


def _itertext_clean(el: ET.Element) -> str:
    """Recursively extract all text from an element, collapsing whitespace."""
    raw = "".join(el.itertext())
    return re.sub(r"\s+", " ", raw).strip()


def _render_inline(el: ET.Element) -> str:
    """
    Render an element's mixed content (text + children) into a single
    string, preserving inline semantics like <italic>, <bold>, <sup>, <sub>,
    <ext-link>, and <xref> while stripping other tags.
    """
    parts: List[str] = []

    if el.text:
        parts.append(el.text)

    for child in el:
        tag = child.tag

        if tag == "italic":
            inner = _render_inline(child)
            parts.append(f"*{inner}*")
        elif tag == "bold":
            inner = _render_inline(child)
            parts.append(f"**{inner}**")
        elif tag == "sup":
            inner = _render_inline(child)
            parts.append(f"^{inner}")
        elif tag == "sub":
            inner = _render_inline(child)
            parts.append(f"_{inner}")
        elif tag == "ext-link":
            url = child.get("{http://www.w3.org/1999/xlink}href", "")
            inner = _render_inline(child)
            parts.append(f"{inner} ({url})" if url else inner)
        elif tag == "xref":
            # Keep reference text (e.g. "Fig. 1", "ref 12") but skip the tag
            inner = _render_inline(child)
            parts.append(inner)
        elif tag == "table-wrap":
            parts.append("\n\n" + _render_table(child) + "\n")
        elif tag == "fig":
            parts.append("\n\n" + _render_figure_caption(child) + "\n")
        elif tag in ("supplementary-material", "media"):
            pass  # skip
        else:
            # Generic fallback: recurse
            parts.append(_render_inline(child))

        if child.tail:
            parts.append(child.tail)

    return "".join(parts)


# ── metadata ─────────────────────────────────────────────────────────


def _extract_metadata(root: ET.Element) -> str:
    """Extract article metadata (title, authors, journal, DOI, dates)."""
    lines: List[str] = []

    # Title
    title_el = root.find(".//article-title")
    if title_el is not None:
        lines.append(f"# {_itertext_clean(title_el)}")
        lines.append("")

    # Authors
    authors: List[str] = []
    for contrib in root.iter("contrib"):
        if contrib.get("contrib-type") != "author":
            continue
        name_el = contrib.find("n") or contrib.find("name")
        if name_el is not None:
            surname = name_el.findtext("surname", "")
            given = name_el.findtext("given-names", "")
            authors.append(f"{given} {surname}".strip())
    if authors:
        lines.append(f"**Authors:** {', '.join(authors)}")
        lines.append("")

    # Journal, volume, pages, year
    journal = root.findtext(".//journal-title", "")
    volume = root.findtext(".//volume", "")
    fpage = root.findtext(".//fpage", "")
    lpage = root.findtext(".//lpage", "")
    year = ""
    for pub_date in root.iter("pub-date"):
        y = pub_date.findtext("year", "")
        if y:
            year = y
            break
    parts: List[str] = []
    if journal:
        parts.append(journal)
    if volume:
        parts.append(f"vol. {volume}")
    if fpage and lpage:
        parts.append(f"pp. {fpage}–{lpage}")
    if year:
        parts.append(f"({year})")
    if parts:
        lines.append(f"**Published in:** {', '.join(parts)}")

    # DOI
    for aid in root.iter("article-id"):
        if aid.get("pub-id-type") == "doi" and aid.text:
            lines.append(f"**DOI:** {aid.text.strip()}")
            break

    if lines:
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── abstract ─────────────────────────────────────────────────────────


def _extract_abstracts(root: ET.Element) -> str:
    """Extract primary abstract(s)."""
    blocks: List[str] = []
    for abstract in root.iter("abstract"):
        # Skip web-summary / graphical abstracts
        atype = abstract.get("abstract-type", "")
        if atype in ("web-summary", "graphical"):
            continue
        blocks.append("## Abstract\n")
        for p in abstract.iter("p"):
            blocks.append(_render_inline(p))
            blocks.append("")
    return "\n".join(blocks)


# ── body ─────────────────────────────────────────────────────────────


def _render_table(tw: ET.Element) -> str:
    """Render a <table-wrap> into markdown table format."""
    lines: List[str] = []

    label_el = tw.find("label")
    caption_el = tw.find("caption")
    if label_el is not None:
        cap_text = ""
        if caption_el is not None:
            cap_text = _itertext_clean(caption_el)
        lines.append(f"**{label_el.text.strip()}** {cap_text}")
        lines.append("")

    table_el = tw.find(".//table")
    if table_el is None:
        return "\n".join(lines)

    rows = table_el.findall(".//tr")
    for i, tr in enumerate(rows):
        cells = tr.findall("th") or tr.findall("td")
        if not cells:
            cells = list(tr)  # fallback
        cell_texts = [_itertext_clean(c) for c in cells]
        lines.append("| " + " | ".join(cell_texts) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * len(cell_texts)) + " |")

    lines.append("")
    return "\n".join(lines)


def _render_figure_caption(fig: ET.Element) -> str:
    """Render a figure's label + caption as a text block."""
    label_el = fig.find("label")
    caption_el = fig.find("caption")
    parts: List[str] = []
    if label_el is not None:
        parts.append(f"**{label_el.text.strip()}:**")
    if caption_el is not None:
        parts.append(_itertext_clean(caption_el))
    if parts:
        return "[" + " ".join(parts) + "]\n"
    return ""


def _render_section(sec: ET.Element, depth: int = 2) -> str:
    """
    Recursively render a <sec> element.
    depth=2 → ##, depth=3 → ###, etc.
    """
    lines: List[str] = []
    hdr = "#" * min(depth, 4)

    title_el = sec.find("title")
    if title_el is not None:
        title_text = _itertext_clean(title_el)
        if title_text:
            lines.append(f"{hdr} {title_text}")
            lines.append("")

    for child in sec:
        tag = child.tag

        if tag == "title":
            continue  # already handled
        elif tag == "p":
            lines.append(_render_inline(child))
            lines.append("")
        elif tag == "sec":
            lines.append(_render_section(child, depth=depth + 1))
        elif tag == "table-wrap":
            lines.append(_render_table(child))
        elif tag == "fig":
            lines.append(_render_figure_caption(child))
        elif tag == "supplementary-material":
            pass  # skip
        else:
            # Generic: just dump text
            txt = _itertext_clean(child)
            if txt:
                lines.append(txt)
                lines.append("")

    return "\n".join(lines)


def _extract_body(root: ET.Element) -> str:
    """Extract <body> sections as markdown."""
    body = root.find(".//body")
    if body is None:
        return ""
    parts: List[str] = []
    for child in body:
        if child.tag == "sec":
            parts.append(_render_section(child, depth=2))
        elif child.tag == "p":
            parts.append(_render_inline(child))
            parts.append("")
    return "\n".join(parts)


# ── public API ───────────────────────────────────────────────────────


def extract_text_from_xml(xml_path: str) -> str:
    """
    Parse a JATS/NLM XML file and return a markdown-formatted string
    suitable for the evidence extraction pipeline.

    Drop-in replacement for ``get_pdf_text()`` — same signature, same
    return type.

    Parameters
    ----------
    xml_path : str
        Path to a .nxml or .xml file in JATS format.

    Returns
    -------
    str
        Full article text in markdown-like format.
    """
    path = Path(xml_path)
    if not path.exists():
        raise FileNotFoundError(f"XML file not found: {xml_path}")

    tree = ET.parse(str(path))
    root = tree.getroot()

    sections: List[str] = [
        _extract_metadata(root),
        _extract_abstracts(root),
        _extract_body(root),
    ]

    text = "\n".join(s for s in sections if s)

    # Light cleanup: collapse 3+ newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text