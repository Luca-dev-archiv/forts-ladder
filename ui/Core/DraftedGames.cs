using System.IO;

namespace FortsLadder.Core;

/// <summary>
/// Which drafted series each recorded game belonged to.
///
/// The series grouping has to guess for any game whose log carries no lobby id,
/// which on a client that is not hosting is *every* game: "the same two people
/// within three hours" is the best it can do, and it filed games under whichever
/// series happened to be last — including a drafted one they had nothing to do
/// with.
///
/// But there is no need to guess for a game the ladder itself reported: at that
/// moment the client knew exactly which series it was. That knowledge was thrown
/// away. Here it is kept — one line per game, `series-id<TAB>game-key` — and the
/// grouping looks at it before anything else.
///
/// Append-only, next to the identity file. Losing it degrades to the old
/// guessing rather than to an error.
/// </summary>
public sealed class DraftedGames
{
    private readonly string _path;
    private readonly Dictionary<string, string> _seriesOf = new();

    public DraftedGames(string? path = null)
    {
        _path = path ?? DefaultPath();
        Load();
    }

    private static string DefaultPath() =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "drafted-games.tsv");

    private void Load()
    {
        try
        {
            if (!File.Exists(_path)) return;
            foreach (var line in File.ReadAllLines(_path))
            {
                var parts = line.Split('\t');
                if (parts.Length == 2 && parts[0].Length > 0 && parts[1].Length > 0)
                    _seriesOf[parts[1]] = parts[0];
            }
        }
        catch (IOException) { /* an unreadable file is an empty one */ }
    }

    /// <summary>Record that this game was played as part of that series.</summary>
    public void Note(string gameKey, string seriesId)
    {
        if (string.IsNullOrEmpty(gameKey) || string.IsNullOrEmpty(seriesId)) return;
        if (_seriesOf.TryGetValue(gameKey, out var had) && had == seriesId) return;
        _seriesOf[gameKey] = seriesId;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            // A key can contain no tab or newline — it is built from a map name,
            // Steam IDs and numbers — so a line is unambiguous.
            File.AppendAllText(_path, seriesId + "\t" + gameKey + Environment.NewLine);
        }
        catch (IOException) { /* it still holds for this session */ }
    }

    /// <summary>The series this game belongs to, if the ladder reported it.</summary>
    public string? SeriesOf(string gameKey)
        => _seriesOf.TryGetValue(gameKey, out var id) ? id : null;

    public IReadOnlyDictionary<string, string> All => _seriesOf;
}
