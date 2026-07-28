using System.IO;

namespace FortsLadder.Core;

/// <summary>
/// Several games that together form a series (Bo3, Bo5, ...).
///
/// Grouping is NOT guessed as long as there is a real key:
///   1. The league's series id — if the game was started through this
///      program we know the series, because we created it.
///   2. The lobby id — even without a league the game supplies the key
///      itself: playing a Bo5 means staying in the same Steam lobby.
///   3. Roster plus time window — a fallback for historical data only.
/// </summary>
public sealed class Series
{
    public List<MatchRecord> Matches { get; } = new();
    public string? LeagueSeriesId { get; set; }

    /// <summary>
    /// The user's Steam ID. Do NOT derive this from the log's local flag:
    /// when reading historical games the logs sometimes come from the
    /// OPPONENT (Forts stores copies of both clients on a desync), and there
    /// `Local 1` marks them. Without this field the list read "vs &lt;your own
    /// name&gt;" with the score the wrong way round.
    /// </summary>
    public string? MySteamId { get; set; }

    public DateTime PlayedAt => Matches.Min(m => m.PlayedAt);
    public bool IsTeam => SteamIds.Count > 2;

    public List<string> SteamIds => Matches
        .SelectMany(m => m.Players.Values)
        .Where(p => !string.IsNullOrEmpty(p.SteamId))
        .Select(p => p.SteamId).Distinct().OrderBy(x => x, StringComparer.Ordinal)
        .ToList();

    public Dictionary<int, List<PlayerRecord>> Sides()
    {
        var byId = new Dictionary<int, Dictionary<string, PlayerRecord>>();
        foreach (var m in Matches)
            foreach (var p in m.Players.Values)
            {
                if (p.Side <= 0 || string.IsNullOrEmpty(p.SteamId)) continue;
                if (!byId.TryGetValue(p.Side, out var d))
                    byId[p.Side] = d = new Dictionary<string, PlayerRecord>();
                d[p.SteamId] = p;
            }
        return byId.OrderBy(kv => kv.Key)
                   .ToDictionary(kv => kv.Key, kv => kv.Value.Values.ToList());
    }

    public (Dictionary<int, int> Wins, int Unclear) Score()
    {
        var wins = new Dictionary<int, int>();
        var unclear = 0;
        foreach (var m in Matches)
        {
            if (m.Status == MatchStatus.Decided)
                wins[m.WinnerSide] = wins.GetValueOrDefault(m.WinnerSide) + 1;
            else unclear++;
        }
        foreach (var s in Sides().Keys) wins.TryAdd(s, 0);
        return (wins, unclear);
    }

    public int? LocalSide()
    {
        if (!string.IsNullOrEmpty(MySteamId))
        {
            var mine = Matches.SelectMany(m => m.Players.Values)
                .FirstOrDefault(p => p.SteamId == MySteamId && p.Side > 0);
            if (mine is not null) return mine.Side;
        }
        // Fallback for games without a known own Steam ID.
        return Matches.SelectMany(m => m.Players.Values)
            .FirstOrDefault(p => p.Local && p.Side > 0)?.Side;
    }

    public List<string> Names(IdentityStore ids, int side) =>
        Sides().TryGetValue(side, out var players)
            ? players.Select(p => ids.UferNameFor(p.SteamId) ?? p.Name)
                     .OrderBy(x => x).ToList()
            : new List<string>();

    /// <summary>Report line in the format the rule set prescribes.</summary>
    public (string Line, List<string> Warnings) Report(IdentityStore ids)
    {
        var warnings = new List<string>();
        var sides = Sides();
        if (sides.Count != 2)
        {
            warnings.Add(Loc.T("warn.sides", sides.Count));
            return ("", warnings);
        }

        var own = LocalSide() ?? sides.Keys.Min();
        var other = sides.Keys.First(s => s != own);
        var (wins, unclear) = Score();
        if (unclear > 0)
            warnings.Add(Loc.T("warn.unclear_games", unclear));

        var label = IsTeam ? "UFER Brawl" : "UFER Duel";
        var line = $"{label}: {string.Join(", ", Names(ids, own))} vs " +
                   $"{string.Join(", ", Names(ids, other))} " +
                   $"{wins.GetValueOrDefault(own)}-{wins.GetValueOrDefault(other)}";

        var missing = sides.Values.SelectMany(x => x)
            .Where(p => ids.UferNameFor(p.SteamId) is null)
            .Select(p => p.Name).Distinct().OrderBy(x => x).ToList();
        if (missing.Count > 0)
            warnings.Add(Loc.T("warn.unlinked", string.Join(", ", missing)));

        // Rule check as far as the log allows: a commander must not be
        // played again after winning with it.
        var burned = new Dictionary<int, HashSet<string>>();
        var i = 0;
        foreach (var m in Matches.OrderBy(m => m.PlayedAt))
        {
            i++;
            foreach (var (side, cmdr) in m.Commanders)
            {
                if (!burned.TryGetValue(side, out var used))
                    burned[side] = used = new HashSet<string>();
                if (used.Contains(cmdr))
                    // Named the way the game names it: a warning that says
                    // "da-overclocker" makes the reader look it up.
                    warnings.Add(Loc.T("warn.commander_reuse", i, side,
                                       CommanderNames.Display(cmdr)));
                if (m.Status == MatchStatus.Decided && m.WinnerSide == side)
                    used.Add(cmdr);
            }
        }
        var lobbies = Matches.Where(m => m.LobbyId is not null)
            .Select(m => m.LobbyId!.Value).Distinct().Count();
        if (lobbies > 1)
            warnings.Add(Loc.T("warn.lobbies", lobbies));

        if (Matches.Count > 9)
            warnings.Add(Loc.T("warn.too_long", Matches.Count));

        return (line, warnings);
    }

