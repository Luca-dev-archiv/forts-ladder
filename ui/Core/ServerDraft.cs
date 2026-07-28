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
    private readonly System.Windows.Threading.DispatcherTimer _display;
    private bool _busy;

    public string? DraftId { get; private set; }
    public string? JoinCode { get; private set; }
    public DraftStateDto? State { get; private set; }
    public string? LastError { get; private set; }

    /// <summary>
    /// Raised when the state actually changed — a move, a join, a reveal.
    ///
    /// Deliberately *not* raised by the display ticker. Redrawing the board five
    /// times a second replaced the tile buttons under the mouse, so a click that
    /// began on one and ended on its replacement never became a click: it took
    /// three attempts to ban a map. A smooth countdown is not worth an
    /// unclickable board.
    /// </summary>
    public event Action? Changed;

    /// <summary>Raised on the display ticker. Only the countdown may listen —
    /// nothing that rebuilds a control.</summary>
    public event Action? Tick;

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

        // Local, talks to nobody: it only re-draws the countdown so the bar
        // moves smoothly instead of once per poll.
        _display = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(200),
        };
        _display.Tick += (_, _) =>
        {
            if (State is { Done: false, Cancelled: false, Full: true })
                Tick?.Invoke();
        };
        _display.Start();
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
                next.ReceivedAt = DateTime.UtcNow;
                var same = State is not null && Signature(State) == Signature(next);
                State = next;
                LastError = null;
                // Nothing left to poll for once it is decided or abandoned. The
                // state is kept so the view can say which of the two it was.
                if (next.Done || next.Cancelled) _timer.Stop();
                if (same)
                {
                    // Nothing to redraw. Announcing it anyway would rebuild the
                    // board once a second for no reason, and that is what made
                    // the tiles hard to click.
                    Tick?.Invoke();
                    return;
                }
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

    /// <summary>
    /// Everything the board draws, as one comparable string.
    ///
    /// `seconds_left` is not in it on purpose: it changes constantly and nothing
    /// but the countdown depends on it.
    /// </summary>
    private static string Signature(DraftStateDto s) => string.Join("|",
        s.Step_Index, s.Waiting_On, s.Action, s.Done, s.Cancelled, s.Full,
        s.Your_Side, s.Your_Pending_Pick, s.Lobby_Id, s.Lobby_Host,
        s.Revealed_Through, s.Voided, s.Series_Over,
        string.Join(",", s.Voided_Games),
        string.Join(",", s.Wins.OrderBy(kv => kv.Key)
                          .Select(kv => kv.Key + "=" + kv.Value)),
        string.Join(",", s.Void_Requests.OrderBy(kv => kv.Key)
                          .Select(kv => kv.Key + "=" + kv.Value.Scope)),
        string.Join(",", s.Locked_In), string.Join(",", s.Banned_Maps),
        string.Join(",", s.Banned_Commanders), string.Join(",", s.Options),
        string.Join(",", s.Seats.OrderBy(kv => kv.Key)
                          .Select(kv => kv.Key + "=" + kv.Value)),
        string.Join(",", s.Plan.Select(g =>
            $"{g.Game}:{g.Map}:{g.Commander_A}:{g.Commander_B}")));

    public void Leave()
    {
        _timer.Stop();
        DraftId = null;
        JoinCode = null;
        State = null;
        Changed?.Invoke();
    }

    /// <summary>
    /// Leave the draft. Both sides are told; nothing is silently deleted.
    ///
    /// Polling stops either way: if the request failed the draft is unreachable
    /// anyway, and continuing to poll would keep a board on screen that cannot
    /// be played.
    /// </summary>
    public async Task<bool> CancelAsync()
    {
        if (DraftId is null) return false;
        var ok = await _api.DeleteAsync($"/drafts/{DraftId}");
        var why = _api.LastError;
        Leave();
        if (!ok) LastError = why;
        return ok;
    }

    /// <summary>
    /// Claim the host role, before a lobby exists.
    ///
    /// Whoever asks first settles it, and the other client stops offering to
    /// host — otherwise both sides open a lobby and neither is in the other's.
    /// </summary>
    public async Task<bool> ClaimHostAsync()
    {
        if (DraftId is null) return false;
        var next = await _api.PostAsync<DraftStateDto>($"/drafts/{DraftId}/host");
        if (next is null) { LastError = _api.LastError; return false; }
        next.ReceivedAt = DateTime.UtcNow;
        State = next;
        Changed?.Invoke();
        return true;
    }

    /// <summary>
    /// Tell the server which Steam lobby this series is played in.
    ///
    /// Sent as a string: 64-bit lobby ids do not survive being parsed as JSON
    /// numbers, and a rounded id matches no game.
    /// </summary>
    public async Task<bool> SetLobbyAsync(ulong lobbyId)
    {
        if (DraftId is null) return false;
        var next = await _api.PostAsync<DraftStateDto>(
            $"/drafts/{DraftId}/lobby",
            new { lobby_id = lobbyId.ToString() });
        if (next is null)
        {
            LastError = _api.LastError;
            Changed?.Invoke();
            return false;
        }
        State = next;
        Changed?.Invoke();
        return true;
    }

    /// <summary>
    /// Report one finished game of the series.
    ///
    /// Read from this machine's own game log, because that is the only place the
    /// result exists. It is what spends the winner's commander, opens the next
    /// game's commanders, and lets the series end at two wins.
    /// </summary>
    public async Task<bool> NoteGameAsync(int game, string winnerSide)
    {
        if (DraftId is null) return false;
        var next = await _api.PostAsync<DraftStateDto>(
            $"/drafts/{DraftId}/game", new { game, winner = winnerSide });
        if (next is null) { LastError = _api.LastError; return false; }
        next.ReceivedAt = DateTime.UtcNow;
        State = next;
        Changed?.Invoke();
        return true;
    }

    /// <summary>
    /// Ask for a game or the series not to count. Takes effect once the other
    /// side asks for the same thing.
    /// </summary>
    public async Task<bool> RequestVoidAsync(string scope, string reason = "")
    {
        if (DraftId is null) return false;
        var next = await _api.PostAsync<DraftStateDto>(
            $"/drafts/{DraftId}/void", new { scope, reason });
        if (next is null) { LastError = _api.LastError; return false; }
        next.ReceivedAt = DateTime.UtcNow;
        State = next;
        Changed?.Invoke();
        return true;
    }

    public async Task<bool> WithdrawVoidAsync()
    {
        if (DraftId is null) return false;
        if (!await _api.DeleteAsync($"/drafts/{DraftId}/void"))
        {
            LastError = _api.LastError;
            return false;
        }
        await RefreshAsync();
        return true;
    }

    public void Dispose()
    {
        _timer.Stop();
        _display.Stop();
    }
}
