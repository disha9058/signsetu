# SignSetu API — Confirmed Bugs

All bugs below were found and confirmed by the automated test suite against the live sandbox.

---

## 🔴 Bug #1 — Authentication Bypass (All Endpoints)

**Severity:** Critical  
**Found by:** `test_02_auth_bugs.py::TestAuthBypass`

**What happens:**
```
POST /api/videos   (no Authorization header)  →  201 Created   ✗ (should be 401)
GET  /api/videos   (no Authorization header)  →  200 OK        ✗ (should be 401)
GET  /api/captions (no Authorization header)  →  200 OK        ✗ (should be 401)
POST /api/videos/{id}/process-captions (no token) → 404        ✗ (should be 401)
```

**Impact:** Any anonymous user can read all video records, create new records, and
fetch caption data without ever authenticating. The auth middleware is either missing
or not applied to any routes.

**The 404 on process-captions is also an auth bypass:** The server routed the request
(looked up the video ID) BEFORE checking credentials. Auth must be the first middleware.

---

## 🔴 Bug #2 — Predictable Token (base64 Timestamp)

**Severity:** Critical  
**Found by:** `test_02_auth_bugs.py::TestWeakToken::test_token_is_not_predictable_timestamp`

**What happens:**
```python
token = "MTc3OTU0Mjc3NjM4MQ=="
base64.b64decode(token) == b"1779542776381"   # Unix timestamp in milliseconds
```

The session token is `base64(current_unix_timestamp_ms)`. Any attacker who knows
approximately when a user authenticated can compute their token to within ±1ms.

**Fix:** Use `secrets.token_urlsafe(32)` or a signed JWT with a random `jti`.

---

## 🔴 Bug #3 — No Token Validation (Any String Accepted)

**Severity:** Critical  
**Found by:** `test_02_auth_bugs.py::TestWeakToken::test_invalid_token_is_rejected`

**What happens:**
```
GET /api/videos  Authorization: Bearer totally-fake-token-99999  →  200 OK  ✗
```

The server never validates tokens. Any string in the `Authorization: Bearer` header
grants full access. Combined with Bug #1, this means auth is completely non-functional.

---

## 🔴 Bug #4 — Ghost Records (Soft Delete Without Hard Delete)

**Severity:** Critical  
**Found by:** `test_03_repeatability_and_data_integrity.py::TestDeleteIntegrity`

**What happens:**
```
DELETE /api/videos/{id}   →  200 OK    (looks successful)
GET    /api/videos/{id}   →  200 OK    (should be 404 — ghost record!)
GET    /api/videos        →  [..., {id: deleted_id}, ...]  (ghost in listing!)
```

**This is the repeatability trap:** The test suite passes on run #1, but on run #2
the listing contains ghost records from run #1, causing state assertions to fail.

**Root cause hypothesis:** DELETE sets `deleted=true` but GET queries don't filter it.

---

## 🔴 Bug #5 — Orphaned Captions After Video Deletion

**Severity:** Critical  
**Found by:** `test_03_repeatability_and_data_integrity.py::TestDeleteIntegrity::test_captions_deleted_with_video`

**What happens:**
```
DELETE /api/videos/{id}              →  200 OK
GET    /api/captions?videoId={id}    →  200 OK { captions: "..." }  ✗ (should be 404)
```

Captions for a deleted video remain fully accessible. The DELETE handler does not
CASCADE to the captions table, leaving dangling records.

---

## 🔴 Bug #6 — Hardcoded / Identical Caption Output

**Severity:** Critical  
**Found by:** `test_05_async_and_caption_integrity.py::TestCaptionContentIntegrity::test_captions_differ_between_videos`

**What happens:**
```
Video A → process → GET /api/captions  →  "Hello, welcome to this video..."
Video B → process → GET /api/captions  →  "Hello, welcome to this video..."  ← IDENTICAL
```

The caption engine returns the same hardcoded string for every video.
The system appears functional (status transitions, no errors) but the core
feature produces meaningless output.

---

## 🟡 Bonus Bug — Missing Input Validation

**Severity:** Medium  
**Found by:** `test_04_input_validation.py::TestMissingFields`

```
POST /api/videos  {}              →  201 Created  ✗ (should be 400)
POST /api/videos  {"url": "..."}  →  201 Created  ✗ (missing required 'title')
POST /api/videos  {"title": ""}   →  201 Created  ✗ (empty string accepted)
```

No required-field or type validation on the create-video endpoint.

---

## 🟡 Bonus Bug — Auth Endpoint Returns Wrong Status Code

**Severity:** Low (API design)  
**Found by:** `conftest.py` (documented as known quirk)

```
POST /api/auth  →  201 Created  ✗ (should be 200 OK)
```

Successful authentication returns `201 Created`. Authentication is not resource
creation — it should return `200 OK` with a session token.
