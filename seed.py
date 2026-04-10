"""
Run once to populate the database with initial data.
Usage: python seed.py
"""
from database import Base, SessionLocal, engine
from models import App, AppScreenshot, SiteSettings

Base.metadata.create_all(bind=engine)

db = SessionLocal()

try:
    # ── SiteSettings ──────────────────────────────────────────────────────────
    existing = db.query(SiteSettings).first()
    if not existing:
        settings = SiteSettings(
            slogan="Умные инструменты для современных команд",
            short_description=(
                "Айс.Продукт — IT-компания, создающая продукты, которые упрощают работу команд: "
                "от планирования задач до автоматизации процессов. "
                "Наши решения адаптируются под любой масштаб бизнеса."
            ),
            meta_title="Айс.Продукт — Умные инструменты для команд",
            meta_description=(
                "Айс.Продукт — IT-компания, создающая продукты для команд: "
                "трекеры задач, инструменты автоматизации и многое другое."
            ),
        )
        db.add(settings)
        print("✓ SiteSettings created")
    else:
        print("· SiteSettings already exists, skipping")

    # ── App: Айс.Трекер ───────────────────────────────────────────────────────
    existing_app = db.query(App).filter(App.slug == "ice-tracker").first()
    if not existing_app:
        tracker = App(
            name="Айс.Трекер",
            slug="ice-tracker",
            short_description=(
                "Бесплатный сервис для планирования задач и управления проектами "
                "и процессами, который адаптируется под вашу команду."
            ),
            full_description=(
                "## Айс.Трекер — управление проектами без лишней сложности\n\n"
                "Айс.Трекер — современная система управления задачами и проектами, "
                "созданная специально для команд, которым важна гибкость. "
                "Выбирайте удобный формат работы: доски Kanban, список дел или "
                "календарный вид — всё это доступно в одном инструменте.\n\n"
                "Система позволяет гибко настраивать рабочие процессы, управлять "
                "командой и ролями, отслеживать прогресс в реальном времени и "
                "интегрироваться с внешними сервисами.\n\n"
                "Начните бесплатно — без ограничений по числу участников команды."
            ),
            features=[
                "Гибкие доски задач (Kanban, список, календарь)",
                "Управление командой и ролями",
                "Треккинг прогресса в реальном времени",
                "Интеграция с внешними сервисами",
                "Бесплатно для команд любого размера",
            ],
            external_url="http://localhost:8001",
            icon_path="",
            is_published=True,
            sort_order=0,
        )
        db.add(tracker)
        print("✓ App 'Айс.Трекер' created")
    else:
        print("· App 'Айс.Трекер' already exists, skipping")

    db.commit()
    print("\nSeed completed successfully.")
    print("Add an icon and screenshots via the admin panel at /admin/apps")

except Exception as e:
    db.rollback()
    print(f"Error during seed: {e}")
    raise
finally:
    db.close()
