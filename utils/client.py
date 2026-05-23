"""
utils/client.py — SignSetu QA Test Client
==========================================
Handles auth, headers, polling, and shared constants.

Key design decisions:
  - CANDIDATE_ID is read from env. Set it uniquely per run:
        export SIGNSETU_CANDIDATE_ID="DISHA_$(date +%s)"
  - authenticate() accepts BOTH 200 and 201. The API returns 201 for auth
    (minor API design bug — auth is not resource creation — but we treat it
    as a known quirk, not a test failure).
  - poll_for_status() does NOT hard-assert on transient errors; it retries.
"""

import base64
import logging
import os
import time

import requests

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_URL     = os.getenv("SIGNSETU_BASE_URL",     "https://qa-testing-navy.vercel.app")
CANDIDATE_ID = os.getenv("SIGNSETU_CANDIDATE_ID", "disha-pragati-2025")

POLL_INTERVAL = 2     # seconds between status polls
POLL_TIMEOUT  = 60    # max seconds to wait for async jobs

# Auth quirk: the API returns 201 Created instead of 200 OK on a successful
# login. Both are treated as success; the 201 quirk is documented in BUGS.md.
AUTH_SUCCESS_CODES = (200, 201)

log = logging.getLogger(__name__)


# ── Header factory ─────────────────────────────────────────────────────────────
def base_headers(token: str | None = None, candidate_id: str | None = None) -> dict:
    """
    Return the mandatory headers for every API request.
    candidate_id defaults to the global CANDIDATE_ID env var.
    Pass candidate_id='' to intentionally omit X-Candidate-ID (for auth tests).
    """
    h = {"Content-Type": "application/json"}

    cid = candidate_id if candidate_id is not None else CANDIDATE_ID
    if cid:
        h["X-Candidate-ID"] = cid

    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── Auth helper ────────────────────────────────────────────────────────────────
def authenticate(session: requests.Session, candidate_id: str | None = None) -> str:
    """POST /api/auth → returns the session token string."""
    resp = session.post(
        f"{BASE_URL}/api/auth",
        json={"username": "testuser", "password": "testpassword"},
        headers=base_headers(candidate_id=candidate_id),
    )
    assert resp.status_code in AUTH_SUCCESS_CODES, (
        f"Auth failed [{resp.status_code}]: {resp.text}\n"
        f"  If you see 409 StateCollision, your CANDIDATE_ID is already active.\n"
        f"  Fix: export SIGNSETU_CANDIDATE_ID=\"DISHA_$(date +%s)\""
    )
    data  = resp.json()
    token = data.get("token") or data.get("access_token") or data.get("sessionToken")
    assert token, f"Auth succeeded but no token in response: {data}"
    log.info("Auth OK (HTTP %s) token=%s…", resp.status_code, token[:12])
    return token


# ── Token analyser (for weak-token tests) ─────────────────────────────────────
def decode_token(token: str) -> dict:
    """
    Attempt to decode the token and determine whether it is predictable.

    The live API returns base64(unix_timestamp_ms), e.g.:
      MTc3OTU0Mjc3NjM4MQ==  →  "1779542776381"  (a millisecond timestamp)

    A cryptographically secure token would be random bytes that do NOT decode
    to a plain decimal integer.

    Returns a dict with:
      raw       – decoded bytes as string (best-effort)
      is_numeric – True if the decoded value is a plain integer (predictable)
      entropy_ok – True if the token looks random (long hex/UUID, NOT numeric)
    """
    result = {"raw": "", "is_numeric": False, "entropy_ok": True}
    try:
        # Pad if needed
        padded  = token + "=" * (-len(token) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        result["raw"]       = decoded
        result["is_numeric"] = decoded.strip().isdigit()
        result["entropy_ok"] = not result["is_numeric"]
    except Exception as exc:
        log.debug("Token decode failed (may be opaque/encrypted): %s", exc)
        result["entropy_ok"] = True   # can't decode → not obviously predictable
    return result


# ── Async polling ──────────────────────────────────────────────────────────────
def poll_for_status(
    session:         requests.Session,
    video_id:        str,
    token:           str,
    target_statuses: list[str],
    timeout:         int = POLL_TIMEOUT,
) -> dict:
    """
    Poll GET /api/videos/{id} until status ∈ target_statuses or timeout.
    Returns the final video dict. Raises TimeoutError on timeout.
    """
    deadline   = time.monotonic() + timeout
    last_data  = {}
    poll_count = 0

    while time.monotonic() < deadline:
        poll_count += 1
        try:
            resp = session.get(
                f"{BASE_URL}/api/videos/{video_id}",
                headers=base_headers(token),
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Poll #%d network error (retrying): %s", poll_count, exc)
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            log.warning("Poll #%d: %s returned %s", poll_count, video_id, resp.status_code)
            time.sleep(POLL_INTERVAL)
            continue

        last_data = resp.json()
        status    = (last_data.get("status") or last_data.get("processingStatus") or "").lower()
        log.debug("Poll #%d: video=%s status=%r", poll_count, video_id, status)

        if status in [s.lower() for s in target_statuses]:
            return last_data

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Video {video_id!r} never reached {target_statuses} "
        f"within {timeout}s ({poll_count} polls). Last: {last_data}"
    )
