"""
test_06_bonus_edge_cases.py
────────────────────────────
Bonus bugs and edge cases beyond the 5 critical ones.

BONUS BUG HYPOTHESES:
  Bonus #1 – HTTP method not allowed returns wrong status (200 instead of 405).
  Bonus #2 – CORS headers missing or misconfigured.
  Bonus #3 – Response Content-Type not application/json on JSON responses.
  Bonus #4 – Pagination: limit=0 causes crash or returns everything.
  Bonus #5 – Integer/type coercion: numeric ID accepted where string expected.
"""

import pytest
import requests
from utils.client import BASE_URL, base_headers


# ─────────────────────────────────────────────────────────────────
# Bonus #1 — HTTP Method Enforcement
# ─────────────────────────────────────────────────────────────────

class TestHttpMethods:

    def test_get_on_auth_endpoint(self, http, auth_token):
        """GET /api/auth should return 405 Method Not Allowed."""
        resp = http.get(
            f"{BASE_URL}/api/auth",
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 405, (
            f"[BONUS BUG] GET /api/auth returned {resp.status_code} instead of 405. "
            f"Incorrect method not enforced."
        )

    def test_delete_on_auth_endpoint(self, http):
        """DELETE /api/auth should return 405."""
        resp = http.delete(
            f"{BASE_URL}/api/auth",
            headers=base_headers(),
        )
        assert resp.status_code == 405, (
            f"[BONUS BUG] DELETE /api/auth returned {resp.status_code} (expected 405)."
        )

    def test_put_on_videos_collection(self, http, auth_token):
        """PUT /api/videos should return 405 (collection doesn't support PUT)."""
        resp = http.put(
            f"{BASE_URL}/api/videos",
            json={"test": "data"},
            headers=base_headers(auth_token),
        )
        assert resp.status_code in (404, 405), (
            f"[BONUS BUG] PUT /api/videos returned {resp.status_code}. "
            f"Undefined method accepted."
        )


# ─────────────────────────────────────────────────────────────────
# Bonus #2 — Response Content-Type Correctness
# ─────────────────────────────────────────────────────────────────

class TestContentType:

    def test_auth_response_is_json(self, http):
        """POST /api/auth response must declare Content-Type: application/json."""
        resp = http.post(
            f"{BASE_URL}/api/auth",
            json={"username": "testuser", "password": "testpassword"},
            headers=base_headers(),
        )
        ct = resp.headers.get("Content-Type", "")
        assert "application/json" in ct, (
            f"[BONUS BUG] POST /api/auth returned Content-Type: {ct!r} "
            f"instead of application/json. Clients relying on Content-Type will break."
        )

    def test_error_responses_are_json(self, http, auth_token):
        """
        Error responses (404, 400) must also return JSON, not plain text or HTML.
        HTML error pages are a common bug in production systems.
        """
        resp = http.get(
            f"{BASE_URL}/api/videos/nonexistent-video-id",
            headers=base_headers(auth_token),
        )
        ct = resp.headers.get("Content-Type", "")
        assert "application/json" in ct, (
            f"[BONUS BUG] 404 error returned Content-Type: {ct!r} instead of JSON. "
            f"Error body: {resp.text[:100]}"
        )
        # Also verify it's parseable
        try:
            resp.json()
        except Exception:
            pytest.fail(
                f"[BONUS BUG] 404 response body is not valid JSON: {resp.text[:200]}"
            )


# ─────────────────────────────────────────────────────────────────
# Bonus #3 — Pagination Edge Cases
# ─────────────────────────────────────────────────────────────────

class TestPagination:

    def test_limit_zero(self, http, auth_token):
        """limit=0 must either return empty list or 400 — not crash."""
        resp = http.get(
            f"{BASE_URL}/api/videos?limit=0",
            headers=base_headers(auth_token),
        )
        assert resp.status_code != 500, (
            f"[BONUS BUG] limit=0 caused a 500 server error. "
            f"No guard against zero-limit pagination."
        )
        if resp.status_code == 200:
            data = resp.json()
            videos = data if isinstance(data, list) else (
                data.get("videos") or data.get("data") or []
            )
            assert isinstance(videos, list), "Response is not a list"

    def test_limit_negative(self, http, auth_token):
        """limit=-1 must not crash or return all records."""
        resp = http.get(
            f"{BASE_URL}/api/videos?limit=-1",
            headers=base_headers(auth_token),
        )
        assert resp.status_code != 500, (
            f"[BONUS BUG] limit=-1 caused a server crash."
        )

    def test_limit_string(self, http, auth_token):
        """limit=abc (non-integer) must return 400, not crash."""
        resp = http.get(
            f"{BASE_URL}/api/videos?limit=abc",
            headers=base_headers(auth_token),
        )
        assert resp.status_code in (400, 200), (
            f"[BONUS BUG] limit=abc returned {resp.status_code}."
        )
        assert resp.status_code != 500, (
            f"[BONUS BUG] Non-integer limit caused a 500 server crash."
        )


# ─────────────────────────────────────────────────────────────────
# Bonus #4 — Type Safety in IDs
# ─────────────────────────────────────────────────────────────────

class TestTypeCoercion:

    def test_numeric_video_id_handled_safely(self, http, auth_token):
        """
        GET /api/videos/0 (integer-like ID) must return 404, not a crash.
        If the backend does `WHERE id = int(id)` without null checking, this breaks.
        """
        for fake_int_id in ["0", "-1", "999999999", "1.5"]:
            resp = http.get(
                f"{BASE_URL}/api/videos/{fake_int_id}",
                headers=base_headers(auth_token),
            )
            assert resp.status_code in (404, 400), (
                f"[BONUS BUG] GET /api/videos/{fake_int_id} returned "
                f"{resp.status_code}. Expected 404/400, not a crash."
            )
            assert resp.status_code != 500, (
                f"[BONUS BUG] 🐛 Integer-like ID '{fake_int_id}' caused a 500 crash."
            )

    def test_special_chars_in_video_id(self, http, auth_token):
        """
        URL-encoded special characters in video ID must not crash the server.
        """
        for bad_id in ["%00", "null", "undefined", "../admin"]:
            resp = http.get(
                f"{BASE_URL}/api/videos/{bad_id}",
                headers=base_headers(auth_token),
            )
            assert resp.status_code != 500, (
                f"[BONUS BUG] 🐛 Special ID '{bad_id}' caused a 500 crash. "
                f"Possible path traversal or null byte injection vulnerability."
            )
