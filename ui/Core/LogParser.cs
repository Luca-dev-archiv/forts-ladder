using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

public sealed class PlayerRecord
{
    public string Name { get; set; } = "";
    public string SteamId { get; set; } = "";
    public int Side { get; set; }
    public int TeamRaw { get; set; }
    public bool Local { get; set; }
    public double Ping { get; set; }
}

public sealed class DefeatRecord
{
    public string Name { get; set; } = "";
    public int AtSeconds { get; set; }
    public string At => $"{AtSeconds / 60}:{AtSeconds % 60:00}";
}

public enum MatchStatus { Decided, VsAi, Unclear }

public sealed class MatchRecord
{
    public string? Map { get; set; }
    public string? Mode { get; set; }
    public bool? HostedLocally { get; set; }
    public ulong? LobbyId { get; set; }
    public string? Replay { get; set; }
    /// <summary>
    /// The game reported the match over. Only the replay line is still
    /// accepted after this.
    /// </summary>
    public bool Closed { get; set; }
    public Dictionary<int, string> Commanders { get; } = new();
    public Dictionary<string, PlayerRecord> Players { get; } = new();
    public List<DefeatRecord> Defeats { get; } = new();
    public DateTime PlayedAt { get; set; }

    public MatchStatus Status { get; private set; } = MatchStatus.Unclear;
    public int WinnerSide { get; private set; }
    public string UnclearReason { get; private set; } = "";

    public IEnumerable<PlayerRecord> BySide(int side) =>
        Players.Values.Where(p => p.Side == side);

    public int? DurationSeconds =>
        Defeats.Count > 0 ? Defeats.Max(d => d.AtSeconds) : null;

    public bool IsInteresting => Map is not null && (Players.Count > 0 || Defeats.Count > 0);

    /// <summary>
    /// Derive the winner from the roster and the defeats — never guess. The
    /// log writes no winner line; it names each loser individually.
    /// </summary>
    public void Resolve()
    {
        var defeated = Defeats.Select(d => d.Name).ToHashSet();
        var sides = Players.Values.Where(p => p.Side > 0)
            .GroupBy(p => p.Side)
            .ToDictionary(g => g.Key, g => g.Select(p => p.Name).ToList());

        var surviving = sides
            .Where(kv => kv.Value.Any(n => !defeated.Contains(n)))
            .Select(kv => kv.Key).ToList();

        if (sides.Count >= 2 && surviving.Count == 1)
        {
            Status = MatchStatus.Decided;
            WinnerSide = surviving[0];
            return;
        }
        // Everyone defeated. This is how a real game normally ends, not an
        // oddity: when the match finishes Forts records a defeat for the
        // remaining forts too, so a 1v1 ends with both players in the list —
        // seconds apart. Treating that as "more than one side has survivors"
        // made every real match unclear, and an unclear match was never
        // reported, so nothing ever reached the ladder.
        //
        // The first fort to fall is the side that lost. The timestamps come from
        // the simulation, so both clients agree on them.
        if (sides.Count >= 2 && defeated.Count > 0 && surviving.Count == 0)
        {
            var firstFall = new Dictionary<int, int>();
            foreach (var d in Defeats)
                foreach (var (side, names) in sides)
                    if (names.Contains(d.Name))
                        firstFall[side] = firstFall.TryGetValue(side, out var at)
                            ? Math.Min(at, d.AtSeconds) : d.AtSeconds;

            if (firstFall.Count == sides.Count)
            {
                var order = firstFall.OrderBy(kv => kv.Value).ToList();
                // A tie decides nothing: simultaneous defeats are a draw or a
                // disconnect, and guessing would be worse than saying so.
                if (order.Count >= 2 && order[0].Value < order[1].Value)
                {
                    Status = MatchStatus.Decided;
                    WinnerSide = order[1].Key;
                    return;
                }
            }
        }

        if (sides.Count == 1)
        {
            // Only one side in the roster: the opponent was the built-in AI,
            // which has no client. Irrelevant to a league, but named
            // properly instead of reported as an error.
            Status = MatchStatus.VsAi;
            WinnerSide = surviving.Count == 1 ? surviving[0] : 0;
            return;
        }
        Status = MatchStatus.Unclear;
        UnclearReason = defeated.Count == 0
            ? "no defeat logged"
            : "more than one side has survivors";
    }

    /// <summary>
    /// Stable identity, whichever log it came from. Both clients of a game
    /// have to produce the same key — reconciling two reports rests on it.
    /// </summary>
    public string Key
    {
        get
        {
            if (!string.IsNullOrEmpty(Replay))
                return "replay:" + Path.GetFileName(Replay.Replace('\\', '/'));
            return ReportKey;
        }
    }

