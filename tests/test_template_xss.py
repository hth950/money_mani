"""Regression tests for DB/LLM/community strings rendered into HTML sinks."""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from fastapi.templating import Jinja2Templates


ROOT = Path(__file__).parent.parent
SAFE_JS = ROOT / "web" / "static" / "safe_html.js"


class _RenderedMarkupInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[tuple[str, str | None]] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            self.attributes.append((name, value))
            if name == "href" and value is not None:
                self.hrefs.append(value)


def _inline_script(template_name: str, marker: str) -> str:
    source = (ROOT / "web" / "templates" / template_name).read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL)
    block = next(candidate for candidate in blocks if marker in candidate)
    return block.split("// Initial load", 1)[0]


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
    assert all(
        href.startswith(("http://", "https://"))
        or re.fullmatch(r"/[A-Za-z0-9_./%~?=&-]*", href)
        for href in inspector.hrefs
    )
    return inspector


def test_shared_html_helpers_escape_attributes_and_reject_non_http_urls():
    result = _node_json(
        f"""
        const safe = require({json.dumps(str(SAFE_JS))});
        process.stdout.write(JSON.stringify({{
          html: safe.escapeHtml('<img src=x onerror="alert(1)"> & \\'quoted\\''),
          attr: safe.escapeAttr('" autofocus onfocus="alert(1)'),
          javascript: safe.safeHttpUrl('javascript:alert(1)'),
          data: safe.safeHttpUrl('data:text/html,<script>alert(1)</script>'),
          relative: safe.safeHttpUrl('/unsafe-relative'),
          http: safe.safeHttpUrl('http://example.com/a'),
          https: safe.safeHttpUrl('https://example.com/?a=1&b=2')
        }}));
        """
    )
    assert result["html"].startswith("&lt;img")
    assert "&quot;" in result["html"] and "&#39;" in result["html"]
    assert result["attr"].startswith("&quot;")
    assert result["javascript"] == result["data"] == result["relative"] == ""
    assert result["http"].startswith("http://")
    assert result["https"].startswith("https://")


