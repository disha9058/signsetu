"""
test_03_repeatability_and_data_integrity.py
──────────────────────────────────────────────
Hunts for bugs that make the test suite fail on the second run.

BUG HYPOTHESES:
  Bug #3 – Ghost records: DELETE succeeds but video still shows up in LIST.
  Bug #4 – Caption pollution: GET /api/captions with no videoId returns ALL captions
            (data from other candidates leaks, or the response explodes on re-runs).
  Bug #5 – Duplicate title rejection: Creating a video with the same title twice
            fails on the second run if unique-title is enforced without cleanup.
"""

import pytest
import requests
from utils.client import BASE_URL, base_headers, poll_for_status


# ─────────────────────────────────────────────────────────────────
# Bug #3 — Ghost Records / Soft Delete
# ─────────────────────────────────────────────────────────────────

class TestDeleteIntegrity:

    def test_deleted_video_returns_404(self, http, auth_token, video):
        """
        After DELETE, GET /api/videos/{id} must return 404.
        If it still returns 200, this is a GHOST RECORD — a soft-delete bug
        that will cause the suite to see stale state on the next run.
        """
        vid = video  # fixture creates and will try to delete, but we delete first
        del_resp = http.delete(
            f"{BASE_URL}/api/videos/{vid}",
            headers=base_headers(auth_token),
        )
        assert del_resp.status_code in (200, 204), (
            f"DELETE itself failed [{del_resp.status_code}]: {del_resp.text}"
        )

        get_resp = http.get(
            f"{BASE_URL}/api/videos/{vid}",
            headers=base_headers(auth_token),
        )
        assert get_resp.status_code == 404, (
            f"[BUG FOUND] 🐛 GHOST RECORD / SOFT DELETE: "
            f"Video {vid} still returned {get_resp.status_code} after DELETE. "
            f"Soft-delete not enforced — record persists indefinitely."
        )

    def test_deleted_video_absent_from_list(self, http, auth_token, video):
        """
        After DELETE, the video must not appear in GET /api/videos listing.
        A ghost in the list is the repeatability trap — rerunning the suite
        finds 'old' videos and causes assertion failures or ID collisions.
        """
        vid = video

        # Delete it
        http.delete(f"{BASE_URL}/api/videos/{vid}", headers=base_headers(auth_token))

        # Check listing
        list_resp = http.get(
            f"{BASE_URL}/api/videos",
            headers=base_headers(auth_token),
        )
        assert list_resp.status_code == 200
        data = list_resp.json()
        videos = data if isinstance(data, list) else (
            data.get("videos") or data.get("data") or []
        )
        ids = [v.get("id") or v.get("videoId") for v in videos]
        assert vid not in ids, (
            f"[BUG FOUND] 🐛 GHOST IN LISTING: Deleted video {vid} still "
            f"appears in GET /api/videos. This breaks suite repeatability."
        )

    def test_delete_is_idempotent(self, http, auth_token, video):
        """
        Deleting the same video twice: second call should return 404, not 500.
        A crash on double-delete is a server robustness bug.
        """
        vid = video
        http.delete(f"{BASE_URL}/api/videos/{vid}", headers=base_headers(auth_token))

        second_del = http.delete(
            f"{BASE_URL}/api/videos/{vid}",
            headers=base_headers(auth_token),
        )
        assert second_del.status_code in (404, 200, 204), (
            f"[BUG FOUND] 🐛 DOUBLE DELETE CRASH: Second DELETE on already-deleted "
            f"video returned {second_del.status_code} — server error on re-delete."
        )
        assert second_del.status_code != 500, (
            f"[BUG FOUND] 🐛 SERVER ERROR on double delete: {second_del.text}"
        )

    def test_captions_deleted_with_video(self, http, auth_token):
        """
        After a video is deleted, its captions must also be gone (404 or empty).
        Orphaned captions are a data integrity bug and a repeatability trap —
        rerunning the suite finds ghost captions from a previous run.
        """
        # Create, process, then delete
        create_resp = http.post(
            f"{BASE_URL}/api/videos",
            json={"title": "Caption Cleanup Test", "url": "https://example.com/t.mp4"},
            headers=base_headers(auth_token),
        )
        assert create_resp.status_code in (200, 201)
        vid_data = create_resp.json()
        vid = (
            vid_data.get("id")
            or vid_data.get("videoId")
            or (vid_data.get("video") or {}).get("id")
        )

        try:
            # Trigger processing
            proc = http.post(
                f"{BASE_URL}/api/videos/{vid}/process-captions",
                headers=base_headers(auth_token),
            )
            assert proc.status_code in (200, 202)

            # Wait for completion
            poll_for_status(http, vid, auth_token, ["completed", "done", "processed"])

            # Now delete the video
            http.delete(f"{BASE_URL}/api/videos/{vid}", headers=base_headers(auth_token))

            # Captions for this video should no longer be accessible
            cap_resp = http.get(
                f"{BASE_URL}/api/captions?videoId={vid}",
                headers=base_headers(auth_token),
            )
            if cap_resp.status_code == 200:
                cap_data = cap_resp.json()
                text = (
                    cap_data.get("captions")
                    or cap_data.get("text")
                    or cap_data.get("content")
                    or ""
                )
                assert not text, (
                    f"[BUG FOUND] 🐛 ORPHANED CAPTIONS: Captions for deleted video "
                    f"{vid} still returned data after video deletion. "
                    f"CASCADE DELETE not implemented."
                )
            else:
                assert cap_resp.status_code == 404, (
                    f"Expected 404 for captions of deleted video, got {cap_resp.status_code}"
                )
        finally:
            # Safety net
            http.delete(f"{BASE_URL}/api/videos/{vid}", headers=base_headers(auth_token))


