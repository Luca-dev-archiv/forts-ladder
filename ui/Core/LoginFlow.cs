namespace FortsLadder.Core;

/// <summary>
/// Signing in, preferring the Discord app that is already running.
///
/// The order matters and is the whole point of this class:
///
///   1. **Ask the local Discord client.** It shows its own permission prompt,
///      hands back an authorization code, and the user never leaves the app or
///      copies anything. The prompt they are asked to trust is Discord's own.
///   2. **Fall back to the browser** only when that is not possible — Discord
///      not running, a sandboxed build whose pipe is unreachable, or an
///      application without RPC access. The fallback is a requirement, not
///      decoration, because any of those is a normal state.
///
/// The code from either route is worthless on its own: exchanging it needs the
/// client secret, which lives on the server. This class never touches one.
/// </summary>
public sealed class LoginFlow
{
    private readonly ApiClient _api;

    public LoginFlow(ApiClient api) => _api = api;

    public sealed record Result(bool Ok, string? Error, bool CanRetryInBrowser);

    private sealed class ConfigDto
    {
        public string? Client_Id { get; set; }
        public bool Native_Login { get; set; }
        public string Redirect_Uri { get; set; } = "http://localhost";
    }

    /// <summary>
    /// Log in without leaving the app, if Discord is there to ask.
    /// </summary>
    public async Task<Result> TryDiscordAppAsync(CancellationToken ct = default)
    {
        if (!_api.Configured)
            return new Result(false, Loc.T("login.no_server"), false);

        var cfg = await _api.GetAsync<ConfigDto>("/auth/discord/config");
        if (cfg is null)
            return new Result(false, _api.LastError ?? Loc.T("login.no_server"), false);
        if (!cfg.Native_Login || string.IsNullOrWhiteSpace(cfg.Client_Id))
            // The operator has not configured a Discord application, so no
            // route works — saying "start Discord" here would be misleading.
            return new Result(false, Loc.T("login.server_has_no_app"), false);

        var auth = await DiscordRpc.AuthorizeAsync(
            cfg.Client_Id!, cfg.Redirect_Uri, ct: ct);
        if (!auth.Ok)
            return new Result(false, auth.Error, true);

        var claimed = await _api.PostAsync<PairClaimDto>("/auth/discord/native",
            new { code = auth.Code, redirect_uri = cfg.Redirect_Uri });
        if (claimed is null || string.IsNullOrWhiteSpace(claimed.Token))
            // Reached Discord but the server could not use the code. Almost
            // always a redirect URI that differs from the registered one.
            return new Result(false, _api.LastError ?? "?", true);

        _api.SetToken(claimed.Token);
        return new Result(true, null, false);
    }

    /// <summary>Exchange a pairing code shown in the browser for a session.</summary>
    public async Task<Result> ClaimPairingAsync(string code)
    {
        var claimed = await _api.PostAsync<PairClaimDto>("/auth/pair/claim",
                                                        new { code });
        if (claimed is null || string.IsNullOrWhiteSpace(claimed.Token))
            return new Result(false, _api.LastError ?? "?", true);
        _api.SetToken(claimed.Token);
        return new Result(true, null, false);
    }

    /// <summary>Where to log in when the app route did not work.</summary>
    public string BrowserLoginUrl() => $"{_api.BaseUrl}/auth/discord/start";

    /// <summary>A page on the server: the account page, or one of the two
    /// management pages. Everything a person edits rather than plays is a form,
    /// and a browser is the right place for a form.</summary>
    public string WebsiteUrl(string path = "/") => $"{_api.BaseUrl}{path}";

    public Task<MeDto?> MeAsync() => _api.GetAsync<MeDto>("/me");

    /// <summary>
    /// Report a finished series to the ladder.
    ///
    /// This is the step the project was missing: everything could be recorded
    /// and nothing was ever sent, so the shared ranking stayed the imported
    /// spreadsheet no matter who won. The server decides whether it counts —
    /// the lobby has to be one the ladder set up and everyone in it has to have
    /// agreed to be tracked — and answers with the reasons when it does not.
    /// </summary>
    public Task<ReportResultDto?> ReportSeriesAsync(
            Dictionary<string, int> sides, int games, int scoreLow,
            DateTime playedAt, ulong? lobbyId, IEnumerable<string> replays,
            string? draftId = null) =>
        _api.PostAsync<ReportResultDto>("/results", new
        {
            // What makes both clients' reports one series rather than two
            // rating changes. It cannot be derived from anything either client
            // sends: only a host's log carries a lobby id, and the two logs
            // disagree about the kickoff second.
            draft_id = draftId,
            sides,
            games,
            score_low = scoreLow,
            // Full timestamp, not just the date: the server treats lobby plus
            // kickoff as the identity of a series, and two Bo3s in the same
            // lobby on one evening would otherwise collapse into one.
            played_at = playedAt.ToString("yyyy-MM-ddTHH:mm:ss"),
            lobby_id = lobbyId?.ToString(),
            replays = replays.ToList(),
        });

