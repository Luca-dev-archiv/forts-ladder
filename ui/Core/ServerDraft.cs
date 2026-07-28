namespace FortsLadder.Core;

/// <summary>
/// A draft played against another person, over the server.
///
/// The client deliberately holds **no** draft rules. It sends a value and
/// re-reads the state; whose turn it is, what is legal, and what the opponent
/// is allowed to see are all decided server-side. Duplicating that here would
/// mean two implementations drifting apart, and the half that decides what you
/// may see is the half that must not be on the machine of the person it is
/// being hidden from.
///
/// Polling rather than a socket: the whole exchange is a handful of clicks per
/// minute, a poll is trivially reconnect-safe, and a dropped connection needs
/// no special handling — the next tick simply succeeds again.
/// </summary>
public sealed class ServerDraft : IDisposable
{
    private readonly ApiClient _api;
    private readonly System.Windows.Threading.DispatcherTimer _timer;
    private bool _busy;

    public string? DraftId { get; private set; }
    public string? JoinCode { get; private set; }
    public DraftStateDto? State { get; private set; }
    public string? LastError { get; private set; }

    /// <summary>Raised on the UI thread whenever the state was re-read.</summary>
    public event Action? Changed;

    public bool Active => DraftId is not null;

    public ServerDraft(ApiClient api)
    {
        _api = api;
        // One second: fast enough that the opponent's ban feels immediate,
        // slow enough to be invisible in the server log.
        _timer = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(1),
        };
        _timer.Tick += async (_, _) => await RefreshAsync();
    }

    public async Task<bool> CreateAsync(IEnumerable<string> maps,
                                        IEnumerable<string> commanders,
                                        int bestOf)
    {
        var created = await _api.PostAsync<DraftCreatedDto>("/drafts", new
        {
            map_pool = maps.ToList(),
            commander_pool = commanders.ToList(),
            best_of = bestOf,
            commander_bans_per_side = 1,
            step_seconds = 30.0,
        });
        if (created is null)
        {
            LastError = _api.LastError;
            return false;
        }
        DraftId = created.Id;
        JoinCode = created.Join_Code;
        State = created.State;
        _timer.Start();
        Changed?.Invoke();
        return true;
    }

    public async Task<bool> JoinAsync(string code)
    {
        var joined = await _api.PostAsync<DraftCreatedDto>(
            $"/drafts/join/{Uri.EscapeDataString(code.Trim())}");
        if (joined is null)
        {
            LastError = _api.LastError;
            return false;
        }
        DraftId = joined.Id;
        JoinCode = code.Trim().ToUpperInvariant();
        State = joined.State;
        _timer.Start();
        Changed?.Invoke();
        return true;
    }

    /// <summary>Send one choice. The answer is the new state.</summary>
    public async Task<bool> ApplyAsync(string value)
    {
        if (DraftId is null) return false;
        var next = await _api.PostAsync<DraftStateDto>(
            $"/drafts/{DraftId}/apply", new { value });
        if (next is null)
        {
            // A refusal is normal here — the timer may have moved the draft on
            // between the click and the request. Keep it and let the next poll
            // correct the view.
            LastError = _api.LastError;
            Changed?.Invoke();
            return false;
        }
        State = next;
        LastError = null;
        Changed?.Invoke();
        return true;
    }

    /// <summary>
    /// Take over a draft the queue created. There is no join code involved —
    /// both seats were filled server-side when the proposal completed.
    /// </summary>
    public async Task AdoptAsync(string draftId)
    {
        DraftId = draftId;
        JoinCode = null;
        _timer.Start();
        await RefreshAsync();
    }

    public async Task RefreshAsync()
    {
        if (DraftId is null || _busy) return;
        _busy = true;
        try
        {
            var next = await _api.GetAsync<DraftStateDto>($"/drafts/{DraftId}");
            if (next is not null)
            {
                State = next;
                LastError = null;
                if (next.Done) _timer.Stop();
            }
            else
            {
                LastError = _api.LastError;
                // A draft the server does not know is never coming back —
                // sessions live in memory, so a restart ends them. Polling it
                // forever produced a stream of 403s and a board frozen on a
                // state that no longer exists.
                if (LastError is { Length: > 0 } e
                    && (e.Contains("unknown draft", StringComparison.OrdinalIgnoreCase)
                        || e.Contains("not in this draft", StringComparison.OrdinalIgnoreCase)))
                {
                    _timer.Stop();
                    DraftId = null;
                    JoinCode = null;
                    State = null;
                    LastError = "The draft is no longer on the server — it was "
                              + "probably restarted. Start or join a new one.";
                }
                // Otherwise keep the board: a moment of network trouble should
                // not look like the draft vanished.
            }
            Changed?.Invoke();
        }
        finally { _busy = false; }
    }

    public void Leave()
    {
        _timer.Stop();
        DraftId = null;
        JoinCode = null;
        State = null;
        Changed?.Invoke();
    }

    public void Dispose() => _timer.Stop();
}
