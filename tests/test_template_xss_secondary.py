"""Stored-XSS regressions for secondary dashboard renderers."""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parent.parent
SAFE_JS = ROOT / "web" / "static" / "safe_html.js"


class _RenderedMarkupInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


def _rendering_helpers(template_name: str, marker: str) -> str:
    source = (ROOT / "web" / "templates" / template_name).read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL)
    block = next(candidate for candidate in blocks if marker in candidate)
    return block.split("// API data loading", 1)[0]


def _node_json(source: str) -> dict:
    completed = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _assert_no_executable_attacker_markup(markup: str) -> _RenderedMarkupInspector:
    inspector = _RenderedMarkupInspector()
    inspector.feed(markup)
    assert not ({"img", "script", "svg", "iframe", "object"} & set(inspector.tags))
    assert all(not name.lower().startswith("on") for name, _ in inspector.attributes)
    for name, value in inspector.attributes:
        if name.lower() in {"href", "src", "xlink:href", "action", "formaction"}:
            normalized = (value or "").strip().lower()
            assert not normalized.startswith(("javascript:", "data:", "vbscript:"))
    return inspector


def test_secondary_templates_load_shared_html_helpers():
    for template_name in (
        "scoring/index.html",
        "monitor/index.html",
        "portfolio/index.html",
        "dashboard.html",
    ):
        source = (ROOT / "web" / "templates" / template_name).read_text(encoding="utf-8")
        assert '<script src="/static/safe_html.js"></script>' in source


def test_scoring_rows_badges_and_sector_names_escape_database_fields():
    items = [
        {
            "ticker": '<img src=x onerror="alert(1)">',
            "ticker_name": "</td><script>alert(2)</script>",
            "market": '\"><svg onload="alert(3)">',
            "composite_score": 0.71,
            "technical_score": 0.61,
            "fundamental_score": 0.51,
            "flow_score": 0.41,
            "intel_score": 0.31,
            "macro_score": 0.21,
            "decision": 'EXECUTE\" onmouseover="alert(4)"><iframe src=javascript:alert(5)>',
            "is_holding": True,
        }
    ]
    sectors = {
        '<img src=x onerror="alert(6)">': 2,
        "</strong><script>alert(7)</script>": '<a href="javascript:alert(8)">x</a>',
    }
    helpers = _rendering_helpers("scoring/index.html", "function renderScoreRows")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {helpers}
        const rows = renderScoreRows({json.dumps(items)});
        const sectorRows = renderSectorPositions({json.dumps(sectors)});
        process.stdout.write(JSON.stringify({{markup: rows + sectorRows}}));
        """
    )

    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup


def test_monitor_signal_renderer_escapes_sse_payload_fields():
    signal = {
        "signal_type": 'BUY\"><img src=x onerror="alert(1)">',
        "ticker_name": "</strong><script>alert(2)</script>",
        "ticker": '\"><svg onload="alert(3)">',
        "price": 12345.5,
        "strategy_name": '<a href="javascript:alert(4)" onfocus="alert(5)">strategy</a>',
        "timestamp": "</small><iframe srcdoc='<script>alert(6)</script>'>",
    }
    helpers = _rendering_helpers("monitor/index.html", "function renderSignalItem")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {helpers}
        const markup = renderSignalItem({json.dumps(signal)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )

    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup
    assert "12,345.5" in markup


def test_portfolio_renderer_escapes_holdings_and_status_uses_text_content():
    holdings = [
        {
            "ticker": '<img src=x onerror="alert(1)">',
            "name": "</td><script>alert(2)</script>",
            "market": '\"><svg onload="alert(3)">',
            "quantity": '<a href="javascript:alert(4)" onclick="alert(5)">1</a>',
            "avg_price": 1000,
            "current_price": 1250,
            "pnl_pct": 25,
        }
    ]
    status_message = "</p><script>alert(6)</script><img src=x onerror=alert(7)>"
    helpers = _rendering_helpers("portfolio/index.html", "function renderHoldingsTable")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        let statusNode = null;
        global.document = {{
          createElement: () => ({{className: '', textContent: ''}}),
          getElementById: () => ({{replaceChildren: child => {{ statusNode = child; }}}})
        }};
        {helpers}
        const markup = renderHoldingsTable({json.dumps(holdings)});
        updateRefreshStatus({json.dumps(status_message)}, true);
        process.stdout.write(JSON.stringify({{
          markup,
          status: {{className: statusNode.className, textContent: statusNode.textContent}}
        }}));
        """
    )

    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup
    assert "1,000" in markup and "1,250" in markup and "25.00%" in markup
    assert result["status"] == {
        "className": "error",
        "textContent": "오류: " + status_message,
    }


def test_dashboard_recent_signal_renderer_escapes_api_fields():
    signals = [
        {
            "signal_type": '<img src=x onerror="alert(1)">',
            "ticker": "</span><script>alert(2)</script>",
            "ticker_name": '\"><svg onload="alert(3)">',
        },
        {
            "signal_type": "SELL",
            "ticker": '<a href="javascript:alert(4)" onmouseover="alert(5)">BAD</a>',
            "ticker_name": "</span><iframe src=javascript:alert(6)>",
        },
    ]
    helpers = _rendering_helpers("dashboard.html", "function renderRecentSignals")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {helpers}
        const markup = renderRecentSignals({json.dumps(signals)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )

    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup
    assert '<a href="javascript:' not in markup.lower()
