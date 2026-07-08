import requests

from bot.messages import format_approval_conditions, format_new_application, format_status_change
from bot.models import Application


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, proxy: str | None = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {"http": None, "https": None}

    def send(self, text: str) -> None:
        self.send_to(self.chat_id, text)

    def send_to(self, chat_id: str, text: str) -> None:

        response = self.session.post(
            self.api_url,
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

    def notify_new_application(self, app: Application) -> None:
        self.send(format_new_application(app))

    def notify_status_change(self, app: Application, old_status: str) -> None:
        self.send(format_status_change(app, old_status))

    def notify_approval_conditions(self, app: Application, conditions: list[dict]) -> None:
        self.send(format_approval_conditions(app, conditions))
