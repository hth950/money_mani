"""Start the money_mani web server."""
import uvicorn
from utils.config_loader import load_config
from utils.logging_config import setup_logging

if __name__ == "__main__":
    config = load_config()
    setup_logging(config.get("logging", {}))
    uvicorn.run(
        "web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
