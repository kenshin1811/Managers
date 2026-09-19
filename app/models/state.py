"""A tiny key/value table for facts about the running system itself.

The dashboard has to answer "is the agent actually alive?", and the honest
answer cannot live in a Python variable: the scheduler may be running in a
different process from the one serving the page.  Writing the heartbeat to the
database is what makes the answer true rather than merely reassuring.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

SWEEP_HEARTBEAT = "last_coverage_sweep"
STARTED_AT = "service_started_at"


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime)
