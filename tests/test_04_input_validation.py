"""
test_04_input_validation.py
────────────────────────────
Hunts for missing input validation — the most common API bug class.

BUG HYPOTHESES:
  Bug #5 – Missing required fields accepted (title/url not validated).
  Bug #6 – SQL/NoSQL injection via video title or URL fields.
  Bug #7 – process-captions on already-processing video causes race/crash.
  Bug #8 – Extremely large payloads accepted without limit (DoS vector).
"""

import pytest
import requests
from utils.client import BASE_URL, base_headers


# ─────────────────────────────────────────────────────────────────
# Bug #5 — Missing required fields
# ─────────────────────────────────────────────────────────────────

class TestMissingFields:

    def test_create_video_no_title(self, http, auth_token):
        """Creating a video without 'title' must return 400, not 200/500."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"url": "https://example.com/notitle.mp4"},  # no title
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 400, (
            f"[BUG FOUND] 🐛 MISSING VALIDATION: POST /api/videos without 'title' "
            f"returned {resp.status_code} (expected 400). "
            f"Response: {resp.text[:200]}"
        )

    def test_create_video_no_url(self, http, auth_token):
        """Creating a video without 'url' must return 400, not 200/500."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": "No URL Video"},  # no url
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 400, (
            f"[BUG FOUND] 🐛 MISSING VALIDATION: POST /api/videos without 'url' "
            f"returned {resp.status_code} (expected 400). "
            f"Response: {resp.text[:200]}"
        )

    def test_create_video_empty_body(self, http, auth_token):
        """Empty JSON body must return 400."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={},
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 400, (
            f"[BUG FOUND] 🐛 MISSING VALIDATION: POST /api/videos with empty body "
            f"returned {resp.status_code}. Server accepted an empty video record."
        )

    def test_create_video_null_title(self, http, auth_token):
        """null title must be rejected."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": None, "url": "https://example.com/x.mp4"},
            headers=base_headers(auth_token),
        )
        assert resp.status_code in (400, 422), (
            f"[BUG FOUND] 🐛 NULL VALUE ACCEPTED: null title returned {resp.status_code}."
        )

    def test_create_video_empty_string_title(self, http, auth_token):
        """Empty string title must be rejected — not a valid video title."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": "", "url": "https://example.com/x.mp4"},
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 400, (
            f"[BUG FOUND] 🐛 EMPTY STRING ACCEPTED: title='' was not rejected "
            f"(status {resp.status_code})."
        )

    def test_create_video_invalid_url_format(self, http, auth_token):
        """A non-URL string in 'url' field should be rejected."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": "Bad URL Test", "url": "not-a-url-at-all"},
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 400, (
            f"[BUG FOUND] 🐛 INVALID URL ACCEPTED: 'not-a-url-at-all' was accepted "
            f"as a video URL (status {resp.status_code}). No URL format validation."
        )


# ─────────────────────────────────────────────────────────────────
# Bug #6 — Injection Attacks
# ─────────────────────────────────────────────────────────────────

class TestInjection:

    INJECTION_PAYLOADS = [
        ("SQL Injection",       "'; DROP TABLE videos; --"),
        ("NoSQL Injection",     '{"$gt": ""}'),
        ("XSS payload",         "<script>alert('xss')</script>"),
        ("Template Injection",  "{{7*7}}"),
        ("Path Traversal",      "../../etc/passwd"),
    ]

    @pytest.mark.parametrize("attack_name,payload", INJECTION_PAYLOADS)
    def test_injection_in_title_does_not_crash(self, http, auth_token, attack_name, payload):
        """
        Malicious payloads in 'title' must either be rejected (400) or
        stored safely (200/201 with the string escaped).
        A 500 error means the payload caused a server crash — injection successful.
        """
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": payload, "url": "https://example.com/safe.mp4"},
            headers=base_headers(auth_token),
        )

        assert resp.status_code != 500, (
            f"[BUG FOUND] 🐛 {attack_name.upper()} CAUSED SERVER CRASH: "
            f"title={payload!r} → 500 Internal Server Error. "
            f"Response: {resp.text[:300]}"
        )

        # If it was accepted (2xx), clean up and verify the payload is stored as-is (not executed)
        if resp.status_code in (200, 201):
            vid_data = resp.json()
            vid = (
                vid_data.get("id")
                or vid_data.get("videoId")
                or (vid_data.get("video") or {}).get("id")
            )
            if vid:
                # Verify the stored title is the raw string, not evaluated
                get_resp = http.get(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )
                if get_resp.status_code == 200:
                    stored_title = get_resp.json().get("title", "")
                    if attack_name == "Template Injection":
                        assert stored_title != "49", (
                            f"[BUG FOUND] 🐛 TEMPLATE INJECTION EXECUTED: "
                            f"'{{{{7*7}}}}' was evaluated to '49'. SSTI vulnerability!"
                        )
                http.delete(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )


