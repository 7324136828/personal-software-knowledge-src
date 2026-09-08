#!/usr/bin/env python3
"""Print the current system time as YYYYMMDDHHMMSS.

Example:
    20260907121900
"""

from datetime import datetime


def current_timestamp() -> str:
    """Return the current local system time as YYYYMMDDHHMMSS."""
    return datetime.now().strftime("%Y%m%d%H%M%S")


if __name__ == "__main__":
    print(current_timestamp())
