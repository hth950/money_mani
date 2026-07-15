"""Production web entry point used by the Hermes container."""

import os

import uvicorn

from utils.config_loader import load_config
from utils.logging_config import setup_logging


def main() -> None:
    config = load_config()
    setup_logging(config.get("logging", {}))
    uvicorn.run(
        "web.app:app",
        host=os.getenv("MONEY_MANI_WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("MONEY_MANI_WEB_PORT", "31234")),
        workers=1,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv(
            "MONEY_MANI_FORWARDED_ALLOW_IPS", "127.0.0.1,::1"
        ),
    )


if __name__ == "__main__":
    main()
