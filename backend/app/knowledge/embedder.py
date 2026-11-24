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

Hosted embeddings — no local model is downloaded or run anywhere.

Embeddings come from the hosted provider API, using the dashboard-configured
provider key of the organization (the same key as its chat model). The
embedder is a plain API client, so building one is cheap and there is nothing
to cache: every call builds a fresh client from the org's own key, which also
means one org's key can never leak into another org's embeddings.

Dimensions are fixed at 384 to match the stored vectors (``tickets.embedding``
and the per-org knowledge tables are ``vector(384)`` columns, created when
the local bge-small model was the embedder). OpenAI's
``text-embedding-3-small`` and Gemini's ``gemini-embedding-001`` both support
truncating to 384 dimensions, so no column migration is needed.

One caveat follows from the dimension match: 384-dim vectors from the new
hosted models live in a different space than the 384-dim vectors the old
local model wrote, so cosine similarity between a new query vector and a
stored old-model vector is meaningless until that row is re-embedded. New
rows are fine; stored rows need a re-embed backfill for exact results.
"""

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from agno.embedder import Embedder
from agno.embedder.google import GeminiEmbedder
from agno.embedder.openai import OpenAIEmbedder

from app.core.config import settings

# Fixed to match the vector(384) columns. See the module docstring.
EMBEDDING_DIMENSIONS = 384
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

# Chat providers whose key speaks the OpenAI embeddings API.
_OPENAI_EMBEDDING_TYPES = {"OPENAI", "OPENAI_COMPATIBLE", "CHATTERMATE"}


class EmbeddingUnavailableError(ValueError):
    """No hosted embedding can be built for this organization.

    Raised instead of falling back to any local model: embeddings either come
    from the org's configured hosted provider or they do not happen, and the
    message tells the owner what to configure.
    """


@dataclass
class _EmbeddingCredentials:
    model_type: str
    api_key: str
    base_url: Optional[str] = None


def _resolve_credentials(org_id: UUID) -> Optional[_EmbeddingCredentials]:
    """The org's active AI config, reduced to what embeddings need.

    Reads the fields inside the session: the returned row is detached once
    the session closes, so nothing ORM-bound may escape this function.
    """
    from app.core.security import decrypt_api_key
    from app.database import SessionLocal
    from app.repositories.ai_config import AIConfigRepository

    with SessionLocal() as db:
        config = AIConfigRepository(db).get_active_config(str(org_id))
        if config is None:
            return None
        model_type = getattr(config.model_type, "value", config.model_type)
        return _EmbeddingCredentials(
            model_type=str(model_type).upper(),
            api_key=decrypt_api_key(config.encrypted_api_key),
            base_url=(config.settings or {}).get("base_url") or None,
        )


def _hosted_embedder(creds: _EmbeddingCredentials) -> Embedder:
    """Build the hosted embedder for one org's configured provider."""
    if creds.model_type in _OPENAI_EMBEDDING_TYPES:
        if not creds.api_key:
            raise EmbeddingUnavailableError(
                "The organization's AI configuration has no API key; add one "
                "in Settings -> AI to enable knowledge search and ticket similarity."
            )
        if creds.model_type == "OPENAI_COMPATIBLE" and not creds.base_url:
            raise EmbeddingUnavailableError(
                "The organization's OpenAI-compatible provider needs a base URL; "
                "add one in Settings -> AI to enable knowledge search and ticket similarity."
            )
        return OpenAIEmbedder(
            id=settings.EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIMENSIONS,
            api_key=creds.api_key,
            base_url=creds.base_url,
        )
    if creds.model_type == "GOOGLE":
        if not creds.api_key:
            raise EmbeddingUnavailableError(
                "The organization's AI configuration has no API key; add one "
                "in Settings -> AI to enable knowledge search and ticket similarity."
            )
        return GeminiEmbedder(
            id=settings.GEMINI_EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIMENSIONS,
            api_key=creds.api_key,
        )
    raise EmbeddingUnavailableError(
        f"Embeddings are not available for the '{creds.model_type}' provider: "
        "it has no hosted embeddings API under its own key. Switch the "
        "organization's AI provider to OpenAI, Google, or an OpenAI-compatible "
        "gateway in Settings -> AI to enable knowledge search and ticket similarity."
    )


def get_embedder(org_id: Optional[UUID] = None) -> Embedder:
    """The hosted embedder for an organization.

    ``org_id`` is required: the embedder is built from that org's configured
    provider key, and building one without an org would mean guessing whose
    key to use. Callers that have no org cannot embed.
    """
    if org_id is None:
        raise EmbeddingUnavailableError(
            "get_embedder() needs an organization id: embeddings are built "
            "from that organization's configured provider key."
        )
    creds = _resolve_credentials(org_id)
    if creds is None:
        raise EmbeddingUnavailableError(
            f"No active AI configuration for organization {org_id}; configure "
            "a provider key in Settings -> AI to enable knowledge search and "
            "ticket similarity."
        )
    return _hosted_embedder(creds)
