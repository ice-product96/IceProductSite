# Айс.Продукт — Веб-сайт IT-компании

Мини-каталог приложений с публичной частью и административной панелью.

## Стек

- **Backend:** Python 3.11 + FastAPI
- **БД:** SQLite (dev) / PostgreSQL (prod) через SQLAlchemy
- **Frontend:** Jinja2 + TailwindCSS CDN
- **Авторизация:** Cookie-сессии (itsdangerous)
- **Изображения:** Pillow + imghdr

## Структура проекта

```
ice_product/
├── main.py               # FastAPI-приложение, все маршруты
├── models.py             # SQLAlchemy-модели
├── database.py           # Подключение к БД
├── auth.py               # Авторизация, CSRF, хэш паролей
├── seed.py               # Начальные данные
├── .env.example          # Пример переменных окружения
├── requirements.txt
├── static/
│   ├── css/
│   └── uploads/
│       ├── icons/
│       └── screenshots/
└── templates/
    ├── base.html
    ├── index.html
    ├── app_detail.html
    ├── 404.html
    ├── 500.html
    └── admin/
        ├── base_admin.html
        ├── login.html
        ├── settings.html
        ├── apps_list.html
        └── app_form.html
```

## Установка и запуск

### 1. Клонировать / перейти в папку проекта

```bash
cd D:/Sites/IceProductSite
```

### 2. Создать виртуальное окружение

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate
```

### 3. Установить зависимости

```bash
pip install -r requirements.txt
```

### 4. Настроить переменные окружения

```bash
cp .env.example .env
```

Отредактируйте `.env`:

```env
SECRET_KEY=ваш-случайный-секретный-ключ

ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=<bcrypt-хэш>

DATABASE_URL=sqlite:///./ice_product.db
```

**Сгенерировать хэш пароля:**

```bash
python -c "from passlib.hash import bcrypt; print(bcrypt.hash('ваш_пароль'))"
```

**Сгенерировать SECRET_KEY:**

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Заполнить начальные данные

```bash
python seed.py
```

### 6. Запустить сервер

```bash
uvicorn main:app --reload
```

Сайт доступен по адресу: **http://localhost:8000**

## Логин по умолчанию

Учётная запись задаётся через `.env`. Нет дефолтного пароля — вы устанавливаете его сами при настройке `.env`.

| Поле     | Значение из `.env`        |
|----------|--------------------------|
| Логин    | `ADMIN_USERNAME` (admin) |
| Пароль   | задаётся вручную          |

Панель управления: **http://localhost:8000/admin**

## Маршруты

### Публичные

| Метод | URL             | Описание               |
|-------|-----------------|------------------------|
| GET   | `/`             | Главная страница        |
| GET   | `/app/{slug}`   | Страница приложения     |

### Административные

| Метод | URL                              | Описание                   |
|-------|----------------------------------|----------------------------|
| GET   | `/admin/login`                   | Страница входа             |
| POST  | `/admin/login`                   | Авторизация                |
| GET   | `/admin/logout`                  | Выход                      |
| GET   | `/admin/settings`                | Настройки компании         |
| POST  | `/admin/settings`                | Сохранить настройки        |
| GET   | `/admin/apps`                    | Список приложений          |
| POST  | `/admin/apps/reorder`            | Изменить порядок (AJAX)    |
| GET   | `/admin/apps/new`                | Форма создания             |
| POST  | `/admin/apps/new`                | Создать приложение         |
| GET   | `/admin/apps/{id}/edit`          | Форма редактирования       |
| POST  | `/admin/apps/{id}/edit`          | Обновить приложение        |
| POST  | `/admin/apps/{id}/delete`        | Удалить приложение         |
| POST  | `/admin/screenshots/{id}/delete` | Удалить скриншот           |

## PostgreSQL (продакшн)

В `.env` замените строку подключения:

```env
DATABASE_URL=postgresql://user:password@localhost/ice_product
```

Убедитесь, что база данных создана:

```sql
CREATE DATABASE ice_product;
```

## Безопасность

- CSRF-токен на всех POST-запросах в админке
- Пароль хранится как bcrypt-хэш, никогда в открытом виде
- Загружаемые файлы проверяются по сигнатуре (imghdr) и сжимаются через Pillow
- Session cookie: `httponly`, `samesite=lax`
