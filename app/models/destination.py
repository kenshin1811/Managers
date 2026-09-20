"""Where the van goes.

A destination is not just an address: it carries the channel, which decides
how the work is handled.  Retail shops take mixed consignments, wholesale
takes bulk, and the wholesale packer team is an internal handover -- product
leaves this team's hands but never leaves the building.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import Channel


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    suburb: Mapped[str] = mapped_column(String(80), default="")
    channel: Mapped[str] = mapped_column(String(20), default=Channel.RETAIL)

    run: Mapped[str] = mapped_column(String(40), default="north")
    """Which delivery run it sits on. Everything on a run leaves together, so
    one late consignment holds up every stop behind it."""

    stop_order: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def internal(self) -> bool:
        """The wholesale packer team is down the corridor, not across town."""
        return self.channel == Channel.INTERNAL

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Destination {self.code} {self.suburb}>"
