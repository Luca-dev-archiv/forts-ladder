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

    /// <summary>
    /// The two teams, keyed 1 and 2 — not the game's side numbers.
    ///
    /// **Sides swap between games in Forts.** Grouping the whole series by side
    /// number therefore put the same people in both buckets, which is how a
    /// series came out reading "rapper and lafpaf vs rapper and lafpaf". It also
    /// made the score wrong in a way nobody would notice: one team winning twice
    /// counted as one win for side 1 and one for side 2, so a 2-0 was reported
    /// as 1-1.
    ///
    /// The first game decides who is on whose team; after that people are
    /// followed by Steam ID, whatever side they happen to be playing.
    /// </summary>
    public Dictionary<int, List<PlayerRecord>> Sides()
    {
        var team = TeamMap();
        var byTeam = new Dictionary<int, Dictionary<string, PlayerRecord>>();
        foreach (var m in Matches.OrderBy(x => x.PlayedAt))
            foreach (var p in m.Players.Values)
            {
                if (p.Side <= 0 || string.IsNullOrEmpty(p.SteamId)) continue;
                if (!team.TryGetValue(p.SteamId, out var n)) continue;
                if (!byTeam.TryGetValue(n, out var d))
                    byTeam[n] = d = new Dictionary<string, PlayerRecord>();
                d[p.SteamId] = p;
            }
        return byTeam.OrderBy(kv => kv.Key)
                     .ToDictionary(kv => kv.Key, kv => kv.Value.Values.ToList());
    }

    /// <summary>
    /// Steam ID -> team (1 or 2), from the first game that has two sides.
    ///
    /// Anyone who only appears later is put on the team they did *not* play
    /// against, which is the best available answer for a substitute.
    /// </summary>
    public Dictionary<string, int> TeamMap()
    {
        var map = new Dictionary<string, int>();
        foreach (var m in Matches.OrderBy(x => x.PlayedAt))
        {
            var sides = m.Players.Values
                .Where(p => p.Side > 0 && !string.IsNullOrEmpty(p.SteamId))
                .GroupBy(p => p.Side).OrderBy(g => g.Key).ToList();
            if (sides.Count < 2) continue;

            if (map.Count == 0)
            {
                foreach (var p in sides[0]) map[p.SteamId] = 1;
                foreach (var p in sides[1]) map[p.SteamId] = 2;
                continue;
            }
            // A later game: work out which side is which team from whoever is
            // already known, then place the newcomers with them.
            foreach (var g in sides)
            {
                var known = g.Select(p => map.GetValueOrDefault(p.SteamId))
                             .Where(x => x != 0).ToList();
                if (known.Count == 0) continue;
                var n = known.GroupBy(x => x).OrderByDescending(x => x.Count())
                             .First().Key;
                foreach (var p in g)
                    if (!map.ContainsKey(p.SteamId)) map[p.SteamId] = n;
            }
        }
        return map;
    }

    /// <summary>Which team a side number belonged to in this game, or 0.</summary>
    public int TeamOfSide(MatchRecord m, int side)
    {
        var team = TeamMap();
        foreach (var p in m.Players.Values)
            if (p.Side == side && !string.IsNullOrEmpty(p.SteamId)
                && team.TryGetValue(p.SteamId, out var n))
                return n;
        return 0;
    }

    /// <summary>Which team won a game, or 0 when it was not decided.</summary>
    public int TeamOfWinner(MatchRecord m) =>
        m.Status == MatchStatus.Decided ? TeamOfSide(m, m.WinnerSide) : 0;

    /// <summary>
    /// Wins per team, keyed the same way as <see cref="Sides"/>.
    ///
    /// Counted by team rather than by the game's side number: the sides swap
    /// between games, so counting numbers turned one team winning twice into
    /// 1-1.
    /// </summary>
    public (Dictionary<int, int> Wins, int Unclear) Score()
    {
        var wins = new Dictionary<int, int>();
        var unclear = 0;
        foreach (var m in Matches)
        {
            var team = TeamOfWinner(m);
            if (team != 0) wins[team] = wins.GetValueOrDefault(team) + 1;
            else unclear++;
        }
        foreach (var s in Sides().Keys) wins.TryAdd(s, 0);
        return (wins, unclear);
    }

    /// <summary>Which team is mine — not which side I played in some game.</summary>
    public int? LocalSide()
    {
        var team = TeamMap();
        if (!string.IsNullOrEmpty(MySteamId)
            && team.TryGetValue(MySteamId!, out var mine))
            return mine;
        // Fallback for games without a known own Steam ID.
        var local = Matches.SelectMany(m => m.Players.Values)
            .FirstOrDefault(p => p.Local && p.Side > 0
                                 && !string.IsNullOrEmpty(p.SteamId));
        return local is not null && team.TryGetValue(local.SteamId, out var n)
            ? n : null;
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

        // Rule check as far as the log allows: a commander must not be played
        // again after winning with it.
        //
        // Tracked per *team*, not per side number. The sides swap between games,
        // so keying on the number split one player's history across two buckets:
        // a genuine reuse went unnoticed and an innocent one was reported.
        var burned = new Dictionary<int, HashSet<string>>();
        var i = 0;
        foreach (var m in Matches.OrderBy(m => m.PlayedAt))
        {
            i++;
            foreach (var (side, cmdr) in m.Commanders)
            {
                var team = TeamOfSide(m, side);
                if (team == 0) continue;      // nobody on it we can identify
                if (!burned.TryGetValue(team, out var used))
                    burned[team] = used = new HashSet<string>();
                if (used.Contains(cmdr))
                    // Named the way the game names it: a warning that says
                    // "da-overclocker" makes the reader look it up.
                    warnings.Add(Loc.T("warn.commander_reuse", i, team,
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
    /// <summary>
    /// Group matches into series.
    ///
    /// `sealedLobbies` are lobbies this ladder set up, and they are a hard
    /// boundary: matches in one are that series and never merge with anything
    /// else. Without it the host-crash rule below folds a genuinely new series
    /// into the previous one whenever the same two people play again within a
    /// few hours — which is what testing found.
    /// </summary>
    public static List<Series> Group(IEnumerable<MatchRecord> matches,
                                     TimeSpan? gap = null,
                                     string? mySteamId = null,
                                     IReadOnlySet<ulong>? sealedLobbies = null,
                                     IReadOnlyDictionary<string, string>? draftedGames = null)
    {
        sealedLobbies ??= new HashSet<ulong>();
        draftedGames ??= new Dictionary<string, string>();
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
            // What the ladder itself recorded, before any guessing.
            //
            // A client that is not hosting has no lobby id in its log at all, so
            // every one of its games used to fall through to "same two people
            // within three hours" — and got appended to whichever series was
            // last, drafted or not. For a game this client reported there is
            // nothing to guess about: it knew the series at the time.
            var known = draftedGames.TryGetValue(m.ReportKey, out var sid)
                ? "series:" + sid : null;
            // The lobby id goes into the key together with the roster: a
            // lobby can stay up all evening while the opponents change.
            var key = known
                      ?? (m.LobbyId is not null ? $"lobby:{m.LobbyId}:{Roster(m)}" : null);
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
        // A series played in a lobby the ladder set up is sealed: its boundary
        // is known rather than guessed, so it neither absorbs nor is absorbed.
        bool Sealed(Series s) => s.Matches.Any(
            m => m.LobbyId is not null && sealedLobbies.Contains(m.LobbyId.Value))
            || s.Matches.Any(m => draftedGames.ContainsKey(m.ReportKey));
        // The series id the ladder recorded for these games, if it did.
        string? SeriesIdOf(Series s) => s.Matches
            .Select(m => draftedGames.TryGetValue(m.ReportKey, out var x) ? x : null)
            .FirstOrDefault(x => x is not null);
        ulong? LobbyOf(Series s) => s.Matches
            .Select(m => m.LobbyId).FirstOrDefault(x => x is not null);

        var merged = new List<Series>();
        foreach (var s in result.OrderBy(x => x.PlayedAt))
        {
            var prev = merged.LastOrDefault();
            var wouldMerge = prev is not null &&
                string.Join("+", prev.SteamIds) == string.Join("+", s.SteamIds) &&
                s.Matches[0].PlayedAt - prev.Matches[^1].PlayedAt <= maxGap;
            // Two different lobbies, at least one of them drafted: that is two
            // series, whatever the clock says.
            if (wouldMerge && (Sealed(s) || Sealed(prev!))
                && LobbyOf(s) != LobbyOf(prev!))
                wouldMerge = false;
            // Two recorded series ids are two series, full stop. This is the
            // one case where nothing is being inferred: the ladder wrote both
            // ids down as the games were reported.
            if (wouldMerge)
            {
                var here = SeriesIdOf(s);
                var there = SeriesIdOf(prev!);
                if (here is not null && there is not null && here != there)
                    wouldMerge = false;
            }

            if (wouldMerge)
            {
                prev!.Matches.AddRange(s.Matches);
                prev.Matches.Sort((a, b) => a.PlayedAt.CompareTo(b.PlayedAt));
            }
            else merged.Add(s);
        }
        return merged.OrderByDescending(s => s.PlayedAt).ToList();
    }
}