    /// <summary>
    /// Identity that does not change when the replay name turns up.
    ///
    /// <see cref="Key"/> switches to the replay filename as soon as one exists,
    /// which makes it useless for "have I already reported this game?": the
    /// answer changes halfway through. This one is built from what the game did
    /// — the map, who played, when each fort fell, how long it lasted — so the
    /// result event and the replay event agree, and two different games never
    /// collide.
    /// </summary>
    public string ReportKey
    {
        get
        {
            var ids = Players.Values
                .Select(p => string.IsNullOrEmpty(p.SteamId) ? p.Name : p.SteamId)
                .OrderBy(x => x, StringComparer.Ordinal);
            var d = string.Join(",", Defeats.Select(x => $"{x.Name}@{x.AtSeconds}"));
            return $"{Map}|{string.Join("+", ids)}|{d}|{DurationSeconds}";
        }
    }
}

/// <summary>
/// State machine over the log lines. Kept in step with ladder/recorder.py —
/// both have to make the same thing out of the same log, or the client and
/// the analysis end up disagreeing about results.
/// </summary>
public sealed class LogParser
{
    private static readonly Regex ReLogin = new(@"Logged into Steam as (.+?) \((\d{17})\)");
    private static readonly Regex ReMode = new(@"^\s*Game mode: (.+?)\s*$");
    private static readonly Regex ReMap = new(@"^\s*Loading map (.+?)\s*$");
    private static readonly Regex ReMultiStart = new(@"OnMultiStart host (\d+), players (\d+)");
    private static readonly Regex ReRoster = new(
        @"^\s*(\d+): (.+?), Id (\d+), Team (\d+), (\w+), join at \d+, " +
        @"\((\w+)\), SteamID (\d{17}),.*?Local (\d), ping ([\d.]+)");
    private static readonly Regex ReClientSide = new(
        @"^\s*Client (.+?), id (\d+), side (-?\d+), fortId (-?\d+)");
    private static readonly Regex ReClientConn = new(
        @"Client Connected: (.+?), index (\d+), id (\d+), side (-?\d+)");
    private static readonly Regex ReCommander = new(@"^\s*Team(\d) commander: (\S+)");
    private static readonly Regex ReDefeat = new(@"^\s*(\d+):(\d\d) (.+?) has been defeated!");
    // To the end of the line, not to the first space: a replay is called
    // "v1.38.2_Up & Down_20260728_181126.fwr". Stopping at the space cost three
    // things at once — the file was never found on disk, the timestamp could not
    // be read out of the name, and two games on maps whose names start with the
    // same word collapsed into one match key.
    private static readonly Regex ReReplay = new(@"Replay saved as (.+\.fwr)\s*$");
    private static readonly Regex ReLobby = new(@"Setting lobby (\d+) game server (\d+)");
    private const string MatchEnd = "World::Execute mDone detected";

    private MatchRecord? _current;
    private ulong? _lobbyId;
    private string? _mode;

    public string? LocalName { get; private set; }
    public string? LocalSteamId { get; private set; }
    public DateTime FallbackTime { get; set; } = DateTime.Now;

    /// <summary>Raised as soon as a game is complete.</summary>
    public event Action<MatchRecord>? MatchFinished;

    /// <summary>
    /// Raised the moment the game says it is over, before the replay line.
    ///
    /// The outcome is decided about ten lines earlier than `Replay saved as`:
    /// the roster and the defeats are all in by then, and that is everything a
    /// result needs. Waiting for the replay name meant the client still showed a
    /// game as running long after it had finished — the filename is needed for
    /// the archive, not for the score.
    ///
    /// The record is not finished here and will be raised again through
    /// <see cref="MatchFinished"/> with the replay name attached.
    /// </summary>
    public event Action<MatchRecord>? MatchDecided;

    /// <summary>
    /// Raised when the game reports a new Steam lobby.
    ///
    /// This is the only place the lobby id exists outside the game: the ladder
    /// reads it out of the log rather than out of the process, which is what
    /// makes the handoff after a draft possible without touching Forts itself.
    /// </summary>
    public event Action<ulong>? LobbySeen;

    private static int SideOf(int team) => team >= 100 ? team % 100 : team;

    private void Finish()
    {
        if (_current is null) return;
        var m = _current;
        _current = null;
        if (!m.IsInteresting) return;
        m.Resolve();
        m.PlayedAt = TimeFromReplay(m.Replay) ?? FallbackTime;
        MatchFinished?.Invoke(m);
    }

    /// <summary>
    /// The real time of play, from the replay name
    /// (v1.38.2_Vanilla_20260719_135021.fwr). The log puts no wall clock on
    /// its lines — without the filename, every game parsed after the fact
    /// would be dated "now".
    /// </summary>
    private static DateTime? TimeFromReplay(string? replay)
    {
        if (string.IsNullOrEmpty(replay)) return null;
        var m = Regex.Match(replay, @"_(\d{8})_(\d{6})");
        if (!m.Success) return null;
        return DateTime.TryParseExact(m.Groups[1].Value + m.Groups[2].Value,
            "yyyyMMddHHmmss", null, System.Globalization.DateTimeStyles.None,
            out var dt) ? dt : null;
    }

    private PlayerRecord Player(string name)
    {
        var cur = _current!;
        if (!cur.Players.TryGetValue(name, out var p))
            cur.Players[name] = p = new PlayerRecord { Name = name };
        return p;
    }

    private void NoteSide(string name, int side, int fortId = -1)
    {
        // Never overwrite a real side with the -1 placeholder.
        if (side > 0) Player(name).Side = side;
    }

