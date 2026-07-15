"""Start the money_mani web server."""
import uvicorn
from utils.config_loader import load_config
from utils.logging_config import setup_logging

if __name__ == "__main__":
    config = load_config()
    setup_logging(config.get("logging", {}))
    web_config = config.get("web", {})
    uvicorn.run(
        "web.app:app",
        host=str(web_config.get("host", "0.0.0.0")),
        port=int(web_config.get("port", 31234)),
        reload=bool(web_config.get("reload", True)),
    )
