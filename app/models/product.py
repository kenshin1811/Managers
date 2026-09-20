"""What the factory packs.

Two things about a product drive every decision downstream: whether it has to
come out of the general freezer first, and how long a unit takes to pack.  The
first creates a dependency the planner cannot schedule around; the second is
what turns "sixty jam donuts" into minutes of somebody's evening.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ProductKind


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default=ProductKind.FRESH)
    category: Mapped[str] = mapped_column(String(60), default="")

    pack_seconds_per_unit: Mapped[float] = mapped_column(Float, default=6.0)
    """How long one unit takes to pack and label. Multiplied by the quantity,
    this is the whole basis of the day's capacity arithmetic."""

    units_per_tray: Mapped[int] = mapped_column(Integer, default=12)
    needs_label: Mapped[bool] = mapped_column(Boolean, default=True)

    required_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), default=None
    )
    required_skill: Mapped[Skill | None] = relationship(lazy="selectin")  # noqa: F821

    @property
    def from_freezer(self) -> bool:
        """Frozen stock has to be pulled from the general freezer before it can
        be packed. Fresh product comes straight off production."""
        return self.kind == ProductKind.FROZEN

    def pack_minutes(self, quantity: int) -> float:
        return quantity * self.pack_seconds_per_unit / 60.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Product {self.code} {self.kind}>"
