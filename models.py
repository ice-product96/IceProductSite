from datetime import datetime
from typing import List, Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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


class Article(Base):
    """Blog post or news item."""

    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("kind", "slug", name="uq_article_kind_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # blog | news
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    excerpt: Mapped[str] = mapped_column(String(400), default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    cover_path: Mapped[str] = mapped_column(String(500), default="")
    seo_title: Mapped[str] = mapped_column(String(255), default="")
    seo_description: Mapped[str] = mapped_column(String(320), default="")
    seo_keywords: Mapped[str] = mapped_column(String(500), default="")
    topic: Mapped[str] = mapped_column(String(255), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | ai
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class PipelineSettings(Base):
    """Singleton: content factory settings, all editable in admin."""

    __tablename__ = "pipeline_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    openai_api_key: Mapped[str] = mapped_column(Text, default="")
    http_proxy: Mapped[str] = mapped_column(String(500), default="")
    text_model: Mapped[str] = mapped_column(String(80), default="gpt-5.6-luna")
    image_model: Mapped[str] = mapped_column(String(80), default="gpt-image-2")
    image_size: Mapped[str] = mapped_column(String(32), default="1536x1024")
    generate_images: Mapped[bool] = mapped_column(Boolean, default=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    content_mix: Mapped[str] = mapped_column(String(16), default="both")  # both | blog | news
    auto_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    posts_per_run: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error
    kind: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    article_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
