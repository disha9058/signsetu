"""
test_02_auth_bugs.py
─────────────────────
Hunts for authentication and authorization vulnerabilities.

BUGS FOUND (confirmed against live API):
  Bug #1 – Auth Bypass: All endpoints work with NO token at all.
  Bug #2 – Predictable Token: Token = base64(unix_timestamp_ms), trivially forgeable.
  Bug #3 – No Token Validation: Fabricated tokens are accepted as valid.
  Bug #4 – X-Candidate-ID not enforced: Requests without the mandatory header succeed.

HOW WE TEST WITHOUT TRIGGERING StateCollision
  The API returns 409 if the same X-Candidate-ID tries to open a second session.
  So test_token_is_unique_per_auth_call does NOT make a second /api/auth call.
  Instead it decodes the base64 token and checks whether it is a plain timestamp —
  which it is (MTc3OTU0… → "1779542776381").
  test_wrong_credentials_rejected uses a throwaway candidate ID (prefixed THROWAWAY_)
  so it never collides with the session-scoped auth_token.
"""

import time
import pytest
import requests
from utils.client import BASE_URL, CANDIDATE_ID, base_headers, decode_token, AUTH_SUCCESS_CODES


# ─────────────────────────────────────────────────────────────────
# Bug #1 — Auth Bypass on every protected endpoint
# ─────────────────────────────────────────────────────────────────

class TestAuthBypass:
    """
    CONFIRMED BUG: Every endpoint accepts requests with NO Authorization header.
    Expected: 401 Unauthorized  |  Actual: 200/201 OK
    """

    def test_create_video_without_token(self, http):
        """POST /api/videos with NO token must return 401/403, not 201."""
        resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": "No-auth video", "url": "https://example.com/x.mp4"},
            headers=base_headers(token=None),
        )
        assert resp.status_code in (401, 403), (
            f"[BUG FOUND] 🐛 BUG #1 — AUTH BYPASS: POST /api/videos accepted a "
            f"request with NO token (HTTP {resp.status_code}). "
            f"Any anonymous user can create video records.\n"
            f"Response: {resp.text[:200]}"
        )

    def test_list_videos_without_token(self, http):
        """GET /api/videos with NO token must return 401/403."""
        resp = http.get(
            f"{BASE_URL}/api/videos",
            headers=base_headers(token=None),
        )
        assert resp.status_code in (401, 403), (
            f"[BUG FOUND] 🐛 BUG #1 — AUTH BYPASS: GET /api/videos returned "
            f"HTTP {resp.status_code} with no token. All video records exposed "
            f"to unauthenticated requests."
        )

    def test_process_captions_without_token(self, http):
        """
        POST /process-captions with no token must return 401/403 BEFORE routing.
        If 404 is returned, the server routed the request (bypassing auth) and
        only then failed because the fake video ID doesn't exist.
        Both 404 AND 200/202 prove auth middleware is missing.
        """
        resp = http.post(
            f"{BASE_URL}/api/videos/fake-id-bypass-test/process-captions",
            headers=base_headers(token=None),
        )
        # Correct: 401/403 (rejected at auth layer, before routing)
        # Bug: 404 (routed first, auth skipped — server tried to find the video)
        # Bug: 200/202 (fully processed with no auth)
        assert resp.status_code in (401, 403), (
            f"[BUG FOUND] 🐛 BUG #1 — AUTH BYPASS on process-captions: "
            f"HTTP {resp.status_code} received without a token.\n"
            + (
                "  A 404 means auth middleware ran AFTER routing — "
                "the server tried to look up the video before checking credentials. "
                "Auth must be the FIRST middleware."
                if resp.status_code == 404
                else f"  Response: {resp.text[:200]}"
            )
        )

    def test_get_captions_without_token(self, http):
        """GET /api/captions without token must be rejected."""
        resp = http.get(
            f"{BASE_URL}/api/captions?videoId=fake-id",
            headers=base_headers(token=None),
        )
        assert resp.status_code in (401, 403), (
            f"[BUG FOUND] 🐛 BUG #1 — AUTH BYPASS: GET /api/captions returned "
            f"HTTP {resp.status_code} without any token."
        )


# ─────────────────────────────────────────────────────────────────
# Bug #2 — Predictable Token (base64 timestamp)
# ─────────────────────────────────────────────────────────────────