# ─────────────────────────────────────────────────────────────────
# Bug #7 — Race / Double-Process
# ─────────────────────────────────────────────────────────────────

class TestProcessingRaceConditions:

    def test_double_trigger_process_captions(self, http, auth_token, video):
        """
        Calling process-captions twice on the same video should return a clean
        error (409 Conflict or 400) the second time — not cause a 500 crash
        or silently create duplicate caption jobs.
        """
        vid = video

        first = http.post(
            f"{BASE_URL}/api/videos/{vid}/process-captions",
            headers=base_headers(auth_token),
        )
        assert first.status_code in (200, 202), (
            f"First process-captions call failed [{first.status_code}]"
        )

        # Immediately trigger again (race condition test)
        second = http.post(
            f"{BASE_URL}/api/videos/{vid}/process-captions",
            headers=base_headers(auth_token),
        )
        assert second.status_code != 500, (
            f"[BUG FOUND] 🐛 DOUBLE-PROCESS CRASH: Triggering process-captions "
            f"twice on video {vid} caused a 500. Concurrent/duplicate job handling "
            f"is broken. Response: {second.text[:300]}"
        )
        # Should be 400/409 (already processing) or 200/202 (idempotent)
        assert second.status_code in (200, 202, 400, 409), (
            f"[BUG FOUND] 🐛 UNEXPECTED STATUS on double-process: {second.status_code}"
        )

    def test_process_nonexistent_video(self, http, auth_token):
        """process-captions on a non-existent video must return 404, not 500."""
        resp = http.post(
            f"{BASE_URL}/api/videos/does-not-exist-xyz/process-captions",
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 404, (
            f"[BUG FOUND] 🐛 WRONG STATUS: process-captions on non-existent video "
            f"returned {resp.status_code} (expected 404). "
            + ("SERVER CRASH!" if resp.status_code == 500 else "")
        )


# ─────────────────────────────────────────────────────────────────
# Bug #8 — Payload Size / DoS
# ─────────────────────────────────────────────────────────────────

class TestPayloadSizeLimits:

    def test_extremely_long_title_rejected(self, http, auth_token):
        """
        A 100KB title must be rejected — unbounded strings are a DoS vector
        and can overflow database column limits causing a 500.
        """
        huge_title = "A" * 100_000
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": huge_title, "url": "https://example.com/x.mp4"},
            headers=base_headers(auth_token),
        )
        assert resp.status_code in (400, 413), (
            f"[BUG FOUND] 🐛 NO SIZE LIMIT: 100KB title accepted (status {resp.status_code}). "
            f"No max-length validation — potential DoS / DB column overflow."
        )
        assert resp.status_code != 500, (
            f"[BUG FOUND] 🐛 100KB title caused a 500 — DB column overflow crash."
        )

        # Clean up if it was somehow accepted
        if resp.status_code in (200, 201):
            vid = (
                resp.json().get("id")
                or resp.json().get("videoId")
                or (resp.json().get("video") or {}).get("id")
            )
            if vid:
                http.delete(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )
