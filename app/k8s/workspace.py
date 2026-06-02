import pathlib

_WORKSPACE_DEFAULTS = pathlib.Path(__file__).parent.parent / "workspace"
WORKSPACE_FILES = ("SOUL.md", "IDENTITY.md")


def _workspace_default(filename: str) -> str | None:
    path = _WORKSPACE_DEFAULTS / filename
    if path.exists():
        return path.read_text()
    return None


def _workspace_default_data() -> dict[str, str]:
    data: dict[str, str] = {}
    for filename in WORKSPACE_FILES:
        content = _workspace_default(filename)
        if content is not None:
            data[filename] = content
    return data
