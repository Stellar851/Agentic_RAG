# -*- coding: utf-8 -*-
"""企业级统一结构化日志与全链路 Trace-ID 治理模块。

设计目标：
1. 每个微服务独立配置 service_name，统一输出人类可读控制台 + 可检索 JSONL 文件；
2. 所有日志自动携带 trace_id，支撑 8002 -> 8000 -> 8001 全链路追踪；
3. 文件日志无 ANSI 控制符，Windows / Linux 跨平台安全；
4. 默认启用敏感信息脱敏，防止 API Key、手机号、身份证号泄漏到日志。

用法示例：
    from common.logging_config import configure_logging, get_logger, install_trace_middleware
    configure_logging("agent-service", log_dir="logs")
    app = FastAPI()
    install_trace_middleware(app, "agent-service")
    logger = get_logger(__name__)
    logger.info("受理用户提问", extra={"event": "chat.accepted", "query_len": 12})
"""
from __future__ import annotations

import contextvars
import datetime
import json
import logging
import os
import re
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

TRACE_HEADER = "X-Trace-ID"
_REQUEST_TRACE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_trace_id", default=None)

# 日志目录默认收敛到项目根目录 logs/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# ---------------------------------------------------------------------------
# Trace 上下文
# ---------------------------------------------------------------------------

def new_trace_id() -> str:
    """生成稳定、低碰撞且可读的短 Trace-ID。"""
    return uuid.uuid4().hex[:16]

def get_trace_id() -> str:
    return _REQUEST_TRACE_ID.get() or "-"

def set_trace_id(trace_id: Optional[str]) -> None:
    _REQUEST_TRACE_ID.set(trace_id or new_trace_id())

def reset_trace_id() -> None:
    _REQUEST_TRACE_ID.set(None)

# ---------------------------------------------------------------------------
# 敏感信息脱敏
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+"), lambda m: "Bearer ***" + m.group(0).split()[-1][-4:]),
    (re.compile(r'api[_-]?key[\'"\s:=]+[A-Za-z0-9._-]{6,}'), lambda m: "api_key=***"),
    (re.compile(r"(?<!Bearer )sk-[A-Za-z0-9_-]{8,}"), lambda m: "sk-***" + m.group(0)[-4:]),
    (re.compile(r"1[3-9]\d{9}"), lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:]),
    (re.compile(r"\b\d{17}[\dXx]\b"), lambda m: m.group(0)[:4] + "***********" + m.group(0)[-4:]),
]

def mask_sensitive(text: str) -> str:
    if not text:
        return text
    for pattern, replacer in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacer, text)
    return text

# ---------------------------------------------------------------------------
# 日志格式化器
# ---------------------------------------------------------------------------

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"
_USE_ANSI = os.getenv("RAG_LOG_ANSI", "").lower() not in {"0", "false", "no"}

class ConsoleFormatter(logging.Formatter):
    """控制台输出：保留关键字段但保持紧凑；支持可关闭的 ANSI 颜色。"""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        message = mask_sensitive(message)
        level = record.levelname
        trace = get_trace_id()
        prefix = ""
        if _USE_ANSI and sys.stderr.isatty():
            prefix = _COLORS.get(level, "")
        suffix = _RESET if prefix else ""
        service = getattr(record, "service", os.getenv("RAG_SERVICE_NAME", "rag"))
        return f"{prefix}{self.formatTime(record, '%H:%M:%S')} {level:<7}{suffix} | {service} | {trace} | {record.name} | {message}"

class JsonFormatter(logging.Formatter):
    """生产文件 JSONL 输出：无 ANSI，字段稳定，便于日志平台采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": record.levelname,
            "service": getattr(record, "service", "unknown"),
            "trace_id": get_trace_id(),
            "logger": record.name,
            "message": mask_sensitive(record.getMessage()),
        }
        for key in ("event", "stage", "latency_ms", "query", "status_code", "path", "error_type", "extra"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)

# ---------------------------------------------------------------------------
# 配置与获取器
# ---------------------------------------------------------------------------

def configure_logging(
    service_name: str,
    log_dir: Optional[str] = None,
    console_level: str = "INFO",
    file_level: str = "DEBUG"
) -> logging.Logger:
    """初始化根日志器与双通道 Handler。重复调用会自动清理旧 Handler。"""
    log_dir = log_dir or DEFAULT_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    os.environ["RAG_SERVICE_NAME"] = service_name

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    file_path = os.path.join(log_dir, f"{service_name}.log")
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, file_level.upper(), logging.DEBUG))
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    # 降低第三方库噪音，避免 ChromaDB / httpx 刷屏。
    for noisy in ("chromadb", "urllib3", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(service_name)
    logger.setLevel(logging.DEBUG)
    return logger

def get_logger(name: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name or "rag")
    if not hasattr(logger, "_rag_service_bound"):
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            if not hasattr(record, "service"):
                record.service = os.getenv("RAG_SERVICE_NAME", "rag")
            return record

        logging.setLogRecordFactory(record_factory)
        logger._rag_service_bound = True
    return logger

# ---------------------------------------------------------------------------
# FastAPI Trace-ID 中间件
# ---------------------------------------------------------------------------

class TraceIDMiddleware(BaseHTTPMiddleware):
    """从请求入口生成/透传 Trace-ID，并输出统一访问日志。"""

    def __init__(
        self,
        app,
        service_name: str,
        silence_prefixes: Optional[list] = None,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(app)
        self.service_name = service_name
        self.silence_prefixes = silence_prefixes or []
        self.logger = logger or get_logger("trace")

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(TRACE_HEADER) or request.headers.get("X-Request-ID") or new_trace_id()
        set_trace_id(trace_id)
        path = request.url.path
        silent = any(path.startswith(prefix) for prefix in self.silence_prefixes)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            reset_trace_id()
            raise
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers[TRACE_HEADER] = trace_id
        if not silent:
            self.logger.info(
                "HTTP 请求完成",
                extra={
                    "event": "http.completed",
                    "path": path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                },
            )
        reset_trace_id()
        return response

def install_trace_middleware(app, service_name: str, silence_prefixes: Optional[list] = None) -> None:
    """挂载 Trace-ID 中间件，并在响应头回传 trace_id。"""
    app.add_middleware(
        TraceIDMiddleware,
        service_name=service_name,
        silence_prefixes=silence_prefixes,
    )


class Span:
    """轻量阶段计时器，输出一条日志抓出单阶段耗时。

    with Span("检索", event="retrieve.start", extra={"top_k": 4}):
        ...
    """

    def __init__(self, name: str, logger: Optional[logging.Logger] = None, **fields: Any):
        self.name = name
        self.logger = logger or get_logger("span")
        self.fields = fields

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        latency_ms = round((time.perf_counter() - self.started) * 1000, 2)
        level = logging.ERROR if exc_type else logging.INFO
        self.logger.log(
            level,
            f"{self.name} 完成",
            extra={"event": self.fields.get("event", "stage.completed"), "stage": self.name, "latency_ms": latency_ms, **self.fields},
        )
        return False

def log_stage(logger: Optional[logging.Logger], stage: str, message: str = "", **fields: Any) -> None:
    """统一记录阶段受理/完成，字段化输出便于后续聚合。"""
    (logger or get_logger("stage")).info(
        f"[{stage}] {message}".strip(),
        extra={"event": fields.pop("event", "stage"), "stage": stage, **fields},
    )
