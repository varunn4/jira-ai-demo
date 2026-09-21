"""Unit tests for app.rca.intake — extraction normalization + LLM injection."""
from __future__ import annotations

import base64
import dataclasses
import json
import unittest
from pathlib import Path

from app.rca import intake
from app.rca.intake import TicketIntake
from tests.rca_helpers import make_settings


def _ticket(**over) -> TicketIntake:
    base = dict(
        key="RFT-1", summary="500 on /api/pay", description="NullPointer in charge()",
        status="Open", issue_type="Bug", priority="High", components=["payments"],
        labels=[], affected_versions=["1.2.0"], fix_versions=[], environment="prod",
        comments=["stacktrace: PaymentService.charge line 42"], attachments=[],
        linked_issues=[], raw={},
    )
    base.update(over)
    return TicketIntake(**base)


class FakeLLM:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0
        self.last_images = None

    def complete(self, system, user, *, max_tokens=4096, images=None):
        self.calls += 1
        self.last_user = user
        self.last_images = images
        return self._payload


class NormalizationTests(unittest.TestCase):
    def test_as_str_list(self):
        self.assertEqual(intake._as_str_list(["a", " b ", "", 3]), ["a", "b", "3"])
        self.assertEqual(intake._as_str_list("solo"), ["solo"])
        self.assertEqual(intake._as_str_list(None), [])

    def test_normalize_frames_mixed(self):
        frames = intake._normalize_frames([
            {"file": "svc/pay.py", "line": "42", "symbol": "charge", "raw": "at charge"},
            "raw frame string",
            {"file": None, "line": None, "symbol": None, "raw": ""},
        ])
        self.assertEqual(frames[0]["line"], 42)
        self.assertEqual(frames[0]["file"], "svc/pay.py")
        self.assertEqual(frames[1]["raw"], "raw frame string")
        self.assertIsNone(frames[1]["file"])


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings(Path("/tmp"))

    def test_extract_signals_happy_path(self):
        payload = json.dumps({
            "error_messages": ["NullPointerException", ""],
            "stack_frames": [{"file": "svc/pay.py", "line": 42, "symbol": "charge", "raw": "x"}],
            "mentioned_endpoints": ["/api/pay"],
            "repro_steps": ["call /api/pay"],
            "suspected_area": "payments",
        })
        llm = FakeLLM(payload)
        out = intake.extract_signals(self.settings, _ticket(), llm_client=llm)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(out["error_messages"], ["NullPointerException"])  # blank dropped
        self.assertEqual(out["stack_frames"][0]["line"], 42)
        self.assertEqual(out["suspected_area"], "payments")
        # the ticket content reached the prompt
        self.assertIn("RFT-1", llm.last_user)
        self.assertIn("/api/pay", llm.last_user)

    def test_extract_signals_bad_json_returns_empty_schema(self):
        out = intake.extract_signals(self.settings, _ticket(), llm_client=FakeLLM("not json at all"))
        self.assertEqual(out["error_messages"], [])
        self.assertEqual(out["stack_frames"], [])
        self.assertEqual(out["suspected_area"], "")

    def test_extract_signals_partial_keys(self):
        out = intake.extract_signals(
            self.settings, _ticket(),
            llm_client=FakeLLM(json.dumps({"error_messages": ["boom"]})),
        )
        self.assertEqual(out["error_messages"], ["boom"])
        self.assertEqual(out["mentioned_endpoints"], [])  # filled from schema default

    def test_extract_signals_new_signal_keys(self):
        payload = json.dumps({
            "expected_behavior": "search by first name returns matches",
            "actual_behavior": "no data found",
            "environment": ["staging", "chrome"],
            "mentioned_code_refs": ["QuickReportAPI.js", "get_failed_loan_retry_report"],
            "linked_ticket_keys": ["RFT-1570"],
        })
        out = intake.extract_signals(self.settings, _ticket(), llm_client=FakeLLM(payload))
        self.assertEqual(out["expected_behavior"], "search by first name returns matches")
        self.assertEqual(out["actual_behavior"], "no data found")
        self.assertEqual(out["environment"], ["staging", "chrome"])
        self.assertIn("RFT-1570", out["linked_ticket_keys"])
        self.assertIn("get_failed_loan_retry_report", out["mentioned_code_refs"])

    def test_intake_user_message_includes_sections(self):
        msg = intake._intake_user_message(_ticket(comments=["c1"], linked_issues=[
            {"key": "RFT-2", "relation": "blocks", "summary": "other"}]))
        self.assertIn("COMPONENTS: payments", msg)
        self.assertIn("COMMENTS:", msg)
        self.assertIn("LINKED ISSUES:", msg)
        self.assertIn("RFT-2", msg)


