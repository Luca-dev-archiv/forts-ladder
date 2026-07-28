using System.IO;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

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
