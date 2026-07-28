using System.IO;

namespace FortsLadder.Core;

/// <summary>
/// Lobbies that were set up by this ladder, remembered locally.
///
/// The series grouping deliberately merges games across a change of lobby: if
/// the host crashes and someone re-hosts, a Bo5 continues in a new lobby and
/// the rules say to play on, so cutting there would report one series as two.
///
/// That rule works against a drafted series. Restart the game, open a fresh
/// lobby, play the same opponent again within a few hours, and the heuristic
/// folds the new series into the old one — which is exactly what happened in
/// testing.
///
/// A drafted lobby is not a guess, though: the server told both clients its id.
/// So those ids are kept here and treated as a hard boundary — matches in one
/// are that series and nothing else. Everything with no id keeps the old
/// heuristic, because there is nothing better to go on.
///
/// A plain list of numbers next to the identity file. It is not a secret and it
/// is not worth a database.
/// </summary>
public sealed class LadderLobbies
{
    private readonly string _path;
    private readonly HashSet<ulong> _ids = new();

    public LadderLobbies(string? path = null)
    {
        _path = path ?? DefaultPath();
        Load();
    }

    private static string DefaultPath() =>
        Path.Combine(Path.GetDirectoryName(IdentityStore.DefaultPath())
                     ?? AppContext.BaseDirectory, "ladder-lobbies.txt");

    private void Load()
    {
        try
        {
            if (!File.Exists(_path)) return;
            foreach (var line in File.ReadAllLines(_path))
                if (ulong.TryParse(line.Trim(), out var id) && id != 0)
                    _ids.Add(id);
        }
        catch (IOException) { /* an unreadable list is an empty one */ }
    }

    /// <summary>Remember a lobby the ladder set up. Returns false if known.</summary>
    public bool Add(ulong id)
    {
        if (id == 0 || !_ids.Add(id)) return false;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
            // Appended, not rewritten: the file is a log of ids and appending
            // cannot lose the ones already in it.
            File.AppendAllText(_path, id + Environment.NewLine);
        }
        catch (IOException) { /* it still holds for this session */ }
        return true;
    }

    public bool Contains(ulong id) => _ids.Contains(id);

    /// <summary>The set the series grouping treats as hard boundaries.</summary>
    public IReadOnlySet<ulong> All => _ids;
}
