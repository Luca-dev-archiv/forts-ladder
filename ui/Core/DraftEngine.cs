using System.IO;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

/// <summary>
/// Pick &amp; ban, interactive version.
///
/// `ladder/draft.py` stays authoritative: it verifies afterwards that what
/// was played matches what was drafted. But a draft is a click-by-click
/// interaction, and the client cannot start a Python process per keystroke.
/// The rules here are kept deliberately identical; where they diverge the
/// Python version wins, because the tests hang off it.
///
/// Both sides ban equally often and pick equally often. If the ban count
/// does not divide evenly, one map is struck by lot beforehand — fair to
/// both, which letting one side ban more often would not be.
/// </summary>
public enum DraftAction { BanMap, PickMap, BanCommander, PickCommander }

public enum DraftSide { A, B }

public sealed record DraftStep(DraftSide? Side, DraftAction Action, int? Game = null)
{
    /// <summary>Whose turn it is, in the user's language.</summary>
    public string Describe() => Action switch
    {
        DraftAction.BanMap => Loc.T("draft.step_ban_map", Side!),
        DraftAction.PickMap => Loc.T("draft.step_pick_map", Side!),
        DraftAction.BanCommander => Loc.T("draft.step_ban_commander", Side!),
        _ => Loc.T("draft.step_pick_commander", Game ?? 1),
    };
}

public sealed record DraftChoice(DraftSide Side, DraftAction Action, string Value,
                                 int? Game = null);

public sealed class DraftEngine
{
    public List<string> MapPool { get; }
    public List<string> CommanderPool { get; }
    public int BestOf { get; }
    public string? NeutralStrike { get; }
    public List<DraftStep> Steps { get; } = new();
    public List<DraftChoice> Choices { get; } = new();

    private readonly Dictionary<DraftSide, string> _pendingBlind = new();
    private readonly Dictionary<DraftSide, HashSet<string>> _burned = new()
    {
        [DraftSide.A] = new(), [DraftSide.B] = new(),
    };

    public DraftEngine(IEnumerable<string> maps, IEnumerable<string> commanders,
                       int bestOf = 3, int commanderBansPerSide = 1,
                       DraftSide firstBan = DraftSide.A, int? strikeSeed = null)
    {
        if (bestOf < 1 || bestOf % 2 == 0)
            throw new ArgumentException("best-of must be odd", nameof(bestOf));

        MapPool = maps.Distinct().ToList();
        CommanderPool = commanders.Distinct().ToList();
        BestOf = bestOf;
        if (MapPool.Count < bestOf)
            throw new ArgumentException(
                $"map pool ({MapPool.Count}) smaller than Bo{bestOf}");

        if ((MapPool.Count - bestOf) % 2 != 0)
        {
            var rnd = new Random(strikeSeed ?? StableSeed(MapPool));
            NeutralStrike = MapPool[rnd.Next(MapPool.Count)];
            MapPool.Remove(NeutralStrike);
        }

        var bans = MapPool.Count - bestOf;
        var picks = bestOf - 1;
        var firstHalf = bans >= 4 ? bans / 2 - (bans / 2) % 2 : bans;

        void Alternate(int count, DraftSide start, DraftAction action, bool numbered)
        {
            var side = start;
            for (var i = 0; i < count; i++)
            {
                Steps.Add(new DraftStep(side, action, numbered ? i + 1 : null));
                side = Other(side);
            }
        }

        Alternate(firstHalf, firstBan, DraftAction.BanMap, false);
        // Whoever bans first picks second.
        Alternate(picks, Other(firstBan), DraftAction.PickMap, true);
        Alternate(bans - firstHalf,
                  firstHalf % 2 == 0 ? firstBan : Other(firstBan),
                  DraftAction.BanMap, false);
        for (var i = 0; i < commanderBansPerSide; i++)
        {
            Steps.Add(new DraftStep(DraftSide.A, DraftAction.BanCommander));
            Steps.Add(new DraftStep(DraftSide.B, DraftAction.BanCommander));
        }
        for (var g = 1; g <= bestOf; g++)
            Steps.Add(new DraftStep(null, DraftAction.PickCommander, g));
    }

