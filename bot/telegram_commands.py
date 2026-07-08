"""Приём команд из Telegram (long polling) для управления автосалонами.

Работает в отдельном потоке параллельно с основным циклом мониторинга заявок,
не требует webhook — просто периодически опрашивает getUpdates.

Возможности:
- /salons — список салонов -> карточка салона (контакты, рассылка, привязанная
  группа, статистика).
- /link <код> — привязка группы Telegram к салону (отправляется внутри самой
  группы автосалона, после того как бота туда добавили).
- /report — PDF-отчёт за период.
"""

import logging
import tempfile
import threading
import time
from pathlib import Path

import requests

from bot.storage import ApplicationStorage
from bot.zenit_statuses import status_category

logger = logging.getLogger(__name__)

COMMANDS_SALONS = {"/salons", "/салоны", "/dealers", "/автосалоны"}
COMMANDS_HELP = {"/start", "/help", "/помощь"}
COMMANDS_REPORT = {"/report", "/отчет", "/отчёт"}
COMMANDS_RECHECK = {"/recheck", "/пересчитать", "/пересчет", "/пересчёт"}
COMMAND_LINK = "/link"
COMMAND_CANCEL = "/cancel"
ISSUED_STATUS_TEXT = "кредит выдан"


class TelegramCommandHandler:
    """Слушает команды и нажатия кнопок в чате, управляет салонами и отчётами."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        storage: ApplicationStorage,
        proxy: str | None = None,
        poll_timeout: int = 25,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.storage = storage
        self.poll_timeout = poll_timeout
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

        self.session = requests.Session()
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {"http": None, "https": None}

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-commands")
        self._thread.start()
        logger.info("Обработчик команд Telegram запущен")

    def stop(self) -> None:
        self._stop_event.set()

    # --- цикл опроса ---

    def _poll_loop(self) -> None:
        offset = self.storage.get_update_offset()
        logger.info(
            "Опрос Telegram getUpdates запущен (chat_id=%s, offset=%s)", self.chat_id, offset
        )
        while not self._stop_event.is_set():
            try:
                updates = self._get_updates(offset)
            except Exception:
                logger.exception("Ошибка получения обновлений Telegram, повтор через 5 сек")
                time.sleep(5)
                continue

            if updates:
                logger.info("Получено обновлений Telegram: %s", len(updates))

            for update in updates:
                offset = update["update_id"] + 1
                self.storage.set_update_offset(offset)
                try:
                    self._handle_update(update)
                except Exception:
                    logger.exception("Ошибка обработки обновления Telegram: %s", update)

    def _get_updates(self, offset: int) -> list[dict]:
        response = self.session.get(
            f"{self.api_url}/getUpdates",
            params={
                "offset": offset,
                "timeout": self.poll_timeout,
                "allowed_updates": '["message","callback_query"]',
            },
            timeout=self.poll_timeout + 10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            logger.warning("Telegram getUpdates вернул ошибку: %s", payload)
            return []
        return payload.get("result", [])

    # --- обработка входящих событий ---

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_title = chat.get("title") or chat.get("username") or chat_id
        raw_text = message.get("text") or ""
        logger.info("Получено сообщение Telegram: chat_id=%s text=%r", chat_id, raw_text)

        stripped = raw_text.strip()
        command = stripped.split()[0].split("@")[0].lower() if stripped else ""

        # /link работает из ЛЮБОГО чата
        if command == COMMAND_LINK:
            parts = stripped.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            try:
                self._handle_link_command(chat_id, chat_title, code)
            except Exception:
                logger.exception("Ошибка при обработке /link")
            return

        if chat_id != self.chat_id:
            logger.info(
                "Игнорирую сообщение из чужого чата: %s (ожидался %s)", chat_id, self.chat_id
            )
            return

        # Если ждём текст с контактами салона — перехватываем ввод раньше команд.
        pending_dealer_id = self.storage.peek_awaiting_contact(chat_id)
        if pending_dealer_id is not None:
            self.storage.clear_awaiting_contact(chat_id)
            if command == COMMAND_CANCEL:
                self._send_message(chat_id, "Отменено.")
                return
            if not stripped.startswith("/"):
                self._save_contact_text(chat_id, pending_dealer_id, raw_text)
                return
            # Если вместо контактов прислали команду — просто выполняем её как обычно.

        try:
            if command in COMMANDS_SALONS:
                self._send_salons_menu(chat_id)
            elif command in COMMANDS_HELP:
                self._send_help(chat_id)
            elif command in COMMANDS_REPORT:
                self._send_report_menu(chat_id)
            elif command in COMMANDS_RECHECK:
                self._handle_recheck_command(chat_id)
            else:
                logger.info("Команда не распознана: %r", command)
        except Exception:
            logger.exception("Ошибка при обработке команды %r", command)

    def _handle_callback(self, callback: dict) -> None:
        callback_id = callback.get("id", "")
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        message_id = callback.get("message", {}).get("message_id")
        data = callback.get("data", "")

        if chat_id != self.chat_id:
            self._answer_callback(callback_id)
            return

        try:
            self._route_callback(chat_id, message_id, callback_id, data)
        except Exception:
            logger.exception("Ошибка при обработке callback %r", data)
            self._answer_callback(callback_id)

    def _route_callback(self, chat_id: str, message_id: int | None, callback_id: str, data: str) -> None:
        if data.startswith("report:"):
            period = data[len("report:"):]
            #
            #
            self._answer_callback(callback_id, text="Формирую отчёт…")
            self._generate_and_send_report(chat_id, period)
            return

        if data == "salons_list":
            self._answer_callback(callback_id)
            if message_id is not None:
                text, keyboard = self._dealer_list_view()
                self._edit_message_text(chat_id, message_id, text, keyboard)
            return

        if data.startswith("card:"):
            dealership_id = int(data.split(":", 1)[1])
            self._answer_callback(callback_id)
            if message_id is not None:
                text, keyboard = self._dealer_card_view(dealership_id)
                self._edit_message_text(chat_id, message_id, text, keyboard)
            return

        if data.startswith("cardtoggle:"):
            dealership_id = int(data.split(":", 1)[1])
            current = self.storage.get_dealership_by_id(dealership_id)
            notice = None
            if current:
                _, enabled = current
                self.storage.set_dealership_enabled_by_id(dealership_id, not enabled)
                notice = "🚫 Рассылка выключена" if enabled else "✅ Рассылка включена"
            self._answer_callback(callback_id, text=notice)
            if message_id is not None:
                text, keyboard = self._dealer_card_view(dealership_id)
                self._edit_message_text(chat_id, message_id, text, keyboard)
            return

        if data.startswith("cardlink:"):
            dealership_id = int(data.split(":", 1)[1])
            self._answer_callback(callback_id)
            info = self.storage.get_dealership_by_id(dealership_id)
            if info:
                name, _ = info
                code = self.storage.create_link_code(dealership_id)
                self._send_message(
                    chat_id,
                    f"Чтобы привязать группу к салону «{name}»:\n\n"
                    f"1) добавьте этого бота участником в группу автосалона в Telegram\n"
                    f"2) отправьте в ЭТУ группу (не сюда) команду:\n/link {code}\n\n"
                    f"Код одноразовый, действует 30 минут.",
                )
            return

        if data.startswith("cardunlink:"):
            dealership_id = int(data.split(":", 1)[1])
            self.storage.set_dealership_chat_id(dealership_id, None)
            self._answer_callback(callback_id, text="Группа отвязана")
            if message_id is not None:
                text, keyboard = self._dealer_card_view(dealership_id)
                self._edit_message_text(chat_id, message_id, text, keyboard)
            return

        if data.startswith("cardcontacts:"):
            dealership_id = int(data.split(":", 1)[1])
            info = self.storage.get_dealership_by_id(dealership_id)
            self._answer_callback(callback_id)
            if info:
                name, _ = info
                self.storage.set_awaiting_contact(chat_id, dealership_id)
                self._send_message(
                    chat_id,
                    f"Отправьте следующим сообщением контакты для салона «{name}» "
                    f"(имя менеджера, телефон и т.п.) или /cancel для отмены.",
                )
            return

        notice = None
        if data == "enable_all":
            self.storage.set_all_dealerships_enabled(True)
            notice = "Все автосалоны включены"
        elif data == "disable_all":
            self.storage.set_all_dealerships_enabled(False)
            notice = "Все автосалоны выключены"

        self._answer_callback(callback_id, text=notice)
        if notice and message_id is not None:
            text, keyboard = self._dealer_list_view()
            self._edit_message_text(chat_id, message_id, text, keyboard)

    # --- /link: привязка группы к салону ---

    def _handle_link_command(self, chat_id: str, chat_title: str, code: str) -> None:
        if not code:
            self._send_message(chat_id, "Использование: /link <код>, который выдал администратор.")
            return

        dealership_id = self.storage.resolve_link_code(code)
        if dealership_id is None:
            self._send_message(chat_id, "Код неверен или истёк (действует 30 минут). Запросите новый код у администратора.")
            return

        info = self.storage.get_dealership_by_id(dealership_id)
        if info is None:
            self._send_message(chat_id, "Салон не найден (возможно, был удалён).")
            return

        name, _enabled = info
        self.storage.set_dealership_chat_id(dealership_id, chat_id)
        self._send_message(
            chat_id,
            f"✅ Эта группа привязана к автосалону «{name}».\n"
            f"Сюда будут приходить условия по одобренным клиентам этого салона.",
        )
        if self.chat_id and chat_id != self.chat_id:
            self._send_message(
                self.chat_id,
                f"🔗 Группа «{chat_title}» привязана к автосалону «{name}».",
            )

    # --- контакты ---

    def _save_contact_text(self, chat_id: str, dealership_id: int, text: str) -> None:
        info = self.storage.get_dealership_by_id(dealership_id)
        self.storage.set_dealership_contacts(dealership_id, text.strip())
        name = info[0] if info else str(dealership_id)
        self._send_message(chat_id, f"Контакты для «{name}» сохранены.")
        text_view, keyboard = self._dealer_card_view(dealership_id)
        self._send_message(chat_id, text_view, reply_markup=keyboard)

    # --- отправка сообщений/справка ---

    def _handle_recheck_command(self, chat_id: str) -> None:
        from bot.conditions_backfill import force_rerun_conditions_backfill

        self._send_message(
            chat_id,
            "Запускаю пересчёт условий «Партнерский+» по последним заявкам в фоне — "
            "это может занять несколько минут. Когда закончится, в логах будет "
            "сообщение «[backfill] Готово». После этого сформируйте /report заново.",
        )
        force_rerun_conditions_backfill(self.storage)

    def _send_help(self, chat_id: str) -> None:
        text = (
            "Команды:\n"
            "/salons — список автосалонов; открывает карточку салона "
            "(контакты, рассылка, привязка группы, статистика)\n"
            "/report — сформировать PDF-отчёт по заявкам за день/неделю/месяц\n"
            "/recheck — принудительно пересчитать ставки и суммы кредита по "
            "последним одобренным заявкам (на случай, если данных в отчёте не хватает)\n\n"
            "Чтобы привязать группу салона: откройте карточку салона в /salons → "
            "«Привязать группу» → бот выдаст код → добавьте бота в группу салона "
            "и отправьте туда /link <код>."
        )
        self._send_message(chat_id, text)

    def _send_report_menu(self, chat_id: str) -> None:
        keyboard = {
            "inline_keyboard": [[
                {"text": "📅 День", "callback_data": "report:day"},
                {"text": "🗓 Неделя", "callback_data": "report:week"},
                {"text": "📆 Месяц", "callback_data": "report:month"},
            ]]
        }
        self._send_message(chat_id, "За какой период сформировать отчёт?", reply_markup=keyboard)

    def _generate_and_send_report(self, chat_id: str, period: str) -> None:
        from bot.report import PERIOD_LABELS, generate_report

        if period not in PERIOD_LABELS:
            self._send_message(chat_id, "Неизвестный период отчёта.")
            return

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                output_path = Path(tmp_dir) / f"report_{period}.pdf"
                generate_report(self.storage, period, output_path)
                self._send_document(chat_id, output_path, caption=f"Отчёт за {PERIOD_LABELS[period]}")
        except Exception:
            logger.exception("Не удалось сформировать отчёт (период=%s)", period)
            self._send_message(chat_id, "Не удалось сформировать отчёт. Подробности — в логах бота.")

    def _send_salons_menu(self, chat_id: str) -> None:
        text, keyboard = self._dealer_list_view()
        self._send_message(chat_id, text, reply_markup=keyboard)

    # --- список и карточка салона ---

    def _dealer_list_view(self) -> tuple[str, dict | None]:
        dealerships = self.storage.list_dealerships()
        if not dealerships:
            return "Автосалоны пока не известны боту — дождитесь очередной проверки заявок.", None

        rows = []
        for dealership_id, name, enabled in dealerships:
            mark = "✅" if enabled else "🚫"
            rows.append([{"text": f"{mark} {name}", "callback_data": f"card:{dealership_id}"}])
        rows.append([
            {"text": "✅ Включить все", "callback_data": "enable_all"},
            {"text": "🚫 Выключить все", "callback_data": "disable_all"},
        ])
        text = "Автосалоны — выберите, чтобы открыть карточку:"
        return text, {"inline_keyboard": rows}

    def _dealer_card_view(self, dealership_id: int) -> tuple[str, dict]:
        info = self.storage.get_dealership_by_id(dealership_id)
        back_keyboard = {"inline_keyboard": [[{"text": "⬅ К списку салонов", "callback_data": "salons_list"}]]}
        if info is None:
            return "Салон не найден (возможно, был удалён).", back_keyboard

        name, enabled = info
        contacts = self.storage.get_dealership_contacts(dealership_id)
        chat_linked = bool(self.storage.get_dealership_chat_id(dealership_id))
        total, issued, declines, avg_rate, avg_amount = self._dealer_stats(dealership_id)

        lines = [
            f"🏢 {name}",
            "",
            f"Рассылка уведомлений: {'✅ включена' if enabled else '🚫 выключена'}",
            f"Группа в Telegram: {'🔗 привязана' if chat_linked else '— не привязана'}",
            f"Контакты: {contacts if contacts else 'не указаны'}",
            "",
            "Статистика (за всё время):",
            f"  Заявок: {total}",
            f"  Кредит выдан: {issued}",
            f"  Отказов: {declines}",
        ]
        if avg_rate is not None:
            lines.append(f"  Средняя ставка по одобренным: {avg_rate:.2f}%")
        if avg_amount is not None:
            from bot.zenit_conditions import format_money

            lines.append(f"  Средняя сумма кредита: {format_money(avg_amount)} руб.")

        keyboard = {
            "inline_keyboard": [
                [{
                    "text": "🚫 Выключить рассылку" if enabled else "✅ Включить рассылку",
                    "callback_data": f"cardtoggle:{dealership_id}",
                }],
                [{
                    "text": "❌ Отвязать группу" if chat_linked else "🔗 Привязать группу",
                    "callback_data": f"cardunlink:{dealership_id}" if chat_linked else f"cardlink:{dealership_id}",
                }],
                [{"text": "📇 Изменить контакты", "callback_data": f"cardcontacts:{dealership_id}"}],
                [{"text": "⬅ К списку салонов", "callback_data": "salons_list"}],
            ]
        }
        return "\n".join(lines), keyboard

    def _dealer_stats(self, dealership_id: int) -> tuple[int, int, int, float | None, float | None]:
        events = self.storage.get_events_for_dealer(dealership_id)
        total = sum(1 for e in events if e["event_type"] == "new_application")
        issued = sum(
            1 for e in events
            if e["status"] and ISSUED_STATUS_TEXT in e["status"].strip().lower()
        )
        declines = sum(1 for e in events if status_category(None, e["status"]) == "decline")
        rates = [
            e["percentage"] for e in events
            if e["event_type"] == "approval_conditions" and e["percentage"] is not None
        ]
        avg_rate = sum(rates) / len(rates) if rates else None
        amounts = [
            e["loan_amount"] for e in events
            if e["event_type"] == "approval_conditions" and e["loan_amount"] is not None
        ]
        avg_amount = sum(amounts) / len(amounts) if amounts else None
        return total, issued, declines, avg_rate, avg_amount

    # --- низкоуровневые вызовы Telegram API ---

    def _send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.session.post(f"{self.api_url}/sendMessage", json=payload, timeout=30)
            logger.info("sendMessage -> HTTP %s: %s", resp.status_code, resp.text[:500])
        except Exception:
            logger.exception("Не удалось отправить сообщение Telegram")

    def _edit_message_text(self, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = self.session.post(f"{self.api_url}/editMessageText", json=payload, timeout=30)
            if resp.status_code != 200:
                logger.warning("editMessageText -> HTTP %s: %s", resp.status_code, resp.text[:300])
        except Exception:
            logger.exception("Не удалось обновить текст сообщения Telegram")

    def _send_document(self, chat_id: str, file_path: Path, caption: str = "") -> None:
        try:
            with open(file_path, "rb") as fh:
                files = {"document": (file_path.name, fh, "application/pdf")}
                data = {"chat_id": chat_id, "caption": caption}
                resp = self.session.post(f"{self.api_url}/sendDocument", data=data, files=files, timeout=120)
            logger.info("sendDocument -> HTTP %s: %s", resp.status_code, resp.text[:300])
        except Exception:
            logger.exception("Не удалось отправить документ Telegram")

    def _answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            self.session.post(f"{self.api_url}/answerCallbackQuery", json=payload, timeout=30)
        except Exception:
            logger.exception("Не удалось ответить на callback Telegram")
