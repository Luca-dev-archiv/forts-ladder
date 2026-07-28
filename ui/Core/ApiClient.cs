using System.IO;
using System.Net.Http;
using System.Text.Json;

namespace FortsLadder.Core;

/// <summary>
/// Talks to the ladder server.
///
/// Live matches and tournaments are *shared* state — several people look at
/// the same thing at the same time. Keeping them on one machine would defeat
/// the point, so both views are server-backed and say so plainly when no
/// server is configured, rather than showing an empty list that looks like
/// "nothing is happening".
///
/// The address is stored next to the identity file so it survives a restart.
/// Reading (`/live`, `/tournaments`) needs no login; anything that changes
/// something does.
/// </summary>
public sealed class ApiClient
{
    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly HttpClient _http = new()
    {
        // Short on purpose: a hanging request must not freeze the window.
        Timeout = TimeSpan.FromSeconds(5),
    };

    public string? BaseUrl { get; private set; }
    public bool Configured => !string.IsNullOrWhiteSpace(BaseUrl);
    public string? LastError { get; private set; }

    /// <summary>
    /// Session token, held instead of a cookie.
    ///
    /// The login happens in Discord — in the browser or in the desktop client
    /// — and neither hands its cookie to this process. So the server issues a
    /// token and it is sent as a bearer header.
    ///
    /// Note what is *not* here: the Discord client secret. The exchange runs
    /// on the server, because a secret shipped inside a downloadable .exe is
    /// not a secret.
    /// </summary>
    public string? Token { get; private set; }
    public bool LoggedIn => !string.IsNullOrWhiteSpace(Token);

