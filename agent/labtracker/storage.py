"""Local files: settings, tracker state and the offline queue.

Nothing secret is stored here. Queued events are already encrypted.
All files are created readable by your user only (mode 600, folders 700).
"""
import json
import os
import tempfile
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "labtracker"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "labtracker"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic write with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


DEFAULT_CONFIG = {
    "name": "",
    "user_id": "",
    "webapp_url": "",
    "dashboard_url": "https://dunky64.github.io/Labtracker/",
    "idle_minutes": 10,
    "prompt_minutes": 30,
}


class Store:
    def __init__(self, cfg_dir: Path = None, st_dir: Path = None):
        self.cfg_dir = cfg_dir or config_dir()
        self.st_dir = st_dir or state_dir()
        self.config_path = self.cfg_dir / "config.json"
        self.state_path = self.st_dir / "state.json"
        self.queue_path = self.st_dir / "queue.json"
        self.lock_path = self.st_dir / "agent.lock"

    def load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(read_json(self.config_path, {}))
        return cfg

    def save_config(self, cfg: dict) -> None:
        write_json(self.config_path, cfg)

    def load_state(self) -> dict:
        return read_json(self.state_path, {})

    def save_state(self, state: dict) -> None:
        write_json(self.state_path, state)

    def load_queue(self) -> list:
        q = read_json(self.queue_path, [])
        return q if isinstance(q, list) else []

    def save_queue(self, queue: list) -> None:
        write_json(self.queue_path, queue)
