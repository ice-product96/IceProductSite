from datetime import datetime
from typing import List, Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON
from database import Base


class SiteSettings(Base):
    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    slogan: Mapped[str] = mapped_column(String(255), default="")
    short_description: Mapped[str] = mapped_column(Text, default="")
    meta_title: Mapped[str] = mapped_column(String(255), default="")
    meta_description: Mapped[str] = mapped_column(String(255), default="")
    yandex_metrika_code: Mapped[str] = mapped_column(Text, default="")


class App(Base):
    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    short_description: Mapped[str] = mapped_column(String(160), default="")
    full_description: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)
    external_url: Mapped[str] = mapped_column(String(500), default="")
    icon_path: Mapped[str] = mapped_column(String(500), default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    screenshots: Mapped[List["AppScreenshot"]] = relationship(
        "AppScreenshot",
        back_populates="app",
        cascade="all, delete-orphan",
        order_by="AppScreenshot.sort_order",
    )


class AppScreenshot(Base):
    __tablename__ = "app_screenshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    app_id: Mapped[int] = mapped_column(Integer, ForeignKey("apps.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    app: Mapped["App"] = relationship("App", back_populates="screenshots")
