"""Разведка API деталей заявки (step 6, тарифы). Запуск: python probe_request_detail.py [request_id]"""
import json
import re
import sys

import requests

from bot.totp_utils import get_totp_code
from bot.zenit_client import AUTO_API, GATEKEEPER_API, OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI, ZenitClient
from config import LOGIN_PASSWORD, LOGIN_USERNAME, TOTP_SECRET

G = GATEKEEPER_API
A = AUTO_API


def login() -> tuple[requests.Session, str]:
    email = ZenitClient.normalize_email(LOGIN_USERNAME)
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    t = s.post(
        f"{G}/api/users/sign_in",
        json={
            "email": email,
            "password": LOGIN_PASSWORD,
            "one_time_password": get_totp_code(TOTP_SECRET),
        },
        timeout=60,
    ).headers.get("authorization", "").replace("Bearer ", "")
    code = s.post(
        f"{G}/api/oauth/authorize",
        json={
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "one_time_password": get_totp_code(TOTP_SECRET),
        },
        headers={"Authorization": f"Bearer {t}"},
        timeout=60,
    ).json()["code"]
    at = s.post(
        f"{A}/v1/gatekeeper/exchange_code",
        json={"grant_type": "authorization_code", "code": code},
        timeout=60,
    ).json()["access_token"]
    return s, at


def main() -> None:
    if not TOTP_SECRET:
        print("Нужен TOTP_SECRET в .env")
        sys.exit(1)

    s, at = login()
    h = {"Authorization": f"Bearer {at}", "Accept": "application/json"}

    params = {"offset": -180, "mode": "light", "sort": "desc", "limit": 20}
    r = s.get(f"{A}/v1/requests/vehicles", params=params, headers=h, timeout=120)
    data = r.json()
    items = data.get("result") if isinstance(data, dict) and "result" in data else data
    if isinstance(items, dict):
        items = items.get("list") or items.get("items") or []
    print(f"Заявок в списке: {len(items)}")

    approved = [
        x for x in items
        if "approve" in str(x.get("state", "")).lower()
        or "одобр" in str(x.get("state_text", "")).lower()
    ]
    print(f"Одобренных: {len(approved)}")
    for x in approved[:3]:
        print(f"  id={x.get('id')} state={x.get('state')} text={x.get('state_text')}")

    req_id = sys.argv[1] if len(sys.argv) > 1 else (approved[0]["id"] if approved else items[0]["id"])
    print(f"\nПробуем request_id={req_id}")

    candidates = [
        f"{A}/v1/requests/vehicles/{req_id}",
        f"{A}/api/requests/vehicles/{req_id}",
        f"{A}/v1/requests/vehicles/{req_id}/steps/6",
        f"{A}/v1/requests/vehicles/{req_id}/step/6",
        f"{A}/v1/requests/vehicles/{req_id}/steps/step_6",
        f"{A}/v1/requests/vehicles/{req_id}?step=6",
    ]

    # scan JS for paths
    try:
        js = requests.get(
            "https://auto.zenit.balance-pl.ru/-static/1a0c45977088/js/main..js",
            timeout=120,
        ).text
        for m in re.findall(r'"/v1/requests/vehicles/[^"]+"', js):
            url = A + m.strip('"').replace("{id}", str(req_id)).replace("${id}", str(req_id))
            if url not in candidates:
                candidates.append(url)
        for m in re.findall(r'"/api/requests/vehicles/[^"]+"', js):
            url = A + m.strip('"').replace("{id}", str(req_id))
            if url not in candidates:
                candidates.append(url)
        for needle in ["step_6", "step6", "Step6", "partner", "партнер", "tariff", "conditions"]:
            idx = 0
            n = 0
            while n < 3:
                idx = js.find(needle, idx)
                if idx < 0:
                    break
                print(f"\nJS [{needle}]:", re.sub(r"\s+", " ", js[idx : idx + 200]))
                idx += len(needle)
                n += 1
    except Exception as exc:
        print("JS fetch failed:", exc)

    for url in candidates:
        try:
            resp = s.get(url, headers=h, timeout=60)
            print(f"\nGET {url} -> {resp.status_code}")
            if resp.status_code == 200:
                body = resp.text[:3000]
                print(body)
                if "партнер" in body.lower() or "partner" in body.lower():
                    print("*** FOUND partner mention ***")
        except Exception as exc:
            print(f"GET {url} -> error: {exc}")


if __name__ == "__main__":
    main()
