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

    /// <summary>
    /// Raised when the server rejected the session we were holding.
    ///
    /// Being signed in used to mean "a token file exists", which is a claim and
    /// not a fact: after the session expired or was revoked, every call failed
    /// with a bare 401 and the client went on believing it was signed in. The
    /// dead token is dropped here so the next thing the person sees is "sign in
    /// again" rather than the same error for the rest of the evening.
    /// </summary>
    public event Action? SignedOut;

    /// <summary>Retire a token the server no longer accepts.</summary>
    private void Invalidate()
    {
        if (Token is null) return;
        Token = null;
        _http.DefaultRequestHeaders.Authorization = null;
        try { if (File.Exists(TokenPath)) File.Delete(TokenPath); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        SignedOut?.Invoke();
    }

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
            LastError = Unreachable(ex);
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
            LastError = Unreachable(ex);
            return default;
        }
    }

    public Task<T?> PutAsync<T>(string path, object? body = null) =>
        SendAsync<T>(HttpMethod.Put, path, body);

    private async Task<T?> SendAsync<T>(HttpMethod method, string path,
                                        object? body)
    {
        LastError = null;
        if (!Configured) return default;
        try
        {
            using var r = await _http.SendAsync(Request(method, path, body));
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
            LastError = Unreachable(ex);
            return default;
        }
    }

    /// <summary>DELETE, keeping the answer.
    ///
    /// Leaving the queue returns the new status, and throwing it away is what
    /// left a stale searcher count on the mode picker.</summary>
    public Task<T?> DeleteAsync<T>(string path) =>
        SendAsync<T>(HttpMethod.Delete, path, null);

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
            LastError = Unreachable(ex);
            return false;
        }
    }

    /// <summary>
    /// Turn a failed reply into something a person can act on.
    ///
    /// Three things, in order of how much they help: the code, so it can be
    /// quoted in Discord and looked up in docs/error-codes.md; what to do about
    /// it; and only then the server's own words, because `detail` is written for
    /// whoever wrote the route.
    ///
    /// A 401 also retires the token — the session is gone, and repeating the
    /// call cannot fix that.
    /// </summary>
    private string Describe(System.Net.HttpStatusCode code, string body)
    {
        var detail = Detail(body);
        switch ((int)code)
        {
            case 401:
                Invalidate();
                return ErrorCodes.Text(ErrorCodes.SessionExpired);
            case 403:
                return ErrorCodes.Text(ErrorCodes.NotAllowed, detail);
            case 404:
                return ErrorCodes.Text(ErrorCodes.NotThere, detail);
            case 409:
                return ErrorCodes.Text(ErrorCodes.Conflict, detail);
            case 400:
            case 422:
                return ErrorCodes.Text(ErrorCodes.Refused, detail);
        }
        if ((int)code >= 500)
            return ErrorCodes.Text(ErrorCodes.ServerBroke, detail);
        return ErrorCodes.Text(ErrorCodes.Unexpected, $"HTTP {(int)code} {detail}");
    }

    /// <summary>
    /// Nothing answered. Named apart from a refusal because the fix is
    /// different: one is about permission, the other about the network.
    /// </summary>
    private string Unreachable(Exception ex)
        => ErrorCodes.Text(ErrorCodes.NoServer, $"{BaseUrl} — {ex.Message}");

    /// <summary>The server's own explanation, if it sent one.</summary>
    private static string Detail(string body)
    {
        try
        {
            using var doc = JsonDocument.Parse(body);
            if (doc.RootElement.TryGetProperty("detail", out var d)
                && d.ValueKind == JsonValueKind.String)
                return d.GetString() ?? "";
        }
        catch (JsonException) { }
        return body.Length > 200 ? body[..200] : body;
    }

    public async Task<bool> PostAsync(string path)
    {
        LastError = null;
        if (!Configured) return false;
        try
        {
            var r = await _http.PostAsync($"{BaseUrl}{path}", null);
            if (r.IsSuccessStatusCode) return true;
            // Through Describe like every other call. This one produced the raw
            // `HTTP 401: {"detail":"not logged in"}` the spectate button showed.
            LastError = Describe(r.StatusCode, await r.Content.ReadAsStringAsync());
            return false;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            LastError = Unreachable(ex);
            return false;
        }
    }

    /// <summary>
    /// Upload one file as multipart form data.
    ///
    /// Used for replays. The server names the stored file itself and caps the
    /// size, so nothing here has to be trusted — but the *reason* it is a
    /// separate method rather than a JSON body is size: a replay base64'd into
    /// JSON is a third larger for no benefit.
    /// </summary>
    public async Task<bool> UploadAsync(string path, string filePath,
                                        string field = "file")
    {
        LastError = null;
        if (!Configured) return false;
        try
        {
            using var content = new MultipartFormDataContent();
            var bytes = await File.ReadAllBytesAsync(filePath);
            var part = new ByteArrayContent(bytes);
            part.Headers.ContentType =
                new System.Net.Http.Headers.MediaTypeHeaderValue(
                    "application/octet-stream");
            content.Add(part, field, Path.GetFileName(filePath));

            using var req = new HttpRequestMessage(HttpMethod.Post,
                                                   $"{BaseUrl}{path}");
            if (Token is not null)
                req.Headers.Authorization =
                    new System.Net.Http.Headers.AuthenticationHeaderValue(
                        "Bearer", Token);
            req.Content = content;
            using var r = await _http.SendAsync(req);
            if (r.IsSuccessStatusCode) return true;
            LastError = Describe(r.StatusCode, await r.Content.ReadAsStringAsync());
            return false;
        }
        catch (Exception ex) when (ex is HttpRequestException
                                      or TaskCanceledException or IOException)
        {
            LastError = Unreachable(ex);
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
    public List<string> Commander_Pool { get; set; } = new();
    public List<string> Locked_In { get; set; } = new();
    public string? Your_Pending_Pick { get; set; }
    public List<string> Options { get; set; } = new();
    public List<PlannedGameDto> Plan { get; set; } = new();

    /// <summary>Set once somebody walked away; the side that did.</summary>
    public bool Cancelled { get; set; }
    public string? Cancelled_By { get; set; }

    /// <summary>
    /// The Steam lobby this series is played in, and which side hosts it.
    ///
    /// A string, not a number: a Steam lobby id needs 64 bits and would be
    /// rounded by anything that parses JSON numbers as doubles. A rounded id
    /// matches no game.
    /// </summary>
    public string? Lobby_Id { get; set; }
    public string? Lobby_Host { get; set; }

    /// <summary>The host's SteamID64. Steam's join URL wants the lobby owner's
    /// account; a zero there makes it guess, and it guessed wrong.</summary>
    public string? Lobby_Host_Steam { get; set; }

    /// <summary>People per side: 1 for a duel, 2 for a 2v2.</summary>
    public int Team_Size { get; set; } = 1;
    /// <summary>Seats still open, so the setup strip can say how many.</summary>
    public int Seats_Open { get; set; }

    /// <summary>
    /// When the lobby was opened, as a unix time, or null before it was.
    ///
    /// The floor under "is this game part of this series?". A client that is not
    /// hosting has no lobby id in its log and has to fall back on the roster,
    /// which matches every game those two ever played together — including the
    /// ones from a session that ended an hour ago.
    /// </summary>
    public double? Lobby_At { get; set; }

    /// <summary>The lobby's opening time as a local timestamp, or null.</summary>
    public DateTime? LobbyOpenedAt => Lobby_At is null ? null
        : DateTimeOffset.FromUnixTimeMilliseconds(
            (long)(Lobby_At.Value * 1000)).ToLocalTime().DateTime;

    /// <summary>Is this client the side that hosts the lobby?</summary>
    public bool YouHostLobby => Lobby_Host is not null && Lobby_Host == Your_Side;

    /// <summary>How far the series has got: game 1 is open, game N opens once
    /// N-1 has been reported. Later games show your own pick and not
    /// theirs.</summary>
    public int Revealed_Through { get; set; } = 1;
    public List<int> Games_Played { get; set; } = new();

    /// <summary>Games won per side, from the reported games.</summary>
    public Dictionary<string, int> Wins { get; set; } = new();

    /// <summary>What was played differently from what was drafted, per game.
    ///
    /// Decided by the server, because only it sees both sides: a client's own
    /// opponent's commander is withheld until the game is over, so no client can
    /// check the whole game. A game listed here was not counted and is played
    /// again under the same number.</summary>
    public Dictionary<string, List<string>> Deviations { get; set; } = new();
    /// <summary>Decided — a Bo3 ends at two, not after three games.</summary>
    public bool Series_Over { get; set; }

    /// <summary>Closed out, so both sides are free to queue again.</summary>
    public bool Concluded { get; set; }

    /// <summary>Decided but not yet closed out — this client may end it.</summary>
    public bool Can_Conclude { get; set; }

    /// <summary>Nothing more will happen here: finished, left, aborted or
    /// voided. Which is also the only question the queue asks.</summary>
    public bool Settled { get; set; }

    /// <summary>Whether leaving from here costs a cooldown. Asked so the warning
    /// comes before the click and not after it.</summary>
    public bool Leaving_Penalised { get; set; }

    /// <summary>The handoff clock, kept by the server so both clients count the
    /// same number down. Two clients counting their own would disagree about
    /// when it ran out, which is the one thing a deadline may not do.</summary>
    public HandoffDto Handoff { get; set; } = new();

    /// <summary>Side that has asked for two more minutes, waiting to be
    /// granted them by the other.</summary>
    public string? Extension_Asked_By { get; set; }

    /// <summary>The host wrote lobby settings into a running Forts, which only
    /// reads them at start — so the password the guest is waiting for does not
    /// exist in the running game.</summary>
    public bool Host_Restart_Pending { get; set; }

    /// <summary>Is this client the one the clock is on?</summary>
    public bool ClockOnYou => Handoff.On is not null && Handoff.On == Your_Side;

    /// <summary>
    /// The lobby password, for the two players only.
    ///
    /// The Steam join link has no field for it and Forts asks on entry, so
    /// without carrying it the guest was sent to a prompt for something only the
    /// host knew.
    /// </summary>
    public string? Lobby_Password { get; set; }

    /// <summary>Ended on a fact rather than an agreement: the people in the
    /// lobby were not the people who drafted.</summary>
    public bool Aborted { get; set; }
    public string? Aborted_Side { get; set; }
    public string? Aborted_Reason { get; set; }

    /// <summary>Was it this client's side that caused the abort?</summary>
    public bool AbortedByYou =>
        Aborted_Side is not null && Aborted_Side == Your_Side;

    /// <summary>Both sides agreed to throw the whole series away.</summary>
    public bool Voided { get; set; }
    /// <summary>Games both sides agreed not to count; they are played again.</summary>
    public List<int> Voided_Games { get; set; } = new();
    /// <summary>Open requests by side: what was asked for and why.</summary>
    public Dictionary<string, VoidRequestDto> Void_Requests { get; set; } = new();

    /// <summary>The opponent's open request, if there is one.</summary>
    public VoidRequestDto? TheirVoidRequest =>
        Your_Side is null ? null
        : Void_Requests.TryGetValue(Your_Side == "A" ? "B" : "A", out var v)
            ? v : null;

    public VoidRequestDto? MyVoidRequest =>
        Your_Side is not null && Void_Requests.TryGetValue(Your_Side, out var v)
            ? v : null;

    /// <summary>When this state arrived, so the step clock can keep running
    /// between polls instead of stepping once a second.</summary>
    public DateTime ReceivedAt { get; set; } = DateTime.UtcNow;

    /// <summary>
    /// Seconds left on this step, counted down locally from what the server
    /// said. Clamped at zero and never rounded up: the server decides when a
    /// step expires, and a bar that showed more time than exists would invite
    /// someone to take it.
    /// </summary>
    public double SecondsLeftNow => Seconds_Left is not { } left ? 0
        : Math.Max(0, left - Math.Max(0, (DateTime.UtcNow - ReceivedAt).TotalSeconds));

    public bool YourTurn =>
        Your_Side is not null &&
        (Waiting_On == Your_Side || Waiting_On == "both");

    public bool IsBanStep =>
        Action is "ban_map" or "ban_commander";

    public bool IsMapStep =>
        Action is "ban_map" or "pick_map";

    /// <summary>
    /// Commander display name, resolved from the game on this machine.
    ///
    /// The server sends ids only, on purpose: display names live in the game's
    /// language files and a server with no Forts installation can only guess
    /// from the id — which produced "Overclocker" for what the game calls
    /// Overdrive.
    /// </summary>
    public string Display(string commanderId) =>
        CommanderNames.Display(commanderId);
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

    /// <summary>A ladder name waiting for an admin to confirm it.</summary>
    public string? Ufer_Claim { get; set; }

    /// <summary>Steam display name as the server last heard it.</summary>
    public string? Steam_Name { get; set; }

    /// <summary>
    /// Own grants — "tournament_host", "referee", … A host is usually a
    /// player by rank, so the role alone does not say what they may open.
    /// </summary>
    public List<string> Grants { get; set; } = new();
}

public sealed class VoidRequestDto
{
    /// <summary>"series" or "game:N".</summary>
    public string Scope { get; set; } = "";
    public string Reason { get; set; } = "";
}

public sealed class TermsDto
{
    public string Terms { get; set; } = "";
}

public sealed class PublishedDto
{
    public string Match_Id { get; set; } = "";
}

public sealed class ObserverPendingDto
{
    public string Id { get; set; } = "";
    public string Match_Id { get; set; } = "";
    /// <summary>Ladder name of whoever is asking.</summary>
    public string Who { get; set; } = "";
}

public sealed class ObserverInboxDto
{
    public List<ObserverPendingDto> Pending { get; set; } = new();
}

public sealed class ObserverRequestDto
{
    /// <summary>Set once a human has been asked to look at this one.</summary>
    public bool Flagged { get; set; }
    public string Flag_Note { get; set; } = "";

    public string Id { get; set; } = "";
    public string Match_Id { get; set; } = "";
    /// <summary>"pending", "approved" or "declined".</summary>
    public string State { get; set; } = "";
    /// <summary>Why — "no room" is arithmetic, not a judgement, and the
    /// difference has to reach the person who asked.</summary>
    public string Reason { get; set; } = "";
    public List<string> Players { get; set; } = new();
    public string? Mode { get; set; }
    /// <summary>Only present once admitted: it is what lets someone in.</summary>
    public string? Lobby_Id { get; set; }
    public string? Join_Url { get; set; }
}

public sealed class MyObserverRequestsDto
{
    public List<ObserverRequestDto> Requests { get; set; } = new();
}

public sealed class NameClaimDto
{
    /// <summary>True when it took effect; false when a human has to confirm.</summary>
    public bool Applied { get; set; }
    public string? Ufer_Name { get; set; }
    public string? Pending { get; set; }
}

public sealed class ReportResultDto
{
    public string Id { get; set; } = "";
    /// <summary>Whether it will affect anybody's rating.</summary>
    public bool Rated { get; set; }
    /// <summary>Why not, when it will not. Shown verbatim — the two possible
    /// reasons call for completely different next steps.</summary>
    public List<string> Reasons { get; set; } = new();
}

/// <summary>One of your own reported series, as the ladder holds it.</summary>
public sealed class MyResultDto
{
    public string Id { get; set; } = "";
    public string Played_At { get; set; } = "";
    public int Games { get; set; }
    public int Score_Low { get; set; }
    public int Your_Side { get; set; }
    public bool Rated { get; set; }
    public List<string> Reasons { get; set; } = new();
    public string? Lobby_Id { get; set; }
}

/// <summary>Lobby ids as strings: 64 bits do not survive a JSON number.</summary>
public sealed class LobbyListDto
{
    public List<string> Lobbies { get; set; } = new();
}

public sealed class MyResultsDto
{
    public List<MyResultDto> Series { get; set; } = new();
}

public sealed class PoolStatusDto
{
    public bool Configured { get; set; }
    public int Maps { get; set; }
    public int Commanders { get; set; }
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

/// <summary>How many clients are currently connected.
///
/// A count and never a list: people agreed to have their matches tracked, which
/// is not the same as publishing when they are sitting at their computer.
/// </summary>
/// <summary>
/// One half of the handoff and how long is left in it.
///
/// `phase` is "none" before the draft is finished, "host" while the lobby is
/// being opened, "guest" while it is being joined, and "playing" once both are
/// in. `on` is the side the clock is running against.
/// </summary>
public sealed class HandoffDto
{
    public string Phase { get; set; } = "none";
    public string? On { get; set; }
    public int? Seconds_Left { get; set; }
    public int? Deadline_S { get; set; }
    public bool Expired { get; set; }

    /// <summary>When this answer arrived, so the seconds can keep moving
    /// between polls instead of standing still and then jumping.</summary>
    public DateTime ReceivedAt { get; set; } = DateTime.UtcNow;

    /// <summary>Counted down locally and never rounded up: a countdown that
    /// claims more time than exists is the one error that costs a match.</summary>
    public int SecondsLeftNow => Seconds_Left is null ? 0 : Math.Max(0,
        Seconds_Left.Value
        - (int)Math.Max(0, (DateTime.UtcNow - ReceivedAt).TotalSeconds));

    public bool Running => Phase is "host" or "guest";
}

public sealed class PresenceDto
{
    public int Online { get; set; }

    /// <summary>Searchers per mode, carried on the heartbeat because it is the
    /// only call an idle client makes.</summary>
    public Dictionary<string, int> Waiting { get; set; } = new();
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

    /// <summary>You are playing in this one.
    ///
    /// Decided by the server: the listing has no lobby id, and the guest of a
    /// series is not recorded on the match at all, so a client cannot tell.
    /// </summary>
    public bool Yours { get; set; }
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
