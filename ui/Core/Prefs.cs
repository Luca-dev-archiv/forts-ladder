using System.IO;
using System.Text.Json;

namespace FortsLadder.Core;

/// <summary>
/// The handful of choices this program remembers.
///
/// Small on purpose. The language already had its own file and stays there; what
/// arrives here is everything that changes how the program *behaves* when nobody
/// is looking at it — whether it keeps running in the tray, whether it starts
/// with Windows, whether it keeps watching the game log with the window closed.
///
/// Every one of those defaults to the cautious answer. A tool that installs
/// itself into startup and then watches a game in the background because it was
/// run once is not a tool anybody asked for; each of these is a box somebody
/// ticks.
///
/// A JSON file next to the identity store. Unreadable means defaults, because a
/// preference that cannot be read is a preference nobody expressed.
/// </summary>
public sealed class Prefs
{
    private readonly string _path;
    private Dictionary<string, bool> _flags = new();

    public Prefs(string? path = null)
    {
        _path = path ?? DefaultPath();
        Load();
    }

    private static string DefaultPath() =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "prefs.json");

    // --- the keys, named once
    /// <summary>Closing the window hides it instead of quitting.</summary>
    public const string CloseToTray = "close_to_tray";
    /// <summary>Keep reading the game log while the window is closed.</summary>
    public const string TrackInBackground = "track_in_background";
    /// <summary>Start with Windows, hidden in the tray.</summary>
    public const string StartWithWindows = "start_with_windows";

    private void Load()
    {
        try
        {
            if (!File.Exists(_path)) return;
            _flags = JsonSerializer.Deserialize<Dictionary<string, bool>>(
                File.ReadAllText(_path)) ?? new();
        }
        catch (Exception ex) when (ex is IOException or JsonException)
        {
            _flags = new();
        }
    }

    public bool Get(string key, bool fallback = false)
        => _flags.TryGetValue(key, out var v) ? v : fallback;

    public void Set(string key, bool value)
    {
        _flags[key] = value;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            File.WriteAllText(_path, JsonSerializer.Serialize(_flags));
        }
        catch (IOException) { /* it still holds for this session */ }
    }
}
