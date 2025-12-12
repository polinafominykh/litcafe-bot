import logging
from datetime import datetime, date

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

import gspread
from google.oauth2.service_account import Credentials
import re
import aiohttp
import asyncio
import os

# ======================== НАСТРОЙКИ ========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not BOT_TOKEN:
    raise ValueError("❗ BOT_TOKEN отсутствует! Добавьте его в переменные окружения.")

GOOGLE_SHEET_NAME = "LitCafe_Control"
ADMIN_ID = 542644262

# Google credentials will be created from env
creds_json = os.getenv("GOOGLE_CREDS_JSON")
if not creds_json:
    raise ValueError("❗ GOOGLE_CREDS_JSON отсутствует в переменных окружения!")

with open("credentials.json", "w", encoding="utf-8") as f:
    f.write(creds_json)

GOOGLE_CREDS_FILE = "credentials.json"

MAX_TG_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# ======================== ЛОГИ ========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ======================== GOOGLE SHEETS ========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open(GOOGLE_SHEET_NAME).sheet1


def get_books():
    return sheet.get_all_records()


# ======================== USERS ========================

def save_user_if_new(user):
    users_sheet = gc.open(GOOGLE_SHEET_NAME).worksheet("Users")
    rows = users_sheet.get_all_records()
    existing_ids = {r["user_id"] for r in rows}

    if user.id in existing_ids:
        return

    users_sheet.append_row([
        user.id,
        user.username or "",
        user.first_name or "",
        user.last_name or ""
    ])


def get_all_user_ids():
    users_sheet = gc.open(GOOGLE_SHEET_NAME).worksheet("Users")
    rows = users_sheet.get_all_records()
    return [r["user_id"] for r in rows]


# ======================== UTILS ========================

def parse_event_date(date_str: str) -> date | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d.%m.%Y").date()
    except ValueError:
        return None


def extract_drive_id(url: str) -> str | None:
    if not url:
        return None

    patterns = [
        r"/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/uc\?id=([a-zA-Z0-9_-]+)"
    ]

    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)

    return None


def convert_drive_to_direct_image(url: str) -> str:
    if not url:
        return ""

    file_id = extract_drive_id(url)
    if not file_id:
        return url

    return f"https://drive.google.com/uc?export=view&id={file_id}"


async def get_drive_file_size(file_id: str) -> int | None:
    direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.head(direct_url) as resp:
                if resp.status == 200:
                    size = resp.headers.get("Content-Length")
                    return int(size) if size else None
    except:
        pass
    return None


async def download_drive_file(url: str):
    file_id = extract_drive_id(url)
    if not file_id:
        return None, None

    direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    size = await get_drive_file_size(file_id)

    if size and size > MAX_TG_FILE_SIZE:
        return None, size

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(direct_url) as resp:
                if resp.status == 200:
                    return await resp.read(), size
    except:
        pass

    return None, size


def get_chat_id(src) -> int | None:
    if hasattr(src, "message") and src.message:
        return src.message.chat.id
    if hasattr(src, "from_user"):
        return src.from_user.id
    if hasattr(src, "effective_chat"):
        return src.effective_chat.id
    return None


# ======================== EVENTS ========================

def get_next_event():
    records = sheet.get_all_records()
    today = date.today()
    events = []

    for row in records:
        event_date = parse_event_date(row.get("Дата_вечера"))
        if event_date and event_date >= today:
            events.append((event_date, row))

    if not events:
        return None

    return sorted(events, key=lambda x: x[0])[0]


def register_user_for_event(user, title: str):
    reg_sheet = gc.open(GOOGLE_SHEET_NAME).worksheet("Registrations")
    rows = reg_sheet.get_all_records()

    if (user.id, title) in [(r["user_id"], r["event_title"]) for r in rows]:
        return False

    reg_sheet.append_row([
        user.id,
        user.username or "",
        f"{user.first_name or ''} {user.last_name or ''}",
        title,
        str(datetime.now().date())
    ])
    return True


def get_event_by_title(title: str):
    for row in sheet.get_all_records():
        if row.get("Название") == title:
            return row
    return None

def get_event_row(title: str):
    records = sheet.get_all_records()
    for row in records:
        if row.get("Название") == title:
            return row
    return None

def get_book_by_title(title: str):
    for book in get_books():
        if book.get("Название") == title:
            return book
    return None



# ======================== FILE SENDING ========================

