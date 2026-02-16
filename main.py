import os
import logging
import telebot
from telebot import types
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta
import requests

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

# Инициализируем бота
bot = telebot.TeleBot(BOT_TOKEN)

# База данных для хранения сообщений пользователей
user_tickets = defaultdict(list)
active_tickets = set()
user_data_cache = {}
pending_messages = defaultdict(list)

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
                     "Привет! Это бот техподдержки SvoiVPN. Напишите ваш вопрос, и мы обязательно вам ответим в скором времени!")


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

<b>🎫 Тикеты:</b>
6. <b>/reply</b> — Показать активные тикеты
7. Ответьте на сообщение тикета, чтобы отправить ответ пользователю
8. Используйте кнопку «Закрыть тикет» для завершения

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

            text = f"""<b>📋 Информация о пользователе</b>

<b>ID:</b> <code>{tg_id}</code>
<b>Username:</b> @{username}
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

            # Пропускаем trial и free — нечего компенсировать
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

    bot.send_message(message.chat.id, "Активные тикеты:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.from_user.id not in ADMIN_IDS,
                     content_types=['text', 'photo', 'document', 'audio', 'video', 'voice', 'sticker'])
def handle_user_message(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"

    user_data_cache[user_id] = username

    msg_info = {
        "username": username,
        "message_id": message.message_id,
        "chat_id": message.chat.id,
        "content_type": message.content_type,
        "content": message
    }
    user_tickets[user_id].append(msg_info)

    bot.reply_to(message, "✅ Ваше сообщение получено и будет обработано в ближайшее время.")

    if user_id in active_tickets:
        for admin_id in ADMIN_IDS:
            try:
                bot.forward_message(admin_id, msg_info['chat_id'], msg_info['message_id'])
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    text="Ответить",
                    callback_data=f"view_ticket_{user_id}"
                ))
                bot.send_message(
                    admin_id,
                    f"📩 Новое сообщение в тикете от @{username} (ID: <code>{user_id}</code>)",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error forwarding message to admin {admin_id}: {e}")
    else:
        active_tickets.add(user_id)
        logger.info(f"New ticket from @{username} (ID: {user_id})")
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"📩 Новый тикет от @{username} (ID: <code>{user_id}</code>)",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(
                            text="Ответить",
                            callback_data=f"view_ticket_{user_id}"
                        )
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error notifying admin {admin_id}: {e}")


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data.startswith('view_ticket_'):
        user_id = int(call.data.split('_')[-1])
        show_user_messages(call.message.chat.id, user_id)
    elif call.data.startswith('close_ticket_'):
        user_id = int(call.data.split('_')[-1])
        close_ticket(call.message.chat.id, user_id)


def show_user_messages(admin_chat_id, user_id):
    if user_id not in user_tickets:
        bot.send_message(admin_chat_id, "Тикет не найден.")
        return

    username = user_data_cache.get(user_id, f"id{user_id}")

    for msg_info in user_tickets[user_id]:
        try:
            bot.forward_message(admin_chat_id, msg_info['chat_id'], msg_info['message_id'])
        except Exception as e:
            logger.error(f"Error forwarding message: {e}")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        text="Закрыть тикет",
        callback_data=f"close_ticket_{user_id}"
    ))

    bot.send_message(
        admin_chat_id,
        f"Вы просматриваете тикет @{username} (ID: <code>{user_id}</code>). Ответьте на это сообщение, чтобы отправить ответ пользователю.",
        reply_markup=markup,
        parse_mode='HTML'
    )


def close_ticket(admin_chat_id, user_id):
    if user_id in active_tickets:
        active_tickets.remove(user_id)
        if user_id in user_tickets:
            del user_tickets[user_id]
        logger.info(f"Ticket closed for user {user_id}")
        bot.send_message(admin_chat_id, "Тикет закрыт.")
    else:
        bot.send_message(admin_chat_id, "Тикет уже закрыт или не существует.")


@bot.message_handler(func=lambda message: message.reply_to_message is not None and
                                          message.from_user.id in ADMIN_IDS,
                     content_types=['text', 'photo', 'document', 'audio', 'video', 'voice', 'sticker'])
def handle_admin_reply(message):
    reply_text = message.reply_to_message.text
    if not reply_text or "Вы просматриваете тикет @" not in reply_text:
        return

    try:
        user_id = int(reply_text.split("(ID: ")[1].split(")")[0])
    except (IndexError, ValueError):
        bot.reply_to(message, "Не удалось определить ID пользователя.")
        return

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
            bot.send_voice(user_id, message.voice.file_id, caption=f"✉️ Ответ поддержки")
        elif message.content_type == 'sticker':
            bot.send_sticker(user_id, message.sticker.file_id)
            bot.send_message(user_id, "✉️ Ответ поддержки (стикер)")

        logger.info(f"Admin {message.from_user.id} replied to user {user_id}")
        bot.reply_to(message, f"Ответ отправлен пользователю @{username}.")
    except Exception as e:
        logger.error(f"Error sending reply to user {user_id}: {e}")
        bot.reply_to(message, f"Ошибка при отправке ответа: {e}")


# Запускаем бота
if __name__ == '__main__':
    logger.info("Tech support bot starting...")
    bot.infinity_polling()
