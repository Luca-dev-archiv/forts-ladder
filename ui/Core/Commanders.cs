using System.IO;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

/// <summary>
/// Commander ids and display names, read from the game rather than typed.
///
/// Two sources, because the game has two. Most commanders sit in
/// <c>data/mods/commander-*</c> as loose folders, but DLC content lives inside
/// the compressed <c>data/packs/*.pack</c> files and has no folder at all —
/// which is why scanning directories alone found 12 of 16 and quietly left the
/// Moonshot commanders out of every pool the ladder published.
///
/// The game's own catalogue, <c>data/db/mods.lua</c>, lists all of them. It is
/// read, never written: editing that file makes Forts declare its data corrupt.
/// </summary>
public static class CommanderNames
{
    private static Dictionary<string, string>? _cache;
    private static List<string>? _installed;

    private static readonly Regex ReCatalogue =
        new(@"\['(commander-[a-z0-9-]+)'\]\s*=\s*true", RegexOptions.Compiled);
    private static readonly Regex ReName =
        new(@"Name\s*=\s*L""([^""]+)""", RegexOptions.Compiled);

    /// <summary>Every commander this installation knows, loose or packed.</summary>
    public static List<string> Installed()
    {
        if (_installed is not null) return _installed;
        var forts = FortsPaths.FindFortsDir();
        if (forts is null) return _installed = new List<string>();

        var ids = new SortedSet<string>(StringComparer.Ordinal);

        // The catalogue first: it covers the packed ones too.
        var catalogue = Path.Combine(forts, "data", "db", "mods.lua");
        try
        {
            if (File.Exists(catalogue))
                foreach (Match m in ReCatalogue.Matches(File.ReadAllText(catalogue)))
                    ids.Add(m.Groups[1].Value);
        }
        catch (IOException) { /* fall through to the directory scan */ }

        // Then the folders, which also cover anything hand-installed that the
        // catalogue has not been regenerated for.
        var mods = Path.Combine(forts, "data", "mods");
        if (Directory.Exists(mods))
            foreach (var dir in Directory.GetDirectories(mods, "commander-*"))
            {
                var name = Path.GetFileName(dir);
                if (name is not null && Regex.IsMatch(name, @"^commander-[a-z0-9-]+$"))
                    ids.Add(name);
            }

        return _installed = ids.ToList();
    }

    /// <summary>Display names, per language, for whatever has a strings file.</summary>
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
                var m = ReName.Match(File.ReadAllText(file));
                if (m.Success) _cache[Path.GetFileName(dir)] = m.Groups[1].Value;
            }
            catch (IOException) { /* one unreadable file must not stop the rest */ }
        }
        return _cache;
    }

    /// <summary>
    /// Display name, with a readable fallback instead of a raw id.
    ///
    /// The fallback is load-bearing for the packed commanders: their strings
    /// file is inside a pack, so the id is all there is. It reads correctly for
    /// them ("commander-cf-moonshine" is Moonshine) and wrongly for some of the
    /// loose ones ("overclocker" is called Overdrive) — which is exactly why
    /// the loose files are preferred whenever they exist.
    /// </summary>
    public static string Display(string id) =>
        All().TryGetValue(id, out var n) ? n
        : System.Globalization.CultureInfo.CurrentCulture.TextInfo.ToTitleCase(
            id.Replace("commander-", "").Split('-', 2).Last().Replace('-', ' '));

    /// <summary>Forget both caches — used after the Forts path is changed.</summary>
    public static void Reset()
    {
        _cache = null;
        _installed = null;
    }
}