    private static string SettingsPath =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "server.txt");

    private static string TokenPath =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "session.txt");

    /// <summary>
    /// The instance clients use unless told otherwise.
    ///
    /// A ladder is only a ladder if everyone is on the same one, so shipping
    /// without a default would mean every player had to run a server to play
    /// against anyone. Overridable in the Live view, and whatever is set there
    /// wins — anyone can run their own.
    /// </summary>
    public const string DefaultBaseUrl = "https://ubuntu.tail5b0dc3.ts.net";

    public ApiClient()
    {
        BaseUrl = Load() ?? DefaultBaseUrl;
        Token = LoadToken();
    }

    private static string? LoadToken()
    {
        try
        {
            return File.Exists(TokenPath)
                ? File.ReadAllText(TokenPath).Trim() : null;
        }
        catch (IOException) { return null; }
    }

    public void SetToken(string? token)
    {
        Token = string.IsNullOrWhiteSpace(token) ? null : token.Trim();
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(TokenPath)!);
            if (Token is null) File.Delete(TokenPath);
            else File.WriteAllText(TokenPath, Token);
        }
        catch (IOException) { /* staying logged in across restarts is a nicety */ }
    }

    private static string? Load()
    {
        try
        {
            return File.Exists(SettingsPath)
                ? File.ReadAllText(SettingsPath).Trim() : null;
        }
        catch (IOException) { return null; }
    }

    public void SetBaseUrl(string? url)
    {
        BaseUrl = string.IsNullOrWhiteSpace(url) ? null : url.Trim().TrimEnd('/');
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
            File.WriteAllText(SettingsPath, BaseUrl ?? "");
        }
        catch (IOException) { /* not being able to remember it is not fatal */ }
    }

    private HttpRequestMessage Request(HttpMethod method, string path,
                                       object? body = null)
    {
        var req = new HttpRequestMessage(method, $"{BaseUrl}{path}");
        if (Token is not null)
            req.Headers.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", Token);
        if (body is not null)
            req.Content = new StringContent(JsonSerializer.Serialize(body),
                                            System.Text.Encoding.UTF8,
                                            "application/json");
        return req;
    }

    public async Task<T?> GetAsync<T>(string path)
    {
        LastError = null;
        if (!Configured) return default;
        try
        {
            using var r = await _http.SendAsync(Request(HttpMethod.Get, path));
            var body = await r.Content.ReadAsStringAsync();
            if (!r.IsSuccessStatusCode)
            {
                LastError = Describe(r.StatusCode, body);
                return default;
            }
            return JsonSerializer.Deserialize<T>(body, Json);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException
                                      or JsonException)
        {
            // Server down, wrong address, garbage response — all the same to
            // the user: it does not work, and here is why.
            LastError = ex.Message;
            return default;
        }
    }

    /// <summary>POST with a JSON body, returning the parsed answer.</summary>
    public async Task<T?> PostAsync<T>(string path, object? body = null)
    {
        LastError = null;
        if (!Configured) return default;
        try
        {
            using var r = await _http.SendAsync(Request(HttpMethod.Post, path, body));
            var text = await r.Content.ReadAsStringAsync();
            if (!r.IsSuccessStatusCode)
            {
                LastError = Describe(r.StatusCode, text);
                return default;
            }
            return JsonSerializer.Deserialize<T>(text, Json);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException
                                      or JsonException)
        {
            LastError = ex.Message;
            return default;
        }
    }

    public async Task<bool> DeleteAsync(string path)
    {
        LastError = null;
        if (!Configured) return false;
        try
        {
            using var r = await _http.SendAsync(Request(HttpMethod.Delete, path));
            if (r.IsSuccessStatusCode) return true;
            LastError = Describe(r.StatusCode,
                                 await r.Content.ReadAsStringAsync());
            return false;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            LastError = ex.Message;
            return false;
        }
    }

    /// <summary>
    /// The server puts the reason in `detail`; showing the raw JSON instead
    /// would hide a perfectly good explanation behind punctuation.
    /// </summary>
    private static string Describe(System.Net.HttpStatusCode code, string body)
    {
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.TryGetProperty("detail", out var d)
                && d.ValueKind == JsonValueKind.String)
                return d.GetString() ?? $"HTTP {(int)code}";
        }
        catch (JsonException) { }
        return $"HTTP {(int)code}: {body}";
    }

    public async Task<bool> PostAsync(string path)
    {
        LastError = null;
        if (!Configured) return false;
        try
        {
            var r = await _http.PostAsync($"{BaseUrl}{path}", null);
            if (r.IsSuccessStatusCode) return true;
            LastError = $"HTTP {(int)r.StatusCode}: {await r.Content.ReadAsStringAsync()}";
            return false;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            LastError = ex.Message;
            return false;
        }
    }

    public Task<bool> PingAsync() => GetAsync<Dictionary<string, JsonElement>>("/health")
        .ContinueWith(t => t.Result is not null);
}

// ---------------------------------------------------------------- Payloads

/// <summary>
/// One player's view of a draft. Mirrors `DraftSession.public_state`.
///
/// What is missing is the point: there is no field for the opponent's pending
/// commander pick, because the server never sends one. `LockedIn` says who has
/// committed, which is all a UI needs to say what it is waiting for.
/// </summary>
public sealed class DraftStateDto
{
    public string Id { get; set; } = "";
    public string? Your_Side { get; set; }
    public Dictionary<string, string> Seats { get; set; } = new();
    public bool Full { get; set; }
    public bool Done { get; set; }
    public int Step_Index { get; set; }
    public int Step_Total { get; set; }
    /// <summary>"A", "B" or "both" (a simultaneous blind pick).</summary>
    public string? Waiting_On { get; set; }
    public string? Action { get; set; }
    public int? Game { get; set; }
    public double? Seconds_Left { get; set; }
    public List<string> Map_Pool { get; set; } = new();
    public string? Neutral_Strike { get; set; }
    public List<string> Banned_Maps { get; set; } = new();
    public Dictionary<string, string> Picked_Maps { get; set; } = new();
    public List<string> Banned_Commanders { get; set; } = new();
    public Dictionary<string, string> Commander_Names { get; set; } = new();
    public List<string> Locked_In { get; set; } = new();
    public string? Your_Pending_Pick { get; set; }
    public List<string> Options { get; set; } = new();
    public List<PlannedGameDto> Plan { get; set; } = new();

