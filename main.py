import os
import logging
import telebot
from telebot import types
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta
import requests
import tempfile
import json

# Загружаем переменные из .env файла
load_dotenv()

# Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Получаем токен бота и список админов из .env
BOT_TOKEN = os.getenv('BOT_TOKEN_SUPPORT')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS').split(',')))
API_URL = os.getenv('API_URL_SUPPORT')
SUPPORT_API_URL = os.getenv('SUPPORT_API_URL', 'http://vpn-api:8080')
PROXYAPI_KEY = os.getenv('PROXYAPI_KEY', '')
ADMIN_KEY = os.getenv('ADMIN_KEY', '')
INTERNAL_KEY = os.getenv('INTERNAL_KEY', '')

def admin_headers():
    return {"X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json"}

def internal_headers():
    return {"X-Internal-Key": INTERNAL_KEY, "Content-Type": "application/json"}

# Инициализируем бота
bot = telebot.TeleBot(BOT_TOKEN)

# Тикет-система (DB-backed via API)
import threading
AUTO_CLOSE_HOURS = 15
auto_close_timers = {}  # user_id -> threading.Timer
user_data_cache = {}  # user_id -> username
# Маппинг: message_id тикета в админ-чате -> user_id (для reply)
ticket_message_to_user = {}
# Хранилище сообщений для пересылки: user_id -> [(chat_id, message_id), ...]
user_conversation = defaultdict(list)
# Текстовый лог переписки: user_id -> [{"role": "user"/"ai"/"admin", "text": "...", "time": "..."}, ...]
chat_log = defaultdict(list)
# Время последнего сообщения: user_id -> datetime
user_last_activity = {}
# In-memory cache of active tickets, synced with DB
active_tickets = set()


def db_open_ticket(user_id: int, username: str = "", reason: str = ""):
    """Create/reopen ticket in DB."""
    try:
        requests.post(f"{SUPPORT_API_URL}/admin/tickets/open",
                      json={"telegram_id": user_id, "username": username, "reason": reason}, headers=admin_headers(), timeout=5)
    except Exception as e:
        logger.error(f"Failed to open ticket in DB: {e}")
    active_tickets.add(user_id)


def db_close_ticket(user_id: int):
    """Close ticket in DB."""
    try:
        requests.post(f"{SUPPORT_API_URL}/admin/tickets/close",
                      json={"telegram_id": user_id}, headers=admin_headers(), timeout=5)
    except Exception as e:
        logger.error(f"Failed to close ticket in DB: {e}")
    active_tickets.discard(user_id)


