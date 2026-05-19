from urllib.parse import urlparse

_CURSOR = "▌"
_UPDATE_INTERVAL = 1.0
_RESULT_MAX = 150
_HEARTBEAT_INTERVAL = 10.0
_MM_MAX_POST = 10_000

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


def _truncate(s: str, n: int) -> str:
    return s[: n - 3] + "..." if len(s) > n else s


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


def _fmt_tool_running(name: str, key: str) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    if key:
        return f"_{icon} `{name}`: {key}..._"
    return f"_{icon} `{name}`..._"


def _fmt_tool_done(name: str, key: str, output: str) -> str:
    icon = _TOOL_ICONS.get(name, _TOOL_ICON_DEFAULT)
    out = output.strip()
    if "no results found" in out.lower():
        summary = "нет результатов"
    elif len(out) > _RESULT_MAX:
        summary = _truncate(out, _RESULT_MAX)
    else:
        summary = out
    if key:
        return f"_{icon} `{name}`: {key} → {summary}_"
    return f"_{icon} `{name}` → {summary}_"
