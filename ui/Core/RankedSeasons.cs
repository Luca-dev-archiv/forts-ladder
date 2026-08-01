using System.IO;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

/// <summary>
/// The game's ranked map pools, season by season.
///
/// <c>data/db/constants.lua</c> holds a table keyed by season number, and Forts
/// ships seasons before they start — the file on a machine playing season 43
/// already contains 44. The season in play is not written anywhere on disk: the
/// game asks its own backend for it. Nothing here can work it out, so nothing
/// here tries. This reads every season it finds and the caller picks.
///
/// Taking the highest key was the previous answer, and it is what published
/// next season's maps to the whole ladder while everyone was still playing this
/// one — a wrong pool that looks exactly like a right one, because the names
/// are real map names either way.
/// </summary>
public static class RankedSeasons
{
    /// <summary>A season number and the maps the game lists for it.</summary>
    public sealed record Season(int Number, List<string> Maps);

    // The table opens at the start of a line — the indented `RankedMaps` inside
    // `Game = { … }` is a fallback list with no season numbers at all, and
    // matching that one instead yields a pool from no particular season.
    private static readonly Regex ReBlock =
        new(@"\[(\d+)\]\s*=\s*\{(.*?)\n\t\}", RegexOptions.Singleline);
    private static readonly Regex ReMap =
        new(@"^\s*""([^""]+)""", RegexOptions.Multiline);

    /// <summary>
    /// Every season in this installation's constants.lua, lowest first.
    ///
    /// Empty when Forts cannot be found or the file has changed shape — the
    /// callers all treat that as "this machine cannot publish a pool", which is
    /// the honest answer and better than a guess.
    /// </summary>
    public static List<Season> All()
    {
        var forts = FortsPaths.FindFortsDir();
        if (forts is null) return new List<Season>();
        var constants = Path.Combine(forts, "data", "db", "constants.lua");
        if (!File.Exists(constants)) return new List<Season>();

        string text;
        try { text = File.ReadAllText(constants); }
        catch (IOException) { return new List<Season>(); }

        var start = text.IndexOf("\nRankedMaps", StringComparison.Ordinal);
        if (start < 0) return new List<Season>();

        var found = new List<Season>();
        foreach (Match block in ReBlock.Matches(text[start..]))
        {
            if (!int.TryParse(block.Groups[1].Value, out var number)) continue;
            var maps = new List<string>();
            foreach (Match m in ReMap.Matches(block.Groups[2].Value))
                maps.Add(m.Groups[1].Value);
            // Ladder rule: Hillfort is permanently banned in duels, so it never
            // reaches a pool this program publishes.
            maps.RemoveAll(x => x == "Hillfort");
            if (maps.Count > 0) found.Add(new Season(number, maps));
        }
        found.Sort((a, b) => a.Number.CompareTo(b.Number));
        return found;
    }

    /// <summary>The maps for one season, or null if this install has no such
    /// season.</summary>
    public static List<string>? Maps(int number) =>
        All().FirstOrDefault(s => s.Number == number)?.Maps;

    /// <summary>"21–44", for telling somebody what they may choose.</summary>
    public static string RangeText(IReadOnlyList<Season> seasons) =>
        seasons.Count == 0 ? ""
        : seasons.Count == 1 ? seasons[0].Number.ToString()
        : $"{seasons[0].Number}–{seasons[^1].Number}";
}
