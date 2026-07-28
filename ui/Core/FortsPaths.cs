using System.IO;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace FortsLadder.Core;

/// <summary>
/// Locates the Forts install and the per-account directories.
///
/// Same search order as ladder/paths.py: environment variable, then Steam
/// from the registry including every library folder, then the usual places.
/// A hardcoded path would be the first reason the tool fails to start for
/// someone else — plenty of people keep Steam on a second drive.
/// </summary>
public static class FortsPaths
{
    public const int AppId = 410900;

    private static readonly string[] Defaults =
    {
        @"C:\Program Files (x86)\Steam\steamapps\common\Forts",
        @"C:\Program Files\Steam\steamapps\common\Forts",
        @"D:\SteamLibrary\steamapps\common\Forts",
        @"E:\SteamLibrary\steamapps\common\Forts",
    };

    private static string? _cached;

    private static bool LooksLikeForts(string dir) =>
        File.Exists(Path.Combine(dir, "Forts.exe")) &&
        Directory.Exists(Path.Combine(dir, "data"));

    private static string? SteamRoot()
    {
        foreach (var (hive, key) in new (RegistryKey, string)[]
                 {
                     (Registry.CurrentUser, @"Software\Valve\Steam"),
                     (Registry.LocalMachine, @"SOFTWARE\WOW6432Node\Valve\Steam"),
                 })
        {
            try
            {
                using var k = hive.OpenSubKey(key);
                if (k is null) continue;
                foreach (var name in new[] { "SteamPath", "InstallPath" })
                {
                    if (k.GetValue(name) is string p && Directory.Exists(p))
                        return p;
                }
            }
            catch (Exception) { /* registry unreadable -- carry on with the rest */ }
        }
        return null;
    }

    private static IEnumerable<string> LibraryFolders(string steam)
    {
        var vdf = Path.Combine(steam, "steamapps", "libraryfolders.vdf");
        if (!File.Exists(vdf)) yield break;
        string text;
        try { text = File.ReadAllText(vdf); }
        catch (IOException) { yield break; }
        foreach (Match m in Regex.Matches(text, "\"path\"\\s+\"([^\"]+)\""))
            yield return m.Groups[1].Value.Replace(@"\\", @"\");
    }

    public static string? FindFortsDir()
    {
        if (_cached is not null) return _cached;

        var env = Environment.GetEnvironmentVariable("FORTS_DIR");
        if (!string.IsNullOrWhiteSpace(env))
            // A variable that is set but wrong is a user error and is not
            // silently ignored.
            return _cached = LooksLikeForts(env) ? env : null;

        var roots = new List<string>();
        var steam = SteamRoot();
        if (steam is not null)
        {
            roots.Add(steam);
            roots.AddRange(LibraryFolders(steam));
        }
        foreach (var root in roots)
        {
            var cand = Path.Combine(root, "steamapps", "common", "Forts");
            if (LooksLikeForts(cand)) return _cached = cand;
        }
        foreach (var d in Defaults)
            if (LooksLikeForts(d)) return _cached = d;
        return null;
    }

    /// <summary>Account-Verzeichnisse, neuestes Log zuerst.</summary>
    public static List<DirectoryInfo> UserDirs()
    {
        var forts = FindFortsDir();
        if (forts is null) return new List<DirectoryInfo>();
        var users = new DirectoryInfo(Path.Combine(forts, "users"));
        if (!users.Exists) return new List<DirectoryInfo>();

        return users.GetDirectories()
            .Where(d => Regex.IsMatch(d.Name, @"^\d{17}$"))
            .OrderByDescending(d =>
            {
                var log = new FileInfo(Path.Combine(d.FullName, "log.txt"));
                return log.Exists ? log.LastWriteTimeUtc : DateTime.MinValue;
            })
            .ToList();
    }

    public static DirectoryInfo? ActiveUserDir() => UserDirs().FirstOrDefault();

    /// <summary>Steam display name, taken from the log's login line.</summary>
    public static string? ReadPersona(DirectoryInfo userDir)
    {
        var log = Path.Combine(userDir.FullName, "log.txt");
        if (!File.Exists(log)) return null;
        try
        {
            using var fs = new FileStream(log, FileMode.Open, FileAccess.Read,
                                          FileShare.ReadWrite);
            var buf = new byte[Math.Min(200_000, fs.Length)];
            _ = fs.Read(buf, 0, buf.Length);
            var text = System.Text.Encoding.Unicode.GetString(buf);
            var hits = Regex.Matches(text, @"Logged into Steam as (.+?) \((\d{17})\)");
            return hits.Count > 0 ? hits[^1].Groups[1].Value : null;
        }
        catch (IOException) { return null; }
    }
}
