import logging
import sys
from pathlib import Path


def setup_logging(log_dir: str = ".") -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s - %(message)s"

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(fmt))

    log_path = Path(log_dir) / "bot.log"
    file_all = logging.FileHandler(log_path, encoding="utf-8")
    file_all.setLevel(logging.INFO)
    file_all.setFormatter(logging.Formatter(fmt))

    err_path = Path(log_dir) / "bot_errors.log"
    file_err = logging.FileHandler(err_path, encoding="utf-8")
    file_err.setLevel(logging.ERROR)
    file_err.setFormatter(logging.Formatter(fmt))

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console, file_all, file_err],
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
