import logging
import sys


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # Silence noisy third-party loggers (GRVT SDK, HTTP connection pools)
    for name in ("pysdk", "urllib3", "asyncio", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # GRVT SDK calls logging.error() on root logger — suppress those
    class _NoRootFilter(logging.Filter):
        def filter(self, record):
            return record.name != "root"

    for h in logging.getLogger().handlers:
        h.addFilter(_NoRootFilter())