    public bool YourTurn =>
        Your_Side is not null &&
        (Waiting_On == Your_Side || Waiting_On == "both");

    public bool IsBanStep =>
        Action is "ban_map" or "ban_commander";

    public bool IsMapStep =>
        Action is "ban_map" or "pick_map";

    public string Display(string commanderId) =>
        Commander_Names.TryGetValue(commanderId, out var n) ? n : commanderId;
}

public sealed class PlannedGameDto
{
    public int Game { get; set; }
    public string? Map { get; set; }
    public string? Map_Picked_By { get; set; }
    public string? Commander_A { get; set; }
    public string? Commander_B { get; set; }
    public bool Decider { get; set; }
}

public sealed class DraftCreatedDto
{
    public string Id { get; set; } = "";
    public string Join_Code { get; set; } = "";
    public DraftStateDto? State { get; set; }
}

public sealed class PairClaimDto
{
    public string Token { get; set; } = "";
    public string? Discord { get; set; }
    public string? Ufer_Name { get; set; }
    public string? Steam_Id { get; set; }
    public bool Tracking_Consent { get; set; }
}

public sealed class MeDto
{
    public bool Logged_In { get; set; }
    public string? Discord { get; set; }
    public string? Ufer_Name { get; set; }
    public string? Steam_Id { get; set; }
    public string? Role { get; set; }
    public bool Verified { get; set; }
    public bool Tracking_Consent { get; set; }
}

public sealed class SteamTicketDto
{
    public string Ticket { get; set; } = "";
    public string Url { get; set; } = "";
}

public sealed class QueueModeDto
{
    public string Key { get; set; } = "";
    public string Label { get; set; } = "";
    public int Best_Of { get; set; }
    public bool Rated { get; set; }
    public int Team_Size { get; set; }
    public bool Available { get; set; }
    public int Waiting { get; set; }

    /// <summary>What the picker shows. Bo and waiting count matter at a glance.</summary>
    public override string ToString() =>
        Available ? $"{Label}  ·  Bo{Best_Of}  ·  {Waiting} waiting"
                  : $"{Label}  (not yet)";
}

public sealed class QueueModesDto
{
    public List<QueueModeDto> Modes { get; set; } = new();
}

public sealed class LiveMatchDto
{
    public string Id { get; set; } = "";
    public string Mode { get; set; } = "";
    public List<string> Players { get; set; } = new();
    public List<string> Observers { get; set; } = new();
    public int Free_Slots { get; set; }
    public bool Accepting_Requests { get; set; }
    public int Running_For_S { get; set; }
    public string? Tournament { get; set; }
}

public sealed class LiveListDto
{
    public List<LiveMatchDto> Matches { get; set; } = new();
}

public sealed class TournamentSummaryDto
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string Mode_Key { get; set; } = "";
    public int Participants { get; set; }
    public int Finished { get; set; }
}

public sealed class TournamentListDto
{
    public List<TournamentSummaryDto> Tournaments { get; set; } = new();
}

public sealed class BracketMatchDto
{
    public string Id { get; set; } = "";
    public string Label { get; set; } = "";
    public string? A_Name { get; set; }
    public string? B_Name { get; set; }
    public List<int>? Score { get; set; }
    public string? Winner { get; set; }
    public bool Bye { get; set; }
    public bool Ready { get; set; }
}

public sealed class BracketRoundDto
{
    /// <summary>Language-neutral: "final", "semi", "quarter", "r16", "r3"…</summary>
    public string Round_Key { get; set; } = "";
    public string Round { get; set; } = "";
    public List<BracketMatchDto> Matches { get; set; } = new();
}

public sealed class TournamentDetailDto
{
    public string Name { get; set; } = "";
    public string Mode { get; set; } = "";
    public List<BracketRoundDto> Bracket { get; set; } = new();
    public string? Champion { get; set; }
}
