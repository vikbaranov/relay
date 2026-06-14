import logging
import re
from urllib.parse import urlparse

_CURSOR = "▌"
_UPDATE_INTERVAL = 1.0
_RESULT_MAX = 150
_HEARTBEAT_INTERVAL = 10.0
_MM_MAX_POST = 10_000
_THINKING_PREVIEW_MAX = 400
_THINKING_BUFFER_MAX = _THINKING_PREVIEW_MAX * 2

_APPROVED_PREFIX = re.compile(r"^approved:\s*(?:true|false),\s*", re.IGNORECASE)

_TOOL_ICONS: dict[str, str] = {
    "web_search_tool": "🔍",
    "content_search": "🔍",
    "web_fetch": "🌐",
    "browser": "🌐",
    "browser_open": "🌐",
    "text_browser": "🌐",
    "http_request": "📡",
    "claude_code": "💻",
    "claude_code_runner": "💻",
    "pdf_read": "📄",
    "file_write": "✏️",
    "file_edit": "✏️",
    "glob_search": "📁",
    "git_operations": "🔧",
    "image_gen": "🖼️",
    "screenshot": "📸",
    "calculator": "🧮",
    "memory_store": "💾",
    "memory_recall": "💾",
    "knowledge_tool": "📚",
}
_TOOL_ICON_DEFAULT = "⚙️"


def _clean_summary(summary: str) -> str:
    """Strip 'approved: false/true, ' prefix injected by the approval wrapper."""
    return _APPROVED_PREFIX.sub("", summary).strip()


def _truncate(s: str, n: int) -> str:
    return s[: n - 3] + "..." if len(s) > n else s


def _tail(s: str, n: int) -> str:
    return s[-n:] if len(s) > n else s


def _key_arg(name: str, args: dict | None) -> str:
    if not args:
        return ""
    val = next(iter(args.values()), "")
    s = str(val).strip()
    if s.startswith(("http://", "https://")):
        p = urlparse(s)
        path = p.path[:50] if len(p.path) > 50 else p.path
        s = p.netloc + path
    return _truncate(s, 80)


def _fmt_tool_running(name: str, key: str, n: int = 0) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    prefix = f"[{n}] " if n else ""
    if key:
        return f"_{prefix}{icon} `{name}`: {key}..._"
    return f"_{prefix}{icon} `{name}`..._"


logger = logging.getLogger(__name__)


def patch_post(driver, post_id: str, text: str) -> None:
    if len(text) > _MM_MAX_POST:
        text = text[: _MM_MAX_POST - 60] + "\n\n_[ответ обрезан — слишком длинный]_"
    try:
        driver.posts.patch_post(post_id, {"message": text})
    except OSError:
        logger.error("patch_post_failed", exc_info=True, extra={"post_id": post_id})


def patch_props(driver, post_id: str, text: str) -> None:
    driver.posts.patch_post(post_id, {"props": {"attachments": [{"text": text}]}})


def _fmt_tool_done(
    name: str, key: str, output: str, n: int = 0, elapsed: float | None = None
) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    out = output.strip()
    if "no results found" in out.lower():
        summary = "нет результатов"
    elif len(out) > _RESULT_MAX:
        summary = _truncate(out, _RESULT_MAX)
    else:
        summary = out
    prefix = f"[{n}] " if n else ""
    time_str = f" ({elapsed:.1f}с)" if elapsed is not None else ""
    if key:
        call_line = f"_{prefix}{icon} `{name}`: {key}{time_str}_"
    else:
        call_line = f"_{prefix}{icon} `{name}`{time_str}_"
    if summary:
        return f"{call_line}\n_→ {summary}_"
    return call_line
