# Viewer logout authorization regression

- **Symptom:** A viewer with a valid session, Origin, and CSRF token received HTTP 403 from `POST /logout`, leaving the session active.
- **Root cause:** The viewer read-only gate rejected every unsafe method before the shared Origin/CSRF checks and logout route could run.
- **Fix:** Allow only the exact viewer `POST /logout` request through the role gate; the common Origin and CSRF validation still applies before session revocation.
- **Regression test:** `tests/test_auth.py::test_viewer_logout_requires_csrf_and_revokes_session` covers invalid Origin, invalid CSRF, successful logout, and old-token rejection.
- **Evidence:** Focused authentication/template tests passed (17 tests), and the complete suite passed (152 tests).
- **Status:** DONE
