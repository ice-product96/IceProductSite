import imghdr
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy.orm import Session
from unidecode import unidecode
import re

import bleach
from markupsafe import Markup, escape

load_dotenv()

from auth import (
    generate_csrf_token,
    generate_session_token,
    validate_csrf_token,
    verify_admin,
)
from database import Base, engine, get_db
from models import App, AppScreenshot, SiteSettings

# ── App setup ─────────────────────────────────────────────────────────────────

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Айс.Продукт")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
ICONS_DIR = UPLOADS_DIR / "icons"
SCREENSHOTS_DIR = UPLOADS_DIR / "screenshots"

for d in (ICONS_DIR, SCREENSHOTS_DIR):
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
    return bleach.clean(
        html,
        tags=_FULL_DESC_TAGS,
        attributes=_FULL_DESC_ATTRS,
        protocols=["http", "https", "mailto"],
        strip=True,
    )


def full_description_html(value: Optional[str]) -> Markup:
    if not value:
        return Markup("")
    cleaned = sanitize_full_description(value)
    if _FULL_DESC_RE.search(cleaned):
        return Markup(cleaned)
    return Markup(
        '<div class="text-gray-500 leading-relaxed whitespace-pre-wrap">'
        f"{escape(cleaned)}</div>"
    )


templates.env.filters["full_description_html"] = full_description_html
templates.env.filters["utf8_safe"] = _utf8_safe_text

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
MAX_ICON_SIZE = int(os.getenv("MAX_ICON_SIZE", 524288))       # 512 KB
MAX_SCREENSHOT_SIZE = int(os.getenv("MAX_SCREENSHOT_SIZE", 2097152))  # 2 MB

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
    detected = imghdr.what(None, h=data)
    if detected is None:
        # imghdr doesn't detect webp or svg well — check magic bytes
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            detected = "webp"
        elif data[:5] == b"<?xml" or data[:4] == b"<svg":
            detected = "svg"
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
        )
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


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
    db = next(get_db())
    settings = get_settings(db)
    return templates.TemplateResponse(
        "404.html", {"request": request, "settings": settings}, status_code=404
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    db = next(get_db())
    settings = get_settings(db)
    return templates.TemplateResponse(
        "500.html", {"request": request, "settings": settings}, status_code=500
    )


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
        "index.html", {"request": request, "settings": settings, "apps": apps}
    )


@app.get("/app/{slug}", response_class=HTMLResponse)
async def app_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    product = (
        db.query(App)
        .filter(App.slug == slug, App.is_published == True)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404)
    settings = get_settings(db)
    return templates.TemplateResponse(
        "app_detail.html",
        {"request": request, "settings": settings, "product": product},
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
):
    require_admin(request)
    if not check_csrf(request, csrf_token):
        raise HTTPException(status_code=403, detail="Неверный CSRF-токен")
    settings = get_settings(db)
    settings.slogan = slogan.strip()
    settings.short_description = short_description.strip()
    settings.meta_title = meta_title.strip()
    settings.meta_description = meta_description.strip()
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
