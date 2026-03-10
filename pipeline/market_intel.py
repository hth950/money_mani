"""Market intelligence scanner: LLM + web search for market issue detection."""

import json
import logging
from datetime import datetime, timedelta, timezone

from llm.client import OpenRouterClient
from llm.prompts import MARKET_INTEL_PROMPT
from market_data.fdr_fetcher import FDRFetcher
from market_data.krx_fetcher import KRXFetcher
from pipeline.web_search import WebSearcher
from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.market_intel")

SCAN_TYPE_CONFIG = {
    "pre_market": {
        "label": "장전 브리핑",
        "queries": [
            "한국 주식시장 오늘 이슈",
            "코스피 코스닥 뉴스",
            "주식 시장 전망 오늘",
            "한국 증시 호재 악재",
        ],
    },
    "midday": {
        "label": "장중 업데이트",
        "queries": [
            "오늘 주식시장 이슈",
            "코스피 장중 뉴스",
            "주식 급등 급락 종목",
            "증시 속보 오늘",
        ],
    },
    "post_market": {
        "label": "장후 분석",
        "queries": [
            "오늘 주식시장 마감 분석",
            "코스피 코스닥 마감",
            "기업 공시 실적 발표",
            "외국인 기관 매매 동향",
        ],
    },
    "overnight": {
        "label": "야간 글로벌",
        "queries": [
            "미국 주식시장 뉴스",
            "나스닥 S&P500 동향",
            "글로벌 경제 이슈",
            "환율 원자재 가격",
        ],
    },
}