    public (List<FileInfo> Found, List<string> Missing) ReplayFiles()
    {
        var found = new List<FileInfo>();
        var missing = new List<string>();
        var roots = FortsPaths.UserDirs();
        foreach (var m in Matches.OrderBy(m => m.PlayedAt))
        {
            if (string.IsNullOrEmpty(m.Replay))
            {
                missing.Add(Loc.T("warn.no_replay", m.Map ?? "?",
                                  m.PlayedAt.ToString("HH:mm")));
                continue;
            }
            var name = Path.GetFileName(m.Replay.Replace('\\', '/'));
            var hit = roots
                .Select(r => new FileInfo(Path.Combine(r.FullName, "replays", name)))
                .FirstOrDefault(f => f.Exists);
            if (hit is not null) found.Add(hit);
            else missing.Add(Loc.T("warn.replay_missing", name));
        }
        return (found, missing);
    }

    /// <summary>Put replays and the report into a folder. Sends nothing.</summary>
    public (int Copied, long Bytes) Collect(string outDir, IdentityStore ids)
    {
        Directory.CreateDirectory(outDir);
        var (found, _) = ReplayFiles();
        long bytes = 0;
        var n = 0;
        foreach (var f in found)
        {
            // Numbered prefix so the game order stays visible — the original
            // names sort by map, not by game.
            var dst = Path.Combine(outDir, $"{++n:00}_{f.Name}");
            if (!File.Exists(dst)) f.CopyTo(dst);
            bytes += f.Length;
        }
        var (line, warnings) = Report(ids);
        File.WriteAllText(Path.Combine(outDir, "report.txt"),
            line + "\n\n" + string.Join("\n", warnings.Select(w => "! " + w)));
        return (n, bytes);
    }

    // ---------------------------------------------------------------- Grouping
    public static List<Series> Group(IEnumerable<MatchRecord> matches,
                                     TimeSpan? gap = null,
                                     string? mySteamId = null)
    {
        var maxGap = gap ?? TimeSpan.FromHours(3);
        var playable = matches
            .Where(m => m.Players.Values.Count(p => !string.IsNullOrEmpty(p.SteamId)) >= 2
                     && m.Players.Values.Select(p => p.Side).Where(s => s > 0).Distinct().Count() >= 2)
            .OrderBy(m => m.PlayedAt).ToList();

        static string Roster(MatchRecord m) => string.Join("+", m.Players.Values
            .Where(p => !string.IsNullOrEmpty(p.SteamId))
            .Select(p => p.SteamId).Distinct().OrderBy(x => x, StringComparer.Ordinal));

        var result = new List<Series>();
        var byKey = new Dictionary<string, Series>();

        foreach (var m in playable)
        {
            // The lobby id goes into the key together with the roster: a
            // lobby can stay up all evening while the opponents change.
            var key = m.LobbyId is not null ? $"lobby:{m.LobbyId}:{Roster(m)}" : null;
            if (key is not null)
            {
                if (!byKey.TryGetValue(key, out var s))
                {
                    byKey[key] = s = new Series();
                    result.Add(s);
                }
                s.Matches.Add(m);
                continue;
            }
            var prev = result.LastOrDefault();
            if (prev is not null && Roster(prev.Matches[^1]) == Roster(m) &&
                m.PlayedAt - prev.Matches[^1].PlayedAt <= maxGap)
                prev.Matches.Add(m);
            else
            {
                var s = new Series();
                s.Matches.Add(m);
                result.Add(s);
            }
        }
        foreach (var s in result)
        {
            s.MySteamId = mySteamId;
            s.Matches.Sort((a, b) => a.PlayedAt.CompareTo(b.PlayedAt));
        }

        // MERGE ACROSS A HOST CHANGE.
        // The lobby id may join games but must never split them: if the host
        // crashes, someone re-hosts and play continues in a NEW lobby. The
        // rule set covers this explicitly (save the state with \save and play
        // on), so cutting by lobby id would report one series as two duels.
        //
        // Merging only happens for the same roster without a longer break.
        // Two separate duels between the same pairing on one evening do not
        // exist under the rules (one per calendar month).
        var merged = new List<Series>();
        foreach (var s in result.OrderBy(x => x.PlayedAt))
        {
            var prev = merged.LastOrDefault();
            if (prev is not null &&
                string.Join("+", prev.SteamIds) == string.Join("+", s.SteamIds) &&
                s.Matches[0].PlayedAt - prev.Matches[^1].PlayedAt <= maxGap)
            {
                prev.Matches.AddRange(s.Matches);
                prev.Matches.Sort((a, b) => a.PlayedAt.CompareTo(b.PlayedAt));
            }
            else merged.Add(s);
        }
        return merged.OrderByDescending(s => s.PlayedAt).ToList();
    }
}
