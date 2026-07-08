import argparse
import asyncio
import logging
import sys
import time

from bot.conditions_backfill import run_initial_conditions_backfill
from bot.exceptions import AuthError
from bot.messages import format_approval_conditions
from bot.models import Application
from bot.notifier import create_notifier
from bot.storage import ApplicationStorage
from bot.zenit_client import ZenitAuthError, ZenitClient, ZenitRequestNotFound
from bot.zenit_conditions import average_loan_amount, average_percentage
from bot.zenit_statuses import is_approved_state, status_category
from config import (
    CHECK_INTERVAL_SECONDS,
    DB_FILE,
    LOGIN_PASSWORD,
    LOGIN_USERNAME,
    NOTIFY_LOG_FILE,
    NOTIFY_MODE,
    SESSION_FILE,
    SITE_URL,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_PROXY,
    TOTP_SECRET,
    load_site_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("autocredit-bot")


def validate_config() -> None:
    missing = []
    if not SITE_URL:
        missing.append("SITE_URL")
    if not LOGIN_USERNAME:
        missing.append("LOGIN_USERNAME")
    if not LOGIN_PASSWORD:
        missing.append("LOGIN_PASSWORD")
    if "zenit.balance-pl.ru" in SITE_URL and not TOTP_SECRET:
        missing.append("TOTP_SECRET")
    if NOTIFY_MODE == "telegram":
        if not TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
    if NOTIFY_MODE not in {"telegram", "console", "file"}:
        raise RuntimeError("NOTIFY_MODE должен быть: telegram, console или file")
    if missing:
        raise RuntimeError(f"Заполните переменные в .env: {', '.join(missing)}")


def _handle_partner_conditions(app, storage, notifier, zenit_client, *, notify: bool, log_event: bool = True) -> bool:
    # Если раньше уже выясняли, что у этой заявки нет доступных условий (архивная/устаревшая) —
    # не дёргаем API заново на каждом цикле и не спамим лог.
    if storage.get_notified_conditions(app.id) == "unavailable":
        return False

    try:
        conditions = zenit_client.fetch_partner_plus_conditions(app.id)
    except ZenitRequestNotFound:
        # Не помечаем "unavailable" сразу с первой попытки — сразу после одобрения
        # банк иногда не успевает прогрузить детали заявки и отдаёт 404.
        # Даём несколько попыток (по числу циклов проверки), прежде чем считать
        # заявку архивной/устаревшей окончательно.
        attempts_key = f"unavailable_attempts:{app.id}"
        attempts = int(storage.get_state(attempts_key, "0")) + 1
        if attempts >= 5:
            storage.set_notified_conditions(app.id, "unavailable")
            storage.set_state(attempts_key, "0")
            logger.info("Заявка %s недоступна для получения условий (архивная/устаревшая) — больше не проверяю её", app.id)
        else:
            storage.set_state(attempts_key, str(attempts))
            logger.info("Заявка %s: детали пока недоступны (попытка %s/5), проверю снова позже", app.id, attempts)
        return False
    except Exception as exc:
        logger.warning("Не удалось загрузить условия для заявки %s: %s", app.id, exc)
        return False

    if not conditions:
        return False

    fingerprint = zenit_client.partner_plus_fingerprint(conditions)
    if storage.get_notified_conditions(app.id) == fingerprint:
        return False

    # Ставку по одобренной заявке всегда фиксируем в журнале событий для статистики
    # отчётов (даже во время --init)
    storage.log_event(
        app.id, app.dealership, app.client_name, "approval_conditions", app.status,
        percentage=average_percentage(conditions),
        loan_amount=average_loan_amount(conditions),
    )

    if notify:
        notifier.notify_approval_conditions(app, conditions)
        logger.info("Отправлены условия «Партнерский+» для заявки %s", app.id)

    # В привязанную группу салона отправляем независимо от того, отключены ли
    # уведомления администратору (это разные адресаты и разные настройки) —
    # но не во время --init, чтобы не спамить группу старыми заявками при первом запуске.
    if log_event and hasattr(notifier, "send_to"):
        dealer_chat_id = storage.get_dealership_chat_id_by_name(app.dealership)
        if dealer_chat_id:
            try:
                notifier.send_to(dealer_chat_id, format_approval_conditions(app, conditions))
                logger.info("Условия отправлены в группу салона «%s» (заявка %s)", app.dealership, app.id)
            except Exception:
                logger.exception("Не удалось отправить условия в группу салона «%s»", app.dealership)

    storage.set_notified_conditions(app.id, fingerprint)
    return notify


def process_updates(apps, storage, notifier, *, notify: bool = True, zenit_client=None) -> int:
    sent = 0
    dealer_status = storage.sync_dealerships({app.dealership for app in apps})

    for app in apps:
        dealer_enabled = dealer_status.get(app.dealership, True)
        notify_this = notify and dealer_enabled

        known_status = storage.get_known_status(app.id)
        if known_status is None:
            if notify:
                storage.log_event(app.id, app.dealership, app.client_name, "new_application", app.status)
            if notify_this:
                notifier.notify_new_application(app)
                sent += 1
            elif notify and not dealer_enabled:
                logger.info("Уведомление о новой заявке %s подавлено (салон %s отключён)", app.id, app.dealership)
            logger.info("Новая заявка: %s (%s)", app.client_name, app.id)
        elif known_status != app.status:
            if notify:
                storage.log_event(app.id, app.dealership, app.client_name, "status_change", app.status)
            if notify_this:
                notifier.notify_status_change(app, known_status)
                sent += 1
            elif notify and not dealer_enabled:
                logger.info("Уведомление о смене статуса %s подавлено (салон %s отключён)", app.id, app.dealership)
            logger.info("Статус изменён: %s -> %s (%s)", known_status, app.status, app.id)

        storage.upsert(app, app.status)

        if zenit_client and is_approved_state(app.state):
            if _handle_partner_conditions(
                app, storage, notifier, zenit_client, notify=notify_this, log_event=notify
            ):
                sent += 1
    return sent


def uses_zenit_api() -> bool:
    return "zenit.balance-pl.ru" in SITE_URL


def fetch_via_zenit_api(force_login: bool = False) -> list[Application]:
    global _zenit_client
    email = ZenitClient.normalize_email(LOGIN_USERNAME)
    if force_login or _zenit_client is None:
        _zenit_client = ZenitClient(email, LOGIN_PASSWORD, TOTP_SECRET)
        _zenit_client.login()
    try:
        return _zenit_client.fetch_applications()
    except ZenitAuthError:
        _zenit_client = ZenitClient(email, LOGIN_PASSWORD, TOTP_SECRET)
        _zenit_client.login()
        return _zenit_client.fetch_applications()


_zenit_client: ZenitClient | None = None


async def fetch_via_browser(force_login: bool = False) -> list[Application]:
    from playwright.async_api import async_playwright

    from bot.auth import create_browser_context
    from bot.scraper import ApplicationScraper

    site_config = load_site_config()
    async with async_playwright() as playwright:
        browser, context, page = await create_browser_context(
            playwright,
            site_config,
            force_login=force_login,
        )
        try:
            scraper = ApplicationScraper(site_config)
            return await scraper.fetch_applications(page)
        finally:
            await context.storage_state(path=str(SESSION_FILE))
            await browser.close()


async def run_once(force_login: bool = False) -> list[Application]:
    if uses_zenit_api():
        return fetch_via_zenit_api(force_login=force_login)
    apps = await fetch_via_browser(force_login=force_login)
    logger.info("Получено заявок: %s", len(apps))
    return apps

def _start_telegram_commands(storage: ApplicationStorage):
    """Запускает фоновый обработчик команд/кнопок Telegram (фильтр по автосалонам)."""
    if NOTIFY_MODE != "telegram":
        return None
    from bot.telegram_commands import TelegramCommandHandler

    handler = TelegramCommandHandler(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        storage,
        TELEGRAM_PROXY,
    )
    handler.start()
    return handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Мониторинг заявок на автокредит")
    parser.add_argument("--once", action="store_true", help="Один проход без цикла")
    parser.add_argument("--init", action="store_true", help="Первый запуск: сохранить заявки без уведомлений")
    parser.add_argument("--force-login", action="store_true", help="Принудительный повторный вход")
    args = parser.parse_args()

    validate_config()
    storage = ApplicationStorage(DB_FILE)

    try:
        run_initial_conditions_backfill(storage)
    except Exception:

        logger.exception("Ошибка при первичном заполнении статистики (не критично, продолжаю запуск)")

    notifier = create_notifier(
        NOTIFY_MODE,
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        TELEGRAM_PROXY,
        NOTIFY_LOG_FILE,
    )

    mode_label = {"console": "консоль", "file": "файл", "telegram": "Telegram"}[NOTIFY_MODE]
    logger.info("Режим уведомлений: %s", mode_label)

    async def loop_body(force_login: bool = False) -> None:
        apps = await run_once(force_login=force_login)
        if uses_zenit_api():
            logger.info("Получено заявок: %s", len(apps))
        sent = process_updates(
            apps,
            storage,
            notifier,
            notify=not args.init,
            zenit_client=_zenit_client if uses_zenit_api() else None,
        )
        if sent:
            logger.info("Отправлено уведомлений: %s", sent)

    if args.once or args.init:
        try:
            asyncio.run(loop_body(force_login=args.force_login))
            if args.init:
                logger.info("Инициализация завершена. Запустите мониторинг: python main.py")
        except (AuthError, ZenitAuthError) as exc:
            logger.error("Ошибка авторизации: %s", exc)
            sys.exit(1)
        return

    _start_telegram_commands(storage)

    try:
        notifier.send("✅ Бот мониторинга заявок запущен")
    except Exception:
        logger.exception("Не удалось отправить стартовое уведомление (не критично, продолжаю работу)")
    while True:
        try:
            asyncio.run(loop_body())
        except (AuthError, ZenitAuthError) as exc:
            logger.warning("Сессия истекла, повторный вход: %s", exc)
            try:
                asyncio.run(loop_body(force_login=True))
            except Exception as retry_exc:
                logger.error("Повторный вход не удался: %s", retry_exc)
                try:
                    notifier.send(f"⚠️ Ошибка авторизации: {retry_exc}")
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("Ошибка цикла мониторинга")
            try:
                notifier.send(f"⚠️ Ошибка мониторинга: {exc}")
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
