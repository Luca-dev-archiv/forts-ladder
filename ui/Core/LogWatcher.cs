using System.IO;
using System.Text;

namespace FortsLadder.Core;

/// <summary>
/// Tails log.txt while Forts is running.
///
/// Three properties of the file drive the whole design:
///  - **UTF-16LE**, so it only decodes on an even byte count.
///  - **Cleared on every game start.** If the file gets shorter, a new
///    session began and the parser has to be reset.
///  - **Lines can be cut off mid-write**, so the remainder is buffered until
///    the next line break.
///
/// Size and timestamp are read through an OPEN handle: NTFS updates the
/// directory entry of open files only lazily, so FileInfo.Length otherwise
/// returns stale values.
/// </summary>
public sealed class LogWatcher : IDisposable
{
    private readonly System.Threading.Timer _timer;
    private readonly object _lock = new();

    private string? _currentPath;
    private long _offset;
    private byte[] _pending = Array.Empty<byte>();
    private LogParser _parser = new();
    private bool _disposed;

    public event Action<MatchRecord>? MatchFinished;
    /// <summary>The result, as soon as the game reports it — about ten log lines
    /// before the replay name arrives.</summary>
    public event Action<MatchRecord>? MatchDecided;
    /// <summary>(text, everything running) — the state must not be guessed
    /// from the text, or it becomes language-dependent.</summary>
    public event Action<string, bool>? StatusChanged;
    public event Action<string, string>? AccountDetected;   // steamId, persona
    /// <summary>A new Steam lobby appeared in the log — the draft handoff
    /// waits for this rather than asking anyone to copy an id.</summary>
    public event Action<ulong>? LobbySeen;

    public string? CurrentAccount { get; private set; }
    public string? CurrentPersona { get; private set; }
    public bool GameLogSeen { get; private set; }

    public LogWatcher(TimeSpan interval)
    {
        _parser.MatchFinished += m => MatchFinished?.Invoke(m);
        _parser.MatchDecided += m => MatchDecided?.Invoke(m);
        _parser.LobbySeen += id => LobbySeen?.Invoke(id);
        _timer = new System.Threading.Timer(_ => Poll(), null, TimeSpan.Zero, interval);
    }

    private void Poll()
    {
        if (_disposed) return;
        lock (_lock)
        {
            try { PollCore(); }
            catch (Exception ex) { StatusChanged?.Invoke(Loc.T("status.read_error", ex.Message), false); }
        }
    }

    private void PollCore()
    {
        var dirs = FortsPaths.UserDirs();
        if (dirs.Count == 0)
        {
            StatusChanged?.Invoke(Loc.T(FortsPaths.FindFortsDir() is null
                ? "status.forts_missing" : "status.no_account"), false);
            return;
        }

        var dir = dirs[0];
        var path = Path.Combine(dir.FullName, "log.txt");
        if (!File.Exists(path))
        {
            StatusChanged?.Invoke(Loc.T("status.waiting_log"), false);
            return;
        }

        if (path != _currentPath)
        {
            _currentPath = path;
            _offset = 0;
            _pending = Array.Empty<byte>();
            ResetParser();
            CurrentAccount = dir.Name;
            CurrentPersona = FortsPaths.ReadPersona(dir);
            AccountDetected?.Invoke(CurrentAccount, CurrentPersona ?? "");
            StatusChanged?.Invoke(Loc.T("status.watching", dir.Name), true);
        }

        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read,
                                      FileShare.ReadWrite | FileShare.Delete);
        var size = fs.Length;

        if (size < _offset)
        {
            // File cleared -> Forts restarted. Anything incomplete up to
            // here is lost; start cleanly.
            _parser.Flush();
            ResetParser();
            _offset = 0;
            _pending = Array.Empty<byte>();
            StatusChanged?.Invoke(Loc.T("status.new_session"), true);
        }
        if (size == _offset) return;

        fs.Seek(_offset, SeekOrigin.Begin);
        var chunk = new byte[size - _offset];
        var read = fs.Read(chunk, 0, chunk.Length);
        _offset += read;

        var data = new byte[_pending.Length + read];
        Buffer.BlockCopy(_pending, 0, data, 0, _pending.Length);
        Buffer.BlockCopy(chunk, 0, data, _pending.Length, read);
        _pending = Array.Empty<byte>();

        // An odd trailing byte belongs to the next character.
        var usable = data.Length - (data.Length % 2);
        if (usable < data.Length)
            _pending = new[] { data[^1] };

        var text = Encoding.Unicode.GetString(data, 0, usable);
        var lastBreak = text.LastIndexOf('\n');
        if (lastBreak < 0)
        {
            // No complete line yet — everything goes back into the buffer.
            _pending = data;
            return;
        }
        if (lastBreak < text.Length - 1)
        {
            var rest = Encoding.Unicode.GetBytes(text[(lastBreak + 1)..]);
            var keep = new byte[rest.Length + _pending.Length];
            Buffer.BlockCopy(rest, 0, keep, 0, rest.Length);
            Buffer.BlockCopy(_pending, 0, keep, rest.Length, _pending.Length);
            _pending = keep;
        }

        GameLogSeen = true;
        foreach (var raw in text[..lastBreak].Split('\n'))
            _parser.Feed(raw.TrimEnd('\r').TrimStart('﻿'));
    }

    private void ResetParser()
    {
        _parser = new LogParser { FallbackTime = DateTime.Now };
        _parser.MatchFinished += m => MatchFinished?.Invoke(m);
        _parser.MatchDecided += m => MatchDecided?.Invoke(m);
        _parser.LobbySeen += id => LobbySeen?.Invoke(id);
    }

    public void Dispose()
    {
        _disposed = true;
        _timer.Dispose();
    }
}
