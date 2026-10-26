#!/usr/bin/env python3
"""
Авторизация для Bedrock Cape Manager.

Цепочка: MSA Device Code -> Xbox Live User Token -> XSTS -> PlayFab LoginWithXbox.

Команды:
  python bedrock_auth.py login                   # войти через браузер, сохранить refresh token
  python bedrock_auth.py login --create-account  # то же, но создать профиль PlayFab, если его нет
  python bedrock_auth.py whoami                  # проверить цепочку, показать только безопасные данные
  python bedrock_auth.py logout   # удалить сохранённый токен

Прокси: requests сам читает HTTPS_PROXY / HTTP_PROXY из окружения.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

MSA_CLIENT_ID = "000000004c12ae6f"
MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
MSA_DEVICE_URL = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN_URL = "https://login.live.com/oauth20_token.srf"

XBL_USER_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
# Relying Party сравнивается как строка, слэш в конце обязателен
# (так в df-mc/go-playfab для Minecraft). Не меняйте.
# RP тайтла PlayFab для Minecraft. Токен, выданный для него, принимает redeem/xbox (проверено на практике).
PLAYFAB_TITLE_RP = "https://b980a380.minecraft.playfabapi.com/"
# Android-клиент в записи просит PlayFab-токен для RP https://b980a380.minecraft.playfabapi.com/ .
# Переопределение: переменная BEDROCK_XSTS_RP.
XSTS_RELYING_PARTY = os.environ.get("BEDROCK_XSTS_RP", "http://playfab.xboxlive.com/")

PLAYFAB_TITLE_ID = "20CA2"
PLAYFAB_URL = "https://20ca2.playfabapi.com"

TOKEN_FILE = Path(os.environ.get("BEDROCK_TOKEN_FILE", Path.home() / ".bedrock_tokens.json"))
TIMEOUT = 15
# По умолчанию профиль PlayFab НЕ создаётся: это делает сама игра при первом запуске.
# Включается явно: login --create-account или BEDROCK_CREATE_ACCOUNT=1.
CREATE_ACCOUNT_DEFAULT = os.environ.get("BEDROCK_CREATE_ACCOUNT") == "1"

# Параметры, которые клиент отправляет в session/start. Настоящий клиент шлёт язык и регион аккаунта
# (в записи: ru-RU / RU, ageRange Adult). Регион может влиять на содержимое страниц магазина.
CLIENT_LANGUAGE = os.environ.get("BEDROCK_LANGUAGE", "en-US")
CLIENT_REGION = os.environ.get("BEDROCK_REGION", "US")
CLIENT_AGE_RANGE = os.environ.get("BEDROCK_AGE_RANGE")  # например Adult; по умолчанию не отправляется

XERR_MESSAGES = {
    2148916233: "У аккаунта нет профиля Xbox. Создайте его на xbox.com.",
    2148916235: "Xbox Live недоступен в стране аккаунта.",
    2148916236: "Аккаунту нужна проверка возраста.",
    2148916237: "Аккаунту нужна проверка возраста.",
    2148916238: "Детский аккаунт: его должен добавить в семью взрослый.",
}


class AuthError(RuntimeError):
    pass


# ---------- MSA ----------

def _post_form(url: str, data: Dict[str, str]) -> tuple:
    r = requests.post(url, data=data, timeout=TIMEOUT)
    try:
        return r.status_code, r.json()
    except ValueError:
        raise AuthError(f"{url}: ответ не JSON (HTTP {r.status_code})")


def device_code_login(prompt: Callable[[str], None] = print) -> Dict[str, Any]:
    status, d = _post_form(
        MSA_DEVICE_URL,
        {"client_id": MSA_CLIENT_ID, "scope": MSA_SCOPE, "response_type": "device_code"},
    )
    if status != 200 or "device_code" not in d:
        raise AuthError(f"Не удалось получить device code: {d}")

    prompt(f"Откройте {d.get('verification_uri', 'https://microsoft.com/link')} "
           f"и введите код: {d['user_code']}")

    interval = int(d.get("interval", 5))
    deadline = time.time() + int(d.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        status, t = _post_form(
            MSA_TOKEN_URL,
            {
                "client_id": MSA_CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": d["device_code"],
            },
        )
        if status == 200 and "access_token" in t:
            return t
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise AuthError(f"Вход прерван: {t.get('error_description') or err}")
    raise AuthError("Код истёк. Запустите login заново.")


def refresh_msa(refresh_token: str) -> Dict[str, Any]:
    status, t = _post_form(
        MSA_TOKEN_URL,
        {
            "client_id": MSA_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": MSA_SCOPE,
        },
    )
    if status != 200 or "access_token" not in t:
        raise AuthError("Refresh token недействителен. Выполните login заново.")
    return t


# ---------- Xbox / PlayFab ----------

_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-xbl-contract-version": "1",
}


def _describe(r: requests.Response) -> str:
    """Описание ответа для диагностики. Токены не выводит."""
    try:
        j = r.json()
        info = {k: j[k] for k in ("XErr", "Message", "Redirect", "error", "errorMessage") if k in j}
    except ValueError:
        info = {"body": r.text[:200]}
    return f"HTTP {r.status_code} {info}"


def xbl_user_token(msa_access_token: str) -> str:
    # Для токенов с login.live.com (MBI_SSL) обычно нужен префикс "t=",
    # для токенов из Azure AD это "d=". Пробуем оба.
    errors = []
    for prefix in ("t=", "d="):
        r = requests.post(
            XBL_USER_URL,
            json={
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": f"{prefix}{msa_access_token}",
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            headers=_JSON_HEADERS,
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()["Token"]
        errors.append(f"{prefix} -> {_describe(r)}")
    raise AuthError("Шаг 2 (Xbox user authenticate) не принял токен: " + "; ".join(errors))


def xsts_token(user_token: str, relying_party: str = XSTS_RELYING_PARTY) -> tuple:
    r = requests.post(
        XSTS_URL,
        json={
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [user_token]},
            "RelyingParty": relying_party,
            "TokenType": "JWT",
        },
        headers=_JSON_HEADERS,
        timeout=TIMEOUT,
    )
    if r.status_code == 401:
        try:
            xerr = r.json().get("XErr")
        except ValueError:
            xerr = None
        if xerr in XERR_MESSAGES:
            raise AuthError(XERR_MESSAGES[xerr])
    if r.status_code != 200:
        raise AuthError(f"Шаг 3 (XSTS): {_describe(r)}")
    j = r.json()
    xui = j["DisplayClaims"]["xui"][0]
    # xid — это 64-битный числовой XUID (например, 2535416302877446)
    return j["Token"], xui["uhs"], xui.get("xid")



def playfab_login(uhs: str, xsts: str, create_account: bool = False) -> Dict[str, Any]:
    r = requests.post(
        f"{PLAYFAB_URL}/Client/LoginWithXbox",
        json={
            "TitleId": PLAYFAB_TITLE_ID,
            "CreateAccount": create_account,
            "InfoRequestParameters": {"GetPlayerProfile": True, "GetUserAccountInfo": True},
            "XboxToken": f"XBL3.0 x={uhs};{xsts}",
        },
        timeout=TIMEOUT,
    )
    try:
        j = r.json()
    except ValueError:
        raise AuthError(f"PlayFab LoginWithXbox: ответ не JSON (HTTP {r.status_code})")
    if r.status_code != 200 or "data" not in j:
        if j.get("error") == "AccountNotFound" or j.get("errorCode") == 1001:
            raise AuthError(
                "У этого аккаунта нет профиля Minecraft Bedrock в PlayFab: игра создаёт его при первом "
                "запуске. Проще всего один раз войти этим аккаунтом в Bedrock. Если нужно создать профиль "
                "отсюда, повторите: python bedrock_auth.py login --create-account")
        raise AuthError(f"PlayFab LoginWithXbox: {j.get('errorMessage') or j}")
    data = j["data"]
    ent = data.get("EntityToken") or {}
    info = data.get("InfoResultPayload") or {}
    xbox_info = ((info.get("AccountInfo") or {}).get("XboxInfo")) or {}
    return {
        "playfab_id": data.get("PlayFabId"),
        "session_ticket": data.get("SessionTicket"),
        "entity_token": ent.get("EntityToken"),
        "entity_id": (ent.get("Entity") or {}).get("Id"),
        "entity_type": (ent.get("Entity") or {}).get("Type"),
        "xbl3_header": f"XBL3.0 x={uhs};{xsts}",
        "xuid": xbox_info.get("XboxUserId"),
        "newly_created": bool(data.get("NewlyCreated")),
    }


def _chain(msa_access_token: str, create_account: bool = False) -> Dict[str, Any]:
    user_token = xbl_user_token(msa_access_token)
    xsts, uhs, xid = xsts_token(user_token)
    session = playfab_login(uhs, xsts, create_account)
    session["user_token"] = user_token  # только в памяти, на диск не пишется
    if not session.get("xuid") and xid:
        session["xuid"] = str(xid)  # запасной источник XUID: claim xid из XSTS
    return session


def xbl3_for_rp(session: Dict[str, Any], relying_party: str) -> str:
    """Токен Xbox (строка XBL3.0 x=...) для другого Relying Party, по тому же user-токену."""
    xsts, uhs, _ = xsts_token(session["user_token"], relying_party)
    return f"XBL3.0 x={uhs};{xsts}"


# ---------- MCToken (franchise API) ----------

FRANCHISE_URL = "https://authorization.franchise.minecraft-services.net/api/v1.0/session/start"


def start_franchise_session(session: Dict[str, Any], game_version: str) -> Dict[str, Any]:
    """
    PlayFab SessionTicket -> ответ franchise API с MCToken.
    Тело запроса взято из публичного примера (bedrock-protocol issue #473);
    поля могут отличаться в новых версиях игры.
    """
    # В записи настоящего клиента device.id (32 hex без дефисов) совпадает с ClientId в запросе redeem.
    device_id = os.environ.get("BEDROCK_DEVICE_ID") or uuid.uuid5(uuid.NAMESPACE_DNS, session["playfab_id"]).hex
    session["device_id"] = device_id
    user = {
        "language": CLIENT_LANGUAGE.split("-")[0],
        "languageCode": CLIENT_LANGUAGE,
        "regionCode": CLIENT_REGION,
        "token": session["session_ticket"],
        "tokenType": "PlayFab",
    }
    if CLIENT_AGE_RANGE:
        user["ageRange"] = CLIENT_AGE_RANGE
    body = {
        "device": {
            "applicationType": "MinecraftPE",
            "capabilities": ["RayTracing"],
            "gameVersion": game_version,
            "id": device_id,
            "memory": str(16 * 1024 ** 3),
            "platform": "Windows10",
            "playFabTitleId": PLAYFAB_TITLE_ID,
            "storePlatform": "uwp.store",
            "treatmentOverrides": None,
            "type": "Windows10",
        },
        "user": user,
    }
    r = requests.post(FRANCHISE_URL, json=body, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise AuthError(f"franchise session/start: {_describe(r)}")
    try:
        return r.json()
    except ValueError:
        raise AuthError("franchise session/start: ответ не JSON")


def extract_mctoken(resp: Dict[str, Any]) -> str:
    """Возвращает значение для заголовка Authorization: 'MCToken <jwt>'."""
    result = resp.get("result", resp)
    value = result.get("authorizationHeader") or result.get("mcToken")
    if not value:
        raise AuthError(f"В ответе нет токена. Ключи: {list(result)}")
    return value if value.startswith("MCToken ") else f"MCToken {value}"


# ---------- Хранилище ----------

def save_refresh_token(refresh_token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": refresh_token}, f)
    try:
        os.chmod(TOKEN_FILE, 0o600)  # на Windows почти no-op
    except OSError:
        pass


def load_refresh_token() -> Optional[str]:
    try:
        return json.loads(TOKEN_FILE.read_text()).get("refresh_token")
    except (OSError, ValueError):
        return None


# ---------- Публичный API ----------

def login(create_account: bool = False) -> Dict[str, Any]:
    t = device_code_login()
    if t.get("refresh_token"):
        save_refresh_token(t["refresh_token"])
    return _chain(t["access_token"], create_account or CREATE_ACCOUNT_DEFAULT)


def get_session() -> Dict[str, Any]:
    """Возвращает dict: playfab_id, session_ticket, entity_token, xbl3_header."""
    rt = load_refresh_token()
    if not rt:
        raise AuthError("Нет сохранённого входа. Выполните: python bedrock_auth.py login")
    t = refresh_msa(rt)
    if t.get("refresh_token"):  # MSA ротирует refresh token
        save_refresh_token(t["refresh_token"])
    return _chain(t["access_token"], CREATE_ACCOUNT_DEFAULT)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "login":
            s = login(create_account="--create-account" in sys.argv)
            print(f"[+] Готово. PlayFabId: {s['playfab_id']}")
            if s.get("newly_created"):
                print("[i] Профиль PlayFab для этого аккаунта создан только что.")
            print(f"[+] Refresh token сохранён в {TOKEN_FILE}")
        elif cmd == "whoami":
            s = get_session()
            print(f"PlayFabId: {s['playfab_id']}")
            print(f"XUID получен: {'да' if s.get('xuid') else 'нет'}")
            print(f"Профиль PlayFab создан при этом входе: {'да' if s.get('newly_created') else 'нет'}")
        elif cmd == "logout":
            TOKEN_FILE.unlink(missing_ok=True)
            print("[+] Сохранённый токен удалён.")
        else:
            print(__doc__)
    except AuthError as e:
        print(f"[!] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