class MarketIntelScanner:
    """Scan web for market issues using LLM analysis."""

    def __init__(self):
        self.llm = OpenRouterClient()
        self.searcher = WebSearcher()
        self.fdr = FDRFetcher()
        self.krx = KRXFetcher(delay=0.5)
        self._krx_listings = None

    def _get_krx_listings(self):
        """Get KRX listings (cached for session)."""
        if self._krx_listings is None:
            try:
                df = self.fdr.get_krx_listings()
                self._krx_listings = df
            except Exception as e:
                logger.error(f"Failed to fetch KRX listings: {e}")
        return self._krx_listings

    def scan(self, scan_type: str = "pre_market") -> dict:
        """Run a market intelligence scan.

        Args:
            scan_type: One of pre_market, midday, post_market, overnight.

        Returns:
            Dict with scan_id, issues_count, tickers_count, status.
        """
        now = datetime.now(KST)
        scan_time = now.strftime("%H:%M")
        config = SCAN_TYPE_CONFIG.get(scan_type, SCAN_TYPE_CONFIG["pre_market"])
        label = config["label"]
        logger.info(f"=== Market Intel Scan: {scan_type} ({label}) ===")

        # Step 1: Web search
        search_results = self.searcher.multi_search(config["queries"], max_per_query=5)
        if not search_results:
            logger.warning("No search results returned")
            return self._save_scan(scan_time, scan_type, status="failed",
                                   error="No search results")

        # Step 2: Format search results for LLM
        search_text = self._format_search_results(search_results)

        # Step 3: LLM analysis
        prompt = MARKET_INTEL_PROMPT.format(
            current_time=now.strftime("%Y-%m-%d %H:%M"),
            scan_type_label=label,
            search_results=search_text,
        )
        try:
            raw_response = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model="google/gemini-3-flash-preview",
                temperature=0.2,
                max_tokens=4096,
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return self._save_scan(scan_time, scan_type, status="failed",
                                   error=str(e))

        # Step 4: Parse JSON
        issues = self._parse_response(raw_response)
        if issues is None:
            return self._save_scan(scan_time, scan_type, raw_response=raw_response,
                                   status="failed", error="JSON parse error")

        # Step 5: Validate tickers
        issues = self._validate_tickers(issues)

        # Step 6: Fetch detection prices
        issues = self._fetch_detection_prices(issues)

        # Step 7: Save to DB
        result = self._save_scan_with_issues(
            scan_time, scan_type, raw_response, issues, now.strftime("%Y-%m-%d")
        )

        # Step 8: Discord alert
        try:
            self._send_discord_alert(issues, scan_time, scan_type, label)
            self._mark_discord_sent(result["scan_id"])
        except Exception as e:
            logger.error(f"Discord alert failed: {e}")

        logger.info(f"Scan complete: {result['issues_count']} issues, "
                     f"{result['tickers_count']} tickers")
        return result

    def _format_search_results(self, results: list[dict]) -> str:
        """Format search results into text for LLM prompt."""
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            source = r.get("source", "")
            date = r.get("date", "")
            meta = f" ({source}, {date})" if source else ""
            lines.append(f"{i}. [{title}]{meta}\n   {snippet}")
        return "\n\n".join(lines)

    def _parse_response(self, raw: str) -> list[dict] | None:
        """Parse LLM response as JSON array."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
            elif "```" in text:
                text = text[:text.rfind("```")].strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            logger.error("LLM response is not a JSON array")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}\nRaw: {text[:500]}")
            return None

    def _validate_tickers(self, issues: list[dict]) -> list[dict]:
        """Validate and fix ticker codes against KRX listings."""
        listings = self._get_krx_listings()
        if listings is None or listings.empty:
            logger.warning("No KRX listings available, skipping ticker validation")
            return issues

        code_set = set(listings.index.astype(str))
        name_col = "Name" if "Name" in listings.columns else listings.columns[0]
        name_to_code = {}
        for code, row in listings.iterrows():
            name_to_code[row[name_col]] = str(code)

        for issue in issues:
            tickers = issue.get("affected_tickers", [])
            validated = []
            for t in tickers:
                ticker = str(t.get("ticker", "")).strip()
                name = t.get("name", "").strip()

                if ticker and len(ticker) < 6:
                    ticker = ticker.zfill(6)

                if ticker in code_set:
                    t["ticker"] = ticker
                    validated.append(t)
                elif name in name_to_code:
                    old_code = ticker
                    t["ticker"] = name_to_code[name]
                    logger.info(f"Ticker fix: {name} {old_code} -> {t['ticker']}")
                    validated.append(t)
                else:
                    matches = [c for n, c in name_to_code.items() if name and name in n]
                    if len(matches) == 1:
                        t["ticker"] = matches[0]
                        logger.info(f"Ticker partial match: {name} -> {t['ticker']}")
                        validated.append(t)
                    else:
                        logger.warning(f"Ticker validation failed: {ticker} ({name})")

            issue["affected_tickers"] = validated
        return issues

    def _fetch_detection_prices(self, issues: list[dict]) -> list[dict]:
        """Fetch current prices for all tickers at detection time."""
        all_tickers = set()
        for issue in issues:
            for t in issue.get("affected_tickers", []):
                if t.get("ticker"):
                    all_tickers.add(t["ticker"])

        if not all_tickers:
            return issues

        prices = {}
        today = datetime.now(KST).strftime("%Y%m%d")
        week_ago = (datetime.now(KST) - timedelta(days=7)).strftime("%Y%m%d")
        for ticker in all_tickers:
            try:
                df = self.krx.get_ohlcv(ticker, week_ago, today)
                if not df.empty:
                    prices[ticker] = int(df["Close"].iloc[-1])
            except Exception as e:
                logger.warning(f"Price fetch failed for {ticker}: {e}")

        for issue in issues:
            issue_prices = {}
            for t in issue.get("affected_tickers", []):
                ticker = t.get("ticker", "")
                if ticker in prices:
                    issue_prices[ticker] = prices[ticker]
            issue["price_at_detection"] = issue_prices
        return issues

    def _save_scan(self, scan_time, scan_type, raw_response=None,
                   status="success", error=None) -> dict:
        """Save scan record only (no issues)."""
        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO market_intel_scans
                   (scan_time, scan_type, model_used, raw_response,
                    issues_count, tickers_count, status, error_message)
                   VALUES (?, ?, ?, ?, 0, 0, ?, ?)""",
                (scan_time, scan_type, "google/gemini-3-flash-preview",
                 raw_response, status, error),
            )
            scan_id = cur.lastrowid
        return {"scan_id": scan_id, "issues_count": 0, "tickers_count": 0,
                "status": status}

    def _save_scan_with_issues(self, scan_time, scan_type, raw_response,
                                issues, detection_date) -> dict:
        """Save scan + issues to DB."""
        total_tickers = sum(len(i.get("affected_tickers", [])) for i in issues)
        status = "success" if issues else "partial"

        with get_db() as conn:
            cur = conn.execute(
                """INSERT INTO market_intel_scans
                   (scan_time, scan_type, model_used, raw_response,
                    issues_count, tickers_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (scan_time, scan_type, "google/gemini-3-flash-preview",
                 raw_response, len(issues), total_tickers, status),
            )
            scan_id = cur.lastrowid

            for issue in issues:
                conn.execute(
                    """INSERT INTO market_intel_issues
                       (scan_id, title, summary, category, sentiment, confidence,
                        source_info, affected_tickers_json, price_at_detection_json,
                        detection_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        scan_id,
                        issue.get("title", ""),
                        issue.get("summary", ""),
                        issue.get("category", ""),
                        issue.get("sentiment", ""),
                        issue.get("confidence", 0.0),
                        issue.get("source_info", ""),
                        json.dumps(issue.get("affected_tickers", []),
                                   ensure_ascii=False),
                        json.dumps(issue.get("price_at_detection", {}),
                                   ensure_ascii=False),
                        detection_date,
                    ),
                )

        return {
            "scan_id": scan_id,
            "issues_count": len(issues),
            "tickers_count": total_tickers,
            "status": status,
        }

    def _send_discord_alert(self, issues, scan_time, scan_type, label):
        """Send market intel alert to Discord."""
        from alerts.discord_webhook import DiscordNotifier
        from alerts.formatter import AlertFormatter

        embed = AlertFormatter.format_market_intel_alert(
            issues, scan_time, scan_type, label
        )
        notifier = DiscordNotifier()
        notifier.send(embed=embed)

    def _mark_discord_sent(self, scan_id):
        """Mark scan as discord-sent."""
        with get_db() as conn:
            conn.execute(
                "UPDATE market_intel_scans SET discord_sent = 1 WHERE id = ?",
                (scan_id,),
            )