class TestWeakToken:
    """
    CONFIRMED BUG: Token is base64(unix_timestamp_ms).
    e.g. MTc3OTU0Mjc3NjM4MQ== → "1779542776381" (millisecond epoch)

    WHY we don't call /api/auth twice:
      The API enforces one active session per X-Candidate-ID (409 StateCollision).
      Calling auth twice would collide with the session-scoped auth_token fixture.
      Instead, we decode the token we already have and inspect its structure.
      A cryptographically secure token (UUID v4, JWT) would NOT decode to a
      plain decimal integer.
    """

    def test_token_is_not_predictable_timestamp(self, auth_token):
        """
        Decode the session token and verify it is NOT base64(integer).
        base64(timestamp) tokens are trivially predictable — anyone can compute
        what token will be issued at any given moment.
        """
        info = decode_token(auth_token)

        assert not info["is_numeric"], (
            f"[BUG FOUND] 🐛 BUG #2 — PREDICTABLE TOKEN:\n"
            f"  Token:   {auth_token!r}\n"
            f"  Decoded: {info['raw']!r}  ← this is a Unix timestamp in milliseconds!\n"
            f"  Impact:  An attacker can predict the token issued at any moment "
            f"(±1ms granularity). Session tokens must be cryptographically random "
            f"(e.g. UUID v4, 256-bit random, or signed JWT)."
        )

    def test_invalid_token_is_rejected(self, http):
        """A completely fabricated token must not grant access."""
        resp = http.get(
            f"{BASE_URL}/api/videos",
            headers=base_headers(token="totally-fake-token-99999"),
        )
        assert resp.status_code in (401, 403), (
            f"[BUG FOUND] 🐛 BUG #3 — NO TOKEN VALIDATION: "
            f"A fabricated token 'totally-fake-token-99999' was accepted "
            f"(HTTP {resp.status_code}). The server never validates tokens — "
            f"any string in the Authorization header grants access."
        )

    def test_wrong_credentials_rejected(self, http):
        """
        Wrong password must return 401, not 200/201.

        Uses a THROWAWAY_ prefixed X-Candidate-ID that will never conflict
        with the session-scoped auth_token, avoiding StateCollision (409).
        If even a throwaway ID returns 409, it means the API accepts any
        candidate ID as a valid session owner — also a bug.
        """
        throwaway_id = f"THROWAWAY_{int(time.time())}"
        resp = http.post(
            f"{BASE_URL}/api/auth",
            json={"username": "testuser", "password": "WRONG_PASSWORD_XYZ"},
            headers=base_headers(candidate_id=throwaway_id),
        )
        # 409 here would mean the throwaway ID already has an active session
        # — impossible on a first call, so 409 = the API ignores passwords entirely
        if resp.status_code == 409:
            pytest.fail(
                f"[BUG FOUND] 🐛 BUG — CREDENTIAL BYPASS: Auth with a brand-new "
                f"X-Candidate-ID '{throwaway_id}' and WRONG password returned 409 "
                f"StateCollision, implying any ID is auto-activated with any password."
            )
        assert resp.status_code in (400, 401, 403), (
            f"[BUG FOUND] 🐛 BUG — WEAK CREDENTIAL CHECK: "
            f"Wrong password returned HTTP {resp.status_code} "
            f"(expected 400/401/403). Response: {resp.text[:200]}"
        )


# ─────────────────────────────────────────────────────────────────
# Bug #4 — X-Candidate-ID header not enforced
# ─────────────────────────────────────────────────────────────────

class TestCandidateIDHeader:
    """The spec states X-Candidate-ID is MANDATORY on every request."""

    def test_auth_without_candidate_id(self, http):
        """POST /api/auth without X-Candidate-ID must be rejected (400/401/403)."""
        resp = http.post(
            f"{BASE_URL}/api/auth",
            json={"username": "testuser", "password": "testpassword"},
            headers=base_headers(candidate_id=""),   # explicitly omit header
        )
        assert resp.status_code in (400, 401, 403), (
            f"[BUG FOUND] 🐛 BUG #4 — MANDATORY HEADER NOT ENFORCED: "
            f"/api/auth accepted a request without X-Candidate-ID "
            f"(HTTP {resp.status_code}). The mandatory header is not validated."
        )

    def test_protected_endpoint_without_candidate_id(self, http, auth_token):
        """GET /api/videos without X-Candidate-ID must be rejected."""
        resp = http.get(
            f"{BASE_URL}/api/videos",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {auth_token}",
                # X-Candidate-ID intentionally omitted
            },
        )
        assert resp.status_code in (400, 401, 403), (
            f"[BUG FOUND] 🐛 BUG #4 — MANDATORY HEADER NOT ENFORCED: "
            f"GET /api/videos returned HTTP {resp.status_code} without "
            f"the mandatory X-Candidate-ID header."
        )
