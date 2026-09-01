import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, selectinload
from unidecode import unidecode
import re

try:
    import imghdr as _imghdr
except ModuleNotFoundError:  # removed in Python 3.13
    _imghdr = None

import bleach
from markupsafe import Markup, escape

load_dotenv()

from auth import (
    generate_csrf_token,
    generate_session_token,
    validate_csrf_token,
    verify_admin,
)
from database import Base, engine, get_db, SessionLocal
from models import App, AppScreenshot, Article, PipelineRun, SiteSettings
from pipeline import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEXT_MODEL,
    get_pipeline_settings,
    normalize_http_proxy,
    run_once as pipeline_run_once,
    start_pipeline_loop,
    unique_article_slug,
)

# ── App setup ─────────────────────────────────────────────────────────────────

Base.metadata.create_all(bind=engine)


def _ensure_site_settings_schema() -> None:
    """Lightweight runtime migration for new settings fields."""
    inspector = inspect(engine)
    try:
        cols = {col["name"] for col in inspector.get_columns("site_settings")}
    except Exception:
        return
    if "yandex_metrika_code" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE site_settings ADD COLUMN yandex_metrika_code TEXT DEFAULT ''"))


def _ensure_pipeline_settings_schema() -> None:
    inspector = inspect(engine)
    try:
        cols = {col["name"] for col in inspector.get_columns("pipeline_settings")}
    except Exception:
        return
    if "http_proxy" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pipeline_settings ADD COLUMN http_proxy VARCHAR(500) DEFAULT ''"))


_ensure_site_settings_schema()
_ensure_pipeline_settings_schema()

app = FastAPI(title="Айс.Продукт")
start_pipeline_loop(app)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
ICONS_DIR = UPLOADS_DIR / "icons"
SCREENSHOTS_DIR = UPLOADS_DIR / "screenshots"
COVERS_DIR = UPLOADS_DIR / "covers"

for d in (ICONS_DIR, SCREENSHOTS_DIR, COVERS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_FULL_DESC_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "h2",
        "h3",
        "h4",
        "ul",
        "ol",
        "li",
        "a",
        "blockquote",
    }
)
_FULL_DESC_ATTRS = {"a": ["href", "title", "target", "rel"]}
_FULL_DESC_RE = re.compile(
    r"</?(?:p|div|h[1-6]|ul|ol|li|strong|b|em|i|u|a|br|blockquote)\b",
    re.I,
)