    public void Feed(string line)
    {
        var m = ReLogin.Match(line);
        if (m.Success)
        {
            LocalName = m.Groups[1].Value;
            LocalSteamId = m.Groups[2].Value;
            return;
        }
        m = ReLobby.Match(line);
        if (m.Success)
        {
            var seen = ulong.Parse(m.Groups[1].Value);
            var isNew = seen != _lobbyId;
            _lobbyId = seen;
            if (_current is not null) _current.LobbyId = _lobbyId;
            // Only on a change: the line is logged repeatedly for the same
            // lobby, and the handoff must not re-announce one lobby all evening.
            if (isNew) LobbySeen?.Invoke(seen);
            return;
        }

        // Handled before the "no match open" guard below: `Game mode` is
        // logged BEFORE `Loading map`, which is what starts a new match, so
        // assigning it to _current loses it every time.
        m = ReMode.Match(line);
        if (m.Success)
        {
            _mode = m.Groups[1].Value;
            if (_current is not null) _current.Mode = _mode;
            return;
        }

        // `Loading map` is the most reliable start marker: `Game mode` also
        // fires while browsing the map menu.
        m = ReMap.Match(line);
        if (m.Success)
        {
            Finish();
            _current = new MatchRecord { LobbyId = _lobbyId, Mode = _mode };
            _current.Map = Path.GetFileNameWithoutExtension(
                m.Groups[1].Value.Replace('\\', '/'));
            return;
        }

        if (_current is null)
        {
            // Roster lines can arrive before `Loading map` (lobby phase).
            if (ReRoster.IsMatch(line))
                _current = new MatchRecord { LobbyId = _lobbyId, Mode = _mode };
            else return;
        }

        if (_current.Closed && !ReReplay.IsMatch(line)
            && !ReCommander.IsMatch(line) && !ReDefeat.IsMatch(line))
            // The game is over, but the log is not finished talking about it:
            // the commander lines and the last defeat arrive after
            // `mDone detected` and before `Replay saved as`. Accepting only the
            // replay line threw the commanders away for every match ever
            // recorded — which is why no match ever showed one.
            return;

        m = ReMultiStart.Match(line);
        if (m.Success)
        {
            _current.HostedLocally = m.Groups[1].Value == "1";
            return;
        }

        m = ReRoster.Match(line);
        if (m.Success)
        {
            var p = Player(m.Groups[2].Value);
            p.SteamId = m.Groups[7].Value;
            p.TeamRaw = int.Parse(m.Groups[4].Value);
            p.Side = SideOf(p.TeamRaw);
            p.Local = m.Groups[8].Value == "1";
            p.Ping = double.Parse(m.Groups[9].Value,
                System.Globalization.CultureInfo.InvariantCulture);
            return;
        }

        m = ReClientSide.Match(line);
        if (m.Success)
        {
            NoteSide(m.Groups[1].Value, int.Parse(m.Groups[3].Value));
            return;
        }
        m = ReClientConn.Match(line);
        if (m.Success)
        {
            NoteSide(m.Groups[1].Value, int.Parse(m.Groups[4].Value));
            return;
        }
        m = ReCommander.Match(line);
        if (m.Success)
        {
            _current.Commanders[int.Parse(m.Groups[1].Value)] = m.Groups[2].Value;
            return;
        }
        m = ReDefeat.Match(line);
        if (m.Success)
        {
            _current.Defeats.Add(new DefeatRecord
            {
                Name = m.Groups[3].Value,
                AtSeconds = int.Parse(m.Groups[1].Value) * 60 + int.Parse(m.Groups[2].Value),
            });
            return;
        }
        m = ReReplay.Match(line);
        if (m.Success)
        {
            _current.Replay = m.Groups[1].Value;
            // The replay line is the real end: everything worth recording has
            // arrived by now.
            Finish();
            return;
        }

        if (line.Contains(MatchEnd))
        {
            // Announce the result now. Everything a score needs — who was in it
            // and who lost — is already here.
            if (_current.IsInteresting)
            {
                _current.Resolve();
                MatchDecided?.Invoke(_current);
            }
            // NOT the end of parsing. `Replay saved as` follows about ten lines
            // later, and finishing here dropped it every time — which also cost
            // the timestamp, since the replay filename is the only wall clock in
            // the log. Mark it closed instead, so lobby chatter from the next
            // match cannot attach itself to this one.
            _current.Closed = true;
        }
    }

    public void Flush() => Finish();

    public static List<MatchRecord> ParseFile(string path)
    {
        var results = new List<MatchRecord>();
        var parser = new LogParser { FallbackTime = File.GetLastWriteTime(path) };
        parser.MatchFinished += results.Add;
        var bytes = File.ReadAllBytes(path);
        var enc = bytes.Length >= 2 && bytes[0] == 0xFF && bytes[1] == 0xFE
            ? Encoding.Unicode : Encoding.UTF8;
        foreach (var line in enc.GetString(bytes).Split('\n'))
            parser.Feed(line.TrimEnd('\r').TrimStart('﻿'));
        parser.Flush();
        return results;
    }
}
