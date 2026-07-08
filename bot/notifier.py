from pathlib import Path

from bot.messages import format_approval_conditions, format_new_application, format_status_change
from bot.models import Application
from bot.telegram_client import TelegramNotifier


class ConsoleNotifier:


    def send(self, text: str) -> None:
        print("\n" + "=" * 50)
        print(text)
        print("=" * 50 + "\n")

    def notify_new_application(self, app: Application) -> None:
        self.send(format_new_application(app))

    def notify_status_change(self, app: Application, old_status: str) -> None:
        self.send(format_status_change(app, old_status))

    def notify_approval_conditions(self, app: Application, conditions: list[dict]) -> None:
        self.send(format_approval_conditions(app, conditions))


class FileNotifier:
    """Запись уведомлений в файл."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, text: str) -> None:
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(text)
            f.write("\n" + "-" * 40 + "\n")

    def notify_new_application(self, app: Application) -> None:
        self.send(format_new_application(app))

    def notify_status_change(self, app: Application, old_status: str) -> None:
        self.send(format_status_change(app, old_status))

    def notify_approval_conditions(self, app: Application, conditions: list[dict]) -> None:
        self.send(format_approval_conditions(app, conditions))


def create_notifier(mode: str, bot_token: str, chat_id: str, proxy: str, log_file: Path):
    if mode == "console":
        return ConsoleNotifier()
    if mode == "file":
        return FileNotifier(log_file)
    return TelegramNotifier(bot_token, chat_id, proxy or None)
