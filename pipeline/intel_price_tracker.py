"""Intel price tracker: update post-detection prices and compute accuracy."""

import json
import logging
from datetime import datetime, timedelta, timezone

from market_data.krx_fetcher import KRXFetcher
from web.db.connection import get_db

KST = timezone(timedelta(hours=9))
logger = logging.getLogger("money_mani.pipeline.intel_price_tracker")


def _is_us_ticker(ticker: str) -> bool:
    """Return True if ticker is a US stock (not a 6-digit KRX code)."""
    return not (ticker.isdigit() and len(ticker) == 6)


class IntelPriceTracker:
    """Track post-detection price changes and compute prediction accuracy."""

    def __init__(self):
        self.krx = KRXFetcher(delay=0.5)

    def run(self):
        """Update price tracking for all eligible issues."""
        logger.info("=== Intel Price Tracker Started ===")
        today = datetime.now(KST).date()

        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, affected_tickers_json, price_at_detection_json,
                          price_after_1d_json, price_after_3d_json, price_after_5d_json,
                          detection_date, accuracy_score
                   FROM market_intel_issues
                   WHERE detection_date IS NOT NULL
                     AND accuracy_score IS NULL"""
            ).fetchall()

        if not rows:
            logger.info("No issues pending price tracking")
            return {"updated": 0}

        updated = 0
        for row in rows:
            try:
                issue_id = row["id"]
                detection_date = datetime.strptime(
                    row["detection_date"], "%Y-%m-%d"
                ).date()
                days_elapsed = self._business_days_between(detection_date, today)

                tickers_json = row["affected_tickers_json"]
                tickers = json.loads(tickers_json) if tickers_json else []
                ticker_codes = [t["ticker"] for t in tickers if t.get("ticker")]

                if not ticker_codes:
                    logger.debug(f"Issue #{issue_id}: no affected_tickers, skipping price tracking")
                    continue

                updates = {}

                if days_elapsed >= 1 and not row["price_after_1d_json"]:
                    target = self._add_business_days(detection_date, 1)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_1d_json"] = json.dumps(
                            prices, ensure_ascii=False
                        )

                if days_elapsed >= 3 and not row["price_after_3d_json"]:
                    target = self._add_business_days(detection_date, 3)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_3d_json"] = json.dumps(
                            prices, ensure_ascii=False
                        )

                if days_elapsed >= 5 and not row["price_after_5d_json"]:
                    target = self._add_business_days(detection_date, 5)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_5d_json"] = json.dumps(
                            prices, ensure_ascii=False
                        )

                # Compute accuracy when 5-day data is complete
                if days_elapsed >= 5:
                    det_prices = json.loads(
                        row["price_at_detection_json"] or "{}"
                    )
                    after_5d_raw = updates.get(
                        "price_after_5d_json",
                        row["price_after_5d_json"] or "{}",
                    )
                    after_5d = json.loads(after_5d_raw)
                    if det_prices and after_5d:
                        accuracy = self._compute_accuracy(
                            tickers, det_prices, after_5d
                        )
                        updates["accuracy_score"] = accuracy

                if updates:
                    self._update_issue(issue_id, updates)
                    updated += 1
                    for day in (1, 3, 5):
                        key = f"price_after_{day}d_json"
                        if key in updates:
                            prices_logged = json.loads(updates[key])
                            logger.info(f"Issue #{issue_id}: updated price_after_{day}d for {len(prices_logged)} tickers")
                    if "accuracy_score" in updates:
                        logger.info(f"Issue #{issue_id} accuracy computed: {updates['accuracy_score']:.1%}")
                        try:
                            from alerts.discord_webhook import DiscordNotifier
                            notifier = DiscordNotifier()
                            notifier.send(content=f"📊 인텔 정확도 측정 완료: Issue #{issue_id} - 적중률 {updates['accuracy_score']:.1%}")
                        except Exception as e:
                            logger.warning(f"Discord notification failed: {e}")

            except Exception as e:
                logger.error(f"Price tracking error for issue {row['id']}: {e}")

        logger.info(f"Price tracker done: {updated} issues updated")
        return {"updated": updated}

    def _business_days_between(self, start, end):
        """Count business days between two dates."""
        count = 0
        current = start
        while current < end:
            current += timedelta(days=1)
            if current.weekday() < 5:
                count += 1
        return count

    def _add_business_days(self, start, days):
        """Add N business days to a date."""
        current = start
        added = 0
        while added < days:
            current += timedelta(days=1)
            if current.weekday() < 5:
                added += 1
        return current

    def _fetch_prices_at_date(self, tickers, target_date):
        """Fetch closing prices for tickers at a specific date.

        KRX tickers (6-digit numeric) use KRXFetcher; all others use USFetcher via yfinance.
        """
        prices = {}
        krx_start = (target_date - timedelta(days=3)).strftime("%Y%m%d")
        krx_end = target_date.strftime("%Y%m%d")
        us_start = (target_date - timedelta(days=3)).strftime("%Y-%m-%d")
        us_end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

        for ticker in tickers:
            try:
                if _is_us_ticker(ticker):
                    from market_data.us_fetcher import USFetcher
                    us_fetcher = USFetcher()
                    df = us_fetcher.get_ohlcv(ticker, us_start, us_end)
                    if df.empty:
                        logger.warning(f"Price fetch (US) for {ticker} at {us_end}: empty data")
                        continue
                    prices[ticker] = float(df["Close"].iloc[-1])
                else:
                    df = self.krx.get_ohlcv(ticker, krx_start, krx_end)
                    if df.empty:
                        logger.warning(f"Price fetch (KRX) for {ticker} at {krx_end}: empty data (possible delisting/suspension)")
                        continue
                    prices[ticker] = int(df["Close"].iloc[-1])
            except Exception as e:
                logger.warning(f"Price fetch for {ticker} at {target_date}: {e}")
        return prices

    def _compute_accuracy(self, tickers, detection_prices, after_prices):
        """Compute direction prediction accuracy (0.0 ~ 1.0)."""
        correct = 0
        total = 0
        for t in tickers:
            code = t.get("ticker", "")
            predicted = t.get("direction", "")
            if code in detection_prices and code in after_prices:
                actual_change = after_prices[code] - detection_prices[code]
                match = (predicted == "up" and actual_change > 0) or \
                        (predicted == "down" and actual_change < 0)
                if match:
                    correct += 1
                logger.info(f"  {code}: predicted={predicted}, actual_change={actual_change:+,}, {'✓' if match else '✗'}")
                total += 1
        return correct / total if total > 0 else 0.0

    def _update_issue(self, issue_id, updates):
        """Update issue fields in DB."""
        set_clauses = []
        values = []
        for k, v in updates.items():
            set_clauses.append(f"{k} = ?")
            values.append(v)
        values.append(issue_id)

        with get_db() as conn:
            conn.execute(
                f"UPDATE market_intel_issues SET {', '.join(set_clauses)} "
                f"WHERE id = ?",
                values,
            )

    def backfill_missing(self):
        """Backfill accuracy_score for issues where it is NULL and detection_date is 5+ days ago.

        Useful for recovering historical issues that were skipped due to missing prices,
        US ticker gaps, or tracker downtime.
        """
        logger.info("=== Intel Price Tracker Backfill Started ===")
        today = datetime.now(KST).date()

        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, affected_tickers_json, price_at_detection_json,
                          price_after_1d_json, price_after_3d_json, price_after_5d_json,
                          detection_date, accuracy_score
                   FROM market_intel_issues
                   WHERE accuracy_score IS NULL
                     AND detection_date IS NOT NULL
                     AND detection_date <= date('now', '-5 days')"""
            ).fetchall()

        if not rows:
            logger.info("Backfill: no eligible issues found")
            return {"backfilled": 0}

        logger.info(f"Backfill: {len(rows)} issues eligible")
        backfilled = 0
        for row in rows:
            try:
                issue_id = row["id"]
                detection_date = datetime.strptime(
                    row["detection_date"], "%Y-%m-%d"
                ).date()

                tickers_json = row["affected_tickers_json"]
                tickers = json.loads(tickers_json) if tickers_json else []
                ticker_codes = [t["ticker"] for t in tickers if t.get("ticker")]

                if not ticker_codes:
                    logger.debug(f"Backfill issue #{issue_id}: no affected_tickers, skipping")
                    continue

                updates = {}

                if not row["price_after_1d_json"]:
                    target = self._add_business_days(detection_date, 1)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_1d_json"] = json.dumps(prices, ensure_ascii=False)

                if not row["price_after_3d_json"]:
                    target = self._add_business_days(detection_date, 3)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_3d_json"] = json.dumps(prices, ensure_ascii=False)

                if not row["price_after_5d_json"]:
                    target = self._add_business_days(detection_date, 5)
                    prices = self._fetch_prices_at_date(ticker_codes, target)
                    if prices:
                        updates["price_after_5d_json"] = json.dumps(prices, ensure_ascii=False)

                # Compute accuracy when 5-day data is available
                det_prices = json.loads(row["price_at_detection_json"] or "{}")
                after_5d_raw = updates.get(
                    "price_after_5d_json",
                    row["price_after_5d_json"] or "{}",
                )
                after_5d = json.loads(after_5d_raw)
                if det_prices and after_5d:
                    accuracy = self._compute_accuracy(tickers, det_prices, after_5d)
                    updates["accuracy_score"] = accuracy

                if updates:
                    self._update_issue(issue_id, updates)
                    backfilled += 1
                    if "accuracy_score" in updates:
                        logger.info(f"Backfill issue #{issue_id} accuracy: {updates['accuracy_score']:.1%}")
                    else:
                        logger.info(f"Backfill issue #{issue_id}: price data updated, accuracy pending")
                else:
                    logger.debug(f"Backfill issue #{issue_id}: no price data could be fetched")

            except Exception as e:
                logger.error(f"Backfill error for issue {row['id']}: {e}")

        logger.info(f"Backfill done: {backfilled} issues updated")
        return {"backfilled": backfilled}
