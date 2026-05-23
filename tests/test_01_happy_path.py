"""
test_01_happy_path.py
─────────────────────
Validates the full documented lifecycle:
  Auth → Create → Process → Poll → Fetch Captions → Delete

This is the baseline run that must pass on EVERY invocation (repeatable check).
"""

import pytest
import requests
from utils.client import BASE_URL, base_headers, authenticate, poll_for_status


def test_full_lifecycle_is_repeatable(http, auth_token):
    """
    Execute the complete workflow and assert correctness of every response.
    Running this test twice in a row must both pass — that's the repeatability trap.
    """
    token = auth_token
    video_id = None

    try:
        # ── 1. Create video ───────────────────────────────────────────────
        create_resp = http.post(
            f"{BASE_URL}/api/videos",
            json={
                "title": "E2E Lifecycle Test",
                "url":   "https://example.com/sample.mp4",
            },
            headers=base_headers(token),
        )
        assert create_resp.status_code in (200, 201), (
            f"[BUG?] Create returned {create_resp.status_code}: {create_resp.text}"
        )
        video_data = create_resp.json()
        video_id = (
            video_data.get("id")
            or video_data.get("videoId")
            or (video_data.get("video") or {}).get("id")
        )
        assert video_id, f"No ID in creation response: {video_data}"

        initial_status = (
            video_data.get("status")
            or (video_data.get("video") or {}).get("status")
            or ""
        ).lower()
        assert initial_status in ("pending", "created", "queued", ""), (
            f"[BUG?] Unexpected initial status after creation: {initial_status!r}"
        )

        # ── 2. GET the video to confirm it exists ─────────────────────────
        get_resp = http.get(
            f"{BASE_URL}/api/videos/{video_id}",
            headers=base_headers(token),
        )
        assert get_resp.status_code == 200
        assert get_resp.json().get("id") == video_id or \
               get_resp.json().get("videoId") == video_id, (
            "GET /videos/{id} returned a different video ID than created"
        )

        # ── 3. Trigger caption processing ────────────────────────────────
        process_resp = http.post(
            f"{BASE_URL}/api/videos/{video_id}/process-captions",
            headers=base_headers(token),
        )
        assert process_resp.status_code in (200, 202), (
            f"[BUG?] process-captions returned {process_resp.status_code}: {process_resp.text}"
        )

        # ── 4. Poll until processing completes ───────────────────────────
        final_video = poll_for_status(
            http, video_id, token,
            target_statuses=["completed", "done", "processed"],
        )
        final_status = (final_video.get("status") or "").lower()
        assert final_status in ("completed", "done", "processed"), (
            f"[BUG?] Final status after processing: {final_status!r}"
        )

        # ── 5. Fetch captions ─────────────────────────────────────────────
        cap_resp = http.get(
            f"{BASE_URL}/api/captions?videoId={video_id}",
            headers=base_headers(token),
        )
        assert cap_resp.status_code == 200, (
            f"[BUG?] Captions fetch returned {cap_resp.status_code}: {cap_resp.text}"
        )
        cap_data = cap_resp.json()

        # Captions must be non-empty after processing
        caption_text = (
            cap_data.get("captions")
            or cap_data.get("text")
            or cap_data.get("content")
            or ""
        )
        assert caption_text, (
            f"[BUG?] No captions returned after successful processing: {cap_data}"
        )

        # ── 6. Delete the video ───────────────────────────────────────────
        del_resp = http.delete(
            f"{BASE_URL}/api/videos/{video_id}",
            headers=base_headers(token),
        )
        assert del_resp.status_code in (200, 204), (
            f"[BUG?] DELETE returned {del_resp.status_code}: {del_resp.text}"
        )

        # ── 7. Verify delete actually worked (repeatability trap!) ────────
        ghost_resp = http.get(
            f"{BASE_URL}/api/videos/{video_id}",
            headers=base_headers(token),
        )
        assert ghost_resp.status_code == 404, (
            f"[BUG FOUND] 🐛 GHOST RECORD: Video {video_id} still accessible "
            f"after DELETE (status {ghost_resp.status_code}). "
            f"This breaks repeatability — next run will find stale data."
        )
        video_id = None  # Don't double-delete in finally

    finally:
        if video_id:
            http.delete(
                f"{BASE_URL}/api/videos/{video_id}",
                headers=base_headers(token),
            )


def test_list_videos_returns_array(http, auth_token):
    """GET /api/videos must return a list (not a dict, not null)."""
    resp = http.get(
        f"{BASE_URL}/api/videos",
        headers=base_headers(auth_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    # Could be {"videos": [...]} or a raw list
    videos = data if isinstance(data, list) else (
        data.get("videos") or data.get("data") or []
    )
    assert isinstance(videos, list), (
        f"[BUG?] GET /api/videos did not return a list: {type(data)}"
    )


def test_list_videos_limit_param(http, auth_token):
    """?limit query param must be respected."""
    resp = http.get(
        f"{BASE_URL}/api/videos?limit=1",
        headers=base_headers(auth_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    videos = data if isinstance(data, list) else (
        data.get("videos") or data.get("data") or []
    )
    assert len(videos) <= 1, (
        f"[BUG?] limit=1 returned {len(videos)} videos — limit param ignored"
    )
