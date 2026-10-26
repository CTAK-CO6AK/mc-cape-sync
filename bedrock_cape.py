#!/usr/bin/env python3
"""
Bedrock Cape Manager.

Переключает плащи, которые УЖЕ есть на вашем аккаунте. Работает по запросам,
снятым с настоящего клиента (persona-secondary и store.mktpl).

  python bedrock_auth.py login                     # один раз
  python bedrock_cape.py list                      # плащи и признак владения
  python bedrock_cape.py status                    # что надето в слотах 1-5
  python bedrock_cape.py diagnose                  # где обрывается цепочка, если плащей 0 (только чтение)
  python bedrock_cape.py sync                      # зачислить покупки Microsoft Store в инвентарь Bedrock
  python bedrock_cape.py snapshot                  # сохранить снимок инвентаря (до и после запуска игры)
  python bedrock_cape.py diff A.json B.json [--names]   # что добавилось/пропало между снимками
  python bedrock_cape.py equip -c "Скрученный" -p 1            # показать план (ничего не меняет)
  python bedrock_cape.py equip -c "Скрученный" -p 1 --apply    # применить
  python bedrock_cape.py equip -c "Follower" -p 1 --create-slot --apply   # аккаунт без внешнего вида: создать слот
  python bedrock_cape.py restore backups/<файл>.json --apply   # откат из резервной копии

Защиты:
  * надеть можно только плащ, у которого сервер отметил владение (ownership == Purchased);
    обходов и флагов для этого нет;
  * по умолчанию показывается только план, запись требует --apply;
  * перед записью читается текущее состояние и сохраняется резервная копия;
  * меняется только элемент плаща в выбранном слоте, остальное копируется как есть;
  * после записи состояние читается заново и сверяется.

Закройте игру перед записью: клиент может перезаписать изменения своим состоянием.
"""

import argparse
import copy
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import bedrock_auth as auth

DISCOVERY_BUILDS = "https://client.discovery.minecraft-services.net/api/v1.0/discovery/MinecraftPE/builds/{v}"
USER_AGENT = "libhttpclient/1.0.0.0"
TIMEOUT = 20

CAPE_SLOTS = ["personaProfile1", "personaProfile2", "personaProfile3", "personaProfile4", "personaProfile5"]
CLASSIC_SLOT = "personaProfile"
ALL_SLOTS = CAPE_SLOTS + [CLASSIC_SLOT]
CAPES_PAGE = "DressingRoom_Capes"
# Нетронутый слот так выглядит в ответе сервера у аккаунта с историей (слот 5 в записи).
NEW_SLOT_VERSION = "0.1.0"
NEW_SLOT_BASE = {"arm": "wide", "skcol": "#0", "skin": False}
OWNED_VALUES = {"Purchased"}  # значения ownership, которые считаются «есть на аккаунте»
BACKUP_DIR = Path(os.environ.get("BEDROCK_BACKUP_DIR", "backups"))
SNAPSHOT_DIR = Path(os.environ.get("BEDROCK_SNAPSHOT_DIR", "snapshots"))


class CapeError(RuntimeError):
    pass


# ======================= чистая логика (без сети) =======================

def base_id(piece_id: str) -> str:
    """'uuid/d' -> 'uuid'. Суффикс после слэша клиент добавляет сам."""
    return piece_id.split("/", 1)[0].lower()


def parse_capes(store_json: Any) -> List[Dict[str, Any]]:
    """Достаёт плащи из ответа страницы DressingRoom_Capes, не завися от вёрстки."""
    found: Dict[str, Dict[str, Any]] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if o.get("pieceType") == "persona_capes":
                ident = next((i for i in (o.get("packIdentity") or []) if i.get("type") == "persona_piece"), None)
                if ident and ident.get("uuid"):
                    uid = ident["uuid"].lower()
                    found[uid] = {
                        "uuid": uid,  # именно он пишется в внешний вид
                        "offer_id": (o.get("id") or "").lower(),
                        "title": o.get("title") or uid,
                        "ownership": o.get("ownership"),
                        "rarity": o.get("rarity"),
                        "list_price": (o.get("price") or {}).get("listPrice"),
                    }
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(store_json)
    return sorted(found.values(), key=lambda c: c["title"].lower())


