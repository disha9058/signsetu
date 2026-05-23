"""
conftest.py — Shared pytest fixtures for the SignSetu QA suite.

Architecture:
  http            (session-scoped)  One requests.Session for the whole run.
  auth_token      (session-scoped)  Authenticate once; token reused everywhere.
  video           (function-scoped) Create a video before test; delete after.
  processed_video (function-scoped) Create + process + poll; delete after.

Repeatability contract:
  Every fixture that creates API state ALSO tears it down (even on failure).
  The suite must produce identical results on run #1, run #2, run #N.
  Guarantee: use a unique SIGNSETU_CANDIDATE_ID per run:
    export SIGNSETU_CANDIDATE_ID="DISHA_$(date +%s)"
"""

import logging

import pytest
import requests

from utils.client import BASE_URL, base_headers, authenticate, poll_for_status

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("conftest")


# ── Session-level fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def http():
    """One persistent requests.Session shared by all tests (connection pooling)."""
    with requests.Session() as session:
        log.info("HTTP session opened")
        yield session
    log.info("HTTP session closed")


@pytest.fixture(scope="session")
def auth_token(http):
    """
    Authenticate ONCE for the entire pytest session and return the token.

    Session-scoped to avoid StateCollision (409): the API only allows one
    active session per X-Candidate-ID. Authenticating once and sharing the
    token across all tests is both correct and required.
    """
    log.info("Authenticating (session-scoped)…")
    token = authenticate(http)
    log.info("auth_token acquired: %s…", token[:12])
    return token


# ── Private helpers ────────────────────────────────────────────────────────────

def _create_video(
    http:  requests.Session,
    token: str,
    title: str = "Automated Test Video",
    url:   str = "https://example.com/test.mp4",
) -> tuple[str, dict]:
    resp = http.post(
        f"{BASE_URL}/api/videos",
        json={"title": title, "url": url},
        headers=base_headers(token),
    )
    assert resp.status_code in (200, 201), (
        f"Video creation failed [{resp.status_code}]: {resp.text}"
    )
    data = resp.json()
    vid  = (
        data.get("id")
        or data.get("videoId")
        or (data.get("video") or {}).get("id")
    )
    assert vid, f"No video ID in creation response: {data}"
    log.info("Created video id=%s title=%r", vid, title)
    return vid, data


def _delete_video(http: requests.Session, token: str, video_id: str) -> None:
    """Best-effort delete — intentionally does NOT assert (used in teardown)."""
    try:
        resp = http.delete(
            f"{BASE_URL}/api/videos/{video_id}",
            headers=base_headers(token),
        )
        log.info("Deleted video %s (HTTP %s)", video_id, resp.status_code)
    except Exception as exc:
        log.warning("Teardown DELETE for %s raised: %s", video_id, exc)


# ── Function-level fixtures ────────────────────────────────────────────────────

@pytest.fixture()
def video(http, auth_token):
    """Create a fresh video before the test; delete it unconditionally after."""
    vid, _ = _create_video(http, auth_token)
    yield vid
    _delete_video(http, auth_token, vid)


@pytest.fixture()
def processed_video(http, auth_token):
    """
    Full lifecycle fixture: create → trigger → poll until complete → yield → delete.
    Use when a test needs captions to already exist.
    """
    vid, _ = _create_video(http, auth_token, title="Processed Test Video")

    proc = http.post(
        f"{BASE_URL}/api/videos/{vid}/process-captions",
        headers=base_headers(auth_token),
    )
    assert proc.status_code in (200, 202), (
        f"process-captions failed [{proc.status_code}]: {proc.text}"
    )
    log.info("Triggered processing for %s", vid)

    poll_for_status(http, vid, auth_token, ["completed", "done", "processed"])
    log.info("Video %s reached completed state", vid)

    yield vid
    _delete_video(http, auth_token, vid)
