from tdagent import credentials
from tdagent.config import TeradataConfig, load_config, save_teradata_settings

CONFIG = """# my settings
[llm]
model = "qwen3.8:27b"  # keep me

[teradata]
enabled = false
host = "old.example.com"
password = "plain-text-secret"
"""


def test_save_keeps_comments_and_never_writes_password(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    save_teradata_settings(path, TeradataConfig(enabled=True, host="td.example.com", user="me",
                                                password="secret", databases=["A", "B"]))
    text = path.read_text(encoding="utf-8")
    assert "# my settings" in text and "# keep me" in text
    assert "secret" not in text and "password" not in text
    cfg = load_config(path)
    assert cfg.teradata.host == "td.example.com" and cfg.teradata.databases == ["A", "B"]
    assert cfg.llm.model == "qwen3.8:27b"


def test_save_creates_missing_file(tmp_path):
    path = tmp_path / "new.toml"
    save_teradata_settings(path, TeradataConfig(enabled=True, host="h", user="u"))
    assert load_config(path).teradata.host == "h"


def test_password_from_keyring_and_env_precedence(tmp_path, memory_keyring, monkeypatch):
    path = tmp_path / "config.toml"
    save_teradata_settings(path, TeradataConfig(enabled=True, host="h", user="u"))
    assert load_config(path).teradata.password == ""
    assert credentials.set_password("h", "u", "from-keyring") == (True, "")
    assert load_config(path).teradata.password == "from-keyring"
    monkeypatch.setenv("TD_PASSWORD", "from-env")
    assert load_config(path).teradata.password == "from-env"
    credentials.delete_password("h", "u")
    credentials.delete_password("h", "u")  # deleting twice is harmless
    assert credentials.get_password("h", "u") is None


def test_no_keyring_backend_degrades(no_keyring):
    ok, err = credentials.set_password("h", "u", "x")
    assert not ok and err
    assert credentials.get_password("h", "u") is None
    credentials.delete_password("h", "u")
