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

"""Dashboard-admin API for the public mobile Chat API (/api/v1/mobile).

Everything here needs a signed-in dashboard user (``get_current_user``) from
the widget's own organisation. NOTHING in this router is reachable with a
visitor JWT, and nothing in the public mobile router exposes an admin
operation — that split is Phase 1 of docs/API-SECURITY.md.

Endpoints:
- FCM credentials (POST/GET /fcm-config) — the org's Firebase service-account
  JSON, encrypted at rest, masked on read.
- Blocked visitors (GET/POST/DELETE /blocked-visitors) — the Phase 1
  blocked-visitor list the mobile API (and the widget socket) enforce.
- Per-widget security (GET/PUT /widgets/{widget_id}/security) — the kill
  switch and per-widget limit overrides.
- Abuse log (GET /abuse-events) — the Phase 1 abuse panel's backend.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.logger import get_logger
from app.core.security import decrypt_api_key, encrypt_api_key
from app.database import get_db
from app.models.mobile import (
    BlockedVisitor,
    MobileAbuseEvent,
    MobileFcmConfig,
    MobileReport,
    MobileWidgetSecurity,
)
from app.models.widget import Widget
from app.repositories.widget import WidgetRepository

router = APIRouter()
logger = get_logger(__name__)


def _org_widget(db: Session, widget_id: str, organization_id):
    """The widget, only when it belongs to the admin's organisation."""
    widget = db.query(Widget).filter(Widget.id == widget_id).first()
    if not widget or str(widget.organization_id) != str(organization_id):
        raise HTTPException(status_code=404, detail="Widget not found")
    return widget


# --------------------------------------------------------------------------
# FCM credentials
# --------------------------------------------------------------------------

class FcmConfigRequest(BaseModel):
    service_account_json: dict