    /// <summary>
    /// Claim a ladder name. Applied straight away when it matches the Discord
    /// login (the spreadsheet lists Discord names, so that *is* the proof) and
    /// otherwise held for an admin instead of refused.
    /// </summary>
    public Task<NameClaimDto?> ClaimUferNameAsync(string name) =>
        _api.PostAsync<NameClaimDto>("/me/ufer_name", new { name });

    /// <summary>
    /// Tell the server the Steam display name this account plays under.
    ///
    /// Read out of the game log by this client, because the server has no way
    /// to know it. Purely so people are shown by name rather than by a
    /// 17-digit id — the id stays the identity, since a display name can be
    /// changed to anybody else's.
    /// </summary>
    public Task<object?> SetSteamNameAsync(string name) =>
        _api.PutAsync<object>("/me/steam_name", new { name });

    public async Task<bool> GrantConsentAsync() =>
        await _api.PostAsync<MeDto>("/me/consent") is not null;

    public async Task<bool> WithdrawConsentAsync() =>
        await _api.DeleteAsync("/me/consent");

    /// <summary>
    /// Link Steam without asking the user to type anything.
    ///
    /// Steam's own login is the only thing that can prove a Steam ID, and it is
    /// a web redirect — there is no local equivalent of Discord's pipe. What can
    /// be removed is the copying: the browser is opened on the right page and
    /// this polls until the ID appears, so the only step left is pressing
    /// "Sign in" at Steam.
    ///
    /// Asking for a Steam password in our own window would be the alternative,
    /// and that is precisely what a phishing tool looks like. It is not on the
    /// table.
    /// </summary>
    public async Task<bool> LinkSteamAsync(IProgress<string>? status = null,
                                           CancellationToken ct = default)
    {
        // A ticket, not the session token: this URL goes into a browser, and
        // browsers keep URLs in history. The ticket authorises one thing —
        // attaching a Steam ID to this account — and expires.
        var t = await _api.PostAsync<SteamTicketDto>("/auth/steam/ticket");
        if (t is null || string.IsNullOrWhiteSpace(t.Url))
        {
            status?.Report(_api.LastError ?? "?");
            return false;
        }
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo(t.Url)
                { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            status?.Report(ex.Message);
            return false;
        }

        // Poll rather than run a local listener: the callback lands on the
        // server, not here, so there is nothing for a listener to catch.
        for (var i = 0; i < 90 && !ct.IsCancellationRequested; i++)
        {
            await Task.Delay(2000, ct);
            var me = await MeAsync();
            if (me?.Steam_Id is { Length: > 0 }) return true;
        }
        return false;
    }

    // ------------------------------------------------------------- Spectators
    /// <summary>
    /// Announce a running match so it can be watched.
    ///
    /// Nothing published one before, so the live list everybody polled was
    /// always empty and the whole spectator flow was unreachable. The lobby id
    /// travels with it and is handed on only to admitted spectators.
    /// </summary>
    public Task<PublishedDto?> PublishLiveAsync(string mode, string label,
            List<string> players, int slotsUsed, string lobbyId) =>
        _api.PostAsync<PublishedDto>("/live", new
        {
            mode_key = mode,
            mode_label = label,
            players,
            slots_used = slotsUsed,
            slots_total = 9,          // Forts' hard limit, spectators included
            lobby_id = long.TryParse(lobbyId, out var l) ? l : (long?)null,
        });

    public Task<object?> HeartbeatLiveAsync(string matchId) =>
        _api.PostAsync<object>($"/live/{matchId}/heartbeat");

    public Task<bool> FinishLiveAsync(string matchId) =>
        _api.DeleteAsync($"/live/{matchId}");

