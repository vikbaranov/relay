import pathlib

WORKSPACE = pathlib.Path(__file__).parent.parent / "app" / "workspace"
IDENTITY = (WORKSPACE / "IDENTITY.md").read_text()


def test_identity_has_skills_section():
    assert "## Навыки (Skills)" in IDENTITY


def test_identity_skills_list_command():
    assert "zeroclaw skills list" in IDENTITY


def test_identity_skills_install_registry():
    assert "zeroclaw skills install" in IDENTITY


def test_identity_skills_install_from_url():
    assert "curl -fsSL" in IDENTITY


def test_identity_skills_github_blob_conversion():
    assert "raw.githubusercontent.com" in IDENTITY


def test_identity_skills_remove():
    assert "rm -rf /zeroclaw-data/workspace/skills/" in IDENTITY


def test_identity_skills_directory_path():
    assert "/zeroclaw-data/workspace/skills/" in IDENTITY