@router.post("/fcm-config")
async def save_fcm_config(
    body: FcmConfigRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cfg = body.service_account_json
    if not isinstance(cfg, dict) or cfg.get("type") != "service_account" \
            or not cfg.get("project_id") or not cfg.get("private_key"):
        raise HTTPException(
            status_code=422,
            detail="service_account_json must be a Firebase service-account JSON object "
                   "(type 'service_account', with project_id and private_key)")
    row = db.query(MobileFcmConfig).filter(
        MobileFcmConfig.organization_id == current_user.organization_id).first()
    encrypted = encrypt_api_key(json.dumps(cfg))
    if row:
        row.encrypted_service_account_json = encrypted
        row.project_id = cfg["project_id"]
    else:
        row = MobileFcmConfig(
            organization_id=current_user.organization_id,
            encrypted_service_account_json=encrypted,
            project_id=cfg["project_id"],
        )
        db.add(row)
    db.commit()
    return {"configured": True, "project_id": cfg["project_id"]}


@router.get("/fcm-config")
async def get_fcm_config(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    row = db.query(MobileFcmConfig).filter(
        MobileFcmConfig.organization_id == current_user.organization_id).first()
    if not row:
        return {"configured": False, "project_id": None, "updated_at": None}
    return {
        "configured": True,
        "project_id": row.project_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# --------------------------------------------------------------------------
# Blocked visitors
# --------------------------------------------------------------------------

class BlockVisitorRequest(BaseModel):
    customer_id: Optional[str] = None
    ip_address: Optional[str] = None
    reason: Optional[str] = None


@router.get("/blocked-visitors")
async def list_blocked_visitors(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    rows = db.query(BlockedVisitor).filter(
        BlockedVisitor.organization_id == current_user.organization_id
    ).order_by(BlockedVisitor.created_at.desc()).limit(200).all()
    return {"blocked": [
        {"id": str(r.id), "customer_id": str(r.customer_id) if r.customer_id else None,
         "ip_address": r.ip_address, "reason": r.reason,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}


@router.post("/blocked-visitors")
async def block_visitor(
    body: BlockVisitorRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not body.customer_id and not body.ip_address:
        raise HTTPException(status_code=422, detail="customer_id or ip_address is required")
    import uuid as _uuid
    customer_uuid = None
    if body.customer_id:
        try:
            customer_uuid = _uuid.UUID(str(body.customer_id))
        except ValueError:
            raise HTTPException(status_code=422, detail="customer_id must be a UUID")
    row = BlockedVisitor(
        organization_id=current_user.organization_id,
        customer_id=customer_uuid,
        ip_address=(body.ip_address or "").strip() or None,
        reason=body.reason,
        blocked_by_user_id=current_user.id if hasattr(current_user, "id") else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": str(row.id), "customer_id": str(row.customer_id) if row.customer_id else None,
            "ip_address": row.ip_address, "reason": row.reason}


@router.delete("/blocked-visitors/{blocked_id}")
async def unblock_visitor(
    blocked_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    import uuid as _uuid
    try:
        blocked_uuid = _uuid.UUID(str(blocked_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Blocked visitor not found")
    row = db.query(BlockedVisitor).filter(
        BlockedVisitor.id == blocked_uuid,
        BlockedVisitor.organization_id == current_user.organization_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Blocked visitor not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Per-widget security (kill switch + limit overrides)
# --------------------------------------------------------------------------

class WidgetSecurityRequest(BaseModel):
    chat_disabled: Optional[bool] = None
    msg_per_min: Optional[int] = None
    msg_per_ip_per_min: Optional[int] = None
    msg_per_widget_per_min: Optional[int] = None
    ai_daily_quota_per_conversation: Optional[int] = None
    ai_daily_quota_per_widget: Optional[int] = None
    upload_files_per_day: Optional[int] = None
    upload_bytes_per_day: Optional[int] = None


def _security_view(row: Optional[MobileWidgetSecurity], widget_id: str) -> dict:
    from app.services import mobile_security
    defaults = mobile_security.WidgetLimits()
    return {
        "widget_id": widget_id,
        "chat_disabled": bool(row.chat_disabled) if row else False,
        "msg_per_min": row.msg_per_min if row and row.msg_per_min is not None else defaults.msg_per_min,
        "msg_per_ip_per_min": row.msg_per_ip_per_min if row and row.msg_per_ip_per_min is not None else defaults.msg_per_ip_per_min,
        "msg_per_widget_per_min": row.msg_per_widget_per_min if row and row.msg_per_widget_per_min is not None else defaults.msg_per_widget_per_min,
        "ai_daily_quota_per_conversation": row.ai_daily_quota_per_conversation if row and row.ai_daily_quota_per_conversation is not None else defaults.ai_per_conversation_per_day,
        "ai_daily_quota_per_widget": row.ai_daily_quota_per_widget if row and row.ai_daily_quota_per_widget is not None else defaults.ai_per_widget_per_day,
        "upload_files_per_day": row.upload_files_per_day if row and row.upload_files_per_day is not None else defaults.upload_files_per_day,
        "upload_bytes_per_day": row.upload_bytes_per_day if row and row.upload_bytes_per_day is not None else defaults.upload_bytes_per_day,
    }


@router.get("/widgets/{widget_id}/security")
async def get_widget_security(
    widget_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _org_widget(db, widget_id, current_user.organization_id)
    row = db.query(MobileWidgetSecurity).filter(
        MobileWidgetSecurity.widget_id == widget_id).first()
    return _security_view(row, widget_id)


@router.put("/widgets/{widget_id}/security")
async def set_widget_security(
    widget_id: str,
    body: WidgetSecurityRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    _org_widget(db, widget_id, current_user.organization_id)
    row = db.query(MobileWidgetSecurity).filter(
        MobileWidgetSecurity.widget_id == widget_id).first()
    if not row:
        row = MobileWidgetSecurity(widget_id=widget_id,
                                   organization_id=current_user.organization_id)
        db.add(row)
    for field in ("chat_disabled", "msg_per_min", "msg_per_ip_per_min",
                  "msg_per_widget_per_min", "ai_daily_quota_per_conversation",
                  "ai_daily_quota_per_widget", "upload_files_per_day",
                  "upload_bytes_per_day"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return _security_view(row, widget_id)


# --------------------------------------------------------------------------
# Abuse log (Phase 1 abuse panel backend)
# --------------------------------------------------------------------------

@router.get("/abuse-events")
async def list_abuse_events(
    widget_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = db.query(MobileAbuseEvent).filter(
        MobileAbuseEvent.organization_id == current_user.organization_id)
    if widget_id:
        _org_widget(db, widget_id, current_user.organization_id)
        query = query.filter(MobileAbuseEvent.widget_id == widget_id)
    if event_type:
        query = query.filter(MobileAbuseEvent.event_type == event_type)
    rows = query.order_by(MobileAbuseEvent.created_at.desc(),
                          MobileAbuseEvent.id.desc()).limit(limit).all()
    return {"events": [
        {"id": r.id, "widget_id": str(r.widget_id) if r.widget_id else None,
         "session_id": str(r.session_id) if r.session_id else None,
         "ip_address": r.ip_address, "event_type": r.event_type,
         "detail": r.detail,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}


# --------------------------------------------------------------------------
# AI-content reports (Google Play AI-Generated Content policy)
# --------------------------------------------------------------------------

@router.get("/reports")
async def list_reports(
    widget_id: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Visitor reports of offensive AI content (POST /api/v1/mobile/reports),
    org-scoped like the abuse log. The dashboard's safety view reads these
    to act on user reports — the Play policy requires the reports to be
    monitored and used to improve filtering, not just collected."""
    query = db.query(MobileReport).filter(
        MobileReport.organization_id == current_user.organization_id)
    if widget_id:
        _org_widget(db, widget_id, current_user.organization_id)
        query = query.filter(MobileReport.widget_id == widget_id)
    if reason:
        query = query.filter(MobileReport.reason == reason)
    rows = query.order_by(MobileReport.created_at.desc(),
                          MobileReport.id.desc()).limit(limit).all()
    return {"reports": [
        {"id": str(r.id), "widget_id": str(r.widget_id),
         "reason": r.reason, "details": r.details, "source": r.source,
         "ip_address": r.ip_address,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}
