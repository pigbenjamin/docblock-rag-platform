import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
from typing import Optional
#from docblock_core.config import LogSettings

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "job_id=%(job_id)s doc_id=%(doc_id)s | %(message)s"
)

LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s | "
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#def setup_logging(settings: LogSettings):
#    if logging.getLogger().hasHandlers():
#        return
#
#    handlers = []
#
#    # file handler
#    handlers.append(logging.FileHandler(settings.log_file))
#
#    # console handler
#    if settings.enable_console:
#        handlers.append(logging.StreamHandler(sys.stdout))
#
#    logging.basicConfig(
#        level=settings.global_level,
#        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
#        handlers=handlers,
#        force=True,
#    )

    
class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        if not hasattr(record, "doc_id"):
            record.doc_id = "-"
        return True


def make_file_handler(log_file: Path, level=logging.INFO) -> logging.Handler:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    #handler.addFilter(ContextFilter())
    return handler


def make_console_handler(level=logging.INFO) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    #handler.addFilter(ContextFilter())
    return handler


def setup_root_logger(log_dir: str, level=logging.INFO) -> logging.Logger:
    """
    root logger:
      - console
      - app.log
    所有 propagate=True 的模組 logger 都會進來
    """
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        return root

    root.addHandler(make_console_handler(level))
    root.addHandler(make_file_handler(Path(log_dir) / "app.log", level))
    return root


def get_module_logger(
    name: str,
    log_dir: str,
    module_log_file: Optional[str] = None,
    level=logging.INFO,
) -> logging.Logger:
    """
    模組 logger:
      - propagate=True -> 會寫到 root 的 app.log
      - 若 module_log_file 有指定，再額外寫自己的檔
    """
    setup_root_logger(log_dir, level)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = True

    if module_log_file:
        target_path = str((Path(log_dir) / module_log_file).resolve())

        exists = False
        for h in logger.handlers:
            if isinstance(h, RotatingFileHandler):
                if Path(getattr(h, "baseFilename", "")).resolve().as_posix() == Path(target_path).as_posix():
                    exists = True
                    break

        if not exists:
            logger.addHandler(make_file_handler(Path(target_path), level))

    return logger


def get_file_logger(name: str, log_path: str, level: int = logging.INFO) -> logging.Logger:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)

    abs_log_path = str(Path(log_path).resolve())

    # avoid duplicated handlers
    already_exists = False
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if getattr(handler, "baseFilename", None) == abs_log_path:
                already_exists = True
                break

    if not already_exists:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        file_handler = logging.FileHandler(abs_log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 保留 propagation=True，這樣也會進 root 的 app.log
    logger.propagate = True

    return logger