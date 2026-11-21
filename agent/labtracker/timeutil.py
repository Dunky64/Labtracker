"""UK time helpers."""
from datetime import datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%H:%M")


def fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%a %d %b %H:%M")


def parse_local(date_text: str, time_text: str) -> float:
    """'2026-11-20', '17:30' (UK time) -> epoch seconds."""
    d = datetime.strptime(f"{date_text.strip()} {time_text.strip()}", "%Y-%m-%d %H:%M")
    return d.replace(tzinfo=UK).timestamp()


def local_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%Y-%m-%d")
