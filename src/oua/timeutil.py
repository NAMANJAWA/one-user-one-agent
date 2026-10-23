"""Time helpers. Everything inside the agent is an aware UTC datetime; IST is only used for
calendar-day and quiet-hour rules, which the policy defines in IST."""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(UTC)


def iso(dt):
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def ist_day(dt):
    return dt.astimezone(IST).date()


def ist_hour(dt):
    return dt.astimezone(IST).hour


def in_quiet_hours(dt, start_h=22, end_h=8):
    h = ist_hour(dt)
    return h >= start_h or h < end_h


def at_ist(day, hour, minute=0):
    """UTC datetime for an IST calendar day and wall-clock time."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(UTC)


def ms(td):
    return int(round(td.total_seconds() * 1000))


def add_ms(dt, millis):
    return dt + timedelta(milliseconds=millis)
