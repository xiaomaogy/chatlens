import os
import sys
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_bundled() -> bool:
    # py2app sets sys.frozen = 'macosx_app' on launched bundles.
    return bool(getattr(sys, "frozen", False))


# When running from source, anchor data at the project root. When running from
# a bundled .app the project root sits inside a read-only Resources tree, so
# fall back to ~/Library/Application Support/ChatLens.
if _is_bundled():
    _DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "ChatLens"
else:
    _DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Inside a .app, GUI launch inherits a minimal PATH (no Homebrew, no pipx).
# Augment it so `wechat-cli` / `claude` resolve from their normal install spots.
if _is_bundled():
    extra_path = [
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    current = os.environ.get("PATH", "").split(":")
    merged = [p for p in extra_path if p not in current] + current
    os.environ["PATH"] = ":".join(p for p in merged if p)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_DEFAULT_DATA_DIR / ".env") if _is_bundled() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8090

    database_url: str = f"sqlite:///{_DEFAULT_DATA_DIR / 'chatlens.db'}"

    wechat_cli_path: str = "wechat-cli"
    wechat_cli_config: str = ""

    auto_open_browser: bool = True

    @property
    def data_dir(self) -> Path:
        return _DEFAULT_DATA_DIR


settings = Settings()
