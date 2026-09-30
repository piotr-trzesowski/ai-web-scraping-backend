from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def window(months: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    if months not in (1, 2, 3):
        raise ValueError("months must be 1, 2 or 3")
    start = (now or datetime.now(WARSAW)).astimezone(WARSAW)
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    end = start.replace(year=year, month=month, day=min(start.day, monthrange(year, month)[1]))
    return start, end
