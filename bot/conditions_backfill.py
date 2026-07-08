

import logging
import threading

from bot.storage import ApplicationStorage
from bot.zenit_client import ZenitClient, ZenitRequestNotFound
from bot.zenit_conditions import average_loan_amount, average_percentage
from bot.zenit_statuses import is_approved_state, status_category
from config import LOGIN_PASSWORD, LOGIN_USERNAME, SITE_URL, TOTP_SECRET

logger = logging.getLogger("conditions-backfill")

BACKFILL_STATE_KEY = "conditions_backfill_done_v1"
BACKFILL_APPLICATIONS_LIMIT = 200

# Общий лок — чтобы /recheck из Telegram и обычный старт-бэкафилл не могли
# случайно выполниться одновременно (например, если юзер нажмёт /recheck
# в первые секунды после запуска бота, пока ещё крутится стартовый бэкафилл).
_backfill_lock = threading.Lock()


def _uses_zenit_api() -> bool:
    return "zenit.balance-pl.ru" in SITE_URL


def _fix_missing_percentages(storage: ApplicationStorage, client: ZenitClient) -> tuple[int, int]:
    missing = storage.get_events_missing_percentage()
    if not missing:
        return 0, 0

    logger.info("[backfill] Найдено %s старых событий без ставки — пересчитываю…", len(missing))
    fixed = errors = 0
    for row in missing:
        try:
            conditions = client.fetch_partner_plus_conditions(row["app_id"])
        except Exception:
            errors += 1
            continue
        if not conditions:
            errors += 1
            continue
        pct = average_percentage(conditions)
        if pct is None:
            errors += 1
            continue
        storage.update_event_percentage(row["id"], pct)
        fixed += 1

    logger.info("[backfill] Догоняющий фикс ставок: обновлено %s, не удалось %s", fixed, errors)
    return fixed, errors


def _run_impl(storage: ApplicationStorage, *, mark_done: bool = True) -> tuple[int, int, int]:
    """Возвращает (записано, пропущено, ошибок)."""
    if not _backfill_lock.acquire(blocking=False):
        logger.info("[backfill] Уже выполняется в другом потоке — пропускаю повторный запуск")
        return 0, 0, 0

    try:
        logger.info(
            "[backfill] Старт: логинюсь в банк, чтобы стянуть условия «Партнерский+» "
            "по последним %s заявкам…",
            BACKFILL_APPLICATIONS_LIMIT,
        )
        try:
            email = ZenitClient.normalize_email(LOGIN_USERNAME)
            client = ZenitClient(email, LOGIN_PASSWORD, TOTP_SECRET)
            client.login()
            logger.info("[backfill] Вход выполнен, запрашиваю список заявок…")
            apps = client.fetch_applications(limit=BACKFILL_APPLICATIONS_LIMIT)
            logger.info("[backfill] Получено заявок: %s", len(apps))
        except Exception:
            logger.exception(
                "[backfill] Не удалось выполнить заполнение статистики — "
                "флаг не выставлен, попробую снова при следующем запуске бота"
            )
            return 0, 0, 0  # флаг НЕ выставляем — повторим попытку при следующем старте

        candidates = [
            app for app in apps
            if is_approved_state(app.state)
            or status_category(app.state, app.status) in {"approved", "deal"}
        ]
        logger.info("[backfill] Кандидатов (одобрено/сделка/выдано): %s", len(candidates))

        logged = skipped = errors = 0
        for i, app in enumerate(candidates, start=1):
            try:
                conditions = client.fetch_partner_plus_conditions(app.id)
            except ZenitRequestNotFound:
                skipped += 1
                continue
            except Exception:
                logger.warning("[backfill] Не удалось получить условия для заявки %s", app.id, exc_info=True)
                errors += 1
                continue

            if not conditions:
                skipped += 1
                continue

            fingerprint = client.partner_plus_fingerprint(conditions)
            if storage.get_notified_conditions(app.id) == fingerprint:
                skipped += 1
                continue

            storage.upsert(app, app.status)
            storage.log_event(
                app.id, app.dealership, app.client_name, "approval_conditions", app.status,
                percentage=average_percentage(conditions),
                loan_amount=average_loan_amount(conditions),
            )
            storage.set_notified_conditions(app.id, fingerprint)
            logged += 1

            if i % 20 == 0 or i == len(candidates):
                logger.info("[backfill] Обработано %s/%s (записано: %s)", i, len(candidates), logged)

        if mark_done:
            storage.set_state(BACKFILL_STATE_KEY, "1")
        logger.info(
            "[backfill] Готово. Записано: %s, пропущено (нет условий/уже было): %s, ошибок: %s",
            logged, skipped, errors,
        )
        if logged == 0 and not candidates:
            logger.info("[backfill] Кандидатов не найдено — новых заявок для записи нет.")
        elif logged == 0:
            logger.warning(
                "[backfill] Внимание: не записано ни одной новой заявки (возможно, все уже "
                "были обработаны штатным циклом бота ранее — это нормально)."
            )

        try:
            _fix_missing_percentages(storage, client)
        except Exception:
            logger.exception("[backfill] Ошибка при догоняющем пересчёте отсутствующих ставок")

        return logged, skipped, errors
    finally:
        _backfill_lock.release()


def run_initial_conditions_backfill(storage: ApplicationStorage) -> None:
    if not _uses_zenit_api():
        return
    if storage.get_state(BACKFILL_STATE_KEY):
        logger.info("[backfill] Уже выполнялся ранее (флаг %s установлен) — пропускаю", BACKFILL_STATE_KEY)
        return

    thread = threading.Thread(
        target=_run_impl,
        args=(storage,),
        kwargs={"mark_done": True},
        daemon=True,
        name="conditions-backfill",
    )
    thread.start()


def force_rerun_conditions_backfill(storage: ApplicationStorage) -> None:
    if not _uses_zenit_api():
        return

    thread = threading.Thread(
        target=_run_impl,
        args=(storage,),
        kwargs={"mark_done": True},
        daemon=True,
        name="conditions-backfill-manual",
    )
    thread.start()
