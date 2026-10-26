using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using BedrockCapeSync.Resources;

namespace BedrockCapeSync;

public record DeviceCodeResult(
    string UserCode,
    string DeviceCode,
    string VerificationUri,
    int Interval,
    int ExpiresIn
);

public record XstsResult(
    string Token,
    string Uhs,
    string? Xid
)
{
    public string FormattedToken => $"XBL3.0 x={Uhs};{Token}";
}

public record PlayFabSession(
    string PlayFabId,
    string SessionTicket,
    string Xuid
);

public record CapeItem(
    string Title,
    string Uuid,
    bool IsNew
);

public record SyncResult(
    int CountBefore,
    int CountAfter,
    List<CapeItem> Capes
);

public class BedrockSyncException : Exception
{
    public string Stage { get; }
    public int? StatusCode { get; }
    public string? ResponseSnippet { get; }

    public BedrockSyncException(
        string stage,
        string message,
        int? statusCode = null,
        string? responseSnippet = null,
        Exception? innerException = null)
        : base(message, innerException)
    {
        Stage = stage;
        StatusCode = statusCode;
        ResponseSnippet = responseSnippet;
    }

    public string ToDisplayString()
    {
        var sb = new StringBuilder();
        sb.AppendLine(Strings.Format("LabelStage", Stage));
        sb.AppendLine(Strings.Format("LabelError", Message));
        if (StatusCode.HasValue)
        {
            sb.AppendLine(Strings.Format("LabelHttpStatus", StatusCode.Value));
        }
        if (!string.IsNullOrWhiteSpace(ResponseSnippet))
        {
            sb.AppendLine(Strings.Get("LabelServerResponse"));
            sb.AppendLine(ResponseSnippet);
        }
        return sb.ToString().TrimEnd();
    }
}

public static class BedrockSync
{
    public const string GameVersion = "1.26.52";
    public const string PanCapeUuid = "ef479b6d-7072-47aa-8985-0f025cd24cdb";

    public const string MsaClientId = "000000004c12ae6f";
    public const string MsaScope = "service::user.auth.xboxlive.com::MBI_SSL";
    public const string MsaDeviceUrl = "https://login.live.com/oauth20_connect.srf";
    public const string MsaTokenUrl = "https://login.live.com/oauth20_token.srf";

    public const string XblUserUrl = "https://user.auth.xboxlive.com/user/authenticate";
    public const string XstsUrl = "https://xsts.auth.xboxlive.com/xsts/authorize";

    public const string PlayFabRp = "http://playfab.xboxlive.com/";
    public const string PlayFabTitleRp = "https://b980a380.minecraft.playfabapi.com/";
    public const string PlayFabTitleId = "20CA2";
    public const string PlayFabLoginUrl = "https://20ca2.playfabapi.com/Client/LoginWithXbox";

    public const string FranchiseSessionUrl = "https://authorization.franchise.minecraft-services.net/api/v1.0/session/start";
    public const string DiscoveryBuildsUrlTemplate = "https://client.discovery.minecraft-services.net/api/v1.0/discovery/MinecraftPE/builds/{0}";

    public const string UserAgent = "libhttpclient/1.0.0.0";

    private static readonly HttpClient HttpClient = new()
    {
        Timeout = TimeSpan.FromSeconds(30)
    };

    private static string TruncateSnippet(string text, int maxChars = 300)
    {
        if (string.IsNullOrEmpty(text))
        {
            return "";
        }
        return text.Length <= maxChars ? text : text[..maxChars];
    }

