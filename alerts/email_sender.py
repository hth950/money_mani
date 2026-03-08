"""Email sender for trading alerts via Gmail SMTP."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.config_loader import load_config

logger = logging.getLogger(__name__)


class EmailSender:
    """Send plain-text emails via Gmail SMTP with STARTTLS."""

    def __init__(self, config: dict = None):
        """Initialize sender from config.

        Args:
            config: Email settings dict. If None, loads from settings.yaml.
                    Expected keys: enabled, smtp_server, smtp_port,
                                   sender, password, recipients.
        """
        if config is None:
            try:
                full_config = load_config()
                config = full_config.get("notifications", {}).get("email", {})
            except Exception as exc:
                logger.warning("Could not load email config: %s", exc)
                config = {}

        self.enabled: bool = config.get("enabled", False)
        self.smtp_server: str = config.get("smtp_server", "smtp.gmail.com")
        self.smtp_port: int = int(config.get("smtp_port", 587))
        self.sender: str = config.get("sender", "")
        self.password: str = config.get("password", "")
        self.default_recipients: list = config.get("recipients", [])

    def send(self, subject: str, body: str, recipients: list = None) -> bool:
        """Send a plain-text email.

        Args:
            subject: Email subject line.
            body: Plain-text email body.
            recipients: List of recipient addresses. Falls back to config recipients.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self.enabled:
            logger.info("Email notifications are disabled. Skipping send.")
            return False

        to_list = recipients or self.default_recipients
        if not to_list:
            logger.warning("No recipients specified. Skipping email send.")
            return False

        if not self.sender or not self.password:
            logger.error("Email sender or password not configured. Cannot send email.")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(to_list)
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.sender, self.password)
                server.sendmail(self.sender, to_list, msg.as_string())
            logger.info("Email sent to %s", ", ".join(to_list))
            return True
        except smtplib.SMTPAuthenticationError as exc:
            logger.error("SMTP authentication failed: %s", exc)
        except smtplib.SMTPException as exc:
            logger.error("SMTP error sending email: %s", exc)
        except OSError as exc:
            logger.error("Network error sending email: %s", exc)
        return False
