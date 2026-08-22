"""Surge I&W — a tipping-and-queuing engine for tactical indications and warning.

The engine collects social media and news, flight, lodging and car-rental data,
tips paid collection from what it finds, and correlates the result into a scored
warning. WHAT it is looking for comes from a mission pack read at startup — see
`services/mission.py` — not from this package.
"""

__version__ = "0.1.0"
