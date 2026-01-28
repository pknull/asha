#!/usr/bin/env python3
"""
Natural language time expression parser for cron scheduling.

Converts human-readable time expressions to 5-field cron syntax.

Usage:
    from time_parser import parse_time_expression
    result = parse_time_expression("Every weekday at 9am")
    # Returns: {"success": True, "cron": "0 9 * * 1-5", "human": "Every weekday at 9am"}

CLI:
    python time_parser.py "Every day at 9am"
"""

import re
import sys
from typing import Optional


# Time patterns
TIME_PATTERNS = {
    # 12-hour format: 9am, 9:30am, 9:30 am
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)': lambda m: _parse_12h(m),
    # 24-hour format: 09:00, 14:30
    r'(\d{1,2}):(\d{2})(?!\s*[ap]m)': lambda m: (int(m.group(1)), int(m.group(2))),
    # Noon/midnight
    r'\b(noon|midday)\b': lambda m: (12, 0),
    r'\b(midnight)\b': lambda m: (0, 0),
}

# Day of week mappings
DAYS = {
    'sunday': 0, 'sun': 0,
    'monday': 1, 'mon': 1,
    'tuesday': 2, 'tue': 2, 'tues': 2,
    'wednesday': 3, 'wed': 3,
    'thursday': 4, 'thu': 4, 'thur': 4, 'thurs': 4,
    'friday': 5, 'fri': 5,
    'saturday': 6, 'sat': 6,
}

# Weekday/weekend shortcuts
DAY_GROUPS = {
    'weekday': '1-5',
    'weekdays': '1-5',
    'weekend': '0,6',
    'weekends': '0,6',
}

# Interval patterns
INTERVAL_PATTERNS = [
    # Every N minutes/hours
    (r'every\s+(\d+)\s+minutes?', 'minute_interval'),
    (r'every\s+(\d+)\s+hours?', 'hour_interval'),
    # Every minute/hour
    (r'every\s+minute', 'every_minute'),
    (r'every\s+hour', 'every_hour'),
    # Every day/daily
    (r'every\s+day|daily', 'daily'),
    # Every weekday
    (r'every\s+weekday', 'weekdays'),
    # Every weekend
    (r'every\s+weekend', 'weekends'),
    # Every week/weekly
    (r'every\s+week|weekly', 'weekly'),
    # Every month/monthly
    (r'every\s+month|monthly', 'monthly'),
    # Every [day of week] - but NOT if followed by "and" or another day (use days extraction for those)
    (r'every\s+(sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue|tues|wed|thu|thur|thurs|fri|sat)(?!\s*(?:and|,|\s+(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue|wed|thu|fri|sat)))', 'specific_day'),
    # On [day of week] - single day only
    (r'on\s+(sundays?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?)(?!\s*(?:and|,))', 'specific_day'),
    # [Day] through [Day] / [Day] to [Day]
    (r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\s*(?:through|to|-)\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)', 'day_range'),
]

# Date patterns
DATE_PATTERNS = [
    # First/last of month
    (r'first\s+(?:of\s+)?(?:the\s+)?(?:every\s+)?month', 'first_of_month'),
    (r'last\s+(?:of\s+)?(?:the\s+)?(?:every\s+)?month', 'last_of_month'),
    # Nth of month
    (r'(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?:the\s+)?(?:every\s+)?month', 'day_of_month'),
    # Every N days
    (r'every\s+(\d+)\s+days?', 'day_interval'),
]


def _parse_12h(match) -> tuple[int, int]:
    """Parse 12-hour time format."""
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    period = match.group(3).lower()

    if period == 'pm' and hour != 12:
        hour += 12
    elif period == 'am' and hour == 12:
        hour = 0

    return (hour, minute)


def _extract_time(text: str) -> Optional[tuple[int, int]]:
    """Extract hour and minute from text."""
    text_lower = text.lower()

    for pattern, parser in TIME_PATTERNS.items():
        match = re.search(pattern, text_lower)
        if match:
            return parser(match)

    return None