def db_load_active_tickets():
    """Load active tickets from DB on startup."""
    try:
        resp = requests.get(f"{SUPPORT_API_URL}/admin/tickets/active", headers=admin_headers(), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return set(t["telegram_id"] for t in data)
    except Exception as e:
        logger.error(f"Failed to load active tickets from DB: {e}")
    return set()


def sync_active_tickets():
    """Periodically sync active tickets from DB (catches tickets opened from web admin)."""
    global active_tickets
    try:
        db_tickets = db_load_active_tickets()
        if db_tickets != active_tickets:
            added = db_tickets - active_tickets
            removed = active_tickets - db_tickets
            active_tickets = db_tickets
            if added:
                logger.info(f"[sync_tickets] Added from DB: {added}")
            if removed:
                logger.info(f"[sync_tickets] Removed (closed in DB): {removed}")
    except Exception as e:
        logger.error(f"[sync_tickets] Error: {e}")


# Маппинг планов
PLAN_NAMES = {
    "base": "Base",
    "bsbase": "BS Base",
    "family": "Family",
    "bsfamily": "BS Family",
    "trial": "Trial",
    "free": "Free",
}

VALID_PLANS = list(PLAN_NAMES.keys())

# Маппинг сквадов
SQUAD_NAMES = {
    "514a5e22-c599-4f72-81a5-e646f0391db7": "Default",
    "9e60626e-32a8-4d91-a2f8-2aa3fecf7b23": "BS",
    "b6a4e86b-b769-4c86-a2d9-f31bbe645029": "PRO",
}

# Фразы-триггеры для эскалации (AI использует их в ответе)
ESCALATION_TRIGGERS = [
    "передаю ваш вопрос оператору",
    "передаю вопрос оператору",
    "связываю вас с оператором",
    "перевожу на оператора",
]

# Фразы пользователя для запроса оператора
USER_ESCALATION_PHRASES = [
    "позови человека", "позовите человека", "позвать человека",
    "хочу оператора", "хочу человека", "позовите оператора",
    "позови оператора", "нужен человек", "нужен оператор",
    "живой человек", "живой оператор", "свяжите с оператором",
    "свяжите с человеком", "соединить с оператором",
]

STATE_FILE = '/data/bot_state.json'


def save_state():
    """Save bot state to disk for persistence across restarts."""
    try:
        state = {
            'active_tickets': list(active_tickets),
            'user_data_cache': user_data_cache,
            'ticket_message_to_user': {str(k): v for k, v in ticket_message_to_user.items()},
            'user_last_activity': {str(k): v.isoformat() for k, v in user_last_activity.items()},
            'chat_log': {str(k): v[-50:] for k, v in chat_log.items()},  # Keep last 50 msgs per user
        }
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


def load_state():
    """Load bot state from disk on startup."""
    global active_tickets, user_data_cache, ticket_message_to_user, user_last_activity, chat_log
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            active_tickets = set(state.get('active_tickets', []))
            user_data_cache = state.get('user_data_cache', {})
            # Convert string keys back to int
            user_data_cache = {int(k): v for k, v in user_data_cache.items()}
            ticket_message_to_user = {int(k): v for k, v in state.get('ticket_message_to_user', {}).items()}
            user_last_activity = {}
            for k, v in state.get('user_last_activity', {}).items():
                try:
                    user_last_activity[int(k)] = datetime.fromisoformat(v)
                except Exception:
                    pass
            # Restore chat_log
            for k, v in state.get('chat_log', {}).items():
                chat_log[int(k)] = v
            logger.info(f"State loaded: {len(active_tickets)} active tickets, {len(user_data_cache)} cached users, {len(chat_log)} chat logs")
    except Exception as e:
        logger.error(f"Failed to load state: {e}")


# Load persisted state
load_state()
# Sync active tickets from DB
active_tickets = db_load_active_tickets()
logger.info(f"Loaded {len(active_tickets)} active tickets from DB")
# Start auto-close timers for existing tickets
for tid in active_tickets:
    schedule_auto_close(tid)
if active_tickets:
    logger.info(f"Scheduled auto-close for {len(active_tickets)} existing tickets")


def format_subscription_end(sub_end_str):
    """Форматирует дату окончания подписки в МСК"""
    try:
        dt_object = datetime.fromisoformat(sub_end_str.replace("Z", "+00:00"))
        dt_object_moscow = dt_object + timedelta(hours=3)
        return dt_object_moscow.strftime("%d.%m.%Y, %H:%M МСК")
    except Exception:
        return sub_end_str


def get_squad_name(uuid):
    """Возвращает имя сквада по UUID"""
    return SQUAD_NAMES.get(uuid, uuid)


def get_ai_response(telegram_id: int, message: str):
    """Call vpn-api AI support endpoint. Returns response text or None on failure."""
    try:
        resp = requests.post(
            f"{SUPPORT_API_URL}/internal/support/chat",
            json={"telegram_id": telegram_id, "message": message},
            headers=internal_headers(),
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("response")
        else:
            logger.error(f"AI API error: {resp.status_code} {resp.text[:200]}")
            return None
    except requests.Timeout:
        logger.error(f"AI API timeout for user {telegram_id}")
        return None
    except Exception as e:
        logger.error(f"AI API exception for user {telegram_id}: {e}")
        return None


def transcribe_voice(file_path: str) -> str:
    """Транскрибирует голосовое сообщение через ProxyAPI Whisper."""
    if not PROXYAPI_KEY:
        return None
    try:
        with open(file_path, 'rb') as f:
            resp = requests.post(
                "https://openai.api.proxyapi.ru/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {PROXYAPI_KEY}"},
                files={"file": ("voice.ogg", f, "audio/ogg")},
                data={"model": "whisper-1"},
                timeout=30
            )
        if resp.status_code == 200:
            return resp.json().get("text", "")
        else:
            logger.error(f"Whisper API error: {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        logger.error(f"Whisper transcription error: {e}")
        return None


def check_user_wants_escalation(text: str) -> bool:
    """Проверяет, просит ли пользователь связать с оператором."""
    lower = text.lower()
    return any(phrase in lower for phrase in USER_ESCALATION_PHRASES)


def check_ai_escalation(ai_text: str) -> bool:
    """Проверяет, решил ли AI передать вопрос оператору."""
    lower = ai_text.lower()
    return any(trigger in lower for trigger in ESCALATION_TRIGGERS)


def create_admin_ticket(user_id: int, username: str, reason: str = ""):
    """Создаёт тикет для админа с инфо о юзере и кнопкой 'Открыть'."""
    db_open_ticket(user_id, username, reason)
    save_state()

    # Получаем информацию о пользователе
    user_info_text = ""
    try:
        resp = requests.get(f"{API_URL}/{user_id}/info")
        if resp.status_code == 200:
            user = resp.json()
            plan = PLAN_NAMES.get(user.get("plan", ""), user.get("plan", "—"))
            sub_end = format_subscription_end(user.get("subscription_end", "—"))
            is_active = "Активна" if user.get("is_active") == 1 else "Неактивна"
            is_pro = "Да" if user.get("is_pro") else "Нет"
            user_info_text = (
                f"\n<b>Тариф:</b> {plan}"
                f"\n<b>Статус:</b> {is_active}"
                f"\n<b>PRO:</b> {is_pro}"
                f"\n<b>Окончание:</b> {sub_end}"
            )
    except Exception as e:
        logger.error(f"Failed to get user info for ticket: {e}")

    msg_count = len(user_conversation.get(user_id, []))

    ticket_text = (
        f"🎫 <b>НОВЫЙ ТИКЕТ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Пользователь:</b> @{username}\n"
        f"<b>ID:</b> <code>{user_id}</code>"
        f"{user_info_text}\n"
        f"<b>Сообщений в диалоге:</b> {msg_count}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    if reason:
        ticket_text += f"\n<b>Причина:</b> {reason}"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(
            text="📖 Открыть переписку",
            callback_data=f"open_ticket_{user_id}"
        ),
        types.InlineKeyboardButton(
            text="✅ Закрыть тикет",
            callback_data=f"close_ticket_{user_id}"
        )
    )

    for admin_id in ADMIN_IDS:
        try:
            sent = bot.send_message(
                admin_id, ticket_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
            ticket_message_to_user[sent.message_id] = user_id
            logger.info(f"Ticket sent to admin {admin_id} for user {user_id}")
        except Exception as e:
            logger.error(f"Error sending ticket to admin {admin_id}: {e}")


def peek_conversation(admin_chat_id: int, user_id: int):
    """Просмотр переписки юзера — загружает из БД через API."""
    username = user_data_cache.get(user_id, f"id{user_id}")

    # Load from DB via API
    try:
        resp = requests.get(f"{SUPPORT_API_URL}/admin/chats/{user_id}", headers=admin_headers(), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            db_messages = data.get("messages", [])
        else:
            db_messages = []
    except Exception as e:
        logger.error(f"Failed to load chat from API for {user_id}: {e}")
        db_messages = []

    # Fallback to in-memory if DB is empty
    if not db_messages:
        log = chat_log.get(user_id, [])
        if not log:
            bot.send_message(admin_chat_id, "Нет сохранённых сообщений.")
            return
        db_messages = [{"role": e["role"], "content": e["text"], "created_at": e.get("time", "")} for e in log[-30:]]

    import re as re_mod

    # Build renderable items: either text lines or photo file_ids
    items = []  # list of {"type": "text", "line": str} or {"type": "photo", "file_id": str, "caption": str, "label": str}
    for entry in db_messages[-30:]:
        role = entry.get("role", "user")
        text = entry.get("content", entry.get("text", ""))

        # Skip [SYSTEM] messages
        if text and text.startswith("[SYSTEM]"):
            continue

        if role == "user":
            icon, name = "👤", f"@{username}"
        elif role in ("assistant", "ai"):
            icon, name = "🤖", "ИИ"
        elif role == "admin":
            icon, name = "👨‍💼", "Админ"
        else:
            icon, name = "❓", role

        created = entry.get("time", entry.get("created_at", ""))
        time_str = ""
        if created:
            try:
                d = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                time_str = d.strftime("%H:%M")
            except Exception:
                pass

        label = f"{icon} <b>{name}</b> [{time_str}]"

        # Photo message
        if text and text.startswith("[photo:"):
            match = re_mod.match(r'\[photo:([^\]]+)\]\s*(.*)', text)
            if match:
                items.append({"type": "photo", "file_id": match.group(1).strip(),
                              "caption": match.group(2).strip(), "label": label})
                continue

        items.append({"type": "text", "line": f"{label}:\n{text}"})

    header = f"💬 <b>Диалог с @{username} (ID: <code>{user_id}</code>):</b>\n\n"

    # Send items: batch text lines, send photos inline
    current_text = header
    for item in items:
        if item["type"] == "photo":
            # Flush accumulated text first
            if current_text.strip():
                try:
                    bot.send_message(admin_chat_id, current_text, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error sending peek text: {e}")
                current_text = ""
            # Send the photo
            try:
                cap = f"{item['label']}:\n📷 {item['caption']}" if item['caption'] else item['label']
                bot.send_photo(admin_chat_id, item["file_id"], caption=cap, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error sending photo: {e}")
                current_text += f"{item['label']}:\n📷 Фото (недоступно)\n\n"
        else:
            line = item["line"]
            if len(current_text) + len(line) + 2 > 4000:
                try:
                    bot.send_message(admin_chat_id, current_text, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error sending peek text: {e}")
                current_text = ""
            current_text += line + "\n\n"

    full_text = current_text if current_text.strip() else header

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton(
        text="💬 Ответить пользователю",
        callback_data=f"reply_to_{user_id}"
    ))
    if user_id in active_tickets:
        markup.add(types.InlineKeyboardButton(
            text="✅ Закрыть тикет",
            callback_data=f"close_ticket_{user_id}"
        ))
    else:
        markup.add(types.InlineKeyboardButton(
            text="👌 Ок",
            callback_data="peek_done"
        ))

    sent = bot.send_message(
        admin_chat_id,
        full_text,
        reply_markup=markup,
        parse_mode="HTML"
    )
    ticket_message_to_user[sent.message_id] = user_id


def open_ticket_conversation(admin_chat_id: int, user_id: int):
    """Показывает переписку из БД и предлагает ответить."""
    # Use peek_conversation to show the chat from DB
    peek_conversation(admin_chat_id, user_id)


def schedule_auto_close(user_id: int):
    """Schedule automatic ticket close after AUTO_CLOSE_HOURS."""
    # Cancel existing timer if any
    old_timer = auto_close_timers.pop(user_id, None)
    if old_timer:
        old_timer.cancel()

    def auto_close():
        logger.info(f"Auto-closing ticket for user {user_id} after {AUTO_CLOSE_HOURS}h")
        close_ticket(None, user_id, auto=True)

    timer = threading.Timer(AUTO_CLOSE_HOURS * 3600, auto_close)
    timer.daemon = True
    timer.start()
    auto_close_timers[user_id] = timer


def handle_escalation(chat_id: int, user_id: int, reason: str = ""):
    """Обработка эскалации — создаёт тикет и уведомляет пользователя."""
    if user_id in active_tickets:
        return  # Already escalated
    username = user_data_cache.get(user_id, f"id{user_id}")
    create_admin_ticket(user_id, username, reason)
    schedule_auto_close(user_id)
    bot.send_message(
        chat_id,
        "🙋 Администратор уже спешит в чат!\n\n"
        "Пожалуйста, ожидайте — оператор ответит в ближайшее время. "
        "Все ваши сообщения будут переданы ему."
    )
    # Notify AI about escalation so it has context when ticket is closed
    get_ai_response(user_id, "[SYSTEM] Пользователь был переведён на оператора. Диалог с ИИ приостановлен до закрытия тикета.")
    logger.info(f"Escalation triggered for user {user_id}")


def process_ai_response(chat_id: int, user_id: int, user_text: str):
    """Отправляет текст в AI, обрабатывает ответ и эскалацию."""
    bot.send_chat_action(chat_id, 'typing')

    ai_text = get_ai_response(user_id, user_text)
    if ai_text:
        # Разбиваем длинные сообщения на части (лимит Telegram: 4096)
        try:
            chunks = []
            if len(ai_text) <= 4096:
                chunks = [ai_text]
            else:
                # Разбиваем по переносам строк, не разрывая слова
                remaining = ai_text
                while remaining:
                    if len(remaining) <= 4096:
                        chunks.append(remaining)
                        break
                    # Ищем последний перенос строки в пределах лимита
                    cut = remaining[:4096].rfind('\n')
                    if cut == -1:
                        cut = remaining[:4096].rfind(' ')
                    if cut == -1:
                        cut = 4096
                    chunks.append(remaining[:cut])
                    remaining = remaining[cut:].lstrip()

            for chunk in chunks:
                sent = bot.send_message(chat_id, chunk)
                user_conversation[user_id].append((chat_id, sent.message_id))
            chat_log[user_id].append({"role": "ai", "text": ai_text, "time": datetime.now().strftime("%H:%M")})
        except Exception as e:
            logger.error(f"Error sending AI response to {chat_id}: {e}")

        # Проверяем, решил ли AI эскалировать
        if check_ai_escalation(ai_text):
            handle_escalation(chat_id, user_id, reason="AI предложил связаться с оператором")
    else:
        # AI недоступен — автоматическая эскалация
        logger.warning(f"AI unavailable for user {user_id}, escalating")
        bot.send_message(chat_id, "ИИ-ассистент временно недоступен.")
        handle_escalation(chat_id, user_id, reason="AI недоступен")


# ===== КОМАНДЫ =====

@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id in ADMIN_IDS:
        logger.info(f"Admin {message.from_user.id} started the bot")
        bot.send_message(message.chat.id,
                         "Вы админ. Используйте /help для списка команд.")
    else:
        logger.info(f"User {message.from_user.id} started the bot")
        bot.reply_to(message,
                     "Здравствуйте! Это бот техподдержки SvoiVPN.\n\n"
                     "Напишите Ваш вопрос — ИИ-ассистент ответит мгновенно.\n"
                     "Если нужен живой оператор — просто напишите \"позовите оператора\".")


@bot.message_handler(commands=['help'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_help(message):
    logger.info(f"Admin {message.from_user.id} requested /help")
    help_text = """
<b>🛠 Список административных команд:</b>

<b>📋 Информация:</b>
1. <b>/info TG_ID</b> — Подробная информация о пользователе
   <i>Пример:</i> <code>/info 123456789</code>

2. <b>/squads TG_ID</b> — Текущие сквады пользователя (из Remnawave)
   <i>Пример:</i> <code>/squads 123456789</code>

<b>⚙️ Управление подпиской:</b>
3. <b>/extend TG_ID PLAN DAYS</b> — Продлить подписку
   <i>Пример:</i> <code>/extend 123456789 base 30</code>
   <i>Планы:</i> base, bsbase, family, bsfamily, trial, free

4. <b>/toggle_pro TG_ID on|off</b> — Включить/выключить PRO режим
   <i>Пример:</i> <code>/toggle_pro 123456789 on</code>
   <i>PRO добавляет:</i> XHTTP, gRPC, Trojan, Shadowsocks

5. <b>/disable_device_limit TG_ID</b> — Отключить лимит устройств
   <i>Пример:</i> <code>/disable_device_limit 123456789</code>

6. <b>/compensate DAYS</b> — Начислить компенсацию всем активным юзерам
   <i>Пример:</i> <code>/compensate 7</code>
   Продлит подписку на N дней по текущему тарифу каждого юзера

<b>🔧 Тех. работы:</b>
7. <b>/maintenance on</b> — Включить режим техработ (ИИ сообщает юзерам)
   <b>/maintenance off</b> — Выключить режим техработ

<b>💬 Мониторинг:</b>
8. <b>/chats</b> — Просмотр всех диалогов юзеров с ИИ
   Можно читать переписку и при необходимости вмешаться

<b>🎫 Тикеты:</b>
9. <b>/reply</b> — Показать активные тикеты (эскалации)
10. Ответьте (reply) на сообщение тикета, чтобы отправить ответ пользователю
11. Используйте кнопку «Закрыть тикет» для завершения

<b>/help</b> — Эта справка
"""
    bot.send_message(chat_id=message.chat.id, text=help_text, parse_mode="HTML")


@bot.message_handler(commands=['info'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_info(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Использование: /info TG_ID\nПример: /info 123456789")
            return

        tg_id = parts[1]
        if not tg_id.isdigit():
            raise ValueError("Telegram ID должен содержать только цифры")

        logger.info(f"Admin {message.from_user.id} requested /info for {tg_id}")

        response = requests.get(f"{API_URL}/{tg_id}/info")

        if response.status_code == 200:
            user = response.json()

            plan = user.get("plan", "—")
            plan_display = PLAN_NAMES.get(plan, plan)
            is_pro = user.get("is_pro", False)
            sub_end = format_subscription_end(user.get("subscription_end", "—"))
            is_active = "Активна" if user.get("is_active") == 1 else "Неактивна"
            username = user.get("username") or "—"
            referrals = user.get("referrals") or []
            referral_id = user.get("referral_id") or "—"
            device_limit = user.get("device_limit", "—")
            auto_renew = user.get("auto_renew", False)
            payed_refs = user.get("payed_refs", 0)
            is_used_trial = user.get("is_used_trial", False)

            # Get email if exists
            user_email = "—"
            try:
                email_resp = requests.get(f"{SUPPORT_API_URL}/internal/user-email/{tg_id}", headers=internal_headers())
                if email_resp.status_code == 200:
                    user_email = email_resp.json().get("email", "—")
            except Exception:
                pass

            text = f"""<b>📋 Информация о пользователе</b>

<b>ID:</b> <code>{tg_id}</code>
<b>Username:</b> @{username}
<b>Email:</b> {user_email}
<b>UUID:</b> <code>{user.get("uuid", "—")}</code>

<b>📊 Подписка:</b>
  Тариф: <b>{plan_display}</b>
  PRO режим: {"⚡ Включён" if is_pro else "❌ Выключен"}
  Статус: {is_active}
  Окончание: {sub_end}
  Автопродление: {"✅ Да" if auto_renew else "❌ Нет"}

<b>🔧 Настройки:</b>
  Лимит устройств: {device_limit}
  Триал использован: {"Да" if is_used_trial else "Нет"}

<b>👥 Рефералы:</b>
  Приглашён: {referral_id}
  Приглашённые: {len(referrals)} чел.
  Оплаченные рефы: {payed_refs}

<b>🔗 Ссылка на подписку:</b>
<code>{user.get("sub_link", "—")}</code>"""

            bot.reply_to(message, text, parse_mode="HTML")
        elif response.status_code == 404:
            bot.reply_to(message, f"❌ Пользователь {tg_id} не найден")
        else:
            bot.reply_to(message, f"❌ Ошибка: {response.status_code} — {response.text}")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /info: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


@bot.message_handler(commands=['squads'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_squads(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Использование: /squads TG_ID\nПример: /squads 123456789")
            return

        tg_id = parts[1]
        if not tg_id.isdigit():
            raise ValueError("Telegram ID должен содержать только цифры")

        logger.info(f"Admin {message.from_user.id} requested /squads for {tg_id}")

        response = requests.get(f"{API_URL}/{tg_id}/squads")

        if response.status_code == 200:
            data = response.json()
            squads = data.get("squads", [])

            if not squads:
                bot.reply_to(message, f"У пользователя {tg_id} нет назначенных сквадов.")
                return

            lines = [f"<b>🏷 Сквады пользователя {tg_id}:</b>\n"]
            for s in squads:
                uuid = s.get("uuid", "—")
                name = s.get("name", get_squad_name(uuid))
                lines.append(f"  • <b>{name}</b>\n    <code>{uuid}</code>")

            bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
        else:
            bot.reply_to(message, f"❌ Ошибка: {response.status_code} — {response.text}")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /squads: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


@bot.message_handler(commands=['extend'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_extend(message):
    try:
        parts = message.text.split()
        if len(parts) != 4:
            plans_list = ", ".join(VALID_PLANS)
            bot.reply_to(message,
                         f"Использование: /extend TG_ID PLAN DAYS\n"
                         f"Пример: /extend 123456789 base 30\n\n"
                         f"Доступные планы: {plans_list}\n\n"
                         f"<b>Сквады по планам:</b>\n"
                         f"  base, family, trial, free → Default\n"
                         f"  bsbase, bsfamily → Default + BS\n"
                         f"  + PRO режим → + PRO сквад",
                         parse_mode="HTML")
            return

        tg_id = parts[1]
        plan = parts[2].lower()
        days = int(parts[3])

        if not tg_id.isdigit():
            raise ValueError("Telegram ID должен содержать только цифры")

        if plan not in VALID_PLANS:
            plans_list = ", ".join(VALID_PLANS)
            bot.reply_to(message, f"❌ Неизвестный план '{plan}'.\nДоступные: {plans_list}")
            return

        if days <= 0:
            raise ValueError("Количество дней должно быть больше 0")

        logger.info(f"Admin {message.from_user.id} extending {tg_id}: plan={plan}, days={days}")

        response = requests.patch(
            f"{API_URL}/{tg_id}/extend",
            json={"days": days, "plan": plan}
        )

        if response.status_code == 200:
            plan_display = PLAN_NAMES.get(plan, plan)
            squads_info = "Default"
            if plan.startswith("bs"):
                squads_info = "Default + BS"

            text = (f"✅ Подписка продлена\n\n"
                    f"<b>ID:</b> <code>{tg_id}</code>\n"
                    f"<b>План:</b> {plan_display}\n"
                    f"<b>Дней:</b> {days}\n"
                    f"<b>Сквады:</b> {squads_info}")
            bot.reply_to(message, text, parse_mode="HTML")
        elif response.status_code == 404:
            bot.reply_to(message, f"❌ Пользователь {tg_id} не найден")
        else:
            bot.reply_to(message, f"❌ Ошибка: {response.text}")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /extend: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


@bot.message_handler(commands=['toggle_pro'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_toggle_pro(message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message,
                         "Использование: /toggle_pro TG_ID on|off\n"
                         "Пример: /toggle_pro 123456789 on\n\n"
                         "PRO режим добавляет протоколы: XHTTP, gRPC, Trojan, Shadowsocks")
            return

        tg_id = parts[1]
        action = parts[2].lower()

        if not tg_id.isdigit():
            raise ValueError("Telegram ID должен содержать только цифры")

        if action not in ("on", "off"):
            bot.reply_to(message, "❌ Укажите on или off.\nПример: /toggle_pro 123456789 on")
            return

        enable = action == "on"
        logger.info(f"Admin {message.from_user.id} toggle PRO for {tg_id}: enable={enable}")

        response = requests.patch(
            f"{API_URL}/{tg_id}/pro",
            json={"is_pro": enable}
        )

        if response.status_code == 200:
            status = "⚡ Включён" if enable else "❌ Выключен"
            bot.reply_to(message,
                         f"✅ PRO режим для <code>{tg_id}</code>: {status}",
                         parse_mode="HTML")
        elif response.status_code == 404:
            bot.reply_to(message, f"❌ Пользователь {tg_id} не найден")
        else:
            bot.reply_to(message, f"❌ Ошибка: {response.text}")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /toggle_pro: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


@bot.message_handler(commands=['disable_device_limit'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_disable_device_limit(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Использование: /disable_device_limit TG_ID\nПример: /disable_device_limit 123456789")
            return

        tg_id = parts[1]
        if not tg_id.isdigit():
            raise ValueError("Telegram ID должен содержать только цифры")

        logger.info(f"Admin {message.from_user.id} disabling device limit for {tg_id}")

        response = requests.post(
            f"{API_URL}/{tg_id}/disable_device",
            headers={"Content-Type": "application/json"}
        )

        if response.status_code == 200:
            bot.reply_to(message, f"✅ Лимит устройств для <code>{tg_id}</code> временно отключен", parse_mode="HTML")
        else:
            bot.reply_to(message, f"❌ Ошибка: {response.text}")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /disable_device_limit: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


@bot.message_handler(commands=['maintenance'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_maintenance(message):
    """Переключить режим тех. работ: /maintenance on|off"""
    try:
        parts = message.text.split()
        if len(parts) != 2 or parts[1] not in ('on', 'off'):
            bot.reply_to(message, "Использование: /maintenance on|off")
            return

        enabled = parts[1] == 'on'
        resp = requests.post(
            f"{SUPPORT_API_URL}/internal/support/maintenance",
            json={"enabled": enabled},
            headers=internal_headers(),
            timeout=10
        )
        if resp.status_code == 200:
            status = "🔴 ВКЛ" if enabled else "🟢 ВЫКЛ"
            bot.reply_to(message, f"Режим тех. работ: {status}\nИИ будет {'сообщать юзерам о техработах' if enabled else 'работать в обычном режиме'}.")
        else:
            bot.reply_to(message, f"Ошибка: {resp.status_code}")
    except Exception as e:
        logger.error(f"Error in /maintenance: {e}")
        bot.reply_to(message, f"Ошибка: {e}")


@bot.message_handler(commands=['compensate'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_compensate(message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Использование: /compensate DAYS\nПример: /compensate 7")
            return

        days = int(parts[1])
        if days <= 0:
            raise ValueError("Количество дней должно быть больше 0")

        logger.info(f"Admin {message.from_user.id} starting compensation: {days} days")

        # Получаем список активных юзеров
        response = requests.get(f"{API_URL.rsplit('/', 1)[0]}/users/active")
        if response.status_code != 200:
            bot.reply_to(message, f"❌ Не удалось получить список пользователей: {response.text}")
            return

        users = response.json()
        total = len(users)
        if total == 0:
            bot.reply_to(message, "Нет активных пользователей.")
            return

        status_msg = bot.reply_to(message,
                                  f"⏳ Начисляю компенсацию {days} дн. для {total} активных пользователей...")

        success = 0
        failed = 0
        skipped = 0

        for user in users:
            tg_id = user.get("telegram_id")
            plan = user.get("plan", "")

            if plan in ("trial", "free", ""):
                skipped += 1
                continue

            try:
                r = requests.patch(
                    f"{API_URL}/{tg_id}/extend",
                    json={"days": days, "plan": plan}
                )
                if r.status_code == 200:
                    success += 1
                else:
                    failed += 1
                    logger.warning(f"Compensate failed for {tg_id}: {r.status_code} {r.text}")
            except Exception as e:
                failed += 1
                logger.error(f"Compensate error for {tg_id}: {e}")

        result_text = (
            f"✅ Компенсация завершена!\n\n"
            f"<b>Дней:</b> {days}\n"
            f"<b>Всего активных:</b> {total}\n"
            f"<b>Успешно:</b> {success}\n"
            f"<b>Пропущено (trial/free):</b> {skipped}\n"
            f"<b>Ошибки:</b> {failed}"
        )

        logger.info(f"Compensation done: {success} success, {skipped} skipped, {failed} failed")
        bot.edit_message_text(result_text, message.chat.id, status_msg.message_id, parse_mode="HTML")

    except ValueError as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")
    except Exception as e:
        logger.error(f"Error in /compensate: {e}")
        bot.reply_to(message, f"⚠️ Произошла ошибка: {str(e)}")


# ===== МОНИТОРИНГ ЧАТОВ =====

def format_time_ago(dt):
    """Форматирует время в 'X мин назад' / 'X ч назад' / 'X дн назад'."""
    delta = datetime.now() - dt
    minutes = int(delta.total_seconds() / 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    days = hours // 24
    return f"{days} дн"


@bot.message_handler(commands=['chats'], func=lambda message: message.from_user.id in ADMIN_IDS)
def show_active_chats(message):
    """Показывает всех юзеров с диалогами — загружает из БД."""
    logger.info(f"Admin {message.from_user.id} requested /chats")

    # Load chats from DB API instead of in-memory chat_log
    try:
        resp = requests.get(f"{SUPPORT_API_URL}/admin/chats", headers=admin_headers(), timeout=10)
        if resp.status_code != 200:
            bot.reply_to(message, "Ошибка загрузки чатов.")
            return
        db_chats = resp.json()
    except Exception as e:
        logger.error(f"Failed to load chats from DB: {e}")
        bot.reply_to(message, "Ошибка соединения с API.")
        return

    if not db_chats:
        bot.reply_to(message, "Нет активных диалогов.")
        return

    # Sync active tickets
    sync_active_tickets()

    markup = types.InlineKeyboardMarkup()
    for chat in db_chats[:20]:
        user_id = chat.get("telegram_id")
        username = chat.get("username", f"id{user_id}")
        msg_count = chat.get("message_count", 0)
        last_time_str = chat.get("last_time", "")

        # Parse time for status
        try:
            last_time = datetime.fromisoformat(last_time_str.replace("+00:00", "").replace("Z", ""))
            time_str = format_time_ago(last_time)
        except Exception:
            time_str = "?"
            last_time = None

        # Статус
        if user_id in active_tickets:
            status = "🎫"
        elif last_time and (datetime.now() - last_time).total_seconds() < 600:
            status = "🟢"
        elif last_time and (datetime.now() - last_time).total_seconds() < 3600:
            status = "🟡"
        else:
            status = "⚪"

        # Cache username for later use
        user_data_cache[user_id] = username

        markup.add(types.InlineKeyboardButton(
            text=f"{status} @{username} · {msg_count} сообщ. · {time_str}",
            callback_data=f"peek_{user_id}",
        ))

    legend = (
        "💬 <b>Диалоги с ИИ:</b>\n\n"
        "🟢 активен (&lt; 10 мин)\n"
        "🟡 недавно (&lt; 1 ч)\n"
        "⚪ давно\n"
        "🎫 есть тикет"
    )
    bot.send_message(message.chat.id, legend, reply_markup=markup, parse_mode="HTML")


# ===== ТИКЕТЫ =====

@bot.message_handler(commands=['reply'], func=lambda message: message.from_user.id in ADMIN_IDS)
def show_active_tickets(message):
    logger.info(f"Admin {message.from_user.id} requested /reply")
    if not active_tickets:
        bot.reply_to(message, "Нет активных тикетов.")
        return

    markup = types.InlineKeyboardMarkup()
    for user_id in active_tickets:
        username = user_data_cache.get(user_id, f"id{user_id}")
        markup.add(types.InlineKeyboardButton(
            text=f"@{username} (ID: {user_id})",
            callback_data=f"view_ticket_{user_id}",
        ))

    bot.send_message(message.chat.id, "🎫 Активные тикеты:", reply_markup=markup)


# ===== ОБРАБОТКА СООБЩЕНИЙ ПОЛЬЗОВАТЕЛЕЙ =====

@bot.message_handler(func=lambda message: message.from_user.id not in ADMIN_IDS,
                     content_types=['text'])
def handle_user_text_message(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    user_data_cache[user_id] = username

    logger.info(f"User @{username} ({user_id}) sent text: {message.text[:50]}...")

    # Sync tickets from DB (catches tickets opened from web admin)
    sync_active_tickets()

    # Сохраняем сообщение юзера для пересылки в тикете
    user_conversation[user_id].append((message.chat.id, message.message_id))
    chat_log[user_id].append({"role": "user", "text": message.text, "time": datetime.now().strftime("%H:%M")})
    user_last_activity[user_id] = datetime.now()
    save_state()

    # If ticket is open, forward to admin and remind user to wait
    if user_id in active_tickets:
        for admin_id in ADMIN_IDS:
            try:
                bot.forward_message(admin_id, message.chat.id, message.message_id)
            except Exception as e:
                logger.error(f"Error forwarding to admin {admin_id}: {e}")
        bot.send_message(message.chat.id, "⏳ Ваш вопрос уже у оператора. Пожалуйста, ожидайте ответа.")
        return

    # Проверяем, просит ли пользователь оператора напрямую
    if check_user_wants_escalation(message.text):
        # Save the user message to DB before escalation
        try:
            requests.post(f"{SUPPORT_API_URL}/admin/chats/{user_id}/save",
                          json={"role": "user", "content": message.text}, headers=admin_headers(), timeout=5)
        except Exception:
            pass
        handle_escalation(message.chat.id, user_id, reason="Пользователь попросил оператора")
        return

    # Отправляем в AI
    process_ai_response(message.chat.id, user_id, message.text)


@bot.message_handler(func=lambda message: message.from_user.id not in ADMIN_IDS,
                     content_types=['voice'])
def handle_user_voice_message(message):
    """Обработка голосовых сообщений: транскрибируем и отправляем в AI."""
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    user_data_cache[user_id] = username

    logger.info(f"User @{username} ({user_id}) sent voice message")

    # Сохраняем голосовое для пересылки в тикете
    user_conversation[user_id].append((message.chat.id, message.message_id))
    save_state()

    # If ticket is open, forward to admin and remind user to wait
    if user_id in active_tickets:
        for admin_id in ADMIN_IDS:
            try:
                bot.forward_message(admin_id, message.chat.id, message.message_id)
            except Exception as e:
                logger.error(f"Error forwarding to admin {admin_id}: {e}")
        bot.send_message(message.chat.id, "⏳ Ваш вопрос уже у оператора. Пожалуйста, ожидайте ответа.")
        return

    bot.send_chat_action(message.chat.id, 'typing')

    try:
        # Скачиваем голосовое сообщение
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)

        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp:
            tmp.write(downloaded)
            tmp_path = tmp.name

        # Транскрибируем
        transcription = transcribe_voice(tmp_path)

        # Удаляем временный файл
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        if transcription:
            logger.info(f"Voice transcribed for {user_id}: {transcription[:50]}...")

            # Проверяем эскалацию
            if check_user_wants_escalation(transcription):
                handle_escalation(message.chat.id, user_id, reason="Пользователь попросил оператора (голосовое)")
                return

            # Отправляем транскрипцию в AI
            process_ai_response(message.chat.id, user_id, transcription)
        else:
            bot.send_message(
                message.chat.id,
                "Не удалось распознать голосовое сообщение. Пожалуйста, напишите текстом."
            )

    except Exception as e:
        logger.error(f"Voice processing error for {user_id}: {e}")
        bot.send_message(
            message.chat.id,
            "Не удалось обработать голосовое сообщение. Пожалуйста, напишите текстом."
        )


@bot.message_handler(func=lambda message: message.from_user.id not in ADMIN_IDS,
                     content_types=['photo', 'document', 'audio', 'video', 'sticker'])
def handle_user_media_message(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    user_data_cache[user_id] = username

    logger.info(f"User @{username} ({user_id}) sent {message.content_type}")

    # Сохраняем сообщение для пересылки в тикете
    user_conversation[user_id].append((message.chat.id, message.message_id))

    # Save photo file_id for later display
    if message.content_type == 'photo' and message.photo:
        file_id = message.photo[-1].file_id  # Largest photo
        media_text = f"[photo:{file_id}]"
        if message.caption:
            media_text += f" {message.caption}"
    elif message.content_type == 'document' and message.document:
        media_text = f"[file:{message.document.file_id}:{message.document.file_name or 'file'}]"
    else:
        media_text = message.caption or f"[{message.content_type}]"

    chat_log[user_id].append({"role": "user", "text": media_text, "time": datetime.now().strftime("%H:%M")})
    user_last_activity[user_id] = datetime.now()
    save_state()

    # Save media message to DB via admin reply endpoint (as user role)
    try:
        requests.post(f"{SUPPORT_API_URL}/admin/chats/{user_id}/save",
                      json={"role": "user", "content": media_text}, headers=admin_headers(), timeout=5)
    except Exception:
        pass

    # If ticket is open, forward to admin and remind user to wait
    if user_id in active_tickets:
        for admin_id in ADMIN_IDS:
            try:
                bot.forward_message(admin_id, message.chat.id, message.message_id)
            except Exception as e:
                logger.error(f"Error forwarding to admin {admin_id}: {e}")
        bot.send_message(message.chat.id, "⏳ Ваш вопрос уже у оператора. Пожалуйста, ожидайте ответа.")
        return

    # Фото с подписью — отправляем только подпись в AI
    if message.caption:
        process_ai_response(message.chat.id, user_id, message.caption)
        return

    bot.send_message(
        message.chat.id,
        "Опишите проблему текстом — ИИ-ассистент сможет помочь быстрее 😊"
    )


# ===== CALLBACKS =====

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data == 'peek_done':
        bot.answer_callback_query(call.id, text="👍")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        return
    elif call.data.startswith('peek_'):
        try:
            user_id = int(call.data.split('_')[-1])
        except ValueError:
            bot.answer_callback_query(call.id, text="Ошибка")
            return
        bot.answer_callback_query(call.id, text="Загружаю переписку...")
        peek_conversation(call.message.chat.id, user_id)
    elif call.data.startswith('open_ticket_'):
        user_id = int(call.data.split('_')[-1])
        bot.answer_callback_query(call.id, text="Загружаю переписку...")
        open_ticket_conversation(call.message.chat.id, user_id)
    elif call.data.startswith('view_ticket_'):
        user_id = int(call.data.split('_')[-1])
        bot.answer_callback_query(call.id, text="Загружаю...")
        open_ticket_conversation(call.message.chat.id, user_id)
    elif call.data.startswith('reply_to_'):
        user_id = int(call.data.split('_')[-1])
        bot.answer_callback_query(call.id, text="Ответьте на это сообщение")
        # Create a reply anchor so admin can reply
        if user_id not in active_tickets:
            db_open_ticket(user_id, user_data_cache.get(user_id, str(user_id)), "reply_to")
            save_state()
        sent = bot.send_message(
            call.message.chat.id,
            f"✍️ <b>Ответьте (reply) на это сообщение, чтобы написать @{user_data_cache.get(user_id, str(user_id))}:</b>",
            parse_mode="HTML"
        )
        ticket_message_to_user[sent.message_id] = user_id
    elif call.data.startswith('close_ticket_'):
        user_id = int(call.data.split('_')[-1])
        close_ticket(call.message.chat.id, user_id)
        bot.answer_callback_query(call.id, text="Тикет закрыт")




def close_ticket(admin_chat_id, user_id, auto=False):
    if user_id in active_tickets:
        db_close_ticket(user_id)
        # Cancel auto-close timer if exists
        timer = auto_close_timers.pop(user_id, None)
        if timer:
            timer.cancel()
        save_state()
        if user_id in user_conversation:
            del user_conversation[user_id]
        # Notify AI that operator finished, so it has full context
        get_ai_response(user_id, "[SYSTEM] Оператор завершил диалог и закрыл тикет. ИИ-ассистент снова активен.")
        # Notify user
        try:
            bot.send_message(user_id, "✅ Всего доброго! Если появятся вопросы — обращайтесь, всегда рады помочь! 😊")
        except Exception as e:
            logger.error(f"Error notifying user {user_id} about ticket close: {e}")
        logger.info(f"Ticket closed for user {user_id} (auto={auto})")
        if admin_chat_id:
            bot.send_message(admin_chat_id, f"✅ Тикет для {user_id} закрыт{' (автоматически)' if auto else ''}.")
    else:
        if admin_chat_id:
            bot.send_message(admin_chat_id, "Тикет уже закрыт или не существует.")


# ===== ОТВЕТ АДМИНА =====

@bot.message_handler(func=lambda message: message.reply_to_message is not None and
                                          message.from_user.id in ADMIN_IDS,
                     content_types=['text', 'photo', 'document', 'audio', 'video', 'voice', 'sticker'])
def handle_admin_reply(message):
    """Админ отвечает на тикет — reply на сообщение тикета."""
    replied_msg_id = message.reply_to_message.message_id

    # Ищем user_id по message_id тикета
    user_id = ticket_message_to_user.get(replied_msg_id)

    # Фоллбэк: пробуем найти по тексту сообщения (старый формат)
    if not user_id:
        reply_text = message.reply_to_message.text or ""
        if "ID:" in reply_text:
            try:
                user_id = int(reply_text.split("ID: ")[1].split(")")[0].strip())
            except (IndexError, ValueError):
                pass
        if "ID:</code>" in reply_text:
            try:
                user_id = int(reply_text.split("ID:</code>")[0].split("<code>")[-1].strip())
            except (IndexError, ValueError):
                pass

    if not user_id:
        return  # Не тикетное сообщение — игнорируем

    username = user_data_cache.get(user_id, f"id{user_id}")

    try:
        if message.content_type == 'text':
            bot.send_message(user_id, f"✉️ Ответ поддержки:\n{message.text}")
        elif message.content_type == 'photo':
            bot.send_photo(user_id, message.photo[-1].file_id, caption=f"✉️ Ответ поддержки:\n{message.caption or ''}")
        elif message.content_type == 'document':
            bot.send_document(user_id, message.document.file_id,
                              caption=f"✉️ Ответ поддержки:\n{message.caption or ''}")
        elif message.content_type == 'audio':
            bot.send_audio(user_id, message.audio.file_id, caption=f"✉️ Ответ поддержки:\n{message.caption or ''}")
        elif message.content_type == 'video':
            bot.send_video(user_id, message.video.file_id, caption=f"✉️ Ответ поддержки:\n{message.caption or ''}")
        elif message.content_type == 'voice':
            bot.send_voice(user_id, message.voice.file_id, caption="✉️ Ответ поддержки")
        elif message.content_type == 'sticker':
            bot.send_sticker(user_id, message.sticker.file_id)
            bot.send_message(user_id, "✉️ Ответ поддержки (стикер)")

        # Record admin reply in chat log and DB (do NOT call AI — ticket is active)
        if message.content_type == 'text':
            chat_log[user_id].append({"role": "admin", "text": message.text, "time": datetime.now().strftime("%H:%M")})
            try:
                requests.post(f"{SUPPORT_API_URL}/admin/chats/{user_id}/save",
                              json={"role": "admin", "content": message.text}, headers=admin_headers(), timeout=5)
            except Exception:
                pass
        else:
            chat_log[user_id].append({"role": "admin", "text": f"[{message.content_type}]", "time": datetime.now().strftime("%H:%M")})

        logger.info(f"Admin {message.from_user.id} replied to user {user_id}")
        bot.reply_to(message, f"✅ Ответ отправлен пользователю @{username}.")
    except Exception as e:
        logger.error(f"Error sending reply to user {user_id}: {e}")
        bot.reply_to(message, f"❌ Ошибка при отправке ответа: {e}")


# Запускаем бота
if __name__ == '__main__':
    logger.info("Tech support bot starting...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30, restart_on_change=False)
        except Exception as e:
            logger.error(f"Polling crashed: {e}, restarting in 5 seconds...")
            import time
            time.sleep(5)
