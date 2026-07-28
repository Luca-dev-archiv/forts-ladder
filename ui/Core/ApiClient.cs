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

    private static string SettingsPath =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "server.txt");

    public ApiClient() => BaseUrl = Load();

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

    public async Task<T?> GetAsync<T>(string path)
    {
        LastError = null;
        if (!Configured) return default;
        try
        {
            var body = await _http.GetStringAsync($"{BaseUrl}{path}");
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