def _extract_days(text: str) -> Optional[str]:
    """Extract day of week specification from text."""
    text_lower = text.lower()

    # Check for day groups first
    for group, cron_value in DAY_GROUPS.items():
        if group in text_lower:
            return cron_value

    # Check for day ranges
    range_pattern = r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\s*(?:through|to|-)\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)'
    range_match = re.search(range_pattern, text_lower)
    if range_match:
        start_day = range_match.group(1)[:3].lower()
        end_day = range_match.group(2)[:3].lower()
        start_num = DAYS.get(start_day)
        end_num = DAYS.get(end_day)
        if start_num is not None and end_num is not None:
            return f"{start_num}-{end_num}"

    # Check for specific days
    found_days = []
    for day_name, day_num in DAYS.items():
        # Match whole word only
        if re.search(rf'\b{day_name}s?\b', text_lower):
            if day_num not in found_days:
                found_days.append(day_num)

    if found_days:
        found_days.sort()
        if len(found_days) == 1:
            return str(found_days[0])
        return ','.join(str(d) for d in found_days)

    return None


def _extract_interval(text: str) -> Optional[dict]:
    """Extract interval specification from text."""
    text_lower = text.lower()

    for pattern, interval_type in INTERVAL_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            return {
                'type': interval_type,
                'value': match.group(1) if match.lastindex else None,
                'match': match
            }

    return None


def _extract_date(text: str) -> Optional[dict]:
    """Extract date specification from text."""
    text_lower = text.lower()

    for pattern, date_type in DATE_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            return {
                'type': date_type,
                'value': match.group(1) if match.lastindex else None,
                'match': match
            }

    return None


def parse_time_expression(expression: str) -> dict:
    """
    Parse a natural language time expression to cron format.

    Args:
        expression: Natural language time string

    Returns:
        dict with keys:
            - success: bool
            - cron: str (5-field cron expression) if success
            - human: str (normalized human-readable form) if success
            - error: str if not success
    """
    if not expression or not expression.strip():
        return {"success": False, "error": "Empty expression"}

    expr = expression.strip()
    expr_lower = expr.lower()

    # Initialize cron fields: minute hour dom month dow
    minute = '*'
    hour = '*'
    dom = '*'
    month = '*'
    dow = '*'

    human_parts = []

    # Extract time
    time_result = _extract_time(expr)
    if time_result:
        hour, minute = time_result
        minute = str(minute)
        hour = str(hour)

        # Format human-readable time
        h = int(hour)
        m = int(minute)
        if m == 0:
            if h == 0:
                human_parts.append("at midnight")
            elif h == 12:
                human_parts.append("at noon")
            elif h < 12:
                human_parts.append(f"at {h}am")
            else:
                human_parts.append(f"at {h-12}pm")
        else:
            if h < 12:
                human_parts.append(f"at {h}:{m:02d}am")
            elif h == 12:
                human_parts.append(f"at 12:{m:02d}pm")
            else:
                human_parts.append(f"at {h-12}:{m:02d}pm")

    # Check if multiple days are mentioned (e.g., "Tuesday and Thursday")
    # If so, we'll use day extraction instead of interval's specific_day
    has_multiple_days = bool(re.search(
        r'\b(and|,)\s*(sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue|wed|thu|fri|sat)',
        expr_lower
    ))

    # Check if day-of-month is specified (e.g., "15th of every month", "first of month")
    # If so, skip monthly interval to avoid redundancy
    has_day_of_month = bool(re.search(
        r'(?:\d{1,2}(?:st|nd|rd|th)?|first|last)\s+(?:of\s+)?(?:the\s+)?(?:every\s+)?month',
        expr_lower
    ))

    # Extract interval type
    interval = _extract_interval(expr)
    skip_interval = (
        (interval and interval['type'] == 'specific_day' and has_multiple_days) or
        (interval and interval['type'] == 'monthly' and has_day_of_month)
    )
    if interval and not skip_interval:
        itype = interval['type']
        ivalue = interval['value']

        if itype == 'every_minute':
            minute = '*'
            hour = '*'
            human_parts.insert(0, "Every minute")

        elif itype == 'minute_interval':
            minute = f"*/{ivalue}"
            hour = '*'
            human_parts.insert(0, f"Every {ivalue} minutes")

        elif itype == 'every_hour':
            if minute == '*':
                minute = '0'
            human_parts.insert(0, "Every hour")

        elif itype == 'hour_interval':
            if minute == '*':
                minute = '0'
            hour = f"*/{ivalue}"
            human_parts.insert(0, f"Every {ivalue} hours")

        elif itype == 'daily':
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, "Daily")

        elif itype == 'weekdays':
            dow = '1-5'
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '9'
            human_parts.insert(0, "Every weekday")

        elif itype == 'weekends':
            dow = '0,6'
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '9'
            human_parts.insert(0, "Every weekend")

        elif itype == 'weekly':
            dow = '0'  # Default to Sunday
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, "Weekly")

        elif itype == 'monthly':
            dom = '1'
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, "Monthly")

        elif itype == 'specific_day':
            day_name = ivalue[:3].lower() if ivalue else None
            if day_name and day_name in DAYS:
                dow = str(DAYS[day_name])
                if minute == '*':
                    minute = '0'
                if hour == '*':
                    hour = '9'
                human_parts.insert(0, f"Every {day_name.capitalize()}")

        elif itype == 'day_range':
            # Already handled in _extract_days
            pass

    # Extract day specification (if not already set by interval)
    if dow == '*':
        days = _extract_days(expr)
        if days:
            dow = days
            if '-' in days:
                start, end = days.split('-')
                start_name = [k for k, v in DAYS.items() if v == int(start) and len(k) == 3][0]
                end_name = [k for k, v in DAYS.items() if v == int(end) and len(k) == 3][0]
                human_parts.insert(0, f"{start_name.capitalize()}-{end_name.capitalize()}")
            elif ',' in days:
                day_nums = [int(d) for d in days.split(',')]
                day_names = []
                for d in day_nums:
                    name = [k for k, v in DAYS.items() if v == d and len(k) == 3][0]
                    day_names.append(name.capitalize())
                human_parts.insert(0, ', '.join(day_names))

    # Extract date specification
    date_spec = _extract_date(expr)
    if date_spec:
        dtype = date_spec['type']
        dvalue = date_spec['value']

        if dtype == 'first_of_month':
            dom = '1'
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, "First of every month")

        elif dtype == 'last_of_month':
            # Cron doesn't support "last day" directly; use 28-31 approximation
            dom = '28-31'
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, "Last of every month")

        elif dtype == 'day_of_month':
            dom = dvalue
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, f"{dvalue}th of every month")

        elif dtype == 'day_interval':
            # Approximate with day-of-month step (not perfect for >28 days)
            dom = f"*/{dvalue}"
            if minute == '*':
                minute = '0'
            if hour == '*':
                hour = '0'
            human_parts.insert(0, f"Every {dvalue} days")

    # Build cron expression
    cron = f"{minute} {hour} {dom} {month} {dow}"

    # Build human-readable string
    if not human_parts:
        # Fallback: describe the cron directly
        human = expression
    else:
        human = ' '.join(human_parts)

    # Validate we got something meaningful
    if cron == "* * * * *":
        # Every minute - valid but confirm intent
        if 'every minute' not in expr_lower and 'every 1 minute' not in expr_lower:
            return {
                "success": False,
                "error": f"Could not parse time expression: '{expression}'. Try 'Every day at 9am' or 'Every weekday at 2pm'"
            }

    return {
        "success": True,
        "cron": cron,
        "human": human,
        "original": expression
    }


