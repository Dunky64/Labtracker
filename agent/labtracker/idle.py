"""Keyboard/mouse idle time. Reads ONLY a number: milliseconds since the last input.

No keystrokes, window titles, screenshots or anything else are ever collected.
"""
import os
import subprocess

# Fixed, root-owned locations only. Searching $PATH would let anything that can write to
# ~/.local/bin (which comes first on Linux Mint) substitute its own program.
TRUSTED_PATHS = ("/usr/bin/xprintidle", "/usr/local/bin/xprintidle")


class IdleSource:
    def __init__(self):
        self.path = next((p for p in TRUSTED_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)), None)

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
