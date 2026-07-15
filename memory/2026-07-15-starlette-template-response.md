# Starlette template response deployment regression

- **Symptom:** The production container returned HTTP 500 for `GET /login` and all other HTML routes.
- **Root cause:** The image installed FastAPI 0.139.0 / Starlette 1.3.1. Starlette 1.3.1 removed the legacy `TemplateResponse(name, context)` positional compatibility path, so the context dictionary was interpreted as the template name and Jinja raised `TypeError: unhashable type: 'dict'`. Local Starlette 0.52.1 still accepted the deprecated form, which hid the deployment-only regression.
- **Fix:** Converted all 29 production router calls to `TemplateResponse(request, name, context)`.
- **Regression test:** `tests/test_template_response_compat.py` rejects legacy calls in production routers and renders a real request-first route.
- **Evidence:** The request-first route rendered successfully under Starlette 1.3.1; the local full suite passed with 151 tests.
- **Related:** This was dependency/API drift exposed by an unbounded major Starlette upgrade in the Docker build.
- **Status:** DONE