def is_owned(cape: Dict[str, Any]) -> bool:
    return cape.get("ownership") in OWNED_VALUES


def find_cape(query: str, capes: List[Dict[str, Any]]) -> Dict[str, Any]:
    q = query.strip().lower()
    exact = [c for c in capes if q in (c["uuid"], c["offer_id"])]
    if exact:
        return exact[0]
    subs = [c for c in capes if q in c["title"].lower()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        raise CapeError(f"Плащ '{query}' не найден. Команда list покажет доступные.")
    raise CapeError("Подходит несколько плащей: " + "; ".join(f"{c['title']} ({c['uuid']})" for c in subs))


def slot_items(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return profile["personaCollection"]["universalApp"]


def cape_positions(items: List[Dict[str, Any]], cape_uuids: set) -> List[int]:
    return [i for i, e in enumerate(items)
            if isinstance(e, dict) and "id" in e and base_id(e["id"]) in cape_uuids]


def observed_id_forms(profiles: List[Dict[str, Any]]) -> Dict[str, str]:
    """Как клиент записал каждый id в ваших слотах (с суффиксом /d, /f или без)."""
    forms: Dict[str, str] = {}
    for p in profiles:
        for e in slot_items(p):
            if isinstance(e, dict) and "id" in e:
                forms.setdefault(base_id(e["id"]), e["id"])
    return forms


def unrecognized_ids(items: List[Dict[str, Any]], cape_uuids: set) -> List[str]:
    """Элементы слота с id, не похожие ни на плащ из списка, ни на стандартную деталь (/d)."""
    return [e["id"] for e in items
            if isinstance(e, dict) and "id" in e and not e["id"].endswith("/d")
            and base_id(e["id"]) not in cape_uuids and "_" not in e["id"]]


def plan_equip(profiles: List[Dict[str, Any]], slot: str, cape: Dict[str, Any],
               cape_uuids: set, add_if_missing: bool = False,
               replace_id: Optional[str] = None, create_slot: bool = False) -> Dict[str, Any]:
    if slot not in CAPE_SLOTS:
        raise CapeError(f"Слот {slot} не подходит для плащей.")
    prof = next((p for p in profiles if p.get("slotname") == slot), None)
    slot_created = False
    if prof is None:
        if not create_slot:
            raise CapeError(
                f"Слот {slot} не найден в ответе сервера: у аккаунта нет сохранённого внешнего вида "
                f"(так бывает, если игру на нём ни разу не запускали). Команда status покажет, что есть. "
                f"Можно создать слот с минимальным содержимым и плащом: добавьте --create-slot.")
        prof = {"slotname": slot, "version": NEW_SLOT_VERSION,
                "personaCollection": {"universalApp": [copy.deepcopy(NEW_SLOT_BASE)]}}
        slot_created = True
        add_if_missing = True

    items = copy.deepcopy(slot_items(prof))
    if replace_id:
        # Явная замена конкретного элемента слота (плащ, которого нет в списке магазина).
        pos = [i for i, e in enumerate(items)
               if isinstance(e, dict) and e.get("id", "").lower() == replace_id.strip().lower()]
        if not pos:
            raise CapeError(f"В слоте {slot} нет элемента с id {replace_id}.")
    else:
        pos = cape_positions(items, cape_uuids)
    if len(pos) > 1:
        raise CapeError(f"В слоте {slot} найдено несколько плащей, не трогаю. Исправьте вручную в игре.")

    forms = observed_id_forms(profiles)
    note = None
    if cape["uuid"] in forms:
        new_id = forms[cape["uuid"]]
    elif cape.get("rarity") == "common":
        new_id = cape["uuid"] + "/f"
        note = "Обычный плащ: клиент записывал такие id с суффиксом /f (одно наблюдение), пишу так же."
    else:
        new_id = cape["uuid"]
        note = "Формат id этого плаща в ваших слотах не встречался, пишу голый UUID (так клиент записывал остальные плащи)."

    old_id = None
    if pos:
        old_id = items[pos[0]]["id"]
        items[pos[0]] = {"id": new_id}
    else:
        if not add_if_missing:
            hint = ""
            cand = unrecognized_ids(items, cape_uuids)
            if cand:
                hint = (f" В слоте есть нераспознанные элементы: {', '.join(cand)}. Если один из них плащ, "
                        f"которого нет в списке магазина, замените его явно: --replace-id <id>.")
            raise CapeError(f"В слоте {slot} не найден надетый плащ из вашего списка.{hint} "
                            f"Если плаща в слоте действительно нет, добавьте --add-if-missing "
                            f"(иначе рискуете получить два плаща).")
        items.append({"id": new_id})

    return {"slot": slot, "version": prof.get("version"), "items": items,
            "old_id": old_id, "new_id": new_id, "note": note, "slot_created": slot_created}


def build_put_body(entries: List[Tuple[str, Any, List[Dict[str, Any]]]]) -> Dict[str, Any]:
    """entries: (имя слота, version, universalApp). Структура как в запросе клиента."""
    return {"appearanceObjects": [
        {"personaAppearance": {"personaCollection": {"universalApp": items}, "version": version},
         "profileName": slot}
        for slot, version, items in entries
    ]}


def equip_put_body(profiles: List[Dict[str, Any]], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Как у клиента: сначала классический слот без изменений, затем изменённый."""
    entries = []
    classic = next((p for p in profiles if p.get("slotname") == CLASSIC_SLOT), None)
    if classic is not None:
        entries.append((CLASSIC_SLOT, classic.get("version"), copy.deepcopy(slot_items(classic))))
    entries.append((plan["slot"], plan["version"], plan["items"]))
    return build_put_body(entries)


def restore_put_body(backup: Dict[str, Any]) -> Dict[str, Any]:
    entries = [(p["slotname"], p.get("version"), slot_items(p)) for p in backup["result"]
               if p.get("slotname") in ALL_SLOTS]
    if not entries:
        raise CapeError("В резервной копии нет слотов.")
    return build_put_body(entries)


def title_of(piece_id: Optional[str], by_uuid: Dict[str, Dict[str, Any]]) -> str:
    if not piece_id:
        return "(нет)"
    c = by_uuid.get(base_id(piece_id))
    return f"{c['title']} [{piece_id}]" if c else f"[{piece_id}]"


# ============================ сеть ============================

def _json(r: requests.Response, what: str) -> Any:
    if not r.ok:
        raise CapeError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise CapeError(f"{what}: ответ не JSON")


class Client:
    def __init__(self, version: str, inventory_etag: Optional[str], list_version: Optional[str]):
        self.manual_etag = inventory_etag      # запасной вариант, обычно не нужен
        self.list_version = list_version       # x-userlists-versions: клиент шлёт его на каждый запрос страницы
        self._inv: Optional[Tuple[Optional[str], List[Dict[str, Any]]]] = None
        try:
            self.session = auth.get_session()
            self.mc = auth.extract_mctoken(auth.start_franchise_session(self.session, version))
        except auth.AuthError as e:
            raise CapeError(str(e))
        disc = _json(requests.get(DISCOVERY_BUILDS.format(v=version), timeout=TIMEOUT), "discovery")
        try:
            envs = disc["result"]["serviceEnvironments"]
            self.persona = envs["persona"]["prod"]["serviceUri"].rstrip("/")
            self.store = envs["store"]["prod"]["serviceUri"].rstrip("/")
            self.entitlements = envs["entitlements"]["prod"]["serviceUri"].rstrip("/")
        except (KeyError, TypeError):
            raise CapeError("Discovery не вернул адреса persona/store/entitlements.")
        self.session_id = str(uuid.uuid4())

    def _headers(self, body: bool = False) -> Dict[str, str]:
        h = {"Authorization": self.mc, "User-Agent": USER_AGENT, "Accept": "application/json"}
        if body:
            h["Content-Type"] = "application/json"
        return h

    # ---- Persona ----
    def retrieve(self) -> Dict[str, Any]:
        h = self._headers()
        h["session-id"] = self.session_id
        r = requests.get(f"{self.persona}/api/v1.0/appearance/retrieve",
                         params={"profilenames": ALL_SLOTS}, headers=h, timeout=TIMEOUT)
        return _json(r, "appearance/retrieve")

    def put(self, body: Dict[str, Any]) -> Dict[str, Any]:
        h = self._headers(body=True)
        h["session-id"] = self.session_id
        r = requests.put(f"{self.persona}/api/v1.0/appearance", headers=h, json=body, timeout=TIMEOUT)
        return _json(r, "appearance (PUT)")

    # ---- Инвентарь (entitlements) ----
    def player_inventory(self, fresh: bool = False) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """(inventoryVersion, список entitlements). Только чтение."""
        if self._inv is not None and not fresh:
            return self._inv
        r = requests.get(f"{self.entitlements}/api/v1.0/player/inventory",
                         params={"includeReceipt": "true"}, headers=self._headers(), timeout=TIMEOUT)
        inv = (_json(r, "player/inventory").get("result") or {}).get("inventory") or {}
        self._inv = (inv.get("inventoryVersion"), inv.get("entitlements") or [])
        return self._inv

    def redeem_xbox(self) -> Dict[str, Any]:
        """Как клиент: просит сервис зачислить покупки Microsoft Store этого аккаунта. Записывающий запрос."""
        xuid = str(self.session.get("xuid") or "")
        if not xuid:
            raise CapeError("XUID не найден в сессии, redeem невозможен.")
        rp = os.environ.get("BEDROCK_REDEEM_RP") or auth.PLAYFAB_TITLE_RP
        token = self.session["xbl3_header"]
        rp_used = auth.XSTS_RELYING_PARTY
        if self.session.get("user_token"):
            try:
                token = auth.xbl3_for_rp(self.session, rp)
                rp_used = rp
            except auth.AuthError as e:
                raise CapeError(f"Не удалось получить токен Xbox для RP {rp}: {e}")
        body = {
            "CustomTags": {
                "BuildPlat": 8,
                "ClientId": self.session.get("device_id") or "",
                "DeviceSessionId": self.session_id,
                "DnAPlat": "Win,D,,GC",
                "Seq": 1,
                "TitleId": auth.PLAYFAB_TITLE_ID,
                "Xuid": xuid,
                "editionType": "win10",
            },
            "Redemption": {"MarketplaceData": {
                "userId": xuid,
                "xboxAuthentication": {"XboxToken": token},
            }},
        }
        h = self._headers(body=True)
        h["inventoryetag"] = ""         # клиент отправляет эти заголовки пустыми
        h["x-userlists-versions"] = ""
        r = requests.post(f"{self.entitlements}/api/v1.0/transaction/redeem/xbox",
                          headers=h, json=body, timeout=TIMEOUT)
        out: Dict[str, Any] = {"status": r.status_code, "succeeded": [], "failed": [], "error": None,
                               "rp": rp_used}
        if r.ok:
            try:
                res = r.json().get("result") or {}
                out["succeeded"] = res.get("succeeded") or []
                out["failed"] = res.get("failed") or []
            except ValueError:
                out["error"] = "ответ не JSON"
        else:
            out["error"] = r.text[:300]
        return out

    def refresh_inventory(self, etag: Optional[str]) -> Dict[str, Any]:
        """POST store/inventory/refresh, как у клиента: без тела, версия в заголовке inventoryetag."""
        h = self._headers()
        if etag:
            h["inventoryetag"] = etag
        r = requests.post(f"{self.store}/api/v1.0/inventory/refresh", headers=h, timeout=TIMEOUT)
        version = None
        if r.ok:
            try:
                version = (r.json().get("result") or {}).get("version")
            except ValueError:
                pass
        return {"status": r.status_code, "version": version}

    # ---- Магазин ----
    def catalog(self, with_list_version: bool = True) -> List[Dict[str, Any]]:
        """Плащи из ответа магазина. Не падает, если список пуст."""
        live: Optional[str] = None
        try:
            live, _ = self.player_inventory()
        except CapeError:
            pass
        etag = live or self.manual_etag
        if live and self.manual_etag and live != self.manual_etag:
            print("[i] --inventory-etag отличается от текущей версии инвентаря аккаунта и игнорируется "
                  "(такие значения привязаны к конкретному аккаунту).")
        h = self._headers(body=True)
        body: Dict[str, Any] = {"entitlements": []}
        if etag:
            h["inventoryetag"] = etag
            body["inventoryVersion"] = etag
        if self.list_version and with_list_version:
            h["x-userlists-versions"] = self.list_version
            body["listVersion"] = self.list_version
        r = requests.post(f"{self.store}/api/v2.0/layout/pages/{CAPES_PAGE}", headers=h, json=body, timeout=TIMEOUT)
        return parse_capes(_json(r, "layout/pages/" + CAPES_PAGE))

    def capes(self) -> List[Dict[str, Any]]:
        capes = self.catalog()
        if not capes:
            raise CapeError("В ответе магазина нет плащей. Запустите: python bedrock_cape.py diagnose")
        return capes


def save_backup(data: Dict[str, Any]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    p = BACKUP_DIR / f"appearance_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ============================ команды ============================

def cmd_list(c: Client, args) -> None:
    capes = c.capes()
    owned = [x for x in capes if is_owned(x)]
    print(f"Плащей в каталоге: {len(capes)}, на аккаунте: {len(owned)}\n")
    for x in sorted(capes, key=lambda x: (not is_owned(x), x["title"].lower())):
        mark = "ЕСТЬ " if is_owned(x) else "нет  "
        extra = "" if x["ownership"] in OWNED_VALUES | {"NotOwned"} else f"  (ownership={x['ownership']})"
        print(f"  [{mark}] {x['title']}\n          {x['uuid']}{extra}")


def cmd_status(c: Client, args) -> None:
    capes = c.capes()
    by_uuid = {x["uuid"]: x for x in capes}
    uuids = set(by_uuid)
    data = c.retrieve()
    result = data.get("result") or []
    print(f"profileVersion: {data.get('profileVersion')}, слотов в ответе сервера: {len(result)}\n")
    present = {p.get("slotname"): p for p in result}
    for slot in CAPE_SLOTS:
        p = present.get(slot)
        if p is None:
            print(f"  {slot}: слота нет на сервере (можно создать: equip ... --create-slot)")
            continue
        items = slot_items(p)
        pos = cape_positions(items, uuids)
        cape = title_of(items[pos[0]]["id"], by_uuid) if pos else "плащ из вашего списка не найден"
        print(f"  {slot}: элементов {len(items)}, {cape}")
    if CLASSIC_SLOT not in present:
        print(f"  {CLASSIC_SLOT}: (классический слот отсутствует)")


def cmd_equip(c: Client, args) -> None:
    if not 1 <= args.profile <= 5:
        raise CapeError("Слот должен быть от 1 до 5.")
    slot = f"personaProfile{args.profile}"
    capes = c.capes()
    by_uuid = {x["uuid"]: x for x in capes}
    cape = find_cape(args.cape, capes)
    if not is_owned(cape):
        raise CapeError(f"Плаща «{cape['title']}» нет на вашем аккаунте (ownership={cape['ownership']}). "
                        f"Менеджер переключает только имеющиеся плащи.")

    before = c.retrieve()
    plan = plan_equip(before.get("result") or [], slot, cape, set(by_uuid), args.add_if_missing,
                      args.replace_id, args.create_slot)
    print(f"Слот:   {slot}" + ("  (будет СОЗДАН: на сервере его нет)" if plan["slot_created"] else ""))
    print(f"Было:   {title_of(plan['old_id'], by_uuid)}")
    print(f"Станет: {title_of(plan['new_id'], by_uuid)}")
    if plan["note"]:
        print(f"Примечание: {plan['note']}")
    if plan["old_id"] and base_id(plan["old_id"]) == cape["uuid"]:
        print("Этот плащ уже надет, менять нечего.")
        return
    if not args.apply:
        print("\nЭто только план. Для записи добавьте --apply.")
        return

    backup = save_backup(before)
    print(f"\nРезервная копия: {backup}")
    res = c.put(equip_put_body(before.get("result") or [], plan))
    print(f"Сервер принял запись, profileVersion: {res.get('profileVersion')}")

    after = c.retrieve()
    ok = True
    for p in after.get("result") or []:
        name = p.get("slotname")
        old = next((q for q in (before.get("result") or []) if q.get("slotname") == name), None)
        if name == slot:
            if slot_items(p) != plan["items"]:
                ok = False
        elif old is not None and slot_items(p) != slot_items(old):
            ok = False
            print(f"  Внимание: слот {name} изменился.")
    if ok:
        print("Проверка: слот совпадает с планом, остальные не тронуты.")
    else:
        print(f"Проверка НЕ прошла. Откат: python bedrock_cape.py restore {backup} --apply")


def cmd_restore(c: Client, args) -> None:
    backup = json.loads(Path(args.file).read_text(encoding="utf-8"))
    body = restore_put_body(backup)
    names = [o["profileName"] for o in body["appearanceObjects"]]
    print(f"Из копии {args.file} будут восстановлены слоты: {', '.join(names)}")
    if not args.apply:
        print("Это только план. Для записи добавьте --apply.")
        return
    before = c.retrieve()
    print(f"Текущее состояние сохранено: {save_backup(before)}")
    res = c.put(body)
    print(f"Готово, profileVersion: {res.get('profileVersion')}")


def diagnose_verdict(ent_count: int, catalog_count: int, in_inventory: int, store_owned: int) -> List[str]:
    if ent_count == 0:
        return ["Инвентарь Bedrock этого аккаунта пуст: ни одна покупка не зачислена. Это объясняет 0 плащей. "
                "Выполните sync. Если и после него пусто, аккаунт, скорее всего, ни разу не проходил первичную "
                "настройку в самой игре, проще один раз войти им в Bedrock."]
    if catalog_count == 0:
        return ["Инвентарь не пуст, но магазин не вернул ни одного плаща. Проверьте --version и повторите."]
    if in_inventory > 0 and store_owned == 0:
        return ["Плащи есть в инвентаре, но магазин не отмечает владение: вероятна устаревшая версия инвентаря "
                "или значения --inventory-etag/--list-version от другого аккаунта. Запустите без них."]
    if in_inventory == 0 and store_owned == 0:
        return ["Инвентарь не пуст, но плащей из каталога магазина среди него нет. Значит, в системе Bedrock "
                "плащей у аккаунта нет. Плащи Java Edition сюда не попадают, это другая система."]
    if in_inventory != store_owned:
        return [f"Данные расходятся: по инвентарю {in_inventory}, по магазину {store_owned}. Пришлите вывод."]
    return ["Инвентарь и магазин согласованы."]


def cmd_diagnose(c: Client, args) -> None:
    print("=== Диагностика (только чтение) ===")
    s = c.session
    print("[1] Вход")
    print(f"    профиль PlayFab создан при этом входе: {'да' if s.get('newly_created') else 'нет'}")
    print(f"    XUID получен: {'да' if s.get('xuid') else 'нет'}")

    print("[2] Инвентарь Bedrock (entitlements)")
    version, ents = c.player_inventory(fresh=True)
    platforms: Dict[str, int] = {}
    for e in ents:
        key = e.get("storePlatform") or "(не из магазина ОС)"
        platforms[key] = platforms.get(key, 0) + 1
    print(f"    inventoryVersion: {version}")
    print(f"    позиций: {len(ents)}, по источникам: {platforms}")
    geo = sum(1 for e in ents if e.get("isSubjectToGeoRestriction"))
    print(f"    позиций с географическим ограничением (isSubjectToGeoRestriction): {geo}")

    print("[3] Каталог плащей в ответе магазина")
    if c.list_version:
        without = c.catalog(with_list_version=False)
        print(f"    без x-userlists-versions: плащей {len(without)}, с владением {sum(is_owned(x) for x in without)}")
        capes = c.catalog(with_list_version=True)
        print(f"    с x-userlists-versions:   плащей {len(capes)}, с владением {sum(is_owned(x) for x in capes)}")
    else:
        capes = c.catalog()
        print("    x-userlists-versions не задан. Настоящий клиент отправляет его на каждый запрос страницы,")
        print("    попробуйте: diagnose --list-version <значение из записи клиента>")
    store_owned = [x for x in capes if is_owned(x)]
    print(f"    итого плащей в ответе: {len(capes)}, с признаком владения: {len(store_owned)}")

    pack_ids = {str(e.get("packId", "")).lower() for e in ents}
    in_inv = [x for x in capes if x["uuid"] in pack_ids]
    print(f"[4] Сверка: плащей из ответа магазина, присутствующих в инвентаре (packId): {len(in_inv)}")
    print("    (сверка идёт только по плащам, которые вернул магазин)")

    print("\nВывод:")
    for line in diagnose_verdict(len(ents), len(capes), len(in_inv), len(store_owned)):
        print(f"  {line}")


def cmd_sync(c: Client, args) -> None:
    ver_before, before = c.player_inventory(fresh=True)
    print(f"До:      позиций {len(before)}, версия {ver_before}")
    res = c.redeem_xbox()
    print(f"Токен Xbox для redeem выдан для RP: {res['rp']}")
    print(f"Redeem:  HTTP {res['status']}, записей в succeeded: {len(res['succeeded'])}, в failed: {len(res['failed'])}")
    print("         (в succeeded могут быть и прежние зачисления)")
    if res["error"]:
        print(f"         ошибка: {res['error']}")
        if res["status"] in (401, 403):
            print("         Сервис не принял токен Xbox. Возможно, для этого запроса клиент использует токен "
                  "с другим Relying Party. Пришлите этот вывод.")
    for f in res["failed"][:10]:
        print("         отказ:", json.dumps(f, ensure_ascii=False)[:200])
    ref = c.refresh_inventory(ver_before)
    print(f"Refresh: HTTP {ref['status']}, версия {ref['version']}")
    ver_after, after = c.player_inventory(fresh=True)
    print(f"После:   позиций {len(after)}, версия {ver_after}")
    print(f"Новых позиций: {len(after) - len(before)}")


def _clean_entitlement(e: Dict[str, Any]) -> Dict[str, Any]:
    """Оставляет описание позиции, но выбрасывает всё, что похоже на чеки и токены."""
    return {k: v for k, v in e.items() if "receipt" not in k.lower() and "token" not in k.lower()}


def cmd_snapshot(c: Client, args) -> None:
    version, ents = c.player_inventory(fresh=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    taken = datetime.now()
    path = SNAPSHOT_DIR / f"inventory_{taken:%Y%m%d_%H%M%S}.json"
    n = 2
    while path.exists():  # два снимка в одну секунду не должны затирать друг друга
        path = SNAPSHOT_DIR / f"inventory_{taken:%Y%m%d_%H%M%S}_{n}.json"
        n += 1
    data = {"taken": taken.isoformat(timespec="seconds"), "inventoryVersion": version,
            "entitlements": [_clean_entitlement(e) for e in ents]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Снимок: {path}\nПозиций: {len(ents)}, версия: {version}")


def diff_snapshots(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ia = {str(e.get("packId", "")).lower(): e for e in a["entitlements"]}
    ib = {str(e.get("packId", "")).lower(): e for e in b["entitlements"]}
    added = [ib[k] for k in ib if k not in ia]
    removed = [ia[k] for k in ia if k not in ib]
    return added, removed


def cmd_diff(c: Optional[Client], args) -> None:
    a = json.loads(Path(args.file_a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.file_b).read_text(encoding="utf-8"))
    added, removed = diff_snapshots(a, b)
    by_uuid: Dict[str, Dict[str, Any]] = {}
    if args.names and c is not None:
        by_uuid = {x["uuid"]: x for x in c.catalog()}
    print(f"Было:  {len(a['entitlements'])} позиций, версия {a.get('inventoryVersion')}, {a.get('taken')}")
    print(f"Стало: {len(b['entitlements'])} позиций, версия {b.get('inventoryVersion')}, {b.get('taken')}")

    def line(sign: str, e: Dict[str, Any]) -> str:
        pid = str(e.get("packId", "")).lower()
        cape = by_uuid.get(pid)
        tag = f"  [плащ: {cape['title']}, {cape['ownership']}]" if cape else ""
        geo = "  geo-ограничение" if e.get("isSubjectToGeoRestriction") else ""
        return (f"  {sign} {pid}  offerIds={e.get('offerIds')}  источник={e.get('storePlatform') or '-'}{geo}{tag}")

    print(f"\nДобавлено: {len(added)}")
    for e in added:
        print(line("+", e))
    print(f"Пропало: {len(removed)}")
    for e in removed:
        print(line("-", e))
    if added and not by_uuid:
        print("\nЧтобы увидеть названия плащей среди добавленных, добавьте --names (нужен вход).")


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--version", default=os.environ.get("BEDROCK_VERSION", "1.26.52"),
                        help="версия Bedrock (или переменная BEDROCK_VERSION)")
    common.add_argument("--inventory-etag", default=os.environ.get("BEDROCK_INVENTORY_ETAG"),
                        help="не нужен: версия берётся с сервера. Значение от ДРУГОГО аккаунта вредно")
    common.add_argument("--list-version", default=os.environ.get("BEDROCK_LIST_VERSION"),
                        help="значение x-userlists-versions (клиент шлёт его на каждый запрос страницы)")

    ap = argparse.ArgumentParser(description="Bedrock Cape Manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", parents=[common], help="плащи и признак владения")
    sub.add_parser("status", parents=[common], help="что надето в слотах 1-5")
    sub.add_parser("diagnose", parents=[common], help="где обрывается цепочка, если плащей 0 (только чтение)")
    sub.add_parser("sync", parents=[common], help="зачислить покупки Microsoft Store в инвентарь Bedrock")
    sub.add_parser("snapshot", parents=[common], help="сохранить снимок инвентаря")
    d = sub.add_parser("diff", parents=[common], help="сравнить два снимка инвентаря")
    d.add_argument("file_a")
    d.add_argument("file_b")
    d.add_argument("--names", action="store_true", help="подписать плащи названиями (нужен вход)")
    e = sub.add_parser("equip", parents=[common], help="надеть плащ из вашего списка")
    e.add_argument("-c", "--cape", required=True, help="часть названия или UUID")
    e.add_argument("-p", "--profile", type=int, default=1, help="слот 1-5")
    e.add_argument("--add-if-missing", action="store_true", help="добавить плащ, если в слоте его нет")
    e.add_argument("--create-slot", action="store_true",
                   help="создать слот, если на сервере его нет (аккаунт без сохранённого внешнего вида)")
    e.add_argument("--replace-id", help="id элемента слота, который нужно заменить (плащ вне списка магазина)")
    e.add_argument("--apply", action="store_true", help="записать (без этого показывается только план)")
    r = sub.add_parser("restore", parents=[common], help="восстановить из резервной копии")
    r.add_argument("file")
    r.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    try:
        if args.cmd == "diff" and not args.names:
            cmd_diff(None, args)  # работает офлайн, вход не нужен
        else:
            client = Client(args.version, args.inventory_etag, args.list_version)
            {"list": cmd_list, "status": cmd_status, "equip": cmd_equip, "restore": cmd_restore,
             "diagnose": cmd_diagnose, "sync": cmd_sync, "snapshot": cmd_snapshot,
             "diff": cmd_diff}[args.cmd](client, args)
    except CapeError as e:
        print(f"[!] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