def _utf8_safe_text(text: Optional[str]) -> str:
    """Lone UTF-16 surrogates (possible after paste from Office/HTML) cannot be
    encoded as UTF-8 and crash the ASGI stack with UnicodeEncodeError → 500."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    return text.encode("utf-8", errors="replace").decode("utf-8")


def sanitize_full_description(html: str) -> str:
    if html is None:
        return ""
    if not isinstance(html, str):
        html = str(html)
    html = _utf8_safe_text(html.strip())
    if not html:
        return ""
    try:
        return bleach.clean(
            html,
            tags=_FULL_DESC_TAGS,
            attributes=_FULL_DESC_ATTRS,
            protocols=frozenset(("http", "https", "mailto")),
            strip=True,
        )
    except Exception:
        return re.sub(r"<[^>]+>", "", html)


def full_description_html(value: Optional[str]) -> Markup:
    if not value:
        return Markup("")
    try:
        cleaned = sanitize_full_description(value)
        cleaned = _utf8_safe_text(cleaned)
        if _FULL_DESC_RE.search(cleaned):
            return Markup(cleaned)
        return Markup(
            '<div class="text-gray-500 leading-relaxed whitespace-pre-wrap">'
            f"{escape(cleaned)}</div>"
        )
    except Exception:
        plain = _utf8_safe_text(re.sub(r"<[^>]+>", "", str(value)))
        return Markup(
            '<div class="text-gray-500 leading-relaxed whitespace-pre-wrap">'
            f"{escape(plain)}</div>"
        )


templates.env.filters["full_description_html"] = full_description_html
templates.env.filters["utf8_safe"] = _utf8_safe_text


def _jinja_finalize(value):
    """Normalize all template text output so the HTML response always encodes as UTF-8."""
    if isinstance(value, Markup):
        # Surrogates inside Markup (e.g. WYSIWYG HTML) still break response encoding.
        return Markup(_utf8_safe_text(str(value)))
    if isinstance(value, str):
        return _utf8_safe_text(value)
    return value


templates.env.finalize = _jinja_finalize

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
MAX_ICON_SIZE = int(os.getenv("MAX_ICON_SIZE", 524288))       # 512 KB
MAX_SCREENSHOT_SIZE = int(os.getenv("MAX_SCREENSHOT_SIZE", 2097152))  # 2 MB
MAX_COVER_SIZE = int(os.getenv("MAX_COVER_SIZE", 3145728))  # 3 MB

# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = unidecode(text).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def unique_slug(base: str, db: Session, exclude_id: Optional[int] = None) -> str:
    slug = slugify(base)
    candidate = slug
    counter = 1
    while True:
        q = db.query(App).filter(App.slug == candidate)
        if exclude_id:
            q = q.filter(App.id != exclude_id)
        if not q.first():
            return candidate
        candidate = f"{slug}-{counter}"
        counter += 1


ALLOWED_IMAGE_TYPES = {"jpeg", "png", "gif", "webp"}
ALLOWED_ICON_TYPES = {"jpeg", "png", "gif", "webp"}


def validate_image(data: bytes, allowed: set) -> bool:
    detected = None
    if _imghdr is not None:
        detected = _imghdr.what(None, h=data)
    if detected is None and len(data) >= 12:
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            detected = "webp"
    if detected is None:
        if data[:5] == b"<?xml" or (len(data) >= 4 and data[:4] == b"<svg"):
            detected = "svg"
        elif len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            detected = "png"
        elif len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            detected = "jpeg"
        elif len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
            detected = "gif"
    return detected in allowed


def save_image(data: bytes, directory: Path, max_size: int, max_dim: int = 2048) -> str:
    img = Image.open(__import__("io").BytesIO(data))
    img = img.convert("RGBA") if img.mode in ("RGBA", "P") else img.convert("RGB")
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    ext = "png" if img.mode == "RGBA" else "jpeg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = directory / filename
    save_kwargs = {"format": "PNG"} if ext == "png" else {"format": "JPEG", "quality": 88, "optimize": True}
    img.save(filepath, **save_kwargs)
    return filename


def delete_file(relative_path: str) -> None:
    if not relative_path:
        return
    full = STATIC_DIR / relative_path.lstrip("/static/").lstrip("static/")
    if full.exists():
        full.unlink(missing_ok=True)


def get_settings(db: Session) -> SiteSettings:
    s = db.query(SiteSettings).first()
    if not s:
        s = SiteSettings(
            slogan="Умные инструменты для современных команд",
            short_description="",
            meta_title="Айс.Продукт",
            meta_description="",
            yandex_metrika_code="",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def published_articles(db: Session, kind: str, limit: Optional[int] = None):
    q = (
        db.query(Article)
        .filter(Article.kind == kind, Article.is_published == True)
        .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
    )
    if limit:
        q = q.limit(limit)
    return q.all()


# ── Session helpers ───────────────────────────────────────────────────────────

SESSION_COOKIE = "ice_admin_session"
SESSION_DATA: dict[str, dict] = {}  # simple in-memory store (fine for single-process)


def get_session(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    return SESSION_DATA.get(token, {})


def is_authenticated(request: Request) -> bool:
    return get_session(request).get("admin") is True


def require_admin(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})


def get_csrf(request: Request) -> str:
    session_token = request.cookies.get(SESSION_COOKIE, "anon")
    return generate_csrf_token(session_token)


def check_csrf(request: Request, csrf_token: str) -> bool:
    session_token = request.cookies.get(SESSION_COOKIE, "anon")
    return validate_csrf_token(csrf_token, session_token)


# ── Error handlers ────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc):
    db = SessionLocal()
    try:
        settings = get_settings(db)
        return templates.TemplateResponse(
            "404.html", {"request": request, "settings": settings}, status_code=404
        )
    finally:
        db.close()


@app.exception_handler(500)
async def server_error(request: Request, exc):
    db = SessionLocal()
    try:
        settings = get_settings(db)
        return templates.TemplateResponse(
            "500.html", {"request": request, "settings": settings}, status_code=500
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    apps = (
        db.query(App)
        .filter(App.is_published == True)
        .order_by(App.sort_order, App.id)
        .all()
    )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "settings": settings,
            "apps": apps,
            "blog_posts": published_articles(db, "blog", 3),
            "news_posts": published_articles(db, "news", 3),
        },
    )


@app.get("/contacts", response_class=HTMLResponse)
@app.get("/contacts/", response_class=HTMLResponse)
@app.get("/support", response_class=HTMLResponse)
async def contacts_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    return templates.TemplateResponse(
        "contacts.html",
        {
            "request": request,
            "settings": settings,
        },
    )


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy/", response_class=HTMLResponse)
@app.get("/politika", response_class=HTMLResponse)
async def privacy_page(request: Request, db: Session = Depends(get_db)):
    settings = get_settings(db)
    return templates.TemplateResponse(
        "privacy.html",
        {
            "request": request,
            "settings": settings,
        },
    )


def _article_list(kind: str, request: Request, db: Session, page: int = 1):
    settings = get_settings(db)
    per_page = 12
    page = max(1, page)
    q = (
        db.query(Article)
        .filter(Article.kind == kind, Article.is_published == True)
        .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
    )
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    pages = max(1, (total + per_page - 1) // per_page)
    prefix = "/blog" if kind == "blog" else "/news"
    return templates.TemplateResponse(
        "article_list.html",
        {
            "request": request,
            "settings": settings,
            "kind": kind,
            "items": items,
            "page": page,
            "pages": pages,
            "total": total,
            "canonical": _abs_url(request, prefix),
        },
    )


@app.get("/blog", response_class=HTMLResponse)
@app.get("/blog/", response_class=HTMLResponse)
async def blog_index(request: Request, db: Session = Depends(get_db), page: int = 1):
    return _article_list("blog", request, db, page)


@app.get("/news", response_class=HTMLResponse)
@app.get("/news/", response_class=HTMLResponse)
@app.get("/novosti", response_class=HTMLResponse)
async def news_index(request: Request, db: Session = Depends(get_db), page: int = 1):
    return _article_list("news", request, db, page)


def _escape_xml(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _abs_url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def _rss_feed(kind: str, request: Request, db: Session) -> Response:
    items = published_articles(db, kind, 40)
    channel_title = "Блог Айс.Продукт" if kind == "blog" else "Новости Айс.Продукт"
    prefix = "/blog" if kind == "blog" else "/news"
    channel_link = _abs_url(request, prefix)
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{_escape_xml(channel_title)}</title>",
        f"<link>{_escape_xml(channel_link)}</link>",
        f"<description>{_escape_xml(channel_title)}</description>",
        "<language>ru</language>",
    ]
    for item in items:
        loc = _abs_url(request, f"{prefix}/{item.slug}")
        pub = (item.published_at or item.created_at)
        pub_str = pub.strftime("%a, %d %b %Y %H:%M:%S GMT") if pub else ""
        rows.append("<item>")
        rows.append(f"<title>{_escape_xml(item.title)}</title>")
        rows.append(f"<link>{_escape_xml(loc)}</link>")
        rows.append(f"<guid>{_escape_xml(loc)}</guid>")
        rows.append(f"<description>{_escape_xml(item.excerpt)}</description>")
        if pub_str:
            rows.append(f"<pubDate>{pub_str}</pubDate>")
        rows.append("</item>")
    rows.extend(["</channel>", "</rss>"])
    return Response("\n".join(rows), media_type="application/rss+xml; charset=utf-8")


@app.get("/blog/feed.xml")
async def blog_feed(request: Request, db: Session = Depends(get_db)):
    return _rss_feed("blog", request, db)


@app.get("/news/feed.xml")
async def news_feed(request: Request, db: Session = Depends(get_db)):
    return _rss_feed("news", request, db)


@app.get("/blog/{slug}", response_class=HTMLResponse)
async def blog_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    return _article_detail("blog", slug, request, db)


@app.get("/news/{slug}", response_class=HTMLResponse)
async def news_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    return _article_detail("news", slug, request, db)


def _article_detail(kind: str, slug: str, request: Request, db: Session):
    settings = get_settings(db)
    article = (
        db.query(Article)
        .filter(Article.kind == kind, Article.slug == slug, Article.is_published == True)
        .first()
    )
    if not article:
        raise HTTPException(status_code=404)
    related = (
        db.query(Article)
        .filter(
            Article.kind == kind,
            Article.is_published == True,
            Article.id != article.id,
        )
        .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
        .limit(3)
        .all()
    )
    prefix = "/blog" if kind == "blog" else "/news"
    return templates.TemplateResponse(
        "article_detail.html",
        {
            "request": request,
            "settings": settings,
            "article": article,
            "kind": kind,
            "related": related,
            "body_html": full_description_html(article.body_html),
            "canonical": _abs_url(request, f"{prefix}/{article.slug}"),
        },
    )


@app.get("/sitemap.xml")
async def sitemap(request: Request, db: Session = Depends(get_db)):
    base = str(request.base_url).rstrip("/")
    entries = [("/", None), ("/contacts", None), ("/privacy", None), ("/blog", None), ("/news", None)]
    apps = db.query(App).filter(App.is_published == True).all()
    entries.extend((f"/app/{a.slug}", a.updated_at) for a in apps)
    articles = db.query(Article).filter(Article.is_published == True).all()
    for a in articles:
        prefix = "/blog" if a.kind == "blog" else "/news"
        entries.append((f"{prefix}/{a.slug}", a.published_at or a.updated_at))
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, dt in entries:
        loc = f"  <url><loc>{base}{path}</loc>"
        if dt:
            loc += f"<lastmod>{dt.strftime('%Y-%m-%d')}</lastmod>"
        loc += "</url>"
        body.append(loc)
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")


@app.get("/robots.txt")
async def robots(request: Request):
    base = str(request.base_url).rstrip("/")
    txt = f"User-agent: *\nAllow: /\nDisallow: /admin\nSitemap: {base}/sitemap.xml\n"
    return Response(txt, media_type="text/plain")


@app.get("/app/{slug}", response_class=HTMLResponse)
async def app_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    # Сначала настройки: при первом запуске get_settings делает commit(), из‑за чего
    # все уже загруженные ORM-объекты помечаются expired. Загружаем продукт после этого.
    settings = get_settings(db)
    product = (
        db.query(App)
        .options(selectinload(App.screenshots))
        .filter(App.slug == slug, App.is_published == True)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404)
    # Не полагаемся на truthiness ORM-значения: strip() — как в админке.
    raw_desc = product.full_description
    if isinstance(raw_desc, bytes):
        raw_desc = raw_desc.decode("utf-8", errors="replace")
    has_long_desc = bool((raw_desc or "").strip())
    description_html = (
        full_description_html(raw_desc) if has_long_desc else None
    )
    return templates.TemplateResponse(
        "app_detail.html",
        {
            "request": request,
            "settings": settings,
            "product": product,
            "description_html": description_html,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/admin/apps", status_code=302)
    return RedirectResponse("/admin/login", status_code=302)


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/admin/apps", status_code=302)
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/login.html", {"request": request, "csrf": csrf, "error": None}
    )


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "csrf": get_csrf(request), "error": "Неверный CSRF-токен."},
            status_code=400,
        )
    if verify_admin(username, password):
        token = generate_session_token()
        SESSION_DATA[token] = {"admin": True}
        response = RedirectResponse("/admin/apps", status_code=302)
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86400 * 7
        )
        return response
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "csrf": csrf, "error": "Неверный логин или пароль."},
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    SESSION_DATA.pop(token, None)
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    settings = get_settings(db)
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/settings.html",
        {"request": request, "settings": settings, "csrf": csrf, "saved": False},
    )


@app.post("/admin/settings", response_class=HTMLResponse)
async def admin_settings_save(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    slogan: str = Form(""),
    short_description: str = Form(""),
    meta_title: str = Form(""),
    meta_description: str = Form(""),
    yandex_metrika_code: str = Form(""),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")
    settings = get_settings(db)
    settings.slogan = slogan.strip()
    settings.short_description = short_description.strip()
    settings.meta_title = meta_title.strip()
    settings.meta_description = meta_description.strip()
    settings.yandex_metrika_code = yandex_metrika_code.strip()
    db.commit()
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/settings.html",
        {"request": request, "settings": settings, "csrf": csrf, "saved": True},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — APPS LIST
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/apps", response_class=HTMLResponse)
async def admin_apps_list(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    apps = db.query(App).order_by(App.sort_order, App.id).all()
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/apps_list.html",
        {"request": request, "apps": apps, "csrf": csrf},
    )


@app.post("/admin/apps/reorder")
async def admin_apps_reorder(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    body = await request.json()
    csrf_token = body.get("csrf_token", "")
    if not check_csrf(request, csrf_token):
        return JSONResponse({"error": "invalid csrf"}, status_code=403)
    order: list[int] = body.get("order", [])
    for idx, app_id in enumerate(order):
        db.query(App).filter(App.id == app_id).update({"sort_order": idx})
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/admin/apps/{app_id}/delete")
async def admin_app_delete(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    product = db.query(App).filter(App.id == app_id).first()
    if not product:
        raise HTTPException(status_code=404)
    # delete files
    delete_file(product.icon_path)
    for ss in product.screenshots:
        delete_file(ss.file_path)
    db.delete(product)
    db.commit()
    return RedirectResponse("/admin/apps", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — APP FORM (create / edit)
# ═══════════════════════════════════════════════════════════════════════════════

def _render_app_form(
    request: Request,
    db: Session,
    product: Optional[App] = None,
    errors: Optional[list[str]] = None,
):
    csrf = get_csrf(request)
    return templates.TemplateResponse(
        "admin/app_form.html",
        {
            "request": request,
            "product": product,
            "errors": errors or [],
            "csrf": csrf,
            "is_new": product is None or product.id is None,
        },
    )


@app.get("/admin/apps/new", response_class=HTMLResponse)
async def admin_app_new(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return _render_app_form(request, db)


@app.get("/admin/apps/{app_id}/edit", response_class=HTMLResponse)
async def admin_app_edit(app_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    product = db.query(App).filter(App.id == app_id).first()
    if not product:
        raise HTTPException(status_code=404)
    return _render_app_form(request, db, product=product)


async def _process_app_form(
    request: Request,
    db: Session,
    product: Optional[App],
    csrf_token: str,
    name: str,
    slug: str,
    short_description: str,
    full_description: str,
    features_raw: str,
    external_url: str,
    is_published: bool,
    sort_order: int,
    icon: Optional[UploadFile],
    screenshots: list[UploadFile],
):
    errors = []

    if not check_csrf(request, csrf_token):
        errors.append("Неверный CSRF-токен.")
        return errors, product

    name = name.strip()
    if not name:
        errors.append("Название обязательно.")

    slug = slug.strip() or slugify(name)
    slug = slugify(slug)
    if not slug:
        errors.append("Не удалось сформировать slug.")

    if errors:
        return errors, product

    # Check slug uniqueness
    exclude_id = product.id if product and product.id else None
    q = db.query(App).filter(App.slug == slug)
    if exclude_id:
        q = q.filter(App.id != exclude_id)
    if q.first():
        errors.append(f"Slug «{slug}» уже используется. Укажите другой.")
        return errors, product

    features = [f.strip() for f in features_raw.splitlines() if f.strip()]

    is_new = product is None or not product.id

    if is_new:
        product = App()
        db.add(product)

    product.name = name
    product.slug = slug
    product.short_description = short_description[:160].strip()
    product.full_description = sanitize_full_description(full_description)
    product.features = features
    product.external_url = external_url.strip()
    product.is_published = is_published
    product.sort_order = sort_order

    # Icon upload
    if icon and icon.filename:
        icon_data = await icon.read()
        if len(icon_data) > MAX_ICON_SIZE:
            errors.append(f"Иконка превышает {MAX_ICON_SIZE // 1024} KB.")
        elif not validate_image(icon_data, ALLOWED_ICON_TYPES):
            errors.append("Иконка должна быть PNG, JPEG или WebP.")
        else:
            old_icon = product.icon_path
            filename = save_image(icon_data, ICONS_DIR, MAX_ICON_SIZE, max_dim=512)
            product.icon_path = f"/static/uploads/icons/{filename}"
            if old_icon:
                delete_file(old_icon)

    if errors:
        return errors, product

    db.flush()  # get product.id if new

    # Screenshots
    existing_count = len(product.screenshots)
    slots_left = 5 - existing_count

    for ss_file in screenshots:
        if not ss_file.filename:
            continue
        if slots_left <= 0:
            errors.append("Максимум 5 скриншотов.")
            break
        data = await ss_file.read()
        if len(data) > MAX_SCREENSHOT_SIZE:
            errors.append(f"Файл {ss_file.filename} превышает {MAX_SCREENSHOT_SIZE // 1024 // 1024} MB.")
            continue
        if not validate_image(data, ALLOWED_IMAGE_TYPES):
            errors.append(f"Файл {ss_file.filename} не является допустимым изображением.")
            continue
        filename = save_image(data, SCREENSHOTS_DIR, MAX_SCREENSHOT_SIZE)
        ss = AppScreenshot(
            app_id=product.id,
            file_path=f"/static/uploads/screenshots/{filename}",
            sort_order=existing_count,
        )
        db.add(ss)
        existing_count += 1
        slots_left -= 1

    return errors, product


@app.post("/admin/apps/new", response_class=HTMLResponse)
async def admin_app_create(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    name: str = Form(""),
    slug: str = Form(""),
    short_description: str = Form(""),
    full_description: str = Form(""),
    features_raw: str = Form(""),
    external_url: str = Form(""),
    is_published: bool = Form(False),
    sort_order: int = Form(0),
    icon: Optional[UploadFile] = File(None),
    screenshots: list[UploadFile] = File([]),
):
    require_admin(request)
    errors, product = await _process_app_form(
        request, db, None, csrf_token, name, slug, short_description,
        full_description, features_raw, external_url, is_published, sort_order,
        icon, screenshots,
    )
    if errors:
        if product is None:
            product = App(name=name, slug=slug, short_description=short_description,
                          full_description=full_description, external_url=external_url,
                          is_published=is_published, sort_order=sort_order)
        return _render_app_form(request, db, product=product, errors=errors)
    db.commit()
    return RedirectResponse("/admin/apps", status_code=302)


@app.post("/admin/apps/{app_id}/edit", response_class=HTMLResponse)
async def admin_app_update(
    app_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    name: str = Form(""),
    slug: str = Form(""),
    short_description: str = Form(""),
    full_description: str = Form(""),
    features_raw: str = Form(""),
    external_url: str = Form(""),
    is_published: bool = Form(False),
    sort_order: int = Form(0),
    icon: Optional[UploadFile] = File(None),
    screenshots: list[UploadFile] = File([]),
):
    require_admin(request)
    product = db.query(App).filter(App.id == app_id).first()
    if not product:
        raise HTTPException(status_code=404)
    errors, product = await _process_app_form(
        request, db, product, csrf_token, name, slug, short_description,
        full_description, features_raw, external_url, is_published, sort_order,
        icon, screenshots,
    )
    if errors:
        db.rollback()
        product = db.query(App).filter(App.id == app_id).first()
        return _render_app_form(request, db, product=product, errors=errors)
    db.commit()
    return RedirectResponse("/admin/apps", status_code=302)


@app.post("/admin/screenshots/{screenshot_id}/delete")
async def admin_screenshot_delete(
    screenshot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    ss = db.query(AppScreenshot).filter(AppScreenshot.id == screenshot_id).first()
    if not ss:
        raise HTTPException(status_code=404)
    app_id = ss.app_id
    delete_file(ss.file_path)
    db.delete(ss)
    db.commit()
    return RedirectResponse(f"/admin/apps/{app_id}/edit", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — CONTENT (blog / news)
# ═══════════════════════════════════════════════════════════════════════════════

KIND_LABEL = {"blog": "Блог", "news": "Новости"}


def _require_kind(kind: str) -> str:
    if kind not in ("blog", "news"):
        raise HTTPException(status_code=404)
    return kind


@app.get("/admin/content", response_class=HTMLResponse)
async def admin_content_list(
    request: Request,
    db: Session = Depends(get_db),
    kind: str = "blog",
):
    require_admin(request)
    kind = _require_kind(kind)
    items = (
        db.query(Article)
        .filter(Article.kind == kind)
        .order_by(Article.id.desc())
        .all()
    )
    return templates.TemplateResponse(
        "admin/content_list.html",
        {
            "request": request,
            "items": items,
            "kind": kind,
            "kind_label": KIND_LABEL[kind],
            "csrf": get_csrf(request),
        },
    )


def _render_article_form(request, article: Optional[Article], kind: str, errors=None, saved=False):
    return templates.TemplateResponse(
        "admin/content_form.html",
        {
            "request": request,
            "article": article,
            "kind": kind,
            "kind_label": KIND_LABEL.get(kind, "Материал"),
            "errors": errors or [],
            "csrf": get_csrf(request),
            "is_new": article is None or not article.id,
            "saved": saved,
        },
    )


@app.get("/admin/content/new", response_class=HTMLResponse)
async def admin_content_new(request: Request, kind: str = "blog"):
    require_admin(request)
    kind = _require_kind(kind)
    return _render_article_form(request, None, kind)


@app.get("/admin/content/{article_id}/edit", response_class=HTMLResponse)
async def admin_content_edit(article_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404)
    return _render_article_form(request, article, article.kind)


async def _save_article_from_form(
    request: Request,
    db: Session,
    article: Optional[Article],
    kind: str,
    csrf_token: str,
    title: str,
    slug: str,
    excerpt: str,
    body_html: str,
    seo_title: str,
    seo_description: str,
    seo_keywords: str,
    topic: str,
    is_published: bool,
    cover: Optional[UploadFile],
):
    errors = []
    if not check_csrf(request, csrf_token):
        return ["Неверный CSRF-токен."], article
    title = title.strip()
    if not title:
        errors.append("Заголовок обязателен.")
    kind = _require_kind(kind)
    slug = unique_article_slug(db, kind, slug.strip() or title, exclude_id=article.id if article and article.id else None)
    if errors:
        return errors, article
    is_new = article is None or not article.id
    if is_new:
        article = Article(kind=kind, source="manual")
        db.add(article)
    article.kind = kind
    article.title = title[:255]
    article.slug = slug
    article.excerpt = excerpt.strip()[:400]
    article.body_html = sanitize_full_description(body_html)
    article.seo_title = seo_title.strip()[:255]
    article.seo_description = seo_description.strip()[:320]
    article.seo_keywords = seo_keywords.strip()[:500]
    article.topic = topic.strip()[:255]
    was_pub = bool(article.is_published)
    article.is_published = is_published
    if is_published and not was_pub:
        article.published_at = datetime.utcnow()
    if not is_published:
        article.published_at = article.published_at
    if cover and cover.filename:
        data = await cover.read()
        if len(data) > MAX_COVER_SIZE:
            errors.append("Обложка слишком большая.")
        elif not validate_image(data, ALLOWED_IMAGE_TYPES):
            errors.append("Обложка: PNG, JPEG или WebP.")
        else:
            old = article.cover_path
            filename = save_image(data, COVERS_DIR, MAX_COVER_SIZE, max_dim=1920)
            article.cover_path = f"/static/uploads/covers/{filename}"
            if old:
                delete_file(old)
    return errors, article


@app.post("/admin/content/new", response_class=HTMLResponse)
async def admin_content_create(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    kind: str = Form("blog"),
    title: str = Form(""),
    slug: str = Form(""),
    excerpt: str = Form(""),
    body_html: str = Form(""),
    seo_title: str = Form(""),
    seo_description: str = Form(""),
    seo_keywords: str = Form(""),
    topic: str = Form(""),
    is_published: bool = Form(False),
    cover: Optional[UploadFile] = File(None),
):
    require_admin(request)
    kind = _require_kind(kind)
    errors, article = await _save_article_from_form(
        request, db, None, kind, csrf_token, title, slug, excerpt, body_html,
        seo_title, seo_description, seo_keywords, topic, is_published, cover,
    )
    if errors:
        return _render_article_form(request, article, kind, errors=errors)
    db.commit()
    return RedirectResponse(f"/admin/content?kind={kind}", status_code=302)


@app.post("/admin/content/{article_id}/edit", response_class=HTMLResponse)
async def admin_content_update(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    kind: str = Form("blog"),
    title: str = Form(""),
    slug: str = Form(""),
    excerpt: str = Form(""),
    body_html: str = Form(""),
    seo_title: str = Form(""),
    seo_description: str = Form(""),
    seo_keywords: str = Form(""),
    topic: str = Form(""),
    is_published: bool = Form(False),
    cover: Optional[UploadFile] = File(None),
):
    require_admin(request)
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404)
    errors, article = await _save_article_from_form(
        request, db, article, kind, csrf_token, title, slug, excerpt, body_html,
        seo_title, seo_description, seo_keywords, topic, is_published, cover,
    )
    if errors:
        db.rollback()
        article = db.query(Article).filter(Article.id == article_id).first()
        return _render_article_form(request, article, article.kind, errors=errors)
    db.commit()
    return RedirectResponse(f"/admin/content?kind={article.kind}", status_code=302)


@app.post("/admin/content/{article_id}/delete")
async def admin_content_delete(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404)
    kind = article.kind
    delete_file(article.cover_path)
    db.delete(article)
    db.commit()
    return RedirectResponse(f"/admin/content?kind={kind}", status_code=302)


@app.post("/admin/content/{article_id}/toggle")
async def admin_content_toggle(
    article_id: int,
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404)
    article.is_published = not article.is_published
    if article.is_published and not article.published_at:
        article.published_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/admin/content?kind={article.kind}", status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN — PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) < 8:
        return ""
    return "•" * 12 + key[-4:]


@app.get("/admin/pipeline", response_class=HTMLResponse)
async def admin_pipeline_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    settings = get_pipeline_settings(db)
    runs = db.query(PipelineRun).order_by(PipelineRun.id.desc()).limit(30).all()
    queued = request.query_params.get("queued") == "1"
    saved = request.query_params.get("saved") == "1"
    return templates.TemplateResponse(
        "admin/pipeline.html",
        {
            "request": request,
            "p": settings,
            "runs": runs,
            "csrf": get_csrf(request),
            "key_mask": _mask_key(settings.openai_api_key),
            "has_env_key": bool(os.getenv("OPENAI_API_KEY")),
            "has_env_proxy": bool(os.getenv("OPENAI_HTTP_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")),
            "queued": queued,
            "saved": saved,
            "default_prompt": DEFAULT_SYSTEM_PROMPT,
        },
    )


@app.post("/admin/pipeline")
async def admin_pipeline_save(
    request: Request,
    db: Session = Depends(get_db),
    csrf_token: str = Form(...),
    openai_api_key: str = Form(""),
    http_proxy: str = Form(""),
    text_model: str = Form(DEFAULT_TEXT_MODEL),
    image_model: str = Form(DEFAULT_IMAGE_MODEL),
    image_size: str = Form("1536x1024"),
    generate_images: bool = Form(False),
    system_prompt: str = Form(""),
    content_mix: str = Form("both"),
    auto_enabled: bool = Form(False),
    auto_publish: bool = Form(False),
    interval_hours: int = Form(24),
    posts_per_run: int = Form(1),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    settings = get_pipeline_settings(db)
    new_key = openai_api_key.strip()
    if new_key and not new_key.startswith("•"):
        settings.openai_api_key = new_key
    settings.http_proxy = normalize_http_proxy(http_proxy)
    settings.text_model = (text_model.strip() or DEFAULT_TEXT_MODEL)[:80]
    settings.image_model = (image_model.strip() or DEFAULT_IMAGE_MODEL)[:80]
    settings.image_size = (image_size.strip() or "1536x1024")[:32]
    settings.generate_images = generate_images
    settings.system_prompt = system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
    settings.content_mix = content_mix if content_mix in ("both", "blog", "news") else "both"
    settings.auto_enabled = auto_enabled
    settings.auto_publish = auto_publish
    settings.interval_hours = max(1, min(168, interval_hours))
    settings.posts_per_run = max(1, min(5, posts_per_run))
    db.commit()
    return RedirectResponse("/admin/pipeline?saved=1", status_code=302)


@app.post("/admin/pipeline/run")
async def admin_pipeline_run(
    request: Request,
    csrf_token: str = Form(...),
    kind: str = Form(""),
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403)
    force = kind if kind in ("blog", "news") else None

    async def _job():
        await asyncio.to_thread(pipeline_run_once, force)

    asyncio.create_task(_job())
    return RedirectResponse("/admin/pipeline?queued=1", status_code=302)
