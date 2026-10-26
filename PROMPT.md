Роль: опытный разработчик C#/.NET. Напиши небольшое Windows-приложение «Bedrock Cape Sync».
Одна задача: зачислить покупки и коды аккаунта Microsoft в инвентарь Minecraft Bedrock (sync)
и показать, какие плащи теперь на аккаунте.

К промпту приложены bedrock_auth.py и bedrock_cape.py. Это рабочий референс, проверенный на реальных
аккаунтах. Переноси логику оттуда один к одному. НЕ придумывай эндпоинты, поля и заголовки, которых нет
в референсе или в этом промпте. Если чего-то не хватает, задай вопрос вместо догадки.
НЕ переноси: сохранение токенов в файл, equip, restore, snapshot, diagnose, backup, запросы persona,
CLI, переменные окружения. Нужен только sync.

## Технологии
- .NET 8, Windows Forms (net8.0-windows). Без сторонних NuGet-пакетов: HttpClient + System.Text.Json.
- Один exe: `dotnet publish -c Release` должен давать один файл (OutputType=WinExe,
  RuntimeIdentifier=win-x64, SelfContained=false, PublishSingleFile=true,
  IncludeNativeLibrariesForSelfExtract=true). Консольного окна быть не должно.
- Файлы: .csproj, Program.cs, MainForm.cs (только UI), BedrockSync.cs (вся логика без UI).
- Верни ПОЛНЫЙ код всех файлов и команду сборки. Без заглушек и TODO.
- Константы вынеси наверх BedrockSync.cs: GameVersion = "1.26.52", PanCapeUuid и все URL.

## Интерфейс (тексты на русском, одно окно, фиксированный размер)
1. Начало: кнопка «Войти через Microsoft».
2. После нажатия: крупно показать код и ссылку https://www.microsoft.com/link?otc={user_code},
   кнопка «Копировать ссылку» (Clipboard) и подсказка «Откройте ссылку в браузере, где вы вошли в нужный
   аккаунт Microsoft, или в приватном окне». Ниже статус «Ожидаю подтверждения…».
3. После подтверждения: полоса прогресса (marquee) и текущий шаг («Вход Xbox», «Вход PlayFab»,
   «Сессия Minecraft», «Синхронизация»).
4. Результат:
   До синхронизации: X позиций
   После синхронизации: Y позиций (+Z)
   Плащи (N): список названий, по одному в строке (ListBox только для чтения).
5. Кнопка «Войти в другой аккаунт» возвращает к шагу 1. Закрытие окна отменяет всё.
Больше ничего: без настроек, тем, иконок, лога, отладочного режима.

## Логика (по порядку, как в референсе)

### A. Вход Microsoft (device_code_login)
- POST https://login.live.com/oauth20_connect.srf, form: client_id=000000004c12ae6f,
  scope=service::user.auth.xboxlive.com::MBI_SSL, response_type=device_code
  -> user_code, device_code, interval (по умолчанию 5), expires_in (по умолчанию 900).
- Опрос каждые interval секунд: POST https://login.live.com/oauth20_token.srf, form: client_id,
  grant_type=urn:ietf:params:oauth:grant-type:device_code, device_code.
  access_token -> успех; error=authorization_pending -> ждать; error=slow_down -> interval += 5;
  иначе ошибка (error_description); по истечении expires_in -> «Код истёк».
- refresh_token не использовать и не хранить.

### B. Токены Xbox (xbl_user_token, xsts_token)
Для всех запросов к *.auth.xboxlive.com заголовки: Content-Type: application/json,
Accept: application/json, x-xbl-contract-version: 1.
- POST https://user.auth.xboxlive.com/user/authenticate:
  {"Properties":{"AuthMethod":"RPS","SiteName":"user.auth.xboxlive.com","RpsTicket":"t=<access_token>"},
   "RelyingParty":"http://auth.xboxlive.com","TokenType":"JWT"}. Если не 200, повтори с "d=<access_token>".
  Ответ: Token = userToken.
- POST https://xsts.auth.xboxlive.com/xsts/authorize:
  {"Properties":{"SandboxId":"RETAIL","UserTokens":[userToken]},"RelyingParty":<RP>,"TokenType":"JWT"}.
  Ответ: Token, DisplayClaims.xui[0].uhs, DisplayClaims.xui[0].xid (может отсутствовать).
  Нужно ДВА токена: RP1 = "http://playfab.xboxlive.com/" (для PlayFab) и
  RP2 = "https://b980a380.minecraft.playfabapi.com/" (для redeem). Строки RP менять нельзя,
  слэш в конце обязателен. Токен Xbox = "XBL3.0 x={uhs};{Token}".
- На 401 прочитай XErr: 2148916233 «У аккаунта нет профиля Xbox, создайте его на xbox.com»;
  2148916235 «Xbox Live недоступен в стране аккаунта»; 2148916236/2148916237 «Нужна проверка возраста»;
  2148916238 «Детский аккаунт: его должен добавить в семью взрослый»; иначе показать код XErr.

### C. PlayFab (playfab_login)
- POST https://20ca2.playfabapi.com/Client/LoginWithXbox:
  {"TitleId":"20CA2","CreateAccount":false,"InfoRequestParameters":{"GetPlayerProfile":true,
   "GetUserAccountInfo":true},"XboxToken":"<токен для RP1>"}.
  Успех: data.PlayFabId, data.SessionTicket, data.InfoResultPayload.AccountInfo.XboxInfo.XboxUserId (xuid).
  Если xuid нет, возьми xid из шага B; если и его нет, ошибка.