async def send_pdf(src, context, link: str, title: str):
    chat_id = get_chat_id(src)
    if not chat_id:
        return

    if not link:
        await context.bot.send_message(chat_id, "PDF недоступен.")
        return

    data, size = await download_drive_file(link)

    if size and size > MAX_TG_FILE_SIZE:
        await context.bot.send_message(chat_id, f"Файл слишком большой.\n{link}")
        return

    if not data:
        await context.bot.send_message(chat_id, "Ошибка загрузки PDF.")
        return

    await context.bot.send_message(chat_id, "📖 *Вот ваша книга:*", parse_mode="Markdown")
    await context.bot.send_document(
        chat_id=chat_id,
        document=data,
        filename=f"{title}.pdf"
    )


async def send_file(src, context, link: str, ext: str, title: str):
    chat_id = get_chat_id(src)
    if not chat_id:
        return

    if not link:
        await context.bot.send_message(chat_id, "Файл недоступен.")
        return

    data, size = await download_drive_file(link)

    if size and size > MAX_TG_FILE_SIZE:
        await context.bot.send_message(chat_id, f"Файл слишком большой.\n{link}")
        return

    if not data:
        await context.bot.send_message(chat_id, "Ошибка загрузки файла.")
        return

    await context.bot.send_document(chat_id, data, filename=f"{title}.{ext}")


