using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace FortsLadder.Core;

/// <summary>
/// The host screen's lobby settings, and the lobby id, as plain files.
///
/// Two files in <c>users/&lt;steamid&gt;/</c> carry everything needed to hand a
/// drafted series over to the game:
///
/// <c>multiplayer.lua</c> is the host screen's own settings, as readable Lua.
/// Writing it before Forts starts is what makes a ladder lobby a ladder lobby:
/// the right size so nobody wanders in, sides and forts locked so nobody
/// switches mid-series, a password, and a name both players recognise. Nothing
/// here is a trick — these are the same values the host screen writes when you
/// click them.
///
/// <c>lobby.dat</c> holds the current Steam lobby id in its first eight bytes,
/// little endian. That is the id the other side needs to join, and reading it
/// here is immediate: waiting for the game log to mention the lobby was the
/// slowest part of the handoff.
///
/// What is *not* in multiplayer.lua: the map. It is chosen in the lobby window,
/// so the drafted map has to be picked by hand — the client says which one
/// rather than pretending it set it.
/// </summary>
public static class LobbySettings
{
    /// <summary>The file is read at startup, so writing it while the game is
    /// running achieves nothing and is overwritten on exit.</summary>
    public static bool FortsRunning() =>
        System.Diagnostics.Process.GetProcessesByName("Forts").Length > 0;

    private static string? UserDir() =>
        FortsPaths.UserDirs().FirstOrDefault()?.FullName;

    /// <summary>The Steam lobby id Forts last used, or null.</summary>
    public static ulong? CurrentLobby()
    {
        var dir = UserDir();
        if (dir is null) return null;
        var path = Path.Combine(dir, "lobby.dat");
        try
        {
            if (!File.Exists(path)) return null;
            var bytes = File.ReadAllBytes(path);
            if (bytes.Length < 8) return null;
            var id = BitConverter.ToUInt64(bytes, 0);
            // A Steam lobby id is never zero, and a partially written file
            // reads as one. Treated as "not yet" rather than as a lobby.
            return id == 0 ? null : id;
        }
        catch (IOException) { return null; }
    }

    /// <summary>When lobby.dat was last written — used to ignore a stale id
    /// from a previous session.</summary>
    public static DateTime? LobbyWrittenAt()
    {
        var dir = UserDir();
        if (dir is null) return null;
        var path = Path.Combine(dir, "lobby.dat");
        try
        {
            return File.Exists(path) ? File.GetLastWriteTimeUtc(path) : null;
        }
        catch (IOException) { return null; }
    }

    public sealed record Result(bool Ok, string Message, string? Password = null);

    private static readonly Regex ReEntry =
        new(@"^(\s*)(\w+)(\s*=\s*)(.+?)(,?)\s*$", RegexOptions.Compiled);

    /// <summary>
    /// Write the league settings for a series that is about to be hosted.
    ///
    /// Every existing key is kept and only the named ones are replaced, so a
    /// setting this ladder has no opinion about stays whatever the host had.
    /// A backup is taken once, next to the file, and never overwritten — the
    /// point of a backup is the state before the first change.
    /// </summary>
    public static Result Apply(string lobbyName, int maxPlayers,
                              string? password = null)
    {
        var dir = UserDir();
        if (dir is null) return new Result(false, Loc.T("lobby.no_account"));
        if (FortsRunning()) return new Result(false, Loc.T("lobby.close_forts"));

        var path = Path.Combine(dir, "multiplayer.lua");
        if (!File.Exists(path)) return new Result(false, Loc.T("lobby.no_file"));

        password ??= NewPassword();
        var wanted = new Dictionary<string, string>
        {
            // Exactly the series size: an extra seat is an extra way for the
            // wrong person to end up in a rated match.
            ["MaxPlayers"] = maxPlayers.ToString(),
            ["Password"] = $"L\"{Escape(password)}\"",
            ["ServerName"] = $"L\"{Escape(lobbyName)}\"",
            // Nobody changes side or fort once the draft has decided them.
            ["TeamsUnlocked"] = "false",
            ["FortsUnlocked"] = "false",
        };

        try
        {
            var backup = path + ".ladder-backup";
            if (!File.Exists(backup)) File.Copy(path, backup);

            var lines = File.ReadAllLines(path).ToList();
            var seen = new HashSet<string>();
            for (var i = 0; i < lines.Count; i++)
            {
                var m = ReEntry.Match(lines[i]);
                if (!m.Success || !wanted.TryGetValue(m.Groups[2].Value, out var v))
                    continue;
                lines[i] = m.Groups[1].Value + m.Groups[2].Value
                         + m.Groups[3].Value + v + ",";
                seen.Add(m.Groups[2].Value);
            }
            // A key the file did not have is added inside the table rather than
            // appended after it, which would not be valid Lua.
            var missing = wanted.Where(kv => !seen.Contains(kv.Key)).ToList();
            if (missing.Count > 0)
            {
                var close = lines.FindLastIndex(l => l.TrimStart().StartsWith("}"));
                if (close < 0) return new Result(false, Loc.T("lobby.unreadable"));
                lines.InsertRange(close, missing.Select(
                    kv => $"\t{kv.Key} = {kv.Value},"));
            }
            File.WriteAllLines(path, lines, new UTF8Encoding(false));
            return new Result(true, Loc.T("lobby.written", maxPlayers), password);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new Result(false, ex.Message);
        }
    }

    /// <summary>Put back whatever was there before the first change.</summary>
    public static Result Restore()
    {
        var dir = UserDir();
        if (dir is null) return new Result(false, Loc.T("lobby.no_account"));
        if (FortsRunning()) return new Result(false, Loc.T("lobby.close_forts"));
        var path = Path.Combine(dir, "multiplayer.lua");
        var backup = path + ".ladder-backup";
        try
        {
            if (!File.Exists(backup))
                return new Result(false, Loc.T("lobby.no_backup"));
            File.Copy(backup, path, overwrite: true);
            return new Result(true, Loc.T("lobby.restored"));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new Result(false, ex.Message);
        }
    }

    /// <summary>Short enough to read out over voice, long enough to keep a
    /// passer-by out. It is not protecting anything valuable.</summary>
    private static string NewPassword()
    {
        const string alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
        var chars = new char[5];
        for (var i = 0; i < chars.Length; i++)
            chars[i] = alphabet[System.Security.Cryptography
                .RandomNumberGenerator.GetInt32(alphabet.Length)];
        return new string(chars);
    }

    /// <summary>
    /// Lua string escaping, and ASCII only.
    ///
    /// The file Forts ships is pure ASCII with no byte-order mark, and its
    /// strings are `L"..."` wide literals whose encoding this project has not
    /// established. A lobby name is cosmetic; a settings file the game cannot
    /// read is not — so a player whose ladder name has an umlaut in it gets a
    /// plainer lobby name rather than a broken config.
    /// </summary>
    private static string Escape(string s)
    {
        var sb = new StringBuilder(s.Length);
        foreach (var ch in s)
        {
            if (ch == '\\' || ch == '"') sb.Append('\\').Append(ch);
            else if (ch >= ' ' && ch < (char)127) sb.Append(ch);
            else sb.Append('?');
        }
        return sb.ToString();
    }
}
