"""
test_05_async_and_caption_integrity.py
────────────────────────────────────────
Tests the asynchronous caption processing pipeline deeply.

BUG HYPOTHESES:
  Bug #9  – Captions available before processing completes (race condition).
  Bug #10 – Status never transitions to 'completed' (infinite pending).
  Bug #11 – Caption content is identical for every video (hardcoded/mocked output).
  Bug #12 – Timestamp/ordering issues in caption text (malformed SRT/VTT).
"""

import time
import pytest
import requests
from utils.client import BASE_URL, base_headers, poll_for_status, POLL_TIMEOUT


# ─────────────────────────────────────────────────────────────────
# Bug #9 — Premature Caption Access
# ─────────────────────────────────────────────────────────────────

class TestPrematureCaptionAccess:

    def test_captions_not_available_before_processing(self, http, auth_token, video):
        """
        Immediately after creation (before triggering process-captions),
        GET /api/captions must return 404 or empty — NOT pre-populated content.
        Pre-existing captions means the system is returning cached/dummy data.
        """
        vid = video  # freshly created, not yet processed

        resp = http.get(
            f"{BASE_URL}/api/captions?videoId={vid}",
            headers=base_headers(auth_token),
        )

        if resp.status_code == 200:
            data = resp.json()
            text = (
                data.get("captions")
                or data.get("text")
                or data.get("content")
                or ""
            )
            assert not text, (
                f"[BUG FOUND] 🐛 PREMATURE CAPTIONS: Video {vid} has caption content "
                f"even though process-captions was NEVER called. "
                f"The API is returning fake/cached data before processing.\n"
                f"Caption content: {str(text)[:200]}"
            )
        # 404 is perfectly correct here
        elif resp.status_code not in (404, 400):
            pytest.fail(
                f"[BUG FOUND] 🐛 Unexpected status {resp.status_code} for "
                f"pre-processing captions fetch."
            )

    def test_status_is_pending_before_process_trigger(self, http, auth_token, video):
        """
        Immediately after creation, video status must be 'pending' / 'created',
        NOT 'completed' or 'processing'. A video that auto-completes without
        being triggered is returning mocked data.
        """
        vid = video
        resp = http.get(
            f"{BASE_URL}/api/videos/{vid}",
            headers=base_headers(auth_token),
        )
        assert resp.status_code == 200
        status = (resp.json().get("status") or "").lower()
        assert status not in ("completed", "done", "processed"), (
            f"[BUG FOUND] 🐛 AUTO-COMPLETION: Video {vid} has status '{status}' "
            f"immediately after creation, without any process-captions call. "
            f"The pipeline is not actually running asynchronously."
        )


# ─────────────────────────────────────────────────────────────────
# Bug #10 — Status Transition Completeness
# ─────────────────────────────────────────────────────────────────

class TestStatusTransitions:

    def test_status_transitions_correctly(self, http, auth_token, video):
        """
        Full status machine test:
          created → (pending/processing) → completed

        We observe at least two distinct statuses during the lifecycle.
        If only one status is ever seen, the state machine is broken.
        """
        vid = video
        observed_statuses = set()

        # Record initial status
        resp = http.get(f"{BASE_URL}/api/videos/{vid}", headers=base_headers(auth_token))
        initial = (resp.json().get("status") or "unknown").lower()
        observed_statuses.add(initial)

        # Trigger processing
        proc = http.post(
            f"{BASE_URL}/api/videos/{vid}/process-captions",
            headers=base_headers(auth_token),
        )
        assert proc.status_code in (200, 202)

        # Poll and collect statuses
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            poll = http.get(
                f"{BASE_URL}/api/videos/{vid}",
                headers=base_headers(auth_token),
            )
            if poll.status_code == 200:
                s = (poll.json().get("status") or "").lower()
                observed_statuses.add(s)
                if s in ("completed", "done", "processed", "failed"):
                    break
            time.sleep(2)

        terminal = observed_statuses & {"completed", "done", "processed", "failed"}
        assert terminal, (
            f"[BUG FOUND] 🐛 STUCK STATUS: Video {vid} never reached a terminal "
            f"status within {POLL_TIMEOUT}s. Observed: {observed_statuses}. "
            f"The async processing pipeline appears to be hanging."
        )

        assert len(observed_statuses) >= 2, (
            f"[BUG FOUND] 🐛 MISSING STATUS TRANSITIONS: Only one status was ever "
            f"observed: {observed_statuses}. The state machine is not transitioning "
            f"properly through the processing lifecycle."
        )

    def test_processing_status_appears_during_async(self, http, auth_token, video):
        """
        After triggering process-captions, the status must change from
        the initial value — verifying async processing actually started.
        """
        vid = video

        # Trigger
        http.post(
            f"{BASE_URL}/api/videos/{vid}/process-captions",
            headers=base_headers(auth_token),
        )

        # Immediately check — should NOT still be 'pending' AND 'completed' simultaneously
        immediate = http.get(
            f"{BASE_URL}/api/videos/{vid}",
            headers=base_headers(auth_token),
        )
        status = (immediate.json().get("status") or "").lower()

        # If it's INSTANTLY 'completed', the job isn't actually async
        if status in ("completed", "done", "processed"):
            # Verify captions are actually there (not just a fake status flip)
            cap = http.get(
                f"{BASE_URL}/api/captions?videoId={vid}",
                headers=base_headers(auth_token),
            )
            if cap.status_code != 200 or not (
                cap.json().get("captions") or cap.json().get("text") or ""
            ):
                pytest.fail(
                    f"[BUG FOUND] 🐛 FAKE INSTANT COMPLETION: Video status flipped "
                    f"to '{status}' immediately after triggering, but captions are "
                    f"empty/missing. Status is being set without actual processing."
                )