# ======================== HANDLERS ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Если запуск через параметр ?start=hello → показываем красивое приветствие и прекращаем выполнение
    if context.args and context.args[0] == "hello":
        await update.message.reply_text(
            "Здравствуй! Мы рады видеть тебя в литературном клубе «.МОНЕ».\n\n"
            "Здесь мы читаем, обсуждаем и находим друзей среди строк великих книг.\n"
            "Выбери действие ниже:\n"
            "📚 Библиотека — книги в электронном формате для наших встреч.\n"
            "🗓️ Мероприятия — расписание вечеров и запись.\n"
            "✨ О клубе — как, зачем и для кого мы это создали.\n"
            "📞 Контакты — где нас найти и как связаться.",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("📚 Библиотека")],
                [KeyboardButton("🗓️ Мероприятия")],
                [KeyboardButton("❓ О клубе"), KeyboardButton("📞 Контакты")]
            ], resize_keyboard=True)
        )
        return  # ← это не даёт функции исполнить остальное приветствие

    # Обычный запуск /start
    save_user_if_new(user)

    text = (
        "Здравствуй! Мы рады видеть тебя в литературном клубе «.МОНЕ».\n\n"
        "Здесь мы читаем, обсуждаем и находим друзей среди строк великих книг.\n"
        "Выбери действие ниже:\n"
        "📚 Библиотека — книги в электронном формате для наших встреч.\n"
        "🗓️ Мероприятия — расписание вечеров и запись.\n"
        "✨ О клубе — как, зачем и для кого мы это создали.\n"
        "📞 Контакты — где нас найти и как связаться.\n"
    )

    menu = [
        [KeyboardButton("📚 Библиотека")],
        [KeyboardButton("🗓️ Мероприятия")],
        [KeyboardButton("❓ О клубе"), KeyboardButton("📞 Контакты")]
    ]

    if update.message:
        await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True))
    else:
        await context.bot.send_message(chat_id=user.id, text=text, reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📚 Библиотека":
        await library(update, context)

    elif text == "🗓️ Мероприятия":
        await events(update, context)

    elif text == "❓ О клубе":
        await update.message.reply_text(
            "Наш клуб — это пространство честных разговоров, глубоких мыслей и открытых людей.\n"
            "Мы собираемся, чтобы читать книги, обсуждать их и открывать новое в знакомых произведениях.\n\n"
            "📍 *Место встреч:*\n"
            "ул. Адмирала Трибуца, 5, Санкт-Петербург\n"
            "Кафе «.МОНЕ» — уют, тёплый свет и атмосфера, в которой хочется говорить о важном.\n\n"
            "📘 *Формат встреч:*\n"
            "• выбираем книгу и встречаемся для её обсуждения через 14 дней\n"
            "• читаем самостоятельно\n"
            "• мы не ищем «правильных» ответов — мы ищем свои\n"
            "• мы не соревнуемся в эрудиции — мы делимся впечатлениями\n"
            "• мы спорим, смеёмся, молчим и открываем книгу и себя с новой стороны\n\n"
            "*Простое правило:* уважение к слову и друг к другу.\n"
            "Здесь можно не соглашаться, можно сомневаться, можно говорить «я не понял» или «я плакал на этой странице».\n"
            "Здесь можно быть собой — читающим, думающим, чувствующим.\n\n"
            "*Мы создали этот круг для тех, кто:*\n"
            "• любит, когда после книги хочется с кем-то поговорить\n"
            "• верит, что кофе и книга — идеальное сочетание\n"
            "• ищет не просто хобби, а своих людей и глубину\n\n"
            "💬 *Чат для обсуждений:*\n"
            "[Telegram-чат клуба](https://t.me/+OqJlHFxPonEzNTBi)\n\n"
            "Добро пожаловать — здесь тебя услышат.",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    elif text == "📞 Контакты":
        # 1️⃣ Геолокация
        await update.message.reply_location(
            latitude=59.853700,
            longitude=30.144926
        )

        # 2️⃣ Текст карточки
        contact_text = (
            "📍 *МОНЕ*\n"
            "ул. Адмирала Трибуца, 5, Санкт-Петербург\n\n"
            "⏰ *Часы работы:*\n"
            "Пн–Вс: 9:00–22:00\n\n"
            "🔗 *Ссылки:*\n"
            f"• [Telegram-канал](https://t.me/monecoffee)\n"
            f"• [Instagram](https://www.instagram.com/mone.coffee.spb?igsh=ZWtsNG45NnJjNnNr)\n"
            "• +79992361626 Телеграм/WhatsApp\n"
        )

        await update.message.reply_text(
            contact_text,
            parse_mode="Markdown"
        )

    else:
        await update.message.reply_text("Выбери действие из меню")


async def library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    books = get_books()
    if not books:
        await update.message.reply_text("Библиотека пуста 📚")
        return

    keyboard = [
        [InlineKeyboardButton(f"{b['Название']} — {b.get('Автор','')}", callback_data=f"book_{i}")]
        for i, b in enumerate(books)
    ]

    await update.message.reply_text(
        "Выбери книгу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def daily_announce_14(context):
    result = get_next_event()
    if not result:
        return

    event_date, row = result
    today = date.today()

    if (event_date - today).days != 14:
        return

    cover = convert_drive_to_direct_image(row.get("Обложка_URL", ""))
    title = row["Название"]
    text = row.get("Анонс_текст", f"Скоро встреча по книге «{title}».").strip()

    keyboard = [
        [InlineKeyboardButton("Записаться", callback_data=f"going_{title}")],
        [InlineKeyboardButton("Начать читать", callback_data=f"formats_title_{title}")]
    ]

    for uid in get_all_user_ids():
        try:
            if cover:
                await context.bot.send_photo(uid, cover, caption=text,
                                              reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await context.bot.send_message(uid, text,
                                               reply_markup=InlineKeyboardMarkup(keyboard))
        except:
            continue


async def daily_remind_1(context):
    result = get_next_event()
    if not result:
        return

    event_date, row = result
    today = date.today()

    if (event_date - today).days != 1:
        return

    title = row["Название"]
    text = row.get("Напоминание_текст", f"Напоминание: завтра встреча по книге «{title}».").strip()

    reg_sheet = gc.open(GOOGLE_SHEET_NAME).worksheet("Registrations")
    rows = reg_sheet.get_all_records()
    user_ids = [r["user_id"] for r in rows if r["event_title"] == title]

    for uid in user_ids:
        try:
            await context.bot.send_message(uid, text)
        except:
            continue


async def book_details(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int):
    book = get_books()[index]

    title = book["Название"]
    author = book.get("Автор")
    desc = book.get("Описание")
    cover = convert_drive_to_direct_image(book.get("Обложка_URL", ""))

    caption = f"📖 *{title}*\nАвтор: {author}\n\n{desc}"

    keyboard = [[InlineKeyboardButton("📖 Начать читать", callback_data=f"formats_{index}")]]

    msg = update.callback_query.message

    if cover:
        try:
            await msg.reply_photo(cover, caption=caption, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
            return
        except:
            await msg.reply_text("⚠️ Не удалось загрузить обложку")

    await msg.reply_text(caption, parse_mode="Markdown",
                         reply_markup=InlineKeyboardMarkup(keyboard))


async def events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = get_next_event()
    if not result:
        await update.message.reply_text("Пока встреч нет.")
        return

    event_date, row = result
    cover = convert_drive_to_direct_image(row.get("Обложка_URL", ""))
    title = row["Название"]
    text = row.get("Анонс_текст", f"Встреча по книге «{title}».").strip()

    keyboard = [
        [
            InlineKeyboardButton("Записаться", callback_data=f"going_{title}"),
            InlineKeyboardButton("Начать читать", callback_data=f"formats_title_{title}")
        ]
    ]

    if cover:
        try:
            await update.message.reply_photo(cover, caption=text,
                                             reply_markup=InlineKeyboardMarkup(keyboard))
            return
        except:
            pass

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # ----------- 1) Открытие книги из библиотеки ----------
    if data.startswith("book_"):
        idx = int(data.split("_")[1])
        await book_details(update, context, idx)
        await query.answer()
        return

    # ----------- 2) МЕРОПРИЯТИЯ: кнопка "Начать читать" ----------
    # (должно стоять ПЕРЕД formats_)
    if data.startswith("formats_title_"):
        title_raw = data.replace("formats_title_", "")

        # нормализуем название (важно!)
        books = get_books()
        norm_title = title_raw.strip().lower()

        book = next(
            (b for b in books if b["Название"].strip().lower() == norm_title),
            None
        )

        if not book:
            await query.message.reply_text("❗ Книга не найдена в библиотеке.")
            await query.answer()
            return

        idx = books.index(book)

        keyboard = []
        if book.get("PDF_ссылка"):
            keyboard.append([InlineKeyboardButton("📕 PDF — подходит для всех устройств", callback_data=f"getpdf_{idx}")])
        if book.get("EPUB_ссылка"):
            keyboard.append([InlineKeyboardButton("📘 EPUB — удобно для iPhone и iPad", callback_data=f"getepub_{idx}")])
        if book.get("FB2_ссылка"):
            keyboard.append([InlineKeyboardButton("📗 FB2 — для Android и электронных книг", callback_data=f"getfb2_{idx}")])

        await query.message.reply_text(
            f"📚 *Форматы книги «{book['Название']}»*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        await query.answer()
        return

    # ----------- 3) БИБЛИОТЕКА: показать форматы ----------
    if data.startswith("formats_"):
        idx = int(data.split("_")[1])
        books = get_books()
        book = books[idx]

        keyboard = []
        if book.get("PDF_ссылка"):
            keyboard.append([InlineKeyboardButton("📕 PDF — подходит для всех устройств", callback_data=f"getpdf_{idx}")])
        if book.get("EPUB_ссылка"):
            keyboard.append([InlineKeyboardButton("📘 EPUB — удобно для iPhone и iPad", callback_data=f"getepub_{idx}")])
        if book.get("FB2_ссылка"):
            keyboard.append([InlineKeyboardButton("📗 FB2 — для Android и электронных книг", callback_data=f"getfb2_{idx}")])

        await query.message.reply_text(
            f"📚 *Форматы книги «{book['Название']}»*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        await query.answer()
        return

    # ----------- 4) Загрузка файлов ----------
    if data.startswith("getpdf_"):
        idx = int(data.split("_")[1])
        book = get_books()[idx]
        await send_pdf(query, context, book.get("PDF_ссылка", ""), book["Название"])
        await query.answer()
        return

    if data.startswith("getepub_"):
        idx = int(data.split("_")[1])
        book = get_books()[idx]
        await send_file(query, context, book.get("EPUB_ссылка", ""), "epub", book["Название"])
        await query.answer()
        return

    if data.startswith("getfb2_"):
        idx = int(data.split("_")[1])
        book = get_books()[idx]
        await send_file(query, context, book.get("FB2_ссылка", ""), "fb2", book["Название"])
        await query.answer()
        return

    # ----------- 5) Запись на мероприятие ----------
    if data.startswith("going_"):
        title = data.split("_", 1)[1]
        user = query.from_user

        if register_user_for_event(user, title):
            await query.message.reply_text(f"Вы записаны на встречу по книге «{title}».")
            await context.bot.send_message(
                ADMIN_ID,
                f"*Новый участник*\n"
                f"{user.first_name} {user.last_name or ''}\n"
                f"@{user.username or '—'}\n"
                f"Книга: {title}",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("Вы уже записаны на эту встречу.")

        await query.answer()
        return

from telegram.ext import CallbackContext

async def scheduler_task(app):
    await asyncio.sleep(3)

    context = CallbackContext.from_update(None, app)

    while True:
        try:
            await daily_announce_14(context)
            await daily_remind_1(context)
        except Exception as e:
            print("Scheduler error:", e)

        await asyncio.sleep(3600)


# ======================== KEEP-ALIVE WEB SERVER ========================
from aiohttp import web

async def handle_health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/health', handle_health)])

    runner = web.AppRunner(app)
    await runner.setup()

    # Railway требует PORT из переменной среды
    import os
    port = int(os.environ.get("PORT", 8080))

    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🚀 Keep-alive server запущен на порту {port}")


# ======================== MAIN ========================

async def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("events", events))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(callback))

    # Сcheduler запускается в фоне
    asyncio.create_task(scheduler_task(app))

    # Keep-alive server
    asyncio.create_task(start_web_server())

    # Запускаем webhook — ЕДИНСТВЕННЫЙ способ работы на Railway/Fly.io
    await app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )


if __name__ == "__main__":
    asyncio.run(run_bot())

