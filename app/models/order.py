"""Customer orders and the driver pickup that makes them time-critical."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import OrderStatus


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    placed_at: Mapped[datetime] = mapped_column(DateTime)
    pickup_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    """When the delivery driver is expected. This is the deadline the engine
    treats as immovable -- everything else bends around it."""

    driver_name: Mapped[str | None] = mapped_column(String(120), default=None)
    driver_service: Mapped[str | None] = mapped_column(String(80), default=None)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.OPEN)

    tasks: Mapped[list[Task]] = relationship(  # noqa: F821
        back_populates="order", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Order {self.code} pickup {self.pickup_at:%H:%M}>"