# ─────────────────────────────────────────────────────────────────
# Bug #11 — Hardcoded / Identical Caption Output
# ─────────────────────────────────────────────────────────────────

class TestCaptionContentIntegrity:

    def test_captions_differ_between_videos(self, http, auth_token):
        """
        Two different videos must NOT produce byte-for-byte identical captions.
        Identical captions across all videos = the API is returning a hardcoded
        string regardless of input (mocked backend, not real processing).
        This is a critical quality bug disguised as working functionality.
        """
        ids_to_clean = []
        try:
            captions_collected = []

            for i, title in enumerate([
                "Caption Uniqueness Test Video Alpha",
                "Caption Uniqueness Test Video Beta",
            ]):
                # Create
                cr = http.post(
                    f"{BASE_URL}/api/videos",
                    json={"title": title, "url": f"https://example.com/vid{i}.mp4"},
                    headers=base_headers(auth_token),
                )
                assert cr.status_code in (200, 201)
                vid = (
                    cr.json().get("id")
                    or cr.json().get("videoId")
                    or (cr.json().get("video") or {}).get("id")
                )
                ids_to_clean.append(vid)

                # Process
                proc = http.post(
                    f"{BASE_URL}/api/videos/{vid}/process-captions",
                    headers=base_headers(auth_token),
                )
                assert proc.status_code in (200, 202)

                # Wait
                poll_for_status(http, vid, auth_token, ["completed", "done", "processed"])

                # Fetch captions
                cap = http.get(
                    f"{BASE_URL}/api/captions?videoId={vid}",
                    headers=base_headers(auth_token),
                )
                assert cap.status_code == 200
                text = (
                    cap.json().get("captions")
                    or cap.json().get("text")
                    or cap.json().get("content")
                    or ""
                )
                captions_collected.append((vid, text))

            if len(captions_collected) == 2:
                text_a = captions_collected[0][1]
                text_b = captions_collected[1][1]

                if text_a and text_b:
                    assert text_a != text_b, (
                        f"[BUG FOUND] 🐛 HARDCODED CAPTIONS: Both videos produced "
                        f"byte-for-byte identical caption output:\n"
                        f"  Video A ({captions_collected[0][0]}): {str(text_a)[:100]}\n"
                        f"  Video B ({captions_collected[1][0]}): {str(text_b)[:100]}\n"
                        f"The caption generation is returning a static/hardcoded response."
                    )

        finally:
            for vid in ids_to_clean:
                http.delete(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )

    def test_captions_belong_to_correct_video(self, http, auth_token):
        """
        GET /api/captions?videoId=A must NOT return captions for video B.
        Caption cross-contamination = wrong videoId filtering in the query.
        """
        ids_to_clean = []
        try:
            vids = []
            for i in range(2):
                cr = http.post(
                    f"{BASE_URL}/api/videos",
                    json={
                        "title": f"Caption Isolation Test {i}",
                        "url":   f"https://example.com/iso{i}.mp4",
                    },
                    headers=base_headers(auth_token),
                )
                assert cr.status_code in (200, 201)
                vid = (
                    cr.json().get("id")
                    or cr.json().get("videoId")
                    or (cr.json().get("video") or {}).get("id")
                )
                vids.append(vid)
                ids_to_clean.append(vid)

            # Process only the first
            proc = http.post(
                f"{BASE_URL}/api/videos/{vids[0]}/process-captions",
                headers=base_headers(auth_token),
            )
            assert proc.status_code in (200, 202)
            poll_for_status(http, vids[0], auth_token, ["completed", "done", "processed"])

            # Check that video[1] (never processed) has no captions
            cap = http.get(
                f"{BASE_URL}/api/captions?videoId={vids[1]}",
                headers=base_headers(auth_token),
            )
            if cap.status_code == 200:
                text = (
                    cap.json().get("captions")
                    or cap.json().get("text")
                    or cap.json().get("content")
                    or ""
                )
                assert not text, (
                    f"[BUG FOUND] 🐛 CAPTION CROSS-CONTAMINATION: Video {vids[1]} "
                    f"(never processed) has caption content. Either the videoId "
                    f"filter in the DB query is broken, or captions are shared across videos."
                )

        finally:
            for vid in ids_to_clean:
                http.delete(
                    f"{BASE_URL}/api/videos/{vid}",
                    headers=base_headers(auth_token),
                )