    private static int StableSeed(IEnumerable<string> pool) =>
        string.Join("|", pool).Aggregate(17, (a, c) => unchecked(a * 31 + c));

    public static DraftSide Other(DraftSide s) =>
        s == DraftSide.A ? DraftSide.B : DraftSide.A;

    // --------------------------------------------------------------- State
    public int StepIndex
    {
        get
        {
            var idx = 0;
            foreach (var step in Steps)
            {
                if (step.Action == DraftAction.PickCommander && step.Side is null)
                {
                    var got = Choices.Count(c => c.Action == DraftAction.PickCommander
                                                 && c.Game == step.Game);
                    if (got < 2) return idx;
                }
                else if (!Choices.Any(c => IndexOf(c) == idx)) return idx;
                idx++;
            }
            return Steps.Count;
        }
    }

    private int IndexOf(DraftChoice c)
    {
        for (var i = 0; i < Steps.Count; i++)
        {
            var s = Steps[i];
            if (s.Action != c.Action || s.Game != c.Game) continue;
            if (s.Side is not null && s.Side != c.Side) continue;
            var before = Choices.TakeWhile(x => !ReferenceEquals(x, c))
                .Count(x => x.Action == c.Action && x.Game == c.Game &&
                            (s.Side is null || x.Side == c.Side));
            if (before == 0) return i;
        }
        return -1;
    }

    public bool Done => StepIndex >= Steps.Count;
    public DraftStep? Current => StepIndex < Steps.Count ? Steps[StepIndex] : null;

    public List<string> BannedMaps => Choices
        .Where(c => c.Action == DraftAction.BanMap).Select(c => c.Value).ToList();

    public Dictionary<int, string> PickedMaps => Choices
        .Where(c => c.Action == DraftAction.PickMap && c.Game is not null)
        .ToDictionary(c => c.Game!.Value, c => c.Value);

    public List<string> RemainingMaps
    {
        get
        {
            var gone = BannedMaps.Concat(PickedMaps.Values).ToHashSet();
            return MapPool.Where(m => !gone.Contains(m)).ToList();
        }
    }

    public List<string> BannedCommanders => Choices
        .Where(c => c.Action == DraftAction.BanCommander).Select(c => c.Value).ToList();

    public bool IsPendingBlind(DraftSide side) => _pendingBlind.ContainsKey(side);

    public List<string> AvailableCommandersFor(DraftSide side)
    {
        var gone = BannedCommanders.Concat(_burned[side]).ToHashSet();
        return CommanderPool.Where(c => !gone.Contains(c)).ToList();
    }

    public List<string> LegalOptions(DraftSide? side = null)
    {
        var step = Current;
        if (step is null) return new List<string>();
        return step.Action switch
        {
            DraftAction.BanMap or DraftAction.PickMap => RemainingMaps,
            DraftAction.BanCommander => CommanderPool
                .Where(c => !BannedCommanders.Contains(c)).ToList(),
            DraftAction.PickCommander when side is DraftSide s =>
                IsPendingBlind(s) ? new List<string>() : AvailableCommandersFor(s),
            _ => new List<string>(),
        };
    }

