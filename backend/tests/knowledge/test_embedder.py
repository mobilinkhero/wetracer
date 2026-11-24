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

Tests for the hosted embedder (app.knowledge.embedder): embeddings come from
the org's configured provider API — no local model is downloaded or run.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.knowledge.embedder import (
    EMBEDDING_DIMENSIONS,
    EmbeddingUnavailableError,
    _EmbeddingCredentials,
    _resolve_credentials,
    get_embedder,
)
from app.models.ai_config import AIModelType


def _creds(model_type, api_key="test-key", base_url=None):
    return _EmbeddingCredentials(model_type=model_type, api_key=api_key, base_url=base_url)


def test_openai_config_uses_openai_embedder_at_384_dims():
    """OPENAI orgs embed with text-embedding-3-small, truncated to 384 dims
    so vectors still fit the stored vector(384) columns."""
    org_id = uuid4()
    with patch("app.knowledge.embedder._resolve_credentials", return_value=_creds("OPENAI")), \
         patch("app.knowledge.embedder.OpenAIEmbedder") as mock_cls:
        embedder = get_embedder(org_id)
    mock_cls.assert_called_once_with(
        id="text-embedding-3-small",
        dimensions=384,
        api_key="test-key",
        base_url=None,
    )
    assert embedder is mock_cls.return_value
    assert EMBEDDING_DIMENSIONS == 384


def test_chattermate_managed_model_uses_openai_embedder():
    """The managed model is OpenAI under the hood, so it embeds the same way."""
    with patch("app.knowledge.embedder._resolve_credentials", return_value=_creds("CHATTERMATE")), \
         patch("app.knowledge.embedder.OpenAIEmbedder") as mock_cls:
        get_embedder(uuid4())
    mock_cls.assert_called_once_with(
        id="text-embedding-3-small",
        dimensions=384,
        api_key="test-key",
        base_url=None,
    )


def test_openai_compatible_passes_the_gateway_base_url():
    """A gateway endpoint must be used — the org's key is meaningless against
    OpenAI's default URL."""
    with patch("app.knowledge.embedder._resolve_credentials",
               return_value=_creds("OPENAI_COMPATIBLE", base_url="https://gw.example.com/v1")), \
         patch("app.knowledge.embedder.OpenAIEmbedder") as mock_cls:
        get_embedder(uuid4())
    mock_cls.assert_called_once_with(
        id="text-embedding-3-small",
        dimensions=384,
        api_key="test-key",
        base_url="https://gw.example.com/v1",
    )


def test_openai_compatible_without_base_url_is_an_error():
    """A compatible provider with no base URL cannot embed anywhere."""
    with patch("app.knowledge.embedder._resolve_credentials",
               return_value=_creds("OPENAI_COMPATIBLE", base_url=None)):
        with pytest.raises(EmbeddingUnavailableError, match="base URL"):
            get_embedder(uuid4())


def test_google_config_uses_gemini_embedder_at_384_dims():
    """GOOGLE orgs embed with gemini-embedding-001, also truncated to 384."""
    with patch("app.knowledge.embedder._resolve_credentials", return_value=_creds("GOOGLE")), \
         patch("app.knowledge.embedder.GeminiEmbedder") as mock_cls:
        get_embedder(uuid4())
    mock_cls.assert_called_once_with(
        id="gemini-embedding-001",
        dimensions=384,
        api_key="test-key",
    )


def test_provider_without_hosted_embeddings_is_an_error():
    """Anthropic has no embeddings API under its own key — fail loudly with
    what to do, instead of silently falling back to a local model."""
    with patch("app.knowledge.embedder._resolve_credentials", return_value=_creds("ANTHROPIC")):
        with pytest.raises(EmbeddingUnavailableError, match="ANTHROPIC"):
            get_embedder(uuid4())


def test_no_active_config_is_an_error():
    """An org that never configured a provider key cannot embed."""
    with patch("app.knowledge.embedder._resolve_credentials", return_value=None):
        with pytest.raises(EmbeddingUnavailableError, match="No active AI configuration"):
            get_embedder(uuid4())


def test_missing_org_id_is_an_error():
    """No org means no key to build the embedder from — never guess."""
    with pytest.raises(EmbeddingUnavailableError, match="organization id"):
        get_embedder(None)


def test_resolve_credentials_reads_inside_the_session():
    """The config row is detached once the session closes, so the key and
    base URL must be read while the session is still open."""
    org_id = uuid4()
    fake_config = SimpleNamespace(
        model_type=AIModelType.OPENAI_COMPATIBLE,
        encrypted_api_key="enc-key",
        settings={"base_url": "https://gw.example.com/v1"},
    )
    mock_db = MagicMock()
    with patch("app.database.SessionLocal") as mock_session_local, \
         patch("app.repositories.ai_config.AIConfigRepository") as mock_repo_cls, \
         patch("app.core.security.decrypt_api_key", return_value="plain-key") as mock_decrypt:
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_repo_cls.return_value.get_active_config.return_value = fake_config
        creds = _resolve_credentials(org_id)
    mock_repo_cls.assert_called_once_with(mock_db)
    mock_repo_cls.return_value.get_active_config.assert_called_once_with(str(org_id))
    mock_decrypt.assert_called_once_with("enc-key")
    assert creds == _EmbeddingCredentials(
        model_type="OPENAI_COMPATIBLE",
        api_key="plain-key",
        base_url="https://gw.example.com/v1",
    )


def test_resolve_credentials_returns_none_without_config():
    mock_db = MagicMock()
    with patch("app.database.SessionLocal") as mock_session_local, \
         patch("app.repositories.ai_config.AIConfigRepository") as mock_repo_cls:
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_repo_cls.return_value.get_active_config.return_value = None
        assert _resolve_credentials(uuid4()) is None
