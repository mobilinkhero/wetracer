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


The canonical conversation-tag taxonomy: shape, grouping, priority score,
and the tags column on the shared conversation row.
"""

import re
from pathlib import Path
from uuid import uuid4

from app.models.session_to_agent import SessionToAgent
from app.repositories.session_to_agent import SessionToAgentRepository
from app.services import tag_taxonomy
from app.services.tag_taxonomy import (
    GROUPS,
    TAGS,
    TAG_BY_SLUG,
    Tag,
    get_tag,
    priority_score,
    tags_by_group,
    valid_tag_slugs,
)


class TestTaxonomyShape:
    def test_twenty_four_tags(self):
        assert len(TAGS) == 24

    def test_slugs_unique(self):
        assert len(TAG_BY_SLUG) == len(TAGS)

    def test_every_tag_has_label_group_weight_and_color(self):
        for tag in TAGS:
            assert tag.slug and tag.label and tag.group and tag.color
            assert 0 <= tag.weight <= 100

    def test_groups_are_the_known_five(self):
        assert GROUPS == ("payment", "priority", "status", "customer", "service")
        assert {tag.group for tag in TAGS} == set(GROUPS)

    def test_group_counts(self):
        grouped = tags_by_group()
        assert [len(grouped[group]) for group in GROUPS] == [4, 3, 5, 2, 10]

    def test_payment_received_is_the_top_priority(self):
        assert TAG_BY_SLUG["payment-received"].weight == max(tag.weight for tag in TAGS)
        assert TAG_BY_SLUG["payment-received"].weight == 100


class TestLookups:
    def test_valid_tag_slugs_contains_every_slug_and_nothing_else(self):
        slugs = valid_tag_slugs()
        assert slugs == {tag.slug for tag in TAGS}
        assert "not-a-tag" not in slugs
        assert isinstance(slugs, frozenset)

    def test_get_tag(self):
        assert get_tag("urgent") == Tag("urgent", "Urgent", "priority", 90, "orange")
        assert get_tag("nope") is None

    def test_tags_by_group_keys_in_order_and_members_match(self):
        grouped = tags_by_group()
        assert list(grouped) == list(GROUPS)
        for group, tags in grouped.items():
            assert tags and all(tag.group == group for tag in tags)
        assert [tag.slug for tag in grouped["service"]] == [
            "svc-sim-owner", "svc-cdr", "svc-imei", "svc-sim-location",
            "svc-cnic-copy", "svc-family-tree", "svc-travel-history",
            "svc-whatsapp-cdr", "svc-passport", "svc-all-network",
        ]


class TestPriorityScore:
    def test_no_tags_scores_zero(self):
        assert priority_score(None) == 0
        assert priority_score([]) == 0

    def test_single_tag_scores_its_weight(self):
        assert priority_score(["payment-pending"]) == 60
        assert priority_score(["delivered"]) == 0

    def test_multiple_tags_take_the_highest_weight(self):
        assert priority_score(["svc-cdr", "payment-pending", "vip"]) == 60
        assert priority_score(["urgent", "payment-received"]) == 100

    def test_unknown_slugs_are_ignored(self):
        assert priority_score(["not-a-tag", "vip"]) == 55
        assert priority_score(["not-a-tag"]) == 0

    def test_tag_objects_are_accepted_too(self):
        assert priority_score([TAG_BY_SLUG["angry"]]) == 85


class TestSessionTagsColumn:
    def test_new_session_defaults_to_no_tags(self, db, test_widget, test_customer):
        session_id = uuid4()
        SessionToAgentRepository(db).create_session(
            session_id=str(session_id), agent_id=test_widget.agent_id,
            customer_id=test_customer.id, organization_id=test_widget.organization_id)
        assert db.get(SessionToAgent, session_id).tags == []

    def test_tags_persist_on_the_session_row(self, db, test_widget, test_customer):
        session_id = uuid4()
        SessionToAgentRepository(db).create_session(
            session_id=str(session_id), agent_id=test_widget.agent_id,
            customer_id=test_customer.id, organization_id=test_widget.organization_id)
        row = db.get(SessionToAgent, session_id)
        row.tags = ["payment-received", "svc-whatsapp-cdr"]
        db.commit()
        db.expire_all()
        assert db.get(SessionToAgent, session_id).tags == [
            "payment-received", "svc-whatsapp-cdr"]
        assert priority_score(db.get(SessionToAgent, session_id).tags) == 100


class TestMigration:
    def test_revision_id_fits_the_varchar_32_column(self):
        text = (Path(__file__).parents[2] / "alembic" / "versions"
                / "session_tags_001.py").read_text()
        revision = re.search(r"^revision: str = '([^']+)'", text, re.M).group(1)
        assert len(revision) <= 32, revision
        assert re.search(r"^down_revision.*= 'mobile_reports_001'", text, re.M)
