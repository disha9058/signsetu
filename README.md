# SignSetu QA — Video Caption Processing Pipeline

**Candidate:** DISHA9058,"DISHA_$(date +%s)" | **Role:** QA Analyst Intern  
**Framework:** Python 3.11+ · Pytest · Requests

---

## Quick Start

```bash
# 1. Create virtualenv and install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Set a UNIQUE candidate ID for every run (IMPORTANT — see below)
export SIGNSETU_CANDIDATE_ID="DISHA_$(date +%s)"

# 3. Run the full suite
pytest -v

# 4. Run with HTML report
pytest -v --html=report.html --self-contained-html
```

> ⚠️ **Why the timestamp suffix?** The API enforces one active session per
> `X-Candidate-ID`. If you run the suite twice with the same ID, the second
> auth call returns `409 StateCollision`. Always use a fresh ID per run.

---

## Project Structure

```
signsetu-qa/
├── conftest.py                              # Shared fixtures (auth, video lifecycle)
├── pytest.ini                               # Pytest config
├── requirements.txt
├── BUGS.md                                  # All confirmed bugs with evidence
├── utils/
│   ├── client.py                            # HTTP client, auth, token analyser, poller
│   └── __init__.py
└── tests/
    ├── test_01_happy_path.py                # Full E2E lifecycle + repeatability check
    ├── test_02_auth_bugs.py                 # Auth bypass, predictable token, header checks
    ├── test_03_repeatability_and_data_integrity.py  # Ghost records, orphaned captions
    ├── test_04_input_validation.py          # Missing fields, injection, DoS
    ├── test_05_async_and_caption_integrity.py       # Async timing, caption quality
    └── test_06_bonus_edge_cases.py          # Method enforcement, pagination, type safety
```

---

## Testing Strategy

### 1. Async Caption Processing

Caption processing is asynchronous — `POST /process-captions` starts a job but
results aren't ready until `status == completed`. We handle this with an **adaptive
poller** (`utils/client.py → poll_for_status()`):

```
trigger process-captions
    ↓
loop every 2s (max 60s):
    GET /api/videos/{id}
    if status ∈ {completed, done, processed} → proceed
    if status ∈ {failed} → fail the test with clear message
    if timeout → TimeoutError (itself surfaces the "infinite pending" bug)
```

The poller does NOT hard-assert on transient errors — a single 5xx during polling
is logged and retried, preventing flaky failures from network blips.

### 2. Suite Repeatability

Every test is **fully isolated** — no shared mutable state between runs:

| Mechanism | Detail |
|-----------|--------|
| **Fixture teardown** | Every `video` fixture deletes itself in `finally:`, even on test failure |
| **Ghost record assertion** | After DELETE, we explicitly assert `GET → 404` |
| **Unique candidate ID per run** | `DISHA_$(date +%s)` prevents StateCollision |
| **try/finally in tests** | Tests that create their own videos always clean up |
| **Double-delete test** | Asserts idempotent DELETE to prevent teardown crashes |

### 3. StateCollision Avoidance

The API allows only **one active session per X-Candidate-ID**. Our suite:
- Authenticates **once** (session-scoped `auth_token` fixture)
- Tests that need a fresh auth call (`test_wrong_credentials_rejected`) use a
  throwaway `THROWAWAY_{timestamp}` candidate ID that never conflicts
- The "predictable token" test decodes the existing token using `decode_token()`
  instead of making a second auth call

---

## Bugs Found (Summary)

See `BUGS.md` for full evidence and impact analysis.

| # | Bug | Severity | Test |
|---|-----|----------|------|
| 1 | **Auth Bypass** — all endpoints work without any token | 🔴 Critical | `test_02::TestAuthBypass` |
| 2 | **Predictable Token** — `base64(unix_timestamp_ms)` | 🔴 Critical | `test_02::test_token_is_not_predictable_timestamp` |
| 3 | **No Token Validation** — any fake string accepted as a token | 🔴 Critical | `test_02::test_invalid_token_is_rejected` |
| 4 | **Ghost Records** — DELETE doesn't hard-delete; records persist | 🔴 Critical | `test_03::TestDeleteIntegrity` |
| 5 | **Orphaned Captions** — captions survive video deletion | 🔴 Critical | `test_03::test_captions_deleted_with_video` |
| 6 | **Hardcoded Captions** — same text returned for every video | 🔴 Critical | `test_05::test_captions_differ_between_videos` |
| B1 | Missing input validation (empty/null fields accepted) | 🟡 Medium | `test_04::TestMissingFields` |
| B2 | Auth returns `201 Created` instead of `200 OK` | 🟡 Low | documented in conftest |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNSETU_BASE_URL` | `https://qa-testing-navy.vercel.app` | API base URL |
| `SIGNSETU_CANDIDATE_ID` | `disha-pragati-2025` | **Set this to a unique value per run** |

---

## Running Subsets

```bash
# Only happy path
pytest tests/test_01_happy_path.py -v

# Only auth bugs
pytest tests/test_02_auth_bugs.py -v

# Only injection tests
pytest tests/test_04_input_validation.py::TestInjection -v

# Repeatability check — run the suite twice and compare
pytest && echo "--- RUN 2 ---" && pytest
```
