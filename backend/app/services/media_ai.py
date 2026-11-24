"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""Media understanding for the widget AI turn.

A visitor talking to the AI may attach files. This module turns each
uploaded file into something the agent can use:

- Images: passed through to the agent as agno ``Image`` objects (agno
  1.7.6 supports multimodal input — ``Agent.arun(images=[...])`` — and its
  OpenAI model formats them as ``image_url`` content parts). No separate
  description call is made; the model sees the image itself.
- PDF / plain text / CSV: text is extracted (pypdf for PDFs, plain decode
  for text) and prepended to the user message as labelled context.
- DOCX / XLSX: text is extracted with a small stdlib-only (zipfile + XML)
  parser — no new dependencies. Legacy OLE .doc/.xls cannot be parsed
  that way and are reported as unsupported.
- Audio (voice notes): transcribed with OpenAI Whisper (``whisper-1``)
  using the agent's own configured OpenAI key. Other providers have no
  Whisper-compatible key available here, so the context says so instead
  of failing silently.

Every file the AI cannot use still produces a context line that says so,
so the agent tells the visitor rather than silently ignoring the file.

No credential is ever written to a file or a log: the key arrives as an
argument from the caller (the agent's own configured key) and is only
passed to the OpenAI client in memory.
"""

import asyncio
import base64
import io
import os
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agno.media import Image as AgnoImage

from app.core.logger import get_logger
from app.services.file_upload_service import ALLOWED_AUDIO_TYPES, ALLOWED_IMAGE_TYPES

logger = get_logger(__name__)

# Text extracted from a document is capped so one attachment cannot eat
# the turn's context window.
MAX_MEDIA_TEXT_CHARS = 12000

# Whisper model used for voice-note transcription.
WHISPER_MODEL = "whisper-1"

# Role rule for every attachment: the agent answers only as the agent its
# instructions define. Media is evidence for the agent's own job, never a
# free-standing thing to describe — an unrelated image, document or voice
# note must not be described or summarised, just declined briefly in the
# agent's own voice with a pointer to what it can help with.
_MEDIA_ROLE_RULE = (
    "Answer only as the agent defined by your instructions. Use each "
    "attachment only insofar as it relates to what you do as that agent. "
    "If an attachment has nothing to do with your job, do NOT describe or "
    "summarise it — briefly say so in your own voice and point the visitor "
    "to what you can help with."
)
_IMAGE_FORMAT_ALIASES = {"jpg": "jpeg"}

# Legacy OLE office files (.doc/.xls) cannot be read without a dedicated
# parser; the stdlib zipfile parsers below only handle the OOXML formats.
_UNSUPPORTED_OFFICE_TYPES = {
    "application/msword",
    "application/vnd.ms-excel",
}


@dataclass
class MediaTurn:
    """What the AI turn needs for this message's attachments."""

    # Labelled text blocks, prepended to the user message sent to the agent.
    context_text: str = ""
    # Images for agno Agent.arun(images=[...]); the model sees them itself.
    images: List[AgnoImage] = field(default_factory=list)


def _read_bytes(entry: Dict[str, Any]) -> Optional[bytes]:
    """The raw bytes of an uploaded file, from whatever the entry carries."""
    if entry.get("content_bytes"):
        return entry["content_bytes"]
    content = entry.get("content")
    if content:
        try:
            return base64.b64decode(content)
        except Exception:
            logger.warning(f"Could not base64-decode attachment {entry.get('filename')}")
            return None
    # Stored file: local uploads/ path, else fetch the signed URL (S3).
    file_url = entry.get("file_url") or ""
    if file_url.startswith("/uploads/"):
        rel = file_url.lstrip("/")
        candidates = [
            os.path.join(os.getcwd(), rel),
            os.path.join(os.path.dirname(__file__), "..", "..", rel),
        ]
        for path in candidates:
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
        logger.warning(f"Stored attachment not found on disk: {file_url}")
        return None
    signed_url = entry.get("signed_url")
    if signed_url:
        try:
            import httpx

            resp = httpx.get(signed_url, timeout=15)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.warning(f"Could not fetch stored attachment {file_url}: {e}")
    return None


def _truncate(text: str, limit: int = MAX_MEDIA_TEXT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n[... truncated, {len(text) - limit} more characters not shown]"
    return text


def _extract_pdf(data: bytes) -> tuple[Optional[str], int]:
    """(text, page_count) from a PDF, or (None, pages) when unreadable."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.error("pypdf not installed; cannot read PDFs")
        return None, 0
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(p.strip() for p in parts if p.strip())
        return (text or None), pages
    except Exception as e:
        logger.warning(f"PDF extraction failed: {e}")
        return None, 0


def _extract_docx(data: bytes) -> Optional[str]:
    """Paragraph text from a .docx (OOXML zip) using only the stdlib."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except Exception as e:
        logger.warning(f"DOCX read failed: {e}")
        return None
    try:
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ET.fromstring(xml)
        lines = []
        for para in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
            text = "".join(t.text or "" for t in para.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
            if text.strip():
                lines.append(text)
        return "\n".join(lines) or None
    except Exception as e:
        logger.warning(f"DOCX parse failed: {e}")
        return None


def _extract_xlsx(data: bytes) -> Optional[str]:
    """Cell values from the first sheet of an .xlsx (OOXML zip), stdlib only."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall("m:si", ns):
                    shared.append("".join(t.text or "" for t in si.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
            sheet_name = next((n for n in names if n.startswith("xl/worksheets/sheet")),
                              None)
            if sheet_name is None:
                return None
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            root = ET.fromstring(zf.read(sheet_name))
            rows = []
            for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
                cells = []
                for cell in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
                    cell_type = cell.get("t")
                    value_el = cell.find("m:v", ns)
                    inline_el = cell.find("m:is/m:t", ns)
                    if cell_type == "s" and value_el is not None and value_el.text is not None:
                        try:
                            cells.append(shared[int(value_el.text)])
                        except (ValueError, IndexError):
                            pass
                    elif inline_el is not None:
                        cells.append(inline_el.text or "")
                    elif value_el is not None:
                        cells.append(value_el.text or "")
                if cells:
                    rows.append("\t".join(cells))
        return "\n".join(rows) or None
    except Exception as e:
        logger.warning(f"XLSX parse failed: {e}")
        return None


def _transcribe_audio(data: bytes, filename: str, content_type: str, api_key: str) -> Optional[str]:
    """Transcribe voice-note bytes with OpenAI Whisper (blocking call).

    Callers must run this in a worker thread (``asyncio.to_thread``).
    Returns None when transcription fails; the caller turns that into a
    context note rather than failing the turn.
    """
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        result = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=(filename, data, content_type),
        )
        return (result.text or "").strip() or None
    except Exception as e:
        logger.warning(f"Whisper transcription failed for {filename}: {e}")
        return None


async def prepare_media_context(
    files: List[Dict[str, Any]],
    *,
    api_key: Optional[str],
    model_type: str,
    model_name: Optional[str] = None,
) -> MediaTurn:
    """Build the media context for one AI turn from uploaded files.

    ``files`` are the upload results (filename/content_type/size plus the
    raw bytes under ``content_bytes`` or base64 ``content``). ``api_key`` /
    ``model_type`` are the agent's own configured credentials, used only
    for Whisper transcription when the agent runs on OpenAI.
    """
    blocks: List[str] = []
    images: List[AgnoImage] = []
    provider = (model_type or "").upper()

    for entry in files or []:
        filename = entry.get("filename") or "file"
        content_type = (entry.get("content_type") or "").lower()
        data = _read_bytes(entry)
        if data is None:
            blocks.append(
                f"[Attachment '{filename}': the file could not be read, so its "
                f"contents are not available. Tell the visitor you could not open it "
                f"and ask them to send it again.]"
            )
            continue

        if content_type in ALLOWED_IMAGE_TYPES:
            ext = os.path.splitext(filename)[1].lower().lstrip(".") or "jpeg"
            images.append(AgnoImage(
                content=data,
                format=_IMAGE_FORMAT_ALIASES.get(ext, ext),
            ))
            blocks.append(
                f"[Image '{filename}' attached below. Look at it only to see "
                f"whether it relates to what you do as this agent — if it does "
                f"not, do not describe it: say so briefly and point the visitor "
                f"to what you can help with.]"
            )
        elif content_type == "application/pdf":
            text, pages = _extract_pdf(data)
            if text:
                blocks.append(
                    f"[PDF '{filename}' ({pages} page{'s' if pages != 1 else ''}) content "
                    f"— use only what relates to your job as this agent:\n"
                    f"{_truncate(text)}]"
                )
            else:
                blocks.append(
                    f"[PDF '{filename}' ({pages} page{'s' if pages != 1 else ''}): no readable "
                    f"text could be extracted — it may be a scanned document. Tell the "
                    f"visitor you cannot read scanned PDFs and ask them to describe it "
                    f"or send the text.]"
                )
        elif content_type in {"text/plain", "text/csv"}:
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            if text.strip():
                blocks.append(
                    f"[Text file '{filename}' content — use only what relates to "
                    f"your job as this agent:\n{_truncate(text)}]"
                )
            else:
                blocks.append(
                    f"[Text file '{filename}': the file is empty or unreadable. "
                    f"Tell the visitor so.]"
                )
        elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            text = _extract_docx(data)
            if text:
                blocks.append(
                    f"[Document '{filename}' content — use only what relates to "
                    f"your job as this agent:\n{_truncate(text)}]"
                )
            else:
                blocks.append(
                    f"[Document '{filename}': no text could be extracted. Tell the "
                    f"visitor you could not read it and ask them to describe it.]"
                )
        elif content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            text = _extract_xlsx(data)
            if text:
                blocks.append(
                    f"[Spreadsheet '{filename}' content — use only what relates "
                    f"to your job as this agent:\n{_truncate(text)}]"
                )
            else:
                blocks.append(
                    f"[Spreadsheet '{filename}': no cell values could be extracted. "
                    f"Tell the visitor you could not read it and ask them to describe it.]"
                )
        elif content_type in ALLOWED_AUDIO_TYPES:
            if provider == "OPENAI" and api_key:
                transcript = await asyncio.to_thread(
                    _transcribe_audio, data, filename, content_type, api_key
                )
                if transcript:
                    blocks.append(
                        f"[Voice note transcript — the visitor's spoken words; "
                        f"answer them only as this agent: {transcript}]"
                    )
                else:
                    blocks.append(
                        f"[Voice note '{filename}': transcription failed. Tell the "
                        f"visitor you could not hear the voice note and ask them to "
                        f"type their message or send it again.]"
                    )
            elif not api_key:
                blocks.append(
                    f"[Voice note '{filename}': this agent has no API key configured, "
                    f"so the voice note cannot be transcribed. Tell the visitor you "
                    f"cannot listen to voice notes right now and ask them to type "
                    f"their message.]"
                )
            else:
                blocks.append(
                    f"[Voice note '{filename}': voice notes can only be transcribed "
                    f"when this agent runs on OpenAI, and this agent uses {provider}. "
                    f"Tell the visitor you cannot listen to voice notes and ask them "
                    f"to type their message.]"
                )
        elif content_type in _UNSUPPORTED_OFFICE_TYPES:
            blocks.append(
                f"[Attachment '{filename}': this file type ({content_type}) cannot be "
                f"read. Tell the visitor you cannot open it and ask them to send it "
                f"as PDF or plain text instead.]"
            )
        else:
            blocks.append(
                f"[Attachment '{filename}' ({content_type}): this file type cannot be "
                f"read. Tell the visitor you cannot open it.]"
            )

    context = ""
    if blocks:
        context = (
            "[Attachments the visitor sent with this message:\n"
            + _MEDIA_ROLE_RULE + "\n\n"
            + "\n\n".join(blocks)
            + "\n]"
        )
    return MediaTurn(context_text=context, images=images)