# ─────────────────────────────────────────────────────────────────
# Bug #4 — Caption Data Leakage
# ─────────────────────────────────────────────────────────────────

class TestCaptionDataLeakage:

    def test_captions_without_video_id_rejected(self, http, auth_token):
        """
        GET /api/captions without ?videoId must NOT dump all captions for all users.
        This is a data leakage bug — other candidates' data could be exposed.
        Expected: 400 (bad request) or 404, never 200 with a big payload.
        """
        resp = http.get(
            f"{BASE_URL}/api/captions",
            headers=base_headers(auth_token),
        )
        if resp.status_code == 200:
            data = resp.json()
            # If it returns data, check it's not a list of all captions
            assert not isinstance(data, list) or len(data) == 0, (
                f"[BUG FOUND] 🐛 DATA LEAKAGE: GET /api/captions without videoId "
                f"returned {len(data)} caption records — ALL captions exposed. "
                f"This leaks other users' data and is a GDPR / privacy violation."
            )
        else:
            # 400/404 is correct behavior
            assert resp.status_code in (400, 404), (
                f"Unexpected status {resp.status_code} for captions without videoId"
            )

    def test_captions_for_nonexistent_video(self, http, auth_token):
        """
        GET /api/captions?videoId=nonexistent should return 404, not 500 or 200.
        """
        resp = http.get(
            f"{BASE_URL}/api/captions?videoId=totally-nonexistent-xyz-99999",
            headers=base_headers(auth_token),
        )
        assert resp.status_code in (404, 400), (
            f"[BUG FOUND] 🐛 INVALID VIDEO ID HANDLING: Captions endpoint returned "
            f"{resp.status_code} for a non-existent video ID (expected 404)."
        )
        assert resp.status_code != 500, (
            f"[BUG FOUND] 🐛 SERVER CRASH: 500 error on captions for bad video ID."
        )


# ─────────────────────────────────────────────────────────────────
# Bug #5 — Duplicate / Unique Constraint Issues
# ─────────────────────────────────────────────────────────────────

class TestDuplicateHandling:

    def test_creating_same_title_twice_is_safe(self, http, auth_token):
        """
        Creating two videos with the same title must either:
          (a) succeed (titles are not unique keys), OR
          (b) return a clean 409 Conflict (not a 500 crash).
        If a 500 is returned, it's a bug — uncaught unique constraint violation.
        This is also a repeatability trap: first run succeeds, second run crashes
        because a record with that title already exists from the first run.
        """
        ids_to_clean = []
        try:
            for i in range(2):
                resp = http.post(
                    f"{BASE_URL}/api/videos",
                    json={
                        "title": "Duplicate Title Test — Static Name",
                        "url":   "https://example.com/dupe.mp4",
                    },
                    headers=base_headers(auth_token),
                )
                if resp.status_code in (200, 201):
                    vid = (
                        resp.json().get("id")
                        or resp.json().get("videoId")
                        or (resp.json().get("video") or {}).get("id")
                    )
                    if vid:
                        ids_to_clean.append(vid)
                elif resp.status_code == 409:
                    pass  # Acceptable — conflict returned cleanly
                else:
                    pytest.fail(
                        f"[BUG FOUND] 🐛 DUPLICATE TITLE CRASH: Second creation with "
                        f"same title returned {resp.status_code}: {resp.text[:300]}. "
                        f"This is the repeatability trap — 2nd run will always fail."
                    )
        finally:
            for vid in ids_to_clean:
                http.delete(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )

    def test_nonexistent_video_returns_404_not_500(self, http, auth_token):
        """GET /api/videos/{bad_id} must return 404, not 500."""
        resp = http.get(
            f"{BASE_URL}/api/videos/i-do-not-exist-12345",
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 404, (
            f"[BUG FOUND] 🐛 WRONG STATUS CODE: GET on unknown video returned "
            f"{resp.status_code} instead of 404. "
            + (
                "Server may be crashing instead of handling gracefully."
                if resp.status_code == 500 else ""
            )
        )