    public void Apply(string value, DraftSide? side = null)
    {
        var step = Current ?? throw new InvalidOperationException("draft is finished");
        if (step.Side is DraftSide fixedSide)
        {
            if (side is not null && side != fixedSide)
                throw new InvalidOperationException($"side {side} is not on turn");
            side = fixedSide;
        }
        else if (side is null)
            throw new InvalidOperationException("simultaneous pick needs a side");

        if (!LegalOptions(side).Contains(value))
            throw new InvalidOperationException($"{value} is not selectable here");

        if (step.Action == DraftAction.PickCommander)
        {
            // Blind: hold picks until both sides have locked in.
            _pendingBlind[side.Value] = value;
            if (_pendingBlind.Count == 2)
            {
                foreach (var (s, v) in _pendingBlind)
                    Choices.Add(new DraftChoice(s, step.Action, v, step.Game));
                _pendingBlind.Clear();
            }
            return;
        }
        Choices.Add(new DraftChoice(side.Value, step.Action, value, step.Game));
    }

    public void NoteResult(int game, DraftSide winner)
    {
        var pick = Choices.FirstOrDefault(
            c => c.Action == DraftAction.PickCommander && c.Game == game
                 && c.Side == winner);
        if (pick is not null) _burned[winner].Add(pick.Value);
    }

    public sealed record PlannedGame(int Game, string? Map, string? PickedBy,
                                     string? CommanderA, string? CommanderB,
                                     bool Decider);

    public List<PlannedGame> Plan()
    {
        var chosen = PickedMaps;
        var leftover = RemainingMaps;
        var list = new List<PlannedGame>();
        for (var g = 1; g <= BestOf; g++)
        {
            var decider = g == BestOf && BestOf > 1;
            string? map = decider
                ? (leftover.Count == 1 ? leftover[0] : null)
                : chosen.GetValueOrDefault(g)
                  ?? (BestOf == 1 && leftover.Count == 1 ? leftover[0] : null);
            var by = Choices.FirstOrDefault(
                c => c.Action == DraftAction.PickMap && c.Game == g)?.Side.ToString();
            var picks = Choices
                .Where(c => c.Action == DraftAction.PickCommander && c.Game == g)
                .ToDictionary(c => c.Side, c => c.Value);
            list.Add(new PlannedGame(g, map, by,
                picks.GetValueOrDefault(DraftSide.A),
                picks.GetValueOrDefault(DraftSide.B), decider));
        }
        return list;
    }
}

/// <summary>Commander display names, read from the game rather than typed.</summary>
public static class CommanderNames
{
    private static Dictionary<string, string>? _cache;

    public static Dictionary<string, string> All(string language = "English")
    {
        if (_cache is not null) return _cache;
        _cache = new Dictionary<string, string>();
        var forts = FortsPaths.FindFortsDir();
        if (forts is null) return _cache;

        var baseDir = Path.Combine(forts, "data", "mods", $"language-{language}", "mods");
        if (!Directory.Exists(baseDir)) return _cache;
        foreach (var dir in Directory.GetDirectories(baseDir, "commander-*"))
        {
            var file = Path.Combine(dir, "strings.lua");
            if (!File.Exists(file)) continue;
            try
            {
                var m = Regex.Match(File.ReadAllText(file), @"Name\s*=\s*L""([^""]+)""");
                if (m.Success) _cache[Path.GetFileName(dir)] = m.Groups[1].Value;
            }
            catch (IOException) { /* one unreadable file must not stop the rest */ }
        }
        return _cache;
    }

    public static List<string> Installed()
    {
        var forts = FortsPaths.FindFortsDir();
        if (forts is null) return new List<string>();
        var mods = Path.Combine(forts, "data", "mods");
        if (!Directory.Exists(mods)) return new List<string>();
        return Directory.GetDirectories(mods, "commander-*")
            .Select(Path.GetFileName).OfType<string>()
            .Where(n => Regex.IsMatch(n, @"^commander-[a-z0-9-]+$"))
            .OrderBy(n => n).ToList();
    }

    /// <summary>Display name, with a readable fallback instead of a raw id.</summary>
    public static string Display(string id) =>
        All().TryGetValue(id, out var n) ? n
        : System.Globalization.CultureInfo.CurrentCulture.TextInfo.ToTitleCase(
            id.Replace("commander-", "").Split('-', 2).Last().Replace('-', ' '));
}