def validate_cron(cron: str) -> bool:
    """Validate a 5-field cron expression."""
    parts = cron.split()
    if len(parts) != 5:
        return False

    # Basic validation for each field
    for part in parts:
        if part == '*':
            continue
        # Handle step values
        if '/' in part:
            base, step = part.split('/', 1)
            if not step.isdigit():
                return False
            part = base
        # Handle ranges
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                if not (start.isdigit() and end.isdigit()):
                    return False
            except ValueError:
                return False
            continue
        # Handle lists
        for segment in part.split(','):
            if segment != '*' and not segment.isdigit():
                return False

    return True


# Common expressions for testing/examples
EXAMPLES = [
    "Every day at 9am",
    "Every weekday at 9am",
    "Every Monday at 2pm",
    "Every hour",
    "Every 15 minutes",
    "Daily at midnight",
    "Every Sunday at 6pm",
    "First of every month at noon",
    "Every 2 hours",
    "Monday through Friday at 8:30am",
    "Every weekend at 10am",
    "Weekly",
    "Monthly",
]


def main():
    """CLI interface for time parser."""
    if len(sys.argv) < 2:
        print("Usage: python time_parser.py \"<time expression>\"")
        print("\nExamples:")
        for example in EXAMPLES:
            result = parse_time_expression(example)
            if result["success"]:
                print(f"  \"{example}\" -> {result['cron']}")
        sys.exit(0)

    expression = ' '.join(sys.argv[1:])
    result = parse_time_expression(expression)

    if result["success"]:
        print(f"Cron: {result['cron']}")
        print(f"Human: {result['human']}")
        sys.exit(0)
    else:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
