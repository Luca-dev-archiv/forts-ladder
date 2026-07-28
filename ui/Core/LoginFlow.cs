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
}
