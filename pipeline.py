"""SEO content factory: GPT-5.6 Luna for text, GPT Image 2 for covers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from unidecode import unidecode

log = logging.getLogger("ice.pipeline")

BASE_DIR = Path(__file__).parent
COVERS_DIR = BASE_DIR / "static" / "uploads" / "covers"

DEFAULT_TEXT_MODEL = "gpt-5.6-luna"
DEFAULT_IMAGE_MODEL = "gpt-image-2"

DEFAULT_SYSTEM_PROMPT = """Ты — главный редактор корпоративного портала Айс.Продукт (https://ice-product.ru/).
Пиши на русском языке экспертные материалы, которые хорошо ранжируются в поисковиках (Яндекс, Google).

Компания и продукты (используй как смысловой каркас, не как рекламный спам):
- Айс.Агент — AI-сотрудник: задачи, звонки, круглосуточная работа
- Айс.Трекер — управление проектами и задачами
- Айс.Щит — антивирус с искусственным интеллектом
- Айс.Контроль — мониторинг рабочих ПК, учёт времени, защита
- Айс.Школа — платформа для частных школ и детских центров
- Темы: автоматизация бизнеса, кибербезопасность, ИИ в компаниях, продуктивность команд, 152-ФЗ и данные (без юридических гарантий)

Правила:
- Деловой ясный тон. Конкретика, списки, подзаголовки. Без эмодзи, без кликбейта, без «воды».
- Статья самодостаточна. Объём: блог 900–1400 слов, новость 400–700 слов.
- HTML только с тегами: h2, h3, p, ul, ol, li, strong, em, a, blockquote.
- slug — латиница, дефисы, без даты.
- excerpt — 140–170 символов, как сниппет в выдаче.
- seo_title до 60 символов, seo_description до 160.
- image_prompt — на английском, photoreal corporate editorial photo, no text, no logos, no watermarks, 16:9.
- Не повторяй темы из списка уже опубликованных заголовков.
- kind: "blog" для вечных гайдов и разборов; "news" для коротких новостных углов (тренды ИИ, безопасность, продуктные обновления отрасли).
"""

ARTICLE_JSON_INSTRUCTION = """Верни ТОЛЬКО JSON-объект без markdown:
{
  "kind": "blog" или "news",
  "title": "заголовок",
  "slug": "latin-slug",
  "excerpt": "сниппет",
  "seo_title": "title",
  "seo_description": "description",
  "seo_keywords": "ключ1, ключ2, ключ3",
  "topic": "короткая тема",
  "body_html": "<h2>...</h2><p>...</p>",
  "image_prompt": "English cover prompt"
}
"""


def slugify(text: str) -> str:
    text = unidecode(text or "").lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80] or "material"


def _json_from_text(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _openai_client(api_key: str):
    from openai import OpenAI

    return OpenAI(api_key=api_key, timeout=180.0)


def generate_article_payload(api_key: str, text_model: str, system_prompt: str, user_prompt: str) -> dict:
    client = _openai_client(api_key)
    instructions = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip() + "\n\n" + ARTICLE_JSON_INSTRUCTION
    last_error = None

    try:
        resp = client.responses.create(
            model=text_model,
            instructions=instructions,
            input=user_prompt,
        )
        text = getattr(resp, "output_text", None)
        if not text:
            chunks = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    t = getattr(c, "text", None)
                    if t:
                        chunks.append(t)
            text = "\n".join(chunks)
        if text:
            return _json_from_text(text)
    except Exception as exc:
        last_error = exc
        log.warning("responses API failed (%s), fallback to chat.completions", exc)

    try:
        chat = client.chat.completions.create(
            model=text_model,
            temperature=0.7,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _json_from_text(chat.choices[0].message.content or "{}")
    except Exception as exc:
        raise RuntimeError(f"Не удалось сгенерировать текст: {exc}") from (last_error or exc)


def generate_cover_image(api_key: str, image_model: str, prompt: str, size: str = "1536x1024") -> str:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    client = _openai_client(api_key)
    kwargs = {"model": image_model, "prompt": prompt, "size": size or "1536x1024", "n": 1}
    try:
        result = client.images.generate(**kwargs)
    except Exception:
        try:
            result = client.images.generate(model=image_model, prompt=prompt, n=1)
        except Exception:
            result = client.images.generate(model=image_model, prompt=prompt)

    item = result.data[0]
    raw = None
    b64 = getattr(item, "b64_json", None)
    url = getattr(item, "url", None)
    if b64:
        raw = base64.b64decode(b64)
    elif url:
        import urllib.request

        with urllib.request.urlopen(url, timeout=120) as resp:
            raw = resp.read()
    if not raw:
        raise RuntimeError("Модель изображения не вернула файл")

    filename = f"{uuid.uuid4().hex}.png"
    (COVERS_DIR / filename).write_bytes(raw)
    return f"/static/uploads/covers/{filename}"


def unique_article_slug(db, kind: str, base: str, exclude_id: Optional[int] = None) -> str:
    from models import Article

    slug = slugify(base)
    candidate = slug
    n = 1
    while True:
        q = db.query(Article).filter(Article.kind == kind, Article.slug == candidate)
        if exclude_id:
            q = q.filter(Article.id != exclude_id)
        if not q.first():
            return candidate
        candidate = f"{slug}-{n}"
        n += 1


def _clean_article_html(html: str) -> str:
    import bleach

    return bleach.clean(
        html or "",
        tags={"h2", "h3", "p", "ul", "ol", "li", "strong", "em", "a", "blockquote", "br"},
        attributes={"a": ["href", "title", "rel"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )


def get_pipeline_settings(db):
    from models import PipelineSettings

    row = db.query(PipelineSettings).first()
    if not row:
        row = PipelineSettings(
            text_model=DEFAULT_TEXT_MODEL,
            image_model=DEFAULT_IMAGE_MODEL,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            interval_hours=24,
            posts_per_run=1,
            content_mix="both",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    elif not (row.system_prompt or "").strip():
        row.system_prompt = DEFAULT_SYSTEM_PROMPT
        db.commit()
        db.refresh(row)
    return row


def resolve_api_key(settings) -> str:
    return (settings.openai_api_key or os.getenv("OPENAI_API_KEY") or "").strip()


def _pick_kind(settings, db) -> str:
    from models import Article

    mix = (settings.content_mix or "both").lower()
    if mix in ("blog", "news"):
        return mix
    last = (
        db.query(Article)
        .filter(Article.source == "ai")
        .order_by(Article.id.desc())
        .first()
    )
    if last and last.kind == "blog":
        return "news"
    return "blog"


def run_once(force_kind: Optional[str] = None) -> dict:
    """Generate one article. Opens its own DB session."""
    from database import SessionLocal
    from models import Article, PipelineRun

    started = time.time()
    db = SessionLocal()
    article = None
    kind = force_kind or ""
    title = ""
    try:
        settings = get_pipeline_settings(db)
        api_key = resolve_api_key(settings)
        if not api_key:
            raise RuntimeError("Не задан OpenAI API ключ (админка → Конвеер или OPENAI_API_KEY).")

        kind = force_kind or _pick_kind(settings, db)
        recent = (
            db.query(Article.title)
            .order_by(Article.id.desc())
            .limit(20)
            .all()
        )
        recent_titles = [r[0] for r in recent if r[0]]
        user_prompt = (
            f"Сгенерируй один материал типа «{kind}» для портала Айс.Продукт.\n"
            f"Сегодня: {datetime.now().strftime('%d.%m.%Y')}.\n"
            "Уже опубликованные заголовки (не повторяй):\n"
            + ("\n".join(f"- {t}" for t in recent_titles) if recent_titles else "- (пока пусто)")
        )

        payload = generate_article_payload(
            api_key,
            settings.text_model or DEFAULT_TEXT_MODEL,
            settings.system_prompt or DEFAULT_SYSTEM_PROMPT,
            user_prompt,
        )
        kind = (payload.get("kind") or kind or "blog").strip().lower()
        if kind not in ("blog", "news"):
            kind = "blog"
        title = (payload.get("title") or "Материал").strip()[:240]
        if db.query(Article).filter(Article.title == title).first():
            title = f"{title} — {datetime.now().strftime('%d.%m.%Y')}"[:255]
        slug = unique_article_slug(db, kind, payload.get("slug") or title)
        excerpt = (payload.get("excerpt") or "")[:400]
        body = _clean_article_html(payload.get("body_html") or payload.get("body") or "")
        cover = ""
        if settings.generate_images:
            image_prompt = payload.get("image_prompt") or (
                f"Photoreal editorial 16:9 photo about {title}, modern office, no text"
            )
            cover = generate_cover_image(
                api_key,
                settings.image_model or DEFAULT_IMAGE_MODEL,
                image_prompt,
                settings.image_size or "1536x1024",
            )

        now = datetime.utcnow()
        publish = bool(settings.auto_publish)
        article = Article(
            kind=kind,
            title=title,
            slug=slug,
            excerpt=excerpt,
            body_html=body,
            cover_path=cover,
            seo_title=(payload.get("seo_title") or title)[:255],
            seo_description=(payload.get("seo_description") or excerpt)[:320],
            seo_keywords=(payload.get("seo_keywords") or "")[:500],
            topic=(payload.get("topic") or "")[:255],
            source="ai",
            is_published=publish,
            published_at=now if publish else None,
        )
        db.add(article)
        db.flush()
        settings.last_run_at = now
        settings.last_error = ""
        duration = int((time.time() - started) * 1000)
        db.add(
            PipelineRun(
                status="ok",
                kind=kind,
                title=title,
                article_id=article.id,
                message="Опубликовано" if publish else "Сохранено как черновик",
                duration_ms=duration,
            )
        )
        db.commit()
        return {"ok": True, "id": article.id, "kind": kind, "title": title, "published": publish}
    except Exception as exc:
        db.rollback()
        duration = int((time.time() - started) * 1000)
        try:
            settings = get_pipeline_settings(db)
            settings.last_run_at = datetime.utcnow()
            settings.last_error = str(exc)[:2000]
            db.add(
                PipelineRun(
                    status="error",
                    kind=kind,
                    title=title,
                    message=str(exc)[:2000],
                    duration_ms=duration,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
        log.exception("pipeline run failed")
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


_loop_started = False


async def pipeline_loop():
    from database import SessionLocal

    await asyncio.sleep(8)
    while True:
        try:
            db = SessionLocal()
            try:
                settings = get_pipeline_settings(db)
                enabled = bool(settings.auto_enabled)
                hours = max(1, int(settings.interval_hours or 24))
                last = settings.last_run_at
                due = last is None or (datetime.utcnow() - last) >= timedelta(hours=hours)
                n = max(1, min(5, int(settings.posts_per_run or 1)))
            finally:
                db.close()
            if enabled and due:
                for _ in range(n):
                    await asyncio.to_thread(run_once)
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pipeline loop error")
        await asyncio.sleep(60)


def start_pipeline_loop(app) -> None:
    global _loop_started
    if _loop_started:
        return
    _loop_started = True

    @app.on_event("startup")
    async def _start():
        asyncio.create_task(pipeline_loop())
