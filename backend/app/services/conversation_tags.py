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

"""Conversation tags: read the taxonomy, score a conversation's tags.

The taxonomy itself lives in ``app.services.tag_taxonomy`` (slug, label,
group, weight, colour per tag). This module is the one place that knows how
to read it: the admin tags API, the inbox list and the tagger all come here,
so a rename inside the taxonomy module only needs fixing once.

If the taxonomy module is not importable yet, the spec weights from the
feature brief are used instead (same slugs, groups and weights), so the API
keeps working and the taxonomy stays the single source of truth once it
lands.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)

# The tag set from the feature brief, used only while
# ``app.services.tag_taxonomy`` is not there yet. Groups are the five the
# inbox uses: priority | payment | service | status | customer.
_FALLBACK_TAGS: List[Dict[str, Any]] = [
    {"slug": "payment-received", "label": "Payment received", "group": "payment", "weight": 100, "color": "#16a34a"},
    {"slug": "urgent", "label": "Urgent", "group": "priority", "weight": 90, "color": "#dc2626"},
    {"slug": "angry", "label": "Angry", "group": "priority", "weight": 85, "color": "#ea580c"},
    {"slug": "needs-human", "label": "Needs human", "group": "priority", "weight": 80, "color": "#2563eb"},
    {"slug": "refund-request", "label": "Refund request", "group": "payment", "weight": 75, "color": "#b45309"},
    {"slug": "payment-proof", "label": "Payment proof", "group": "payment", "weight": 70, "color": "#059669"},
    {"slug": "delivery-pending", "label": "Delivery pending", "group": "status", "weight": 65, "color": "#d97706"},
    {"slug": "payment-pending", "label": "Payment pending", "group": "payment", "weight": 60, "color": "#ca8a04"},
    {"slug": "vip", "label": "VIP", "group": "customer", "weight": 55, "color": "#7c3aed"},
    {"slug": "follow-up-needed", "label": "Follow-up needed", "group": "status", "weight": 50, "color": "#0284c7"},
    {"slug": "waiting-customer", "label": "Waiting for customer", "group": "status", "weight": 40, "color": "#64748b"},
    {"slug": "repeat-customer", "label": "Repeat customer", "group": "customer", "weight": 20, "color": "#0d9488"},
    {"slug": "svc-sim-owner", "label": "SIM Owner Details", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-cdr", "label": "CDR", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-imei", "label": "IMEI Tracking", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-sim-location", "label": "SIM Location", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-cnic-copy", "label": "CNIC Copy", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-family-tree", "label": "Family Tree", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-travel-history", "label": "Travel History", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-whatsapp-cdr", "label": "WhatsApp CDR", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-passport", "label": "Passport Details", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "svc-all-network", "label": "All Network Hide", "group": "service", "weight": 10, "color": "#475569"},
    {"slug": "delivered", "label": "Delivered", "group": "status", "weight": 0, "color": "#16a34a"},
    {"slug": "spam", "label": "Spam", "group": "status", "weight": 0, "color": "#9ca3af"},
]


def _taxonomy_definitions() -> Optional[List[Dict[str, Any]]]:
    """The taxonomy module's definitions, in the TagDefinition shape, or
    None when the module (or a definitions list in it) is not there."""
    try:
        from app.services import tag_taxonomy
    except ImportError:
        return None
    raw = (
        getattr(tag_taxonomy, "TAGS", None)
        or getattr(tag_taxonomy, "TAG_DEFINITIONS", None)
        or getattr(tag_taxonomy, "TAG_LIST", None)
    )
    if not raw:
        return None
    definitions: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            definitions.append({
                "slug": str(item.get("slug", "")),
                "label": str(item.get("label", item.get("slug", ""))),
                "group": str(item.get("group", "")),
                "weight": int(item.get("weight", 0)),
                "color": str(item.get("color", "")),
            })
        else:
            # A small object / dataclass / namedtuple with the same fields.
            definitions.append({
                "slug": str(getattr(item, "slug", "")),
                "label": str(getattr(item, "label", getattr(item, "slug", ""))),
                "group": str(getattr(item, "group", "")),
                "weight": int(getattr(item, "weight", 0)),
                "color": str(getattr(item, "color", "")),
            })
    definitions = [d for d in definitions if d["slug"]]
    if not definitions:
        return None
    # A weights-only mapping in the module wins over the definitions' weights.
    weights = getattr(tag_taxonomy, "TAG_WEIGHTS", None)
    if isinstance(weights, dict):
        for d in definitions:
            if d["slug"] in weights:
                d["weight"] = int(weights[d["slug"]])
    return definitions


def list_tags() -> List[Dict[str, Any]]:
    """Every defined tag, heaviest first, as TagDefinition dicts."""
    definitions = _taxonomy_definitions() or [dict(t) for t in _FALLBACK_TAGS]
    return sorted(definitions, key=lambda d: (-d["weight"], d["slug"]))


def _by_slug() -> Dict[str, Dict[str, Any]]:
    return {d["slug"]: d for d in list_tags()}


def get_tag(slug: Optional[str]) -> Optional[Dict[str, Any]]:
    """One tag's definition, or None for an unknown slug."""
    if not slug:
        return None
    return _by_slug().get(str(slug).strip())


def is_valid_tag(slug: Optional[str]) -> bool:
    return get_tag(slug) is not None


def tag_weight(slug: str) -> int:
    definition = get_tag(slug)
    return definition["weight"] if definition else 0


def priority_score(tags: Optional[List[str]]) -> int:
    """The conversation's priority: its heaviest tag's weight, 0 with none."""
    if not tags:
        return 0
    return max((tag_weight(slug) for slug in tags), default=0)


def clean_tags(raw: Any) -> List[str]:
    """A stored value back to slugs: strings only, known slugs only, in
    order, no duplicates. Anything else is dropped, never an error."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    seen = set()
    cleaned: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        slug = item.strip()
        if slug and slug not in seen and is_valid_tag(slug):
            seen.add(slug)
            cleaned.append(slug)
    return cleaned


def tags_column_available() -> bool:
    """Whether SessionToAgent carries the tags column yet (the model +
    migration land separately from this API)."""
    from app.models.session_to_agent import SessionToAgent

    return hasattr(SessionToAgent, "tags")


def read_tags(session: Any) -> List[str]:
    """A conversation row's tags, cleaned; [] when the column is not there."""
    if session is None or not tags_column_available():
        return []
    return clean_tags(getattr(session, "tags", None))