    /// <summary>Allow or forbid spectators for this match at all.
    ///
    /// Different from "not right now": a match closed here declines everybody,
    /// a caster included.</summary>
    public async Task<bool> SetSpectatorsAllowedAsync(string matchId, bool value) =>
        await _api.PostAsync<object>(
            $"/live/{matchId}/spectators?value={(value ? "true" : "false")}")
        is not null;

    /// <summary>What a spectator accepts by being admitted.</summary>
    public Task<TermsDto?> ObserverTermsAsync() =>
        _api.GetAsync<TermsDto>("/observe/terms");

    /// <summary>Ask a human to look at one of your own series.</summary>
    public async Task<bool> FlagResultAsync(string resultId, string note) =>
        await _api.PostAsync<object>($"/results/{resultId}/flag", new { note })
        is not null;

    /// <summary>Open or close a match to spectator requests.</summary>
    public async Task<bool> SetAcceptingAsync(string matchId, bool value) =>
        await _api.PostAsync<object>(
            $"/live/{matchId}/accepting?value={(value ? "true" : "false")}")
        is not null;

    /// <summary>Who is asking to watch a match of mine.</summary>
    public Task<ObserverInboxDto?> ObserverInboxAsync() =>
        _api.GetAsync<ObserverInboxDto>("/observe/requests");

    /// <summary>My own requests, and what became of them.</summary>
    public Task<MyObserverRequestsDto?> MyObserverRequestsAsync() =>
        _api.GetAsync<MyObserverRequestsDto>("/observe/mine");

    public async Task<bool> AnswerObserverAsync(string requestId, bool approve) =>
        await _api.PostAsync<object>(
            $"/observe/{requestId}/answer?approve={(approve ? "true" : "false")}")
        is not null;

    public Task<QueueModesDto?> ModesAsync() => _api.GetAsync<QueueModesDto>("/queue/modes");

    /// <summary>"I am still here" — and how many others are.
    ///
    /// The queue poll only runs while somebody is queueing, so a client on any
    /// other screen needs its own way of saying so. It is also what takes an
    /// account *out* of the queue when the client is closed: an entry nobody is
    /// asking about is nobody at the keyboard.
    /// </summary>
    public Task<PresenceDto?> PingAsync() => _api.PostAsync<PresenceDto>("/presence");

    /// <summary>
    /// Send one game's replay up, so a dispute has something to look at.
    ///
    /// Kept for a week on the server and then deleted. The point is a referee
    /// being able to watch the game somebody complained about — not an archive,
    /// which is a different thing from what the community agreed to.
    /// </summary>
    public Task<bool> UploadReplayAsync(string resultId, int game, string path)
        => _api.UploadAsync($"/results/{resultId}/replay?index={game}", path);

    /// <summary>
    /// What the ladder makes of your series — the label, not a guess at it.
    /// </summary>
    public Task<LadderSeriesListDto?> MySeriesAsync() =>
        _api.GetAsync<LadderSeriesListDto>("/series/mine");

    /// <summary>
    /// Which of your lobbies the ladder set up.
    ///
    /// The client keeps its own list as each draft hands off a lobby, but that
    /// list is per machine: a reinstall or a second computer loses it, and then a
    /// real ladder series looks like a casual game. This is the same fact from
    /// the side that cannot be lost.
    /// </summary>
    public Task<LobbyListDto?> MyLobbiesAsync() =>
        _api.GetAsync<LobbyListDto>("/lobbies/mine");

    /// <summary>Your own reported series, so a case can be opened against one
    /// long after this client reported it.</summary>
    public Task<MyResultsDto?> MyResultsAsync() =>
        _api.GetAsync<MyResultsDto>("/results/mine");

    public Task<PoolStatusDto?> PoolsAsync() => _api.GetAsync<PoolStatusDto>("/queue/pools");

    /// <summary>
    /// Send this machine's map and commander pools to the server. Admin only.
    ///
    /// The server cannot read them itself — it has no Forts installation — and
    /// they must not come from whoever is queueing, or one side could choose the
    /// map list before the veto starts. So an admin publishes them once from a
    /// client that has the game, and that is what everyone then drafts from.
    /// </summary>
    public async Task<bool> PublishPoolsAsync(IEnumerable<string> maps,
                                              IEnumerable<string> commanders)
    {
        var body = new { map_pool = maps.ToList(), commander_pool = commanders.ToList() };
        return await _api.PutAsync<Dictionary<string, int>>("/admin/pools", body)
               is not null;
    }
}
