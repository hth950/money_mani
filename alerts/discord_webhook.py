"""Discord webhook notifier for trading alerts."""

import time
import logging

import requests

from alerts.formatter import AlertFormatter
from utils.config_loader import load_config, get_env

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Send messages and embeds to a Discord webhook."""

    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds to wait after a 429 before retrying

    def __init__(self, webhook_url: str = None):
        """Initialize notifier.

        Args:
            webhook_url: Discord webhook URL. If None, loads from config/env.
        """
        if webhook_url:
            self.webhook_url = webhook_url
        else:
            try:
                config = load_config()
                self.webhook_url = config.get("notifications", {}).get("discord", {}).get("webhook_url", "")
            except Exception:
                self.webhook_url = ""
            if not self.webhook_url or self.webhook_url.startswith("${"):
                self.webhook_url = get_env("DISCORD_WEBHOOK_URL", "")

        if not self.webhook_url:
            logger.warning("Discord webhook URL is not configured.")

    def send(self, content: str = None, embed: dict = None) -> bool:
        """Send a plain message or an embed to Discord.

        Args:
            content: Plain text message (optional).
            embed: Discord embed dict (optional).

        Returns:
            True if the message was sent successfully, False otherwise.
        """
        if not self.webhook_url:
            logger.error("No Discord webhook URL set. Cannot send message.")
            return False

        payload = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]

        if not payload:
            logger.warning("Nothing to send (no content or embed provided).")
            return False

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = requests.post(self.webhook_url, json=payload, timeout=10)

                if response.status_code in (200, 204):
                    logger.info("Discord message sent successfully.")
                    return True

                if response.status_code == 429:
                    retry_after = self.RETRY_DELAY
                    try:
                        retry_after = response.json().get("retry_after", self.RETRY_DELAY * 1000) / 1000
                    except Exception:
                        pass
                    logger.warning(
                        "Discord rate limit hit (429). Retrying after %.1fs (attempt %d/%d).",
                        retry_after,
                        attempt,
                        self.MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                    continue

                logger.error(
                    "Discord webhook returned status %d: %s",
                    response.status_code,
                    response.text[:200],
                )
                return False

            except requests.RequestException as exc:
                logger.error("Request error sending Discord message (attempt %d/%d): %s", attempt, self.MAX_RETRIES, exc)
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY)

        logger.error("Failed to send Discord message after %d attempts.", self.MAX_RETRIES)
        return False

    def send_signal_alert(self, signal: dict) -> bool:
        """Format and send a trading signal alert.

        Args:
            signal: Signal dict with strategy_name, ticker, ticker_name,
                    signal_type, price, indicators, date.

        Returns:
            True on success.
        """
        embed = AlertFormatter.format_signal_alert(signal)
        return self.send(embed=embed)

    def send_backtest_report(self, result: dict) -> bool:
        """Format and send a backtest result report.

        Args:
            result: Backtest result dict.

        Returns:
            True on success.
        """
        embed = AlertFormatter.format_backtest_report(result)
        return self.send(embed=embed)

    def send_daily_summary(self, signals: list, date: str) -> bool:
        """Format and send a daily summary of signals.

        Args:
            signals: List of signal dicts.
            date: Date string (YYYY-MM-DD).

        Returns:
            True on success.
        """
        embed = AlertFormatter.format_daily_summary(signals, date)
        return self.send(embed=embed)
