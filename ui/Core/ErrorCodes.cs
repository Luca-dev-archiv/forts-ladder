namespace FortsLadder.Core;

/// <summary>
/// Short codes for everything that can go wrong, so people can say *which*
/// thing went wrong.
///
/// "Something went wrong" is unquotable. A player who can write FL-100 in
/// Discord gets an answer in one message instead of five, and a referee reading
/// FL-231 knows the match was matched to the wrong series without having to
/// reconstruct it. The list is in docs/error-codes.md and the numbers there are
/// these numbers — a code that means something different in the two places is
/// worse than no code.
///
/// Numbering: 1xx the connection and the account, 2xx a match or a series,
/// 3xx the game and its files, 4xx this program itself.
/// </summary>
public static class ErrorCodes
{
    // --- 1xx: the connection and the account
    public const string SessionExpired = "FL-100";
    public const string NoServer = "FL-101";
    public const string NotAllowed = "FL-102";
    public const string ServerBroke = "FL-103";
    public const string NotThere = "FL-104";
    public const string Refused = "FL-105";
    public const string Conflict = "FL-106";
    public const string Unexpected = "FL-109";
    public const string NoSteamLink = "FL-110";
    public const string NoConsent = "FL-111";

    // --- 2xx: a match or a series
    public const string NoLobbyYet = "FL-200";
    public const string NotYourMatch = "FL-201";
    public const string HandoffExpired = "FL-202";
    public const string SeriesOpen = "FL-203";
    public const string Dodged = "FL-204";
    public const string WrongMap = "FL-210";
    public const string WrongCommander = "FL-211";
    public const string LobbySettingsChanged = "FL-212";
    public const string StrangerInLobby = "FL-213";
    public const string UnmatchedGame = "FL-230";
    public const string NotLadderMatch = "FL-232";
    public const string PoolTooSmall = "FL-233";
    public const string GameNotCounted = "FL-231";

    // --- 3xx: the game and its files
    public const string NoFortsFound = "FL-300";
    public const string NoLogYet = "FL-301";
    public const string GameNotRestarted = "FL-302";
    public const string SettingsNotWritten = "FL-303";

    // --- 4xx: this program
    public const string AlreadyRunning = "FL-400";
    public const string StartupRefused = "FL-401";

    /// <summary>
    /// One line: the code, what to do, and the server's own words if it sent
    /// any. The advice comes from the locale files, so the code is the only part
    /// that is the same in every language — which is the point of having one.
    /// </summary>
    public static string Text(string code, string? detail = null)
    {
        var advice = Loc.T("err." + code);
        // An unknown code must not render as the lookup key: better the bare
        // code and whatever the server said than "err.FL-217".
        if (advice.StartsWith("err.")) advice = "";
        var parts = new List<string> { $"[{code}]" };
        if (advice.Length > 0) parts.Add(advice);
        if (!string.IsNullOrWhiteSpace(detail)) parts.Add(detail!.Trim());
        return string.Join("  ", parts);
    }
}
