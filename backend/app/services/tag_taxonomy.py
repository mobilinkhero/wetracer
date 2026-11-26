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

"""Canonical conversation tags for AI auto-tagging.

One fixed list, shared by the tagger (which assigns tags from message
content), the API (which validates them) and the inbox (which sorts and
filters by them). The slug is the stored value — ``session_to_agents.tags``
holds a JSON list of slugs; the label, group, weight and colour live here
only, so renaming a label never rewrites stored rows.

Priority: the inbox sorts by the highest weight among a conversation's tags
(see :func:`priority_score`). ``payment-received`` sits at the top on
purpose — money is in and the result is not delivered yet, so that chat
waits on us, not the other way round.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tag:
    """One tag in the canonical taxonomy."""

    slug: str
    label: str
    group: str
    weight: int
    color: str


#: Groups, in the order the inbox filter lists them.
GROUPS = ("payment", "priority", "status", "customer", "service")

TAGS: tuple[Tag, ...] = (
    # Payment — the money trail.
    Tag("payment-received", "Payment received", "payment", 100, "red"),
    Tag("refund-request", "Refund request", "payment", 75, "red"),
    Tag("payment-proof", "Payment proof", "payment", 70, "blue"),
    Tag("payment-pending", "Payment pending", "payment", 60, "amber"),
    # Priority — chats that need a human first.
    Tag("urgent", "Urgent", "priority", 90, "orange"),
    Tag("angry", "Angry", "priority", 85, "red"),
    Tag("needs-human", "Needs human", "priority", 80, "amber"),
    # Status — where the chat stands.
    Tag("delivery-pending", "Delivery pending", "status", 65, "amber"),
    Tag("follow-up-needed", "Follow-up needed", "status", 50, "blue"),
    Tag("waiting-customer", "Waiting for customer", "status", 40, "grey"),
    Tag("delivered", "Delivered", "status", 0, "green"),
    Tag("spam", "Spam", "status", 0, "grey"),
    # Customer — who they are.
    Tag("vip", "VIP", "customer", 55, "purple"),
    Tag("repeat-customer", "Repeat customer", "customer", 20, "grey"),
    # Service — what they asked about.
    Tag("svc-sim-owner", "SIM Owner Details", "service", 10, "teal"),
    Tag("svc-cdr", "CDR", "service", 10, "teal"),
    Tag("svc-imei", "IMEI Tracking", "service", 10, "teal"),
    Tag("svc-sim-location", "SIM Location", "service", 10, "teal"),
    Tag("svc-cnic-copy", "CNIC Copy", "service", 10, "teal"),
    Tag("svc-family-tree", "Family Tree", "service", 10, "teal"),
    Tag("svc-travel-history", "Travel History", "service", 10, "teal"),
    Tag("svc-whatsapp-cdr", "WhatsApp CDR", "service", 10, "teal"),
    Tag("svc-passport", "Passport Details", "service", 10, "teal"),
    Tag("svc-all-network", "All Network Hide", "service", 10, "teal"),
)

TAG_BY_SLUG: dict[str, Tag] = {tag.slug: tag for tag in TAGS}


def get_tag(slug: str) -> Tag | None:
    """The tag for a slug, or None when the slug is not in the taxonomy."""
    return TAG_BY_SLUG.get(slug)


def valid_tag_slugs() -> frozenset[str]:
    """Every slug the taxonomy defines — the set the API validates against."""
    return frozenset(TAG_BY_SLUG)


def tags_by_group() -> dict[str, list[Tag]]:
    """Tags grouped by their group, groups in :data:`GROUPS` order."""
    grouped: dict[str, list[Tag]] = {group: [] for group in GROUPS}
    for tag in TAGS:
        grouped[tag.group].append(tag)
    return grouped


def priority_score(tags) -> int:
    """How urgent a conversation is: the highest weight among its tags.

    ``tags`` is the stored value — an iterable of slugs (or Tag objects).
    Unknown slugs are ignored, so a tag removed from the taxonomy later
    never breaks sorting; no tags (or none known) scores 0.
    """
    if not tags:
        return 0
    score = 0
    for item in tags:
        tag = item if isinstance(item, Tag) else TAG_BY_SLUG.get(item)
        if tag is not None and tag.weight > score:
            score = tag.weight
    return score