def test_intel_issue_renderer_escapes_stored_llm_and_database_fields():
    payload = {
        "title": '<img src=x onerror="alert(1)">',
        "category": '"><svg onload="alert(2)">',
        "sentiment": "</span><script>alert(3)</script>",
        "confidence": 0.91,
        "summary": "</p><iframe srcdoc='<script>alert(4)</script>'>",
        "affected_tickers": [
            {
                "name": '<img src=x onerror="alert(5)">',
                "ticker": '"><svg onload="alert(6)">',
                "direction": "up",
                "reason": "</small><script>alert(7)</script>",
            }
        ],
        "price_at_detection": {},
        "price_after_1d": {},
        "price_after_3d": {},
        "price_after_5d": {},
        "accuracy_score": 0.75,
        "source_info": '<img src=x onerror="alert(8)">',
        "source_url": "javascript:alert(9)",
    }
    script = _inline_script("intel/index.html", "function renderIssueCard")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {script}
        const markup = renderIssueCard({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    inspector = _assert_no_executable_attacker_markup(markup)
    assert not inspector.hrefs
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup
    assert "javascript:" not in markup.lower()


def test_macro_post_renderer_escapes_content_and_only_links_http_urls():
    payload = {
        "llm_comment": "</p><script>alert(1)</script>",
        "posts_sample": [
            {
                "source": '<img src=x onerror="alert(2)">',
                "title": '"><svg onload="alert(3)">',
                "url": "javascript:alert(4)",
            },
            {
                "source": "community",
                "title": '<img src=x onerror="alert(5)">',
                "url": 'https://example.com/post?a=1&b=" onmouseover="alert(6)',
            },
            "</td><script>alert(7)</script>",
        ],
    }
    script = _inline_script("macro/index.html", "function renderPostsContent")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {script}
        const markup = renderPostsContent({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    inspector = _assert_no_executable_attacker_markup(markup)
    assert len(inspector.hrefs) == 1
    assert inspector.hrefs[0].startswith("https://example.com/")
    assert "javascript:" not in markup.lower()
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup


def test_viewer_strategy_list_renderer_escapes_payload_and_validates_ids():
    templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))
    request = SimpleNamespace(
        state=SimpleNamespace(
            user=SimpleNamespace(username="guest", role="viewer"),
            csrf_token="test-token",
        )
    )
    rendered_page = templates.env.get_template("strategies/list.html").render(request=request)
    assert 'href="/strategies/new"' not in rendered_page

    payload = [
        {
            "id": '"><img src=x onerror="alert(1)">',
            "name": '<img src=x onerror="alert(2)">',
            "category": '"><svg onload="alert(3)">',
            "source": "</td><script>alert(4)</script>",
            "status": 'draft\" onmouseover=\"alert(5)',
            "created_at": "</td><iframe srcdoc=x>",
        },
        {
            "id": 42,
            "name": "safe strategy",
            "category": "momentum",
            "source": "internal",
            "status": "validated_v2",
            "created_at": "2026-07-15T00:00:00",
        },
    ]
    script = _inline_script("strategies/list.html", "function renderStrategies")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        let rendered = '';
        global.document = {{getElementById: () => ({{
          get value() {{ return ''; }},
          set innerHTML(value) {{ rendered = value; }},
          get innerHTML() {{ return rendered; }}
        }})}};
        {script}
        renderStrategies({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup: rendered}}));
        """
    )
    markup = result["markup"]
    inspector = _assert_no_executable_attacker_markup(markup)
    assert inspector.hrefs == ["/strategies/42", "/strategies/42"]
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup


def test_discovery_ranking_renderer_escapes_rankings_json_fields():
    payload = [
        {
            "strategy_name": '<img src=x onerror="alert(1)">',
            "composite_score": '"><svg onload="alert(2)">',
            "avg_return": "</td><script>alert(3)</script>",
            "avg_sharpe": "not-a-number",
            "valid_count": "</td><iframe srcdoc=x>",
        }
    ]
    block = _inline_script("discovery/report_detail.html", "function renderRankings")
    script = block.split("const rankingsPayload", 1)[0]
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {script}
        const markup = renderRankings({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;iframe" in markup
    assert "<svg" not in markup and "<script" not in markup


def test_backtest_result_renderer_escapes_database_fields_and_validates_detail_id():
    payload = [
        {
            "id": '"><img src=x onerror="alert(1)">',
            "strategy_name": '<img src=x onerror="alert(2)">',
            "ticker": '"><svg onload="alert(3)">',
            "market": "</td><script>alert(4)</script>",
            "total_return": 0.1,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.2,
            "win_rate": 0.5,
            "num_trades": "</td><iframe srcdoc=x>",
            "is_valid": True,
            "created_at": "</a><img src=x>",
        },
        {
            "id": 7,
            "strategy_name": "safe",
            "ticker": "AAPL",
            "market": "US",
            "created_at": "2026-07-15T00:00:00",
        },
    ]
    script = _inline_script("backtest/index.html", "function renderBacktestResults")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        global.document = {{getElementById: () => null}};
        {script}
        const markup = renderBacktestResults({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    inspector = _assert_no_executable_attacker_markup(markup)
    assert inspector.hrefs == ["/backtest/7"]
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup


def test_performance_renderers_escape_ticker_strategy_and_report_fields():
    dangerous = '<img src=x onerror="alert(1)">'
    record = {
        "signal_date": "</td><script>alert(2)</script>",
        "ticker_name": dangerous,
        "strategy_name": '"><svg onload="alert(3)">',
        "signal_type": "SELL",
        "signal_price": 100,
        "close_price": 90,
        "pnl_pct": -10,
    }
    report = {
        "report_date": "</td><iframe srcdoc=x>",
        "report_type": "daily",
        "total_signals": dangerous,
        "total_pnl_pct": 2,
        "win_rate": 55,
        "discord_sent": True,
    }
    script = _inline_script("performance/index.html", "function renderPerformanceRecords")
    result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        {script}
        const markup = renderSignalSummary({json.dumps(record)})
          + renderPerformanceRecords([{json.dumps(record)}])
          + renderPerformanceReports([{json.dumps(report)}]);
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    _assert_no_executable_attacker_markup(markup)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup and "&lt;iframe" in markup


def test_owner_discovery_and_scan_history_renderers_escape_job_data():
    dangerous = '<img src=x onerror="alert(1)">'
    discovery_payload = [
        {
            "id": '"><svg onload="alert(2)">',
            "run_date": dangerous,
            "market": "</td><script>alert(3)</script>",
            "videos_found": "</td><iframe srcdoc=x>",
            "strategies_extracted": dangerous,
            "strategies_ranked": dangerous,
            "strategies_validated": dangerous,
        },
        {"id": 9, "run_date": "2026-07-15", "market": "KRX"},
    ]
    discovery_script = _inline_script("discovery/index.html", "function renderDiscoveryReports")
    discovery_result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        global.document = {{getElementById: () => null}};
        {discovery_script}
        const markup = renderDiscoveryReports({json.dumps(discovery_payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )["markup"]
    inspector = _assert_no_executable_attacker_markup(discovery_result)
    assert inspector.hrefs == ["/discovery/9"]
    assert "&lt;img" in discovery_result and "&lt;script" in discovery_result

    scan_payload = [
        {
            "scan_date": dangerous,
            "signals_count": "</td><script>alert(4)</script>",
            "markets_open": '"><svg onload="alert(5)">',
            "created_at": "</td><iframe srcdoc=x>",
        }
    ]
    scan_script = _inline_script("scanner/index.html", "function renderScanHistory")
    scan_result = _node_json(
        f"""
        global.window = globalThis;
        require({json.dumps(str(SAFE_JS))});
        global.document = {{getElementById: () => null}};
        {scan_script}
        const markup = renderScanHistory({json.dumps(scan_payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )["markup"]
    _assert_no_executable_attacker_markup(scan_result)
    assert "&lt;img" in scan_result and "&lt;script" in scan_result and "&lt;svg" in scan_result


def test_signal_card_escapes_viewer_visible_conviction_date_and_price():
    payload = {
        "ticker_name": '<img src=x onerror="alert(1)">',
        "ticker": '"><svg onload="alert(2)">',
        "market": "</span><script>alert(3)</script>",
        "action": "WATCH",
        "composite_score": 0.6,
        "conviction": "</span><iframe srcdoc=x>",
        "last_signal_date": '<img src=x onerror="alert(4)">',
        "signal_price": "</div><script>alert(5)</script>",
        "is_holding": False,
    }
    block = _inline_script("signals/index.html", "function renderCard")
    script = block.split("// Merge exit score data", 1)[0]
    script = re.sub(r"\{\{[\s\S]*?\}\}", "false", script)
    result = _node_json(
        f"""
        global.document = {{createElement: () => {{
          let value = '';
          return {{
            set textContent(next) {{ value = String(next ?? ''); }},
            get innerHTML() {{
              return value.replace(/[&<>\"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}})[ch]);
            }}
          }};
        }}}};
        var _paperPositions = new Map();
        {script}
        const markup = renderCard({json.dumps(payload)});
        process.stdout.write(JSON.stringify({{markup}}));
        """
    )
    markup = result["markup"]
    inspector = _RenderedMarkupInspector()
    inspector.feed(markup)
    assert not ({"img", "script", "svg", "iframe", "object"} & set(inspector.tags))
    event_attributes = {
        (name.lower(), value)
        for name, value in inspector.attributes
        if name.lower().startswith("on")
    }
    assert event_attributes == {
        ("onmouseover", "this.style.boxShadow='0 2px 8px rgba(0,0,0,0.1)'"),
        ("onmouseout", "this.style.boxShadow='none'"),
    }
    assert inspector.hrefs and all(href.startswith("/scoring?ticker=") for href in inspector.hrefs)
    assert "&lt;img" in markup and "&lt;script" in markup and "&lt;svg" in markup and "&lt;iframe" in markup
