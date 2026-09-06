import asyncio
import logging
import sys
from typing import Any

import uvicorn
from dotenv import load_dotenv

from core import settings

load_dotenv()

if __name__ == "__main__":
    root_logger = logging.getLogger()
    if root_logger.handlers:
        print(
            f"Warning: Root logger already has {len(root_logger.handlers)} handler(s) configured. "
            f"basicConfig() will be ignored. Current level: {logging.getLevelName(root_logger.level)}"
        )

    logging.basicConfig(level=settings.LOG_LEVEL.to_logging_level())
    # Psycopg's async implementation requires a selector loop on Windows. Python
    # 3.14 deprecates event-loop policies, so pass an explicit loop factory to
    # Uvicorn instead of relying on WindowsSelectorEventLoopPolicy.
    loop_factory: Any = asyncio.SelectorEventLoop if sys.platform == "win32" else "auto"
    uvicorn.run(
        "service:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
        loop=loop_factory,
    )
