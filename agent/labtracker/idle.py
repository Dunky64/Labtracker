"""Keyboard/mouse idle time. Reads ONLY a number: milliseconds since the last input.

No keystrokes, window titles, screenshots or anything else are ever collected.
"""
import shutil
import subprocess


class IdleSource:
    def __init__(self):
        self.path = shutil.which("xprintidle")

    @property
    def available(self) -> bool:
        return self.path is not None

    def seconds(self):
        """Seconds since last input, or None if unknown."""
        if not self.path:
            return None
        try:
            out = subprocess.run([self.path], capture_output=True, text=True, timeout=5, check=True)
            return int(out.stdout.strip()) / 1000.0
        except (subprocess.SubprocessError, ValueError, OSError):
            return None
