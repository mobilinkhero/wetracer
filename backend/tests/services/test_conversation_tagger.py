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

"""Unit tests for the rule-based conversation tagger
(app/services/conversation_tagger.py).

Rules only — no model, no DB, no network. Every rule is covered in
English and Roman Urdu, including the payment-received ->
delivery-pending rule, the payment-pending suppression once paid, the
negation guard ("I have not paid yet" is not a payment), the amount
guard (a phone number or CNIC is not an amount), and no-duplicate
merging that never drops an existing tag.
"""

from unittest.mock import MagicMock

import pytest

from app.services.conversation_tagger import (
    apply_tags_to_conversation,
    merge_tags,
    suggest_tags,
)


def tags(text, **kwargs):
    return suggest_tags(text, **kwargs)


# --- payment-pending -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "How do I pay?",
    "payment kaise karni hai",
    "JazzCash number do",
    "EasyPaisa account number?",
    "I will pay tomorrow",
    "payment karunga kal",
    "payment karungi",
    "pay karna hai mujhe",
])
def test_payment_pending(text):
    assert "payment-pending" in tags(text)


# --- payment-received ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "I paid",
    "payment kar di hai",
    "pay kar diya",
    "paisay bhej diye",
    "paise bhej diye",
    "sent payment just now",
    "my transaction id is 123456",
    "TID 98765",
    "TRXID abc123",
])
def test_payment_received(text):
    assert "payment-received" in tags(text)


def test_payment_received_also_adds_delivery_pending():
    result = tags("payment kar di")
    assert "payment-received" in result
    assert "delivery-pending" in result


def test_payment_received_suppresses_payment_pending():
    # JazzCash is a payment-pending word, but they already paid: the
    # chat must not carry both tags.
    result = tags("JazzCash se payment kar di hai")
    assert "payment-received" in result
    assert "payment-pending" not in result


@pytest.mark.parametrize("text", [
    "I haven't paid yet",
    "I have not paid",
    "payment nahi ki abhi",
    "pay nahi kiya",
    "I didn't pay",
])
def test_negated_payment_is_not_payment_received(text):
    assert "payment-received" not in tags(text)


def test_transaction_id_beats_negation():
    result = tags("I haven't paid through the app, my transaction id is 445566")
    assert "payment-received" in result


def test_paid_as_part_of_another_word_does_not_tag():
    assert "payment-received" not in tags("I am unpaid and worried")


# --- payment-proof ---------------------------------------------------------

def test_payment_proof_image_with_payment_words():
    result = tags(
        "payment kar di, screenshot bhej raha hoon",
        attachments=[{"content_type": "image/png", "filename": "receipt.png"}],
    )
    assert "payment-proof" in result


def test_payment_proof_pdf_with_payment_context():
    result = tags(
        "JazzCash receipt attached",
        attachments=[{"filename": "receipt.pdf"}],
    )
    assert "payment-proof" in result


def test_image_without_payment_context_is_not_proof():
    result = tags(
        "yeh meri sim ki photo hai",
        attachments=[{"content_type": "image/jpeg", "filename": "sim.jpg"}],
    )
    assert "payment-proof" not in result


def test_audio_attachment_is_not_proof():
    result = tags(
        "payment kar di",
        attachments=[{"content_type": "audio/ogg", "filename": "voice.ogg"}],
    )
    assert "payment-proof" not in result


# --- refund ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "I want a refund",
    "paisay wapis chahiye",
    "money back please",
    "refund my payment",
])
def test_refund_request(text):
    assert "refund-request" in tags(text)


# --- urgent / angry / needs-human ------------------------------------------

@pytest.mark.parametrize("text", [
    "this is urgent",
    "jaldi karo please",
    "emergency hai",
    "foran chahiye",
    "abhi chahiye mujhe",
])
def test_urgent(text):
    assert "urgent" in tags(text)


@pytest.mark.parametrize("text", [
    "this is fraud",
    "scam hai ye",
    "dhoka diya aap ne",
    "bekar service",
    "bakwas",
    "you cheated me",
    "no reply from you for two days",
])
def test_angry(text):
    assert "angry" in tags(text)


@pytest.mark.parametrize("text", [
    "I want to talk to a human",
    "agent se baat karni hai",
    "call me please",
    "kisi insan se baat karao",
    "operator se connect karo",
    "staff ko bulao",
])
def test_needs_human(text):
    assert "needs-human" in tags(text)


# --- services ---------------------------------------------------------------