    public static async Task<DeviceCodeResult> StartDeviceCodeLoginAsync(CancellationToken cancellationToken)
    {
        var parameters = new Dictionary<string, string>
        {
            ["client_id"] = MsaClientId,
            ["scope"] = MsaScope,
            ["response_type"] = "device_code"
        };

        using var request = new HttpRequestMessage(HttpMethod.Post, MsaDeviceUrl)
        {
            Content = new FormUrlEncodedContent(parameters)
        };

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var text = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageMsa"),
                Strings.Get("ErrDeviceCodeFailed"),
                (int)response.StatusCode,
                TruncateSnippet(text));
        }

        using var doc = JsonDocument.Parse(text);
        var root = doc.RootElement;

        string userCode = root.GetProperty("user_code").GetString() ?? "";
        string deviceCode = root.GetProperty("device_code").GetString() ?? "";
        string verificationUri = root.TryGetProperty("verification_uri", out var vUri)
            ? (vUri.GetString() ?? "https://microsoft.com/link")
            : "https://microsoft.com/link";

        int interval = root.TryGetProperty("interval", out var intProp) ? intProp.GetInt32() : 5;
        if (interval <= 0)
        {
            interval = 5;
        }

        int expiresIn = root.TryGetProperty("expires_in", out var expProp) ? expProp.GetInt32() : 900;
        if (expiresIn <= 0)
        {
            expiresIn = 900;
        }

        return new DeviceCodeResult(userCode, deviceCode, verificationUri, interval, expiresIn);
    }

    public static async Task<string> PollMsaTokenAsync(
        string deviceCode,
        int intervalSeconds,
        int expiresInSeconds,
        CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow.AddSeconds(expiresInSeconds);
        int currentInterval = intervalSeconds > 0 ? intervalSeconds : 5;

        while (DateTime.UtcNow < deadline)
        {
            await Task.Delay(TimeSpan.FromSeconds(currentInterval), cancellationToken);

            var parameters = new Dictionary<string, string>
            {
                ["client_id"] = MsaClientId,
                ["grant_type"] = "urn:ietf:params:oauth:grant-type:device_code",
                ["device_code"] = deviceCode
            };

            using var request = new HttpRequestMessage(HttpMethod.Post, MsaTokenUrl)
            {
                Content = new FormUrlEncodedContent(parameters)
            };

            using var response = await HttpClient.SendAsync(request, cancellationToken);
            var text = await response.Content.ReadAsStringAsync(cancellationToken);

            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;

            if (response.IsSuccessStatusCode && root.TryGetProperty("access_token", out var tokenProp))
            {
                string? accessToken = tokenProp.GetString();
                if (!string.IsNullOrEmpty(accessToken))
                {
                    return accessToken;
                }
            }

            if (root.TryGetProperty("error", out var errorProp))
            {
                string error = errorProp.GetString() ?? "";
                if (error == "authorization_pending")
                {
                    continue;
                }

                if (error == "slow_down")
                {
                    currentInterval += 5;
                    continue;
                }

                string desc = root.TryGetProperty("error_description", out var descProp)
                    ? (descProp.GetString() ?? error)
                    : error;

                throw new BedrockSyncException(
                    Strings.Get("StageMsa"),
                    Strings.Format("ErrMsaAborted", desc),
                    (int)response.StatusCode,
                    TruncateSnippet(desc));
            }

            throw new BedrockSyncException(
                Strings.Get("StageMsa"),
                Strings.Get("ErrMsaTokenFailed"),
                (int)response.StatusCode,
                TruncateSnippet(text));
        }

        throw new BedrockSyncException(
            Strings.Get("StageMsa"),
            Strings.Get("ErrCodeExpired"));
    }

    public static async Task<string> AuthenticateXboxUserAsync(string msaAccessToken, CancellationToken cancellationToken)
    {
        string[] prefixes = ["t=", "d="];
        string lastSnippet = "";
        int? lastStatus = null;

        foreach (var prefix in prefixes)
        {
            var body = new
            {
                Properties = new
                {
                    AuthMethod = "RPS",
                    SiteName = "user.auth.xboxlive.com",
                    RpsTicket = prefix + msaAccessToken
                },
                RelyingParty = "http://auth.xboxlive.com",
                TokenType = "JWT"
            };

            using var request = new HttpRequestMessage(HttpMethod.Post, XblUserUrl);
            request.Headers.Accept.ParseAdd("application/json");
            request.Headers.TryAddWithoutValidation("x-xbl-contract-version", "1");
            request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

            using var response = await HttpClient.SendAsync(request, cancellationToken);
            var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

            if (response.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(responseText);
                if (doc.RootElement.TryGetProperty("Token", out var tokenProp))
                {
                    string? token = tokenProp.GetString();
                    if (!string.IsNullOrEmpty(token))
                    {
                        return token;
                    }
                }
            }

            lastStatus = (int)response.StatusCode;
            lastSnippet = TruncateSnippet(responseText);
        }

        throw new BedrockSyncException(
            Strings.Get("StageXbox"),
            Strings.Get("ErrXboxAuthFailed"),
            lastStatus,
            lastSnippet);
    }

    public static async Task<XstsResult> AuthorizeXstsAsync(string userToken, string relyingParty, CancellationToken cancellationToken)
    {
        var body = new
        {
            Properties = new
            {
                SandboxId = "RETAIL",
                UserTokens = new[] { userToken }
            },
            RelyingParty = relyingParty,
            TokenType = "JWT"
        };

        using var request = new HttpRequestMessage(HttpMethod.Post, XstsUrl);
        request.Headers.Accept.ParseAdd("application/json");
        request.Headers.TryAddWithoutValidation("x-xbl-contract-version", "1");
        request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

        if (response.StatusCode == HttpStatusCode.Unauthorized)
        {
            long xerr = 0;
            try
            {
                using var errDoc = JsonDocument.Parse(responseText);
                if (errDoc.RootElement.TryGetProperty("XErr", out var xerrProp))
                {
                    if (xerrProp.ValueKind == JsonValueKind.Number)
                    {
                        xerr = xerrProp.GetInt64();
                    }
                    else if (long.TryParse(xerrProp.GetString(), out var parsedXerr))
                    {
                        xerr = parsedXerr;
                    }
                }
            }
            catch
            {
                // Игнорируем ошибки парсинга XErr
            }

            string msg = xerr switch
            {
                2148916233 => Strings.Get("ErrXboxNoProfile"),
                2148916235 => Strings.Get("ErrXboxCountry"),
                2148916236 or 2148916237 => Strings.Get("ErrXboxAgeVerification"),
                2148916238 => Strings.Get("ErrXboxChildAccount"),
                _ => Strings.Format("ErrXboxGeneric", xerr)
            };

            throw new BedrockSyncException(
                Strings.Get("StageXbox"),
                msg,
                401,
                TruncateSnippet(responseText));
        }

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageXbox"),
                Strings.Get("ErrXstsFailed"),
                (int)response.StatusCode,
                TruncateSnippet(responseText));
        }

        using var doc = JsonDocument.Parse(responseText);
        var root = doc.RootElement;
        string token = root.GetProperty("Token").GetString() ?? "";
        var xui = root.GetProperty("DisplayClaims").GetProperty("xui")[0];
        string uhs = xui.GetProperty("uhs").GetString() ?? "";

        string? xid = null;
        if (xui.TryGetProperty("xid", out var xidProp))
        {
            xid = xidProp.ValueKind == JsonValueKind.Number ? xidProp.GetRawText() : xidProp.GetString();
        }

        return new XstsResult(token, uhs, xid);
    }

    public static async Task<PlayFabSession> PlayFabLoginAsync(
        string xboxTokenRP1,
        string? fallbackXid,
        Func<Task<bool>> confirmCreateAccount,
        CancellationToken cancellationToken)
    {
        bool createAccount = false;

        while (true)
        {
            var body = new
            {
                TitleId = PlayFabTitleId,
                CreateAccount = createAccount,
                InfoRequestParameters = new
                {
                    GetPlayerProfile = true,
                    GetUserAccountInfo = true
                },
                XboxToken = xboxTokenRP1
            };

            using var request = new HttpRequestMessage(HttpMethod.Post, PlayFabLoginUrl);
            request.Headers.Accept.ParseAdd("application/json");
            request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
            request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

            using var response = await HttpClient.SendAsync(request, cancellationToken);
            var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

            using var doc = JsonDocument.Parse(responseText);
            var root = doc.RootElement;

            bool isAccountNotFound = false;
            if (root.TryGetProperty("error", out var errorProp) && errorProp.GetString() == "AccountNotFound")
            {
                isAccountNotFound = true;
            }
            else if (root.TryGetProperty("errorCode", out var codeProp) && codeProp.GetInt32() == 1001)
            {
                isAccountNotFound = true;
            }

            if (isAccountNotFound && !createAccount)
            {
                bool proceed = await confirmCreateAccount();
                if (proceed)
                {
                    createAccount = true;
                    continue;
                }
                throw new OperationCanceledException(Strings.Get("ErrProfileCancelled"));
            }

            if (!response.IsSuccessStatusCode || !root.TryGetProperty("data", out var dataProp))
            {
                string errorMsg = root.TryGetProperty("errorMessage", out var msgProp)
                    ? (msgProp.GetString() ?? Strings.Get("ErrPlayFabGeneric"))
                    : Strings.Get("ErrPlayFabGeneric");

                throw new BedrockSyncException(
                    Strings.Get("StagePlayFab"),
                    errorMsg,
                    (int)response.StatusCode,
                    TruncateSnippet(responseText));
            }

            string playFabId = dataProp.GetProperty("PlayFabId").GetString() ?? "";
            string sessionTicket = dataProp.GetProperty("SessionTicket").GetString() ?? "";

            string? xuid = null;
            if (dataProp.TryGetProperty("InfoResultPayload", out var payload) &&
                payload.TryGetProperty("AccountInfo", out var accInfo) &&
                accInfo.TryGetProperty("XboxInfo", out var xboxInfo) &&
                xboxInfo.TryGetProperty("XboxUserId", out var xuidProp))
            {
                xuid = xuidProp.GetString();
            }

            if (string.IsNullOrEmpty(xuid))
            {
                xuid = fallbackXid;
            }

            if (string.IsNullOrEmpty(xuid))
            {
                throw new BedrockSyncException(
                    Strings.Get("StagePlayFab"),
                    Strings.Get("ErrXuidNotFound"),
                    (int)response.StatusCode);
            }

            return new PlayFabSession(playFabId, sessionTicket, xuid);
        }
    }

    public static async Task<string> StartFranchiseSessionAsync(
        string playFabId,
        string sessionTicket,
        string deviceId,
        CancellationToken cancellationToken)
    {
        string langCode = string.IsNullOrWhiteSpace(CultureInfo.CurrentUICulture.Name)
            ? "en-US"
            : CultureInfo.CurrentUICulture.Name;

        string lang = langCode.Split('-')[0].ToLowerInvariant();
        if (string.IsNullOrWhiteSpace(lang) || lang.Length < 2)
        {
            lang = "en";
        }

        string region = "US";
        try
        {
            var reg = RegionInfo.CurrentRegion;
            if (!string.IsNullOrWhiteSpace(reg.TwoLetterISORegionName))
            {
                region = reg.TwoLetterISORegionName.ToUpperInvariant();
            }
        }
        catch
        {
            region = "US";
        }

        var body = new
        {
            device = new
            {
                applicationType = "MinecraftPE",
                capabilities = new[] { "RayTracing" },
                gameVersion = GameVersion,
                id = deviceId,
                memory = "17179869184",
                platform = "Windows10",
                playFabTitleId = PlayFabTitleId,
                storePlatform = "uwp.store",
                treatmentOverrides = (object?)null,
                type = "Windows10"
            },
            user = new
            {
                language = lang,
                languageCode = langCode,
                regionCode = region,
                token = sessionTicket,
                tokenType = "PlayFab"
            }
        };

        using var request = new HttpRequestMessage(HttpMethod.Post, FranchiseSessionUrl);
        request.Headers.Accept.ParseAdd("application/json");
        request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageFranchise"),
                Strings.Get("ErrFranchiseSession"),
                (int)response.StatusCode,
                TruncateSnippet(responseText));
        }

        using var doc = JsonDocument.Parse(responseText);
        var target = doc.RootElement.TryGetProperty("result", out var res) ? res : doc.RootElement;

        string? token = null;
        if (target.TryGetProperty("authorizationHeader", out var authHeaderProp))
        {
            token = authHeaderProp.GetString();
        }
        else if (target.TryGetProperty("mcToken", out var mcTokenProp))
        {
            token = mcTokenProp.GetString();
        }

        if (string.IsNullOrEmpty(token))
        {
            throw new BedrockSyncException(
                Strings.Get("StageFranchise"),
                Strings.Get("ErrMcTokenNotFound"),
                (int)response.StatusCode);
        }

        return token.StartsWith("MCToken ", StringComparison.OrdinalIgnoreCase) ? token : $"MCToken {token}";
    }

    public static async Task<(string EntitlementsUri, string StoreUri)> GetServiceDiscoveryAsync(CancellationToken cancellationToken)
    {
        string url = string.Format(DiscoveryBuildsUrlTemplate, GameVersion);
        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageSync"),
                Strings.Get("ErrDiscoveryFailed"),
                (int)response.StatusCode,
                TruncateSnippet(responseText));
        }

        using var doc = JsonDocument.Parse(responseText);
        var envs = doc.RootElement.GetProperty("result").GetProperty("serviceEnvironments");
        string entitlements = envs.GetProperty("entitlements").GetProperty("prod").GetProperty("serviceUri").GetString() ?? "";
        string store = envs.GetProperty("store").GetProperty("prod").GetProperty("serviceUri").GetString() ?? "";

        return (entitlements.TrimEnd('/'), store.TrimEnd('/'));
    }

    public static async Task<(string? Version, int Count)> GetPlayerInventoryAsync(
        string entitlementsUri,
        string mcToken,
        CancellationToken cancellationToken)
    {
        string url = $"{entitlementsUri}/api/v1.0/player/inventory?includeReceipt=true";
        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.TryAddWithoutValidation("Authorization", mcToken);
        request.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
        request.Headers.Accept.ParseAdd("application/json");

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageSync"),
                Strings.Get("ErrInventoryFailed"),
                (int)response.StatusCode,
                TruncateSnippet(responseText));
        }

        using var doc = JsonDocument.Parse(responseText);
        var inv = doc.RootElement.GetProperty("result").GetProperty("inventory");
        string? version = inv.TryGetProperty("inventoryVersion", out var v) ? v.GetString() : null;
        int count = inv.TryGetProperty("entitlements", out var ent) && ent.ValueKind == JsonValueKind.Array
            ? ent.GetArrayLength()
            : 0;

        return (version, count);
    }

    public static async Task RedeemXboxAsync(
        string entitlementsUri,
        string mcToken,
        string deviceId,
        string xuid,
        string xboxTokenRP2,
        Action<string>? onWarning,
        CancellationToken cancellationToken)
    {
        string url = $"{entitlementsUri}/api/v1.0/transaction/redeem/xbox";
        using var request = new HttpRequestMessage(HttpMethod.Post, url);
        request.Headers.TryAddWithoutValidation("Authorization", mcToken);
        request.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
        request.Headers.TryAddWithoutValidation("inventoryetag", "");
        request.Headers.TryAddWithoutValidation("x-userlists-versions", "");

        var body = new
        {
            CustomTags = new
            {
                BuildPlat = 8,
                ClientId = deviceId,
                DeviceSessionId = Guid.NewGuid().ToString(),
                DnAPlat = "Win,D,,GC",
                Seq = 1,
                TitleId = PlayFabTitleId,
                Xuid = xuid,
                editionType = "win10"
            },
            Redemption = new
            {
                MarketplaceData = new
                {
                    userId = xuid,
                    xboxAuthentication = new
                    {
                        XboxToken = xboxTokenRP2
                    }
                }
            }
        };

        request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            var responseText = await response.Content.ReadAsStringAsync(cancellationToken);
            string snippet = TruncateSnippet(responseText);
            string warning = Strings.Format("WarningRedeem", (int)response.StatusCode, snippet);
            onWarning?.Invoke(warning);
        }
    }

    public static async Task RefreshInventoryAsync(
        string storeUri,
        string mcToken,
        string? versionBefore,
        CancellationToken cancellationToken)
    {
        try
        {
            string url = $"{storeUri}/api/v1.0/inventory/refresh";
            using var request = new HttpRequestMessage(HttpMethod.Post, url);
            request.Headers.TryAddWithoutValidation("Authorization", mcToken);
            request.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
            if (!string.IsNullOrEmpty(versionBefore))
            {
                request.Headers.TryAddWithoutValidation("inventoryetag", versionBefore);
            }

            using var response = await HttpClient.SendAsync(request, cancellationToken);
        }
        catch
        {
            // Ошибку игнорируем согласно требованиям спецификации
        }
    }

    public static async Task<List<(string Title, string Uuid)>> GetCapesDetailedAsync(
        string storeUri,
        string mcToken,
        string? version,
        CancellationToken cancellationToken)
    {
        string url = $"{storeUri}/api/v2.0/layout/pages/DressingRoom_Capes";
        using var request = new HttpRequestMessage(HttpMethod.Post, url);
        request.Headers.TryAddWithoutValidation("Authorization", mcToken);
        request.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
        if (!string.IsNullOrEmpty(version))
        {
            request.Headers.TryAddWithoutValidation("inventoryetag", version);
        }

        var body = new
        {
            entitlements = Array.Empty<object>(),
            inventoryVersion = version ?? ""
        };

        request.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");

        using var response = await HttpClient.SendAsync(request, cancellationToken);
        var responseText = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new BedrockSyncException(
                Strings.Get("StageCapes"),
                Strings.Get("ErrCapesFailed"),
                (int)response.StatusCode,
                TruncateSnippet(responseText));
        }

        using var doc = JsonDocument.Parse(responseText);
        var rawCapes = new List<(string Title, string Uuid, string Ownership)>();
        FindCapesRecursive(doc.RootElement, rawCapes);

        var ownedCapes = rawCapes
            .Where(c => string.Equals(c.Ownership, "Purchased", StringComparison.OrdinalIgnoreCase) ||
                        string.Equals(c.Uuid, PanCapeUuid, StringComparison.OrdinalIgnoreCase))
            .Select(c => (Title: string.IsNullOrWhiteSpace(c.Title) ? c.Uuid : c.Title, Uuid: c.Uuid.ToLowerInvariant()))
            .GroupBy(c => c.Uuid)
            .Select(g => g.First())
            .ToList();

        return ownedCapes;
    }

    private static void FindCapesRecursive(JsonElement element, List<(string Title, string Uuid, string Ownership)> capes)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            if (element.TryGetProperty("pieceType", out var pieceType) && pieceType.GetString() == "persona_capes")
            {
                string title = element.TryGetProperty("title", out var titleProp) ? (titleProp.GetString() ?? "") : "";
                string ownership = element.TryGetProperty("ownership", out var ownProp) ? (ownProp.GetString() ?? "") : "";
                string uuid = "";

                if (element.TryGetProperty("packIdentity", out var packIdProp) && packIdProp.ValueKind == JsonValueKind.Array)
                {
                    foreach (var item in packIdProp.EnumerateArray())
                    {
                        if (item.TryGetProperty("type", out var typeProp) &&
                            typeProp.GetString() == "persona_piece" &&
                            item.TryGetProperty("uuid", out var uuidProp))
                        {
                            uuid = uuidProp.GetString() ?? "";
                            break;
                        }
                    }
                }

                capes.Add((title, uuid, ownership));
            }

            foreach (var prop in element.EnumerateObject())
            {
                FindCapesRecursive(prop.Value, capes);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                FindCapesRecursive(item, capes);
            }
        }
    }

    public static async Task<SyncResult> RunSyncWorkflowAsync(
        string msaAccessToken,
        Action<string> onStepChanged,
        Func<Task<bool>> confirmCreateAccount,
        Action<string>? onWarning,
        CancellationToken cancellationToken)
    {
        onStepChanged(Strings.Get("StepXboxLogin"));
        string userToken = await AuthenticateXboxUserAsync(msaAccessToken, cancellationToken);
        var xstsRP1 = await AuthorizeXstsAsync(userToken, PlayFabRp, cancellationToken);
        var xstsRP2 = await AuthorizeXstsAsync(userToken, PlayFabTitleRp, cancellationToken);

        onStepChanged(Strings.Get("StepPlayFabLogin"));
        var playFab = await PlayFabLoginAsync(xstsRP1.FormattedToken, xstsRP1.Xid ?? xstsRP2.Xid, confirmCreateAccount, cancellationToken);

        onStepChanged(Strings.Get("StepFranchiseSession"));
        byte[] hash = SHA256.HashData(Encoding.UTF8.GetBytes("bedrock-cape-sync:" + playFab.PlayFabId));
        string deviceId = Convert.ToHexString(hash).ToLowerInvariant()[..32];

        string mcToken = await StartFranchiseSessionAsync(playFab.PlayFabId, playFab.SessionTicket, deviceId, cancellationToken);

        var (entitlementsUri, storeUri) = await GetServiceDiscoveryAsync(cancellationToken);

        onStepChanged(Strings.Get("StepSync"));
        var (verBefore, countBefore) = await GetPlayerInventoryAsync(entitlementsUri, mcToken, cancellationToken);

        // Получаем список плащей ДО синхронизации
        List<(string Title, string Uuid)> capesBefore = [];
        try
        {
            capesBefore = await GetCapesDetailedAsync(storeUri, mcToken, verBefore, cancellationToken);
        }
        catch
        {
            capesBefore = [];
        }
        var beforeUuids = capesBefore.Select(c => c.Uuid).ToHashSet(StringComparer.OrdinalIgnoreCase);

        // Синхронизация (redeem + refresh)
        await RedeemXboxAsync(entitlementsUri, mcToken, deviceId, playFab.Xuid, xstsRP2.FormattedToken, onWarning, cancellationToken);
        await RefreshInventoryAsync(storeUri, mcToken, verBefore, cancellationToken);

        var (verAfter, countAfter) = await GetPlayerInventoryAsync(entitlementsUri, mcToken, cancellationToken);

        // Получаем список плащей ПОСЛЕ синхронизации
        var capesAfter = await GetCapesDetailedAsync(storeUri, mcToken, verAfter, cancellationToken);

        // Сопоставляем и выявляем новые плащи
        var capes = capesAfter
            .Select(c => new CapeItem(c.Title, c.Uuid, !beforeUuids.Contains(c.Uuid)))
            .OrderByDescending(c => c.IsNew)
            .ThenBy(c => c.Title, StringComparer.CurrentCultureIgnoreCase)
            .ToList();

        return new SyncResult(countBefore, countAfter, capes);
    }
}
