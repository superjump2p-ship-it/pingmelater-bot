"""
message.bot.py

Минимальный Telegram-бот на aiogram 3 с обработкой входящих текстовых сообщений.

Файл разбит на логические блоки: импорты, in-memory хранилище,
регистрация обработчиков и старт polling.
"""

# ...existing code...
# ...existing code...
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    CallbackQuery,
)
from aiogram.filters import Command

from config import BOT_TOKEN

"""
In-memory state

Мы храним временные идентификаторы напоминаний (reminder_id) в памяти,
чтобы связать последовательность: пользователь отправляет текст -> создаём запись в БД ->
пользователь выбирает время -> обновляем запись в БД по id.

Заметьте: сами напоминания хранятся в SQLite; этот словарь хранит только последние
незавершённые операции для каждого `chat_id`.
"""

# pending_reminder_ids: хранит список незавершённых reminder_id для каждого чата
pending_reminder_ids: dict[int, list[int]] = {}
# pending_custom_time: если пользователь выбрал "своё время", ждём ввода и
# храним reminder_id здесь: chat_id -> reminder_id
pending_custom_time: dict[int, int] = {}


# ---------------------
# Bot setup and handlers
# ---------------------
async def main() -> None:
    """
    Блок инициализации бота и диспетчера

    Здесь же инициализируем базу данных (файл и таблицу), чтобы убедиться,
    что таблица `reminders` существует до сохранения данных.
    """
    # Инициализация БД
    from db import init_db

    # Настройка логирования для бота (если ещё не настроено)
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(
            level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s"
        )

    init_db()

    # Создаём клиент и диспетчер
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # /start: инструкция пользователю
    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        try:
            await message.answer(
                "Просто отправь мне сообщение, и я напомню о нём позже"
            )
        except Exception:
            logging.exception("cmd_start failed for chat=%s", message.chat.id)
            try:
                await message.answer("Произошла ошибка при обработке /start")
            except Exception:
                pass

    # /later: показать активные напоминания пользователя
    @dp.message(Command("later"))
    async def cmd_later(message: Message) -> None:
        """Показывает все активные (не выполненные) напоминания для пользователя.

        Формат вывода:
        текст
        время отправки
        """
        try:
            from db import get_active_reminders

            chat_id = message.chat.id
            reminders = get_active_reminders(chat_id)
            if not reminders:
                await message.answer("У вас нет активных напоминаний.")
                return

            parts: list[str] = []
            for _id, user_id, text, send_at, done in reminders:
                send_display = send_at if send_at is not None else "(время не выбрано)"
                parts.append(f"{text}\nВремя: {send_display}")

            await message.answer("Ваши активные напоминания:\n\n" + "\n\n".join(parts))
        except Exception:
            logging.exception("cmd_later failed for chat=%s", message.chat.id)
            try:
                await message.answer("Произошла ошибка при получении напоминаний.")
            except Exception:
                pass

    # /inbox: показать все записи из SQLite где done = 0, отсортированные по send_at
    @dp.message(Command("inbox"))
    async def cmd_inbox(message: Message) -> None:
        """Показывает все записи из базы, где `done = 0`.

        Формат каждого элемента:
        текст

        время отправки
        """
        try:
            from db import get_inbox_reminders

            rows = get_inbox_reminders()
            if not rows:
                await message.answer("В базе нет невыполненных напоминаний.")
                return

            parts: list[str] = []
            for _id, user_id, text, send_at, done in rows:
                send_display = send_at if send_at is not None else "(время не выбрано)"
                parts.append(f"{text}\n\n{send_display}")

            await message.answer(
                "Inbox (все невыполненные напоминания):\n\n" + "\n\n".join(parts)
            )
        except Exception:
            logging.exception("cmd_inbox failed for chat=%s", message.chat.id)
            try:
                await message.answer("Произошла ошибка при получении inbox.")
            except Exception:
                pass

    # Inline buttons used after incoming text
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="через 1 час", callback_data="rem:1h")],
            [InlineKeyboardButton(text="сегодня вечером", callback_data="rem:evening")],
            [InlineKeyboardButton(text="завтра", callback_data="rem:tomorrow")],
            [InlineKeyboardButton(text="своё время", callback_data="rem:custom")],
        ]
    )

    # Обработчик callback'ов от inline-кнопок
    @dp.callback_query(lambda c: c.data and c.data.startswith("rem:"))
    async def process_reminder_callback(call: CallbackQuery) -> None:
        """Обрабатывает выбор времени из inline-кнопок.

        Для первых трёх кнопок сохраняем текст и рассчитанное `send_at` в БД.
        Для "своё время" сохраняем запись без `send_at` и просим прислать время.
        """
        from db import save_send_time, get_reminder

        try:
            chat_id = call.message.chat.id
            key = call.data.split(":", 1)[1]
            # Берём последний добавленный reminder_id для чата
            lst = pending_reminder_ids.get(chat_id)
            if not lst:
                await call.answer(
                    "Текст напоминания не найден или уже обработан.", show_alert=True
                )
                return
            reminder_id = lst.pop()
            logging.getLogger("main").info(
                "process_reminder_callback: chat_id=%s reminder_id=%s key=%s",
                chat_id,
                reminder_id,
                key,
            )
            if not lst:
                pending_reminder_ids.pop(chat_id, None)

            from datetime import datetime, time, timedelta

            now = datetime.now()
            if key == "1h":
                send_at = now + timedelta(hours=1)
                label = "через 1 час"
            elif key == "evening":
                target = datetime.combine(now.date(), time(hour=20))
                if target <= now:
                    target = target + timedelta(days=1)
                send_at = target
                label = "сегодня вечером"
            elif key == "tomorrow":
                tomorrow = now.date() + timedelta(days=1)
                send_at = datetime.combine(tomorrow, time(hour=9))
                label = "завтра"
            else:  # custom
                # Переводим запись в режим ожидания пользовательского времени
                pending_custom_time[chat_id] = reminder_id
                await call.message.edit_reply_markup(reply_markup=None)
                await call.message.answer(
                    "Напоминание сохранено. Отправьте время в формате YYYY-MM-DD HH:MM или ISO",
                )
                await call.answer()
                return

            # Сохраняем время отправки для уже созданной записи
            send_at_iso = send_at.isoformat()
            save_send_time(reminder_id, send_at_iso)

            # Получаем сохранённый текст напоминания для вывода пользователю
            row = get_reminder(reminder_id)
            text_saved = row[2] if row is not None else "(текст не найден)"
            logging.getLogger("main").info(
                "Saved send_time for reminder_id=%s send_at=%s",
                reminder_id,
                send_at_iso,
            )

            await call.message.edit_reply_markup(reply_markup=None)
            await call.message.answer(
                f"Напоминание сохранено:\n\nТекст: {text_saved}\nВремя: {label}"
            )
            await call.answer()
        except Exception:
            logging.exception(
                "process_reminder_callback failed for chat=%s",
                getattr(call.message, "chat", None),
            )
            try:
                await call.answer(
                    "Произошла ошибка при выборе времени", show_alert=True
                )
            except Exception:
                pass
        return

    # Обработчик action-кнопок в отправленных напоминаниях
    @dp.callback_query(lambda c: c.data and c.data.startswith("rem_action:"))
    async def process_rem_action(call: CallbackQuery) -> None:
        """Обрабатывает кнопки 'Сделано' и 'Отложить' в отправленных напоминаниях.

        Формат callback_data: rem_action:{reminder_id}:<action>
        Поддерживаемые action: done, snooze_1h, snooze_tomorrow
        """
        from db import mark_done, save_send_time, get_reminder

        try:
            parts = call.data.split(":")
            if len(parts) < 3:
                await call.answer("Неправильные данные", show_alert=True)
                return

            _, rem_id_str, action = parts
            try:
                rem_id = int(rem_id_str)
            except ValueError:
                await call.answer("Неверный id напоминания", show_alert=True)
                return

            from datetime import datetime, timedelta

            now = datetime.now()
            if action == "done":
                mark_done(rem_id)
                await call.message.edit_reply_markup(reply_markup=None)
                await call.message.answer("Напоминание отмечено как выполненное.")
                await call.answer()
                return
            elif action == "snooze_1h":
                new_dt = now + timedelta(hours=1)
                save_send_time(rem_id, new_dt.isoformat())
                await call.message.edit_reply_markup(reply_markup=None)
                await call.message.answer("Напоминание отложено на 1 час.")
                await call.answer()
                return
            elif action == "snooze_tomorrow":
                new_dt = now + timedelta(days=1)
                save_send_time(rem_id, new_dt.isoformat())
                await call.message.edit_reply_markup(reply_markup=None)
                await call.message.answer("Напоминание отложено на завтра.")
                await call.answer()
                return

            await call.answer("Неизвестное действие", show_alert=True)
        except Exception:
            logging.exception("process_rem_action failed: %s", call.data)
            try:
                await call.answer(
                    "Произошла ошибка при обработке действия", show_alert=True
                )
            except Exception:
                pass
        return

    # Обработчик любого текстового сообщения — показываем inline-кнопки выбора времени
    @dp.message()
    async def handle_any_text(message: Message) -> None:
        """
        При получении любого текстового сообщения сохраняем текст в памяти
        и показываем inline-кнопки для выбора времени.
        """
        chat_id = message.chat.id
        # Игнорируем командные сообщения, чтобы обработчики Command(...) сработали
        if message.text and message.text.strip().startswith("/"):
            return
        # Если сейчас ожидаем от пользователя ввод времени для "своё время",
        # не обрабатываем это сообщение как новый текст напоминания.
        if chat_id in pending_custom_time:
            return

        from db import save_message

        # Сохраняем напоминание в БД сразу и добавляем в очередь pending_reminder_ids
        reminder_id = save_message(chat_id, message.text)
        pending_reminder_ids.setdefault(chat_id, []).append(reminder_id)
        await message.answer("Выберите время напоминания:", reply_markup=inline_kb)

    # Обработчик сообщений с пользовательским временем (простая парсинг-логика)
    @dp.message()
    async def handle_custom_time_text(message: Message) -> None:
        """Если у пользователя есть `pending_reminder_ids`, пытаемся распарсить
        присланную строку как время и сохранить `send_at` в БД.
        """
        from db import save_send_time, get_reminder

        chat_id = message.chat.id
        # Не удаляем запись из pending_custom_time до успешного парсинга —
        # иначе при неверном формате мы потеряем состояние ожидания
        reminder_id = pending_custom_time.get(chat_id)
        if reminder_id is None:
            return  # не нашу задачу — пусть другие хэндлеры обрабатывают

        from datetime import datetime

        text = message.text.strip()
        dt = None
        # Простая попытка парсинга: ISO или 'YYYY-MM-DD HH:MM'
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            except Exception:
                await message.answer(
                    "Не понял формат. Отправьте время в формате YYYY-MM-DD HH:MM или ISO."
                )
                # вернуть ожидание: пользователь может попробовать снова
                # Восстанавливаем reminder_id в очереди pending_reminder_ids
                pending_reminder_ids.setdefault(chat_id, []).append(reminder_id)
                return

        send_at_iso = dt.isoformat()
        # на успешном парсинге удаляем флаг ожидания и сохраняем время
        pending_custom_time.pop(chat_id, None)
        save_send_time(reminder_id, send_at_iso)
        row = get_reminder(reminder_id)
        text_saved = row[2] if row is not None else "(текст не найден)"
        await message.answer(
            f"Напоминание сохранено:\n\nТекст: {text_saved}\nВремя: {send_at_iso}"
        )

    # Запуск long polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