@pytest.mark.parametrize("text, slug", [
    ("SIM Owner Details chahiye", "svc-sim-owner"),
    ("sim details batao", "svc-sim-owner"),
    ("CDR chahiye mujhe", "svc-cdr"),
    ("call history nikalni hai", "svc-cdr"),
    ("sms history bhi chahiye", "svc-cdr"),
    ("IMEI tracking karna hai", "svc-imei"),
    ("sim location batao", "svc-sim-location"),
    ("CNIC copy chahiye", "svc-cnic-copy"),
    ("cnic front back copy", "svc-cnic-copy"),
    ("family tree banwana hai", "svc-family-tree"),
    ("travel history chahiye", "svc-travel-history"),
    ("WhatsApp CDR nikalna hai", "svc-whatsapp-cdr"),
    ("passport details chahiye", "svc-passport"),
    ("all network sim tag hide karna hai", "svc-all-network"),
    ("location hide karni hai", "svc-all-network"),
])
def test_service_tags(text, slug):
    assert slug in tags(text)


def test_whatsapp_cdr_is_not_plain_cdr():
    result = tags("WhatsApp CDR kitne ka hai")
    assert "svc-whatsapp-cdr" in result
    assert "svc-cdr" not in result


def test_case_insensitive():
    assert "urgent" in tags("URGENT karo")
    assert "payment-received" in tags("I PAID")


# --- vip --------------------------------------------------------------------

def test_vip_from_whatsapp_cdr_service():
    assert "vip" in tags("WhatsApp CDR kitne ka hai")


def test_vip_from_all_network_service():
    assert "vip" in tags("all network hide karna hai")


@pytest.mark.parametrize("text", [
    "28000 PKR",
    "25000",
    "25k",
    "Rs. 80,000 for whatsapp cdr",
])
def test_vip_from_amount(text):
    assert "vip" in tags(text)


@pytest.mark.parametrize("text", [
    "1150 PKR",
    "350 PKR",
    "1500 PKR",
    "03001234567",          # a phone number, not an amount
    "35202-1234567-1",      # a CNIC, not an amount
    "mera number 03001234567 hai",
])
def test_small_amounts_and_ids_are_not_vip(text):
    assert "vip" not in tags(text)


# --- repeat customer / spam -------------------------------------------------

def test_repeat_customer():
    assert "repeat-customer" in tags("salam", prior_conversation_count=1)
    assert "repeat-customer" in tags("salam", prior_conversation_count=3)
    assert "repeat-customer" not in tags("salam", prior_conversation_count=0)


def test_spam_repeated_text():
    result = tags(
        "salam",
        recent_texts=["salam", "salam"],
    )
    assert "spam" in result


def test_two_identical_texts_are_not_spam_yet():
    result = tags("salam", recent_texts=["salam"])
    assert "spam" not in result


@pytest.mark.parametrize("text", [
    "earn money fast, click here",
    "click here for free recharge",
    "free recharge offer",
])
def test_spam_phrases(text):
    assert "spam" in tags(text)


# --- merging ----------------------------------------------------------------

def test_merge_adds_new_without_duplicates():
    result = tags("jaldi karo", existing_tags=["urgent", "payment-pending"])
    assert result.count("urgent") == 1
    assert "payment-pending" in result


def test_merge_never_drops_existing_tags():
    # Even a tag this module would never suggest (hand-added) survives.
    result = merge_tags(["urgent", "custom-manual"], ["payment-pending"])
    assert "urgent" in result
    assert "custom-manual" in result
    assert "payment-pending" in result


def test_merge_drops_unknown_new_slugs():
    result = merge_tags([], ["urgent", "not-a-real-tag"])
    assert result == ["urgent"]


def test_merge_keeps_existing_order_then_canonical_new_order():
    result = merge_tags(["vip"], ["urgent", "payment-received"])
    assert result[0] == "vip"
    assert result.index("payment-received") < result.index("urgent")


def test_empty_message_suggests_nothing():
    assert tags("") == []
    assert tags("   ") == []
    assert tags(None) == []


def test_unrelated_message_suggests_nothing():
    assert tags("salam, kaisy ho ap") == []


def test_multiple_tags_in_one_message():
    result = tags("WhatsApp CDR chahiye, jaldi karo, payment kar di hai")
    assert "svc-whatsapp-cdr" in result
    assert "vip" in result
    assert "urgent" in result
    assert "payment-received" in result
    assert "delivery-pending" in result


# --- persistence helper robustness ------------------------------------------

def test_apply_tags_never_raises_when_db_fails():
    """Tagging must never break storing the message: a DB failure
    degrades to no tags, not an exception."""
    db = MagicMock()
    db.query.side_effect = Exception("database is down")
    result = apply_tags_to_conversation(
        db,
        session_id="00000000-0000-0000-0000-000000000000",
        message_id=1,
        message_text="payment kar di",
    )
    assert result == []
