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

"""Tests for app.services.media_ai — media understanding for the AI turn."""

import base64
import io
import struct
import zipfile
import zlib

import pytest

from app.services.media_ai import prepare_media_context


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _png_bytes() -> bytes:
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00\x00\xff\x00\x00"
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    return png


def _pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    pdf = b"%PDF-1.4\n"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return pdf


def _docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml",
                    '<?xml version="1.0"?><w:document '
                    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>Docx line one.</w:t></w:r></w:p>'
                    '</w:body></w:document>')
    return buf.getvalue()


def _entry(filename, content_type, data: bytes):
    return {"filename": filename, "content_type": content_type,
            "size": len(data), "content": _b64(data)}


@pytest.mark.asyncio
async def test_image_becomes_agno_image_with_context_note():
    result = await prepare_media_context(
        [_entry("photo.png", "image/png", _png_bytes())],
        api_key="unused", model_type="GROQ")
    assert len(result.images) == 1
    assert result.images[0].format == "png"
    assert "[Image 'photo.png'" in result.context_text


@pytest.mark.asyncio
async def test_image_instruction_keeps_agent_in_role():
    # An unrelated image must not be described: the instruction tells the
    # model to answer only as the agent and to decline off-role media.
    result = await prepare_media_context(
        [_entry("promo.png", "image/png", _png_bytes())],
        api_key="unused", model_type="GROQ")
    assert len(result.images) == 1
    assert "answer based on what it shows" not in result.context_text
    assert "Answer only as the agent defined by your instructions" in result.context_text
    assert "do NOT describe or summarise it" in result.context_text
    assert "do not describe it" in result.context_text


@pytest.mark.asyncio
async def test_document_blocks_carry_in_role_rule():
    result = await prepare_media_context(
        [_entry("doc.pdf", "application/pdf", _pdf_bytes("Refund policy is 30 days."))],
        api_key="unused", model_type="GROQ")
    assert "Refund policy is 30 days." in result.context_text
    assert "use only what relates to your job as this agent" in result.context_text
    assert "Answer only as the agent defined by your instructions" in result.context_text


@pytest.mark.asyncio
async def test_pdf_text_extracted_with_page_count():
    result = await prepare_media_context(
        [_entry("doc.pdf", "application/pdf", _pdf_bytes("Refund policy is 30 days."))],
        api_key="unused", model_type="GROQ")
    assert "Refund policy is 30 days." in result.context_text
    assert "(1 page)" in result.context_text


@pytest.mark.asyncio
async def test_text_and_docx_extracted():
    result = await prepare_media_context([
        _entry("notes.txt", "text/plain", b"Order number is 12345."),
        _entry("doc.docx",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               _docx_bytes()),
    ], api_key="unused", model_type="GROQ")
    assert "Order number is 12345." in result.context_text
    assert "Docx line one." in result.context_text


@pytest.mark.asyncio
async def test_audio_non_openai_provider_says_so():
    result = await prepare_media_context(
        [_entry("voice.webm", "audio/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 10)],
        api_key="some-key", model_type="GROQ")
    assert "Voice note" in result.context_text
    assert "GROQ" in result.context_text
    assert "type their message" in result.context_text


@pytest.mark.asyncio
async def test_audio_without_key_says_so():
    result = await prepare_media_context(
        [_entry("voice.webm", "audio/webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 10)],
        api_key=None, model_type="OPENAI")
    assert "no API key configured" in result.context_text


@pytest.mark.asyncio
async def test_legacy_office_file_reported_unsupported():
    result = await prepare_media_context(
        [_entry("old.xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0")],
        api_key="unused", model_type="OPENAI")
    assert "cannot be read" in result.context_text


@pytest.mark.asyncio
async def test_unreadable_file_reported_not_silently_ignored():
    result = await prepare_media_context(
        [{"filename": "ghost.png", "content_type": "image/png", "size": 0,
          "content": "!!!not-base64!!!"}],
        api_key="unused", model_type="OPENAI")
    assert "could not be read" in result.context_text
    assert result.images == []
