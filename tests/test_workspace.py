import pathlib

WORKSPACE = pathlib.Path(__file__).parent.parent / "app" / "workspace"


def _identity() -> str:
    return (WORKSPACE / "IDENTITY.md").read_text()


def test_identity_has_skills_section():
    assert "## Skills" in _identity()


def test_identity_skills_list_command():
    assert "zeroclaw skills list" in _identity()


def test_identity_skills_install_registry():
    assert "zeroclaw skills install" in _identity()


def test_identity_skills_install_from_url():
    assert "curl -fsSL" in _identity()


def test_identity_skills_github_blob_conversion():
    assert "raw.githubusercontent.com" in _identity()


def test_identity_skills_remove():
    assert "rm -rf /zeroclaw-data/workspace/skills/" in _identity()