- Если ответ содержит error == "AccountNotFound" (или errorCode == 1001), покажи диалог Да/Нет:
  «У этого аккаунта ещё нет профиля Minecraft Bedrock. Игра создаёт его при первом запуске.
  Создать профиль сейчас?». «Да» -> повтори этот запрос с CreateAccount=true. «Нет» -> остановись.

### D. Сессия Minecraft (start_franchise_session, extract_mctoken)
- deviceId (без хранения): первые 32 hex-символа SHA-256 строки "bedrock-cape-sync:" + PlayFabId.
  Одно и то же значение идёт в session/start и в redeem (ClientId). Это обязательно.
- POST https://authorization.franchise.minecraft-services.net/api/v1.0/session/start:
  {"device":{"applicationType":"MinecraftPE","capabilities":["RayTracing"],"gameVersion":GameVersion,
   "id":deviceId,"memory":"17179869184","platform":"Windows10","playFabTitleId":"20CA2",
   "storePlatform":"uwp.store","treatmentOverrides":null,"type":"Windows10"},
   "user":{"language":<две буквы из языка ОС>,"languageCode":<CultureInfo.CurrentUICulture.Name, иначе en-US>,
   "regionCode":<RegionInfo страны ОС, две буквы, иначе US>,"token":<SessionTicket>,"tokenType":"PlayFab"}}.
- Из ответа: result.authorizationHeader (или result.mcToken); если нет префикса "MCToken ", добавь его.
  Далее во всех запросах к minecraft-services заголовки: Authorization: <это значение>,
  User-Agent: libhttpclient/1.0.0.0 (добавляй через TryAddWithoutValidation).

### E. Адреса сервисов
- GET https://client.discovery.minecraft-services.net/api/v1.0/discovery/MinecraftPE/builds/{GameVersion}
  (без авторизации): entitlementsUri = result.serviceEnvironments.entitlements.prod.serviceUri,
  storeUri = result.serviceEnvironments.store.prod.serviceUri (без завершающего "/").

### F. Sync
1. GET {entitlementsUri}/api/v1.0/player/inventory?includeReceipt=true ->
   result.inventory.inventoryVersion (versionBefore) и длина result.inventory.entitlements (countBefore).
2. POST {entitlementsUri}/api/v1.0/transaction/redeem/xbox. Заголовки: Authorization, User-Agent,
   Content-Type: application/json, inventoryetag: "" и x-userlists-versions: "" (отправлять ПУСТЫМИ,
   но присутствующими; если HttpClient не даёт, используй TryAddWithoutValidation). Тело:
   {"CustomTags":{"BuildPlat":8,"ClientId":deviceId,"DeviceSessionId":<новый Guid строкой>,
   "DnAPlat":"Win,D,,GC","Seq":1,"TitleId":"20CA2","Xuid":xuid,"editionType":"win10"},
   "Redemption":{"MarketplaceData":{"userId":xuid,"xboxAuthentication":{"XboxToken":<токен для RP2>}}}}.
   200 = успех (тело ответа не нужно). Иначе покажи предупреждение с HTTP-статусом и первыми 300
   символами тела и продолжай.
3. POST {storeUri}/api/v1.0/inventory/refresh. Заголовки: Authorization, User-Agent,
   inventoryetag: versionBefore (если есть). Без тела. Ошибку игнорируй.
4. GET inventory ещё раз -> countAfter, versionAfter.

### G. Список плащей
- POST {storeUri}/api/v2.0/layout/pages/DressingRoom_Capes. Заголовки: Authorization, User-Agent,
  Content-Type: application/json, inventoryetag: versionAfter.
  Тело: {"entitlements":[],"inventoryVersion":"<versionAfter>"}.
- Обойди JSON рекурсивно: любой объект с "pieceType":"persona_capes" — плащ. Поля: title, ownership,
  packIdentity (массив; нужен элемент с type == "persona_piece", у него uuid).
- Выводи названия (title) тех плащей, у которых ownership == "Purchased" ИЛИ uuid ==
  "ef479b6d-7072-47aa-8985-0f025cd24cdb" (The Pan Cape: известный баг API, по факту он есть у всех).
  Без пометок «есть/нет». Без дублей, сортировка по алфавиту.

## Жёсткие ограничения
- Ничего не сохранять: ни токены, ни refresh_token, ни логи, ни настройки. Никаких файлов, реестра,
  Credential Manager. Всё только в памяти и исчезает при закрытии.
- Использовать только перечисленные запросы. Никаких PUT, equip, изменения внешнего вида.
- Ничего не писать в Console/Debug.
- Любая ошибка: красный текст в окне («что случилось» + этап, HTTP-статус, до 300 символов тела) и кнопка
  «Начать заново». Токены, коды и заголовки Authorization в сообщения не включать.
- HttpClient один на приложение, таймаут 30 секунд, async/await без блокировки UI.

## Проверь себя перед ответом
- Нет ни одной записи на диск/в реестр, нет NuGet-пакетов.
- ClientId в redeem равен device.id из session/start.
- Выведены оба числа («до» и «после») и список названий без пометок.
- Для RP1 и RP2 запрашиваются отдельные токены XSTS.
- Выданы все файлы целиком и команда сборки.