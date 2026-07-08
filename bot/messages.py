from bot.models import Application
from bot.zenit_conditions import TARIFF_LABEL, format_single_condition
from bot.zenit_statuses import format_status_with_emoji, status_emoji


def _status_line(app: Application) -> str:
    return format_status_with_emoji(app.state, app.status)


def format_new_application(app: Application) -> str:
    emoji = status_emoji(app.state, app.status)
    return (
        f"{emoji} Новая заявка\n\n"
        f"Клиент: {app.client_name}\n"
        f"Автосалон: {app.dealership}\n"
        f"Статус: {_status_line(app)}\n"
        f"ID: {app.id}"
    )


def format_status_change(app: Application, old_status: str) -> str:
    emoji = status_emoji(app.state, app.status)
    return (
        f"{emoji} Изменение статуса заявки\n\n"
        f"Клиент: {app.client_name}\n"
        f"Автосалон: {app.dealership}\n"
        f"Было: {old_status}\n"
        f"Стало: {_status_line(app)}\n"
        f"ID: {app.id}"
    )


def format_approval_conditions(app: Application, conditions: list[dict]) -> str:
    emoji = status_emoji(app.state, app.status)
    blocks = [
        format_single_condition(cond, index + 1, len(conditions))
        for index, cond in enumerate(conditions)
    ]
    return (
        f"{emoji} Условия кредитования ({TARIFF_LABEL})\n\n"
        f"Клиент: {app.client_name}\n"
        f"Автосалон: {app.dealership}\n"
        f"Статус: {_status_line(app)}\n"
        f"ID: {app.id}\n\n"
        + "\n\n".join(blocks)
    )
