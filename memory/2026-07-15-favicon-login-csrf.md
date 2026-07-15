# Browser login CSRF rotation regression

- **Symptom:** Public HTTPS login failed with HTTP 403 in a real browser while the same login succeeded with curl.
- **Root cause:** Browsers automatically requested `/favicon.ico`. The unauthenticated request redirected to `/login`, rendered a second login page, and rotated the login-CSRF cookie while the original form retained the old hidden token.
- **Fix:** Return a secured empty HTTP 204 response for the exact `/favicon.ico` path before authentication redirects run.
- **Regression test:** `tests/test_auth.py::test_favicon_is_public_without_rotating_login_csrf` reproduces the browser request order, verifies the cookie remains unchanged, and completes login with the original token.
- **Evidence:** Authentication tests passed (16 tests) and the complete suite passed (154 tests).
- **Status:** DONE