class ScreenshotIntakeTests(unittest.TestCase):
    def setUp(self):
        # creds required so _collect_ticket_images attempts the download
        self.settings = dataclasses.replace(
            make_settings(Path("/tmp")), jira_email="u@x.com", jira_api_token="tok")

    def test_collect_images_filters_and_base64_encodes(self):
        ticket = _ticket(attachments=[
            {"filename": "shot.png", "mime": "image/png", "size": 10, "url": "http://j/1"},
            {"filename": "doc.pdf", "mime": "application/pdf", "size": 10, "url": "http://j/2"},
            {"filename": "huge.png", "mime": "image/png",
             "size": intake._MAX_IMAGE_BYTES + 1, "url": "http://j/3"},
            {"filename": "nourl.jpg", "mime": "image/jpeg", "size": 10, "url": ""},
        ])
        orig = intake._download_attachment
        intake._download_attachment = lambda s, url: b"\x89PNGbytes"
        try:
            blocks = intake._collect_ticket_images(self.settings, ticket)
        finally:
            intake._download_attachment = orig
        # only the valid png survives (pdf, oversized, and no-url are dropped)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[0]["source"]["data"]), b"\x89PNGbytes")

    def test_collect_images_skips_when_no_credentials(self):
        no_creds = dataclasses.replace(self.settings, jira_email="", jira_api_token="")
        ticket = _ticket(attachments=[
            {"filename": "s.png", "mime": "image/png", "size": 10, "url": "http://j/1"}])
        self.assertEqual(intake._collect_ticket_images(no_creds, ticket), [])

    def test_download_failure_degrades_gracefully(self):
        ticket = _ticket(attachments=[
            {"filename": "s.png", "mime": "image/png", "size": 10, "url": "http://j/1"}])
        orig = intake._download_attachment
        def boom(s, url):
            raise RuntimeError("network down")
        intake._download_attachment = boom
        try:
            self.assertEqual(intake._collect_ticket_images(self.settings, ticket), [])
        finally:
            intake._download_attachment = orig

    def test_extract_signals_passes_images_to_llm(self):
        ticket = _ticket(attachments=[
            {"filename": "s.png", "mime": "image/png", "size": 10, "url": "http://j/1"}])
        sentinel = [{"type": "image", "source": {"type": "base64",
                     "media_type": "image/png", "data": "AAAA"}}]
        orig = intake._collect_ticket_images
        intake._collect_ticket_images = lambda s, t: sentinel
        llm = FakeLLM(json.dumps({"mentioned_endpoints": ["/aml-leads"]}))
        try:
            out = intake.extract_signals(self.settings, ticket, llm_client=llm)
        finally:
            intake._collect_ticket_images = orig
        self.assertEqual(llm.last_images, sentinel)
        self.assertEqual(out["mentioned_endpoints"], ["/aml-leads"])

    def test_extract_signals_no_attachments_sends_no_images(self):
        llm = FakeLLM(json.dumps({}))
        intake.extract_signals(self.settings, _ticket(attachments=[]), llm_client=llm)
        self.assertIsNone(llm.last_images)


class LinkedDocIntakeTests(unittest.TestCase):
    def test_intake_message_includes_readable_docs_only(self):
        t = _ticket(linked_docs=[
            {"label": "PRD", "url": "http://d/1",
             "text": "Expected: download a TXT file", "error": ""},
            {"label": "TechDoc", "url": "http://d/2", "text": "", "error": "auth required"},
        ])
        msg = intake._intake_user_message(t)
        self.assertIn("LINKED DESIGN DOCS", msg)
        self.assertIn("Expected: download a TXT file", msg)
        self.assertIn("http://d/1", msg)
        self.assertNotIn("http://d/2", msg)  # errored/empty doc is omitted

    def test_to_dict_strips_doc_text_keeps_metadata(self):
        t = _ticket(linked_docs=[
            {"label": "PRD", "url": "http://d/1", "text": "big spec text", "error": ""}])
        doc = t.to_dict()["linked_docs"][0]
        self.assertTrue(doc["has_text"])
        self.assertNotIn("text", doc)            # heavy text not stored
        self.assertEqual(doc["url"], "http://d/1")

    def test_fetch_linked_docs_reads_via_doc_review(self):
        from app import doc_review
        orig_extract, orig_fetch = doc_review.extract_doc_links, doc_review.fetch_doc_text
        doc_review.extract_doc_links = lambda desc: [
            doc_review.DocLink(label="PRD", url="http://d/1")]
        doc_review.fetch_doc_text = lambda url, limit_chars=60_000: ("spec text", "")
        try:
            docs = intake._fetch_linked_docs({"type": "doc"})
        finally:
            doc_review.extract_doc_links, doc_review.fetch_doc_text = orig_extract, orig_fetch
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["text"], "spec text")
        self.assertEqual(docs[0]["url"], "http://d/1")

    def test_fetch_linked_docs_degrades_on_extraction_error(self):
        from app import doc_review
        orig = doc_review.extract_doc_links
        def boom(desc):
            raise RuntimeError("adf parse failed")
        doc_review.extract_doc_links = boom
        try:
            self.assertEqual(intake._fetch_linked_docs({"x": 1}), [])
        finally:
            doc_review.extract_doc_links = orig


if __name__ == "__main__":
    unittest.main()
