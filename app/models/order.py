"""Orders, their line items, and the van that will not wait for them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import Channel, OrderStatus


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(120), default="")
    channel: Mapped[str] = mapped_column(String(20), default=Channel.RETAIL)

    destination_id: Mapped[int | None] = mapped_column(
        ForeignKey("destinations.id", ondelete="SET NULL"), default=None, index=True
    )

    placed_at: Mapped[datetime] = mapped_column(DateTime)
    pickup_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    """When the van leaves for this order. This is the deadline the engine
    treats as immovable -- everything else bends around it."""

    driver_name: Mapped[str | None] = mapped_column(String(120), default=None)
    driver_service: Mapped[str | None] = mapped_column(String(80), default=None)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.OPEN)

    destination: Mapped[Destination | None] = relationship(lazy="selectin")  # noqa: F821
    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    tasks: Mapped[list[Task]] = relationship(  # noqa: F821
        back_populates="order", lazy="selectin"
    )

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def where(self) -> str:
        if self.destination is not None:
            return f"{self.destination.name}, {self.destination.suburb}"
        return self.customer_name or "unassigned"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Order {self.code} {self.units}u van {self.pickup_at:%H:%M}>"


class OrderLine(Base):
    """One product on one order.

    ``packed`` is tracked separately from ``quantity`` because a part-packed
    line is the normal state of affairs mid-shift, and the difference between
    them is what the planner still has to find time for.
    """

    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    packed: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped[Order] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship(lazy="selectin")  # noqa: F821

    @property
    def outstanding(self) -> int:
        return max(0, self.quantity - self.packed)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OrderLine {self.quantity}x product {self.product_id}>"
