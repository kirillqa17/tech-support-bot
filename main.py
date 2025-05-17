import os
import telebot
from telebot import types
from dotenv import load_dotenv
from collections import defaultdict
import requests  # Добавляем для работы с API

# Загружаем переменные из .env файла
load_dotenv()

# Получаем токен бота и список админов из .env
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS').split(',')))
API_URL = os.getenv('API_URL')  # URL вашего API для продления подписки

# Инициализируем бота
bot = telebot.TeleBot(BOT_TOKEN)

# База данных для хранения сообщений пользователей
user_tickets = defaultdict(list)  # {user_id: [{"username": str, "message_id": int, "chat_id": int}]}
active_tickets = set()  # Множество активных тикетов (user_id)
user_data_cache = {}  # Кэш для хранения username пользователей {user_id: username}
pending_messages = defaultdict(list)  # {user_id: [message1, message2, ...]}


# Обработчик команды /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.id in ADMIN_IDS:
        bot.send_message(message.chat.id,
                         "Вы админ. Используйте /reply для ответа на сообщения или /extend для продления подписки.")
    else:
        bot.reply_to(message,
                     "Привет! Это бот техподдержки SvoiVPN. Напишите ваш вопрос, и мы обязательно вам ответим в скором времени!")


# Обработчик команды /extend для админов
@bot.message_handler(commands=['extend'], func=lambda message: message.from_user.id in ADMIN_IDS)
def handle_extend_command(message):
    try:
        # Разбиваем сообщение на части: /extend TG_ID
        parts = message.text.split()
        if len(parts) <= 2:
            bot.reply_to(message, "Использование: /extend TG_ID PLAN DAYS")
            return

        tg_id = int(parts[1])
        plan = parts[2]
        days = int(parts[3])
        # Отправляем запрос на API для продления подписки
        response = requests.post(f"{API_URL}/{tg_id}/extend", json={"days" : days, "plan" : plan})

        if response.status_code == 200:
            bot.reply_to(message, f"Подписка для пользователя с ID {tg_id} успешно продлена.")
        else:
            bot.reply_to(message, f"Ошибка при продлении подписки: {response.text}")
    except ValueError:
        bot.reply_to(message, "Неверный формат ID. ID должен быть числом.")
    except Exception as e:
        bot.reply_to(message, f"Произошла ошибка: {str(e)}")


# Обработчик всех сообщений от пользователей
@bot.message_handler(func=lambda message: message.from_user.id not in ADMIN_IDS,
                     content_types=['text', 'photo', 'document', 'audio', 'video', 'voice', 'sticker'])
def handle_user_message(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"

    # Сохраняем username в кэш
    user_data_cache[user_id] = username

    # Сохраняем информацию о сообщении
    user_tickets[user_id].append({
        "username": username,
        "message_id": message.message_id,
        "chat_id": message.chat.id,
        "content_type": message.content_type,
        "content": message  # Сохраняем все сообщение для последующей пересылки
    })

    # Добавляем в активные тикеты, если это первое сообщение
    if user_id not in active_tickets:
        active_tickets.add(user_id)

        # Уведомляем админов о новом тикете
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"📩 Новый тикет от @{username} (ID: {user_id})",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton(
                            text="Ответить",
                            callback_data=f"view_ticket_{user_id}"
                        )
                    )
                )
            except Exception as e:
                print(f"Ошибка при уведомлении админа {admin_id}: {e}")


# Обработчик команды /reply для админов
@bot.message_handler(commands=['reply'], func=lambda message: message.from_user.id in ADMIN_IDS)
def show_active_tickets(message):
    if not active_tickets:
        bot.reply_to(message, "Нет активных тикетов.")
        return

    markup = types.InlineKeyboardMarkup()
    for user_id in active_tickets:
        username = user_data_cache.get(user_id, f"id{user_id}")
        markup.add(types.InlineKeyboardButton(
            text=f"@{username} (ID: {user_id})",
            callback_data=f"view_ticket_{user_id}"
        ))

    bot.send_message(message.chat.id, "Активные тикеты:", reply_markup=markup)


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

    # Пересылаем все сообщения пользователя
    for msg_info in user_tickets[user_id]:
        try:
            # Пересылаем оригинальное сообщение
            bot.forward_message(admin_chat_id, msg_info['chat_id'], msg_info['message_id'])
        except Exception as e:
            print(f"Ошибка при пересылке сообщения: {e}")

    # Кнопка для закрытия тикета
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        text="Закрыть тикет",
        callback_data=f"close_ticket_{user_id}"
    ))

    bot.send_message(
        admin_chat_id,
        f"Вы просматриваете тикет @{username} (ID: {user_id}). Ответьте на это сообщение, чтобы отправить ответ пользователю.",
        reply_markup=markup
    )


def close_ticket(admin_chat_id, user_id):
    if user_id in active_tickets:
        active_tickets.remove(user_id)
        # Очищаем историю сообщений при закрытии тикета
        if user_id in user_tickets:
            del user_tickets[user_id]
        bot.send_message(admin_chat_id, "Тикет закрыт.")
    else:
        bot.send_message(admin_chat_id, "Тикет уже закрыт или не существует.")


# Обработчик ответов админов
@bot.message_handler(func=lambda message: message.reply_to_message is not None and
                                          message.from_user.id in ADMIN_IDS,
                     content_types=['text', 'photo', 'document', 'audio', 'video', 'voice', 'sticker'])
def handle_admin_reply(message):
    reply_text = message.reply_to_message.text
    if not reply_text or "Вы просматриваете тикет @" not in reply_text:
        return

    # Получаем user_id из текста сообщения
    try:
        user_id = int(reply_text.split("(ID: ")[1].split(")")[0])
    except (IndexError, ValueError):
        bot.reply_to(message, "Не удалось определить ID пользователя.")
        return

    # Получаем username из кэша
    username = user_data_cache.get(user_id, f"id{user_id}")

    # Отправляем ответ пользователю
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

        bot.reply_to(message, f"Ответ отправлен пользователю @{username}.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка при отправке ответа: {e}")


# Запускаем бота
if __name__ == '__main__':
    bot.infinity_polling()