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

"""Dashboard-admin API for conversation tags.

Mounted at ``/api/v1/admin`` (see backend/app/main.py). Everything here
needs a signed-in dashboard user (``get_current_user``) from the
conversation's own organisation — a conversation from another org is a 404,
never a leak.

Endpoints:
- GET /tags — every defined tag (slug, label, group, weight, color), for
  the inbox's filter chips and tag menus.
- GET /conversations/{id}/tags — one conversation's tags + priority score.
- POST /conversations/{id}/tags {"tag": "<slug>"} — add one tag. The body
  also accepts {"slug": ...}, which is what the inbox client sends.
- DELETE /conversations/{id}/tags/{tag} — remove one tag. The inbox client
  instead DELETEs /conversations/{id}/tags with {"slug": ...} in the body;
  that route is accepted too, same result.
"""

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.session_to_agent import SessionToAgent
from app.services import conversation_tags

router = APIRouter()
logger = get_logger(__name__)


class TagDefinition(BaseModel):
    slug: str
    label: str
    group: str
    weight: int
    color: str


class ConversationTagsResponse(BaseModel):
    conversation_id: Optional[str] = None
    tags: List[str] = []
    priority_score: int = 0


class TagBody(BaseModel):
    """One tag to add or remove. The spec says {"tag": ...}; the inbox
    client sends {"slug": ...}. Both are accepted."""

    tag: Optional[str] = None
    slug: Optional[str] = None

    def resolved_slug(self) -> Optional[str]:
        value = self.tag or self.slug
        return value.strip() if isinstance(value, str) and value.strip() else None


def _load_session(db: Session, conversation_id: str, organization_id) -> SessionToAgent:
    """The conversation, only when it belongs to the admin's organisation."""
    try:
        session_id = UUID(str(conversation_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Conversation not found")
    session = (
        db.query(SessionToAgent)
        .filter(
            SessionToAgent.session_id == session_id,
            SessionToAgent.organization_id == organization_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return session


def _checked_slug(value: Optional[str]) -> str:
    """A tag slug from the request, or 422 when it is missing or unknown."""
    if not value or not conversation_tags.is_valid_tag(value):
        raise HTTPException(status_code=422, detail=f"Unknown tag: {value}")
    return value


def _response(session: SessionToAgent) -> ConversationTagsResponse:
    tags = conversation_tags.read_tags(session)
    return ConversationTagsResponse(
        conversation_id=str(session.session_id),
        tags=tags,
        priority_score=conversation_tags.priority_score(tags),
    )


def _save_tags(db: Session, session: SessionToAgent, tags: List[str]) -> ConversationTagsResponse:
    if not conversation_tags.tags_column_available():
        raise HTTPException(
            status_code=503,
            detail="Conversation tags are not available yet",
        )
    session.tags = tags
    db.commit()
    db.refresh(session)
    return _response(session)


@router.get("/tags", response_model=List[TagDefinition])
async def list_tags():
    """Every defined tag, heaviest first, for the inbox's filters and menus."""
    return conversation_tags.list_tags()


@router.get(
    "/conversations/{conversation_id}/tags",
    response_model=ConversationTagsResponse,
)
async def get_conversation_tags(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    session = _load_session(db, conversation_id, current_user.organization_id)
    return _response(session)


@router.post(
    "/conversations/{conversation_id}/tags",
    response_model=ConversationTagsResponse,
)
async def add_conversation_tag(
    conversation_id: str,
    body: TagBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add one tag. Adding a tag the conversation already has is a no-op."""
    slug = _checked_slug(body.resolved_slug())
    session = _load_session(db, conversation_id, current_user.organization_id)
    tags = conversation_tags.read_tags(session)
    if slug not in tags:
        tags = tags + [slug]
    return _save_tags(db, session, tags)


def _remove_tag(
    db: Session, conversation_id: str, slug: str, organization_id
) -> ConversationTagsResponse:
    """Shared by both DELETE shapes: removing a tag the conversation does
    not have is a no-op."""
    slug = _checked_slug(slug)
    session = _load_session(db, conversation_id, organization_id)
    tags = [t for t in conversation_tags.read_tags(session) if t != slug]
    return _save_tags(db, session, tags)


@router.delete(
    "/conversations/{conversation_id}/tags/{tag}",
    response_model=ConversationTagsResponse,
)
async def remove_conversation_tag(
    conversation_id: str,
    tag: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return _remove_tag(db, conversation_id, tag, current_user.organization_id)


@router.delete(
    "/conversations/{conversation_id}/tags",
    response_model=ConversationTagsResponse,
)
async def remove_conversation_tag_by_body(
    conversation_id: str,
    body: TagBody,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """The inbox client DELETEs with the slug in the body, not the path."""
    return _remove_tag(
        db, conversation_id, body.resolved_slug(), current_user.organization_id
    )
