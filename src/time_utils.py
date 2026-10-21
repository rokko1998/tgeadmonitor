"""Moscow calendar boundaries shared by reporting and the spending ledger."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3), "MSK")


def month_bounds(period: str) -> tuple[datetime, datetime]:
    """Return [start, end) in Moscow, including December/year transitions."""
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=MSK)
    if start.strftime("%Y-%m") != period:
        raise ValueError("Expected YYYY-MM")
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end
