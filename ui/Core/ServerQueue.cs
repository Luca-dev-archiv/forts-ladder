namespace FortsLadder.Core;

/// <summary>
/// The matchmaking queue, as the client sees it.
///
/// Same shape as <see cref="ServerDraft"/> and for the same reason: the pairing
/// rules, the accept window and the penalty for letting a proposal lapse all
/// live on the server. A local timer would only be a second opinion, and the
/// side that decides has to be the side that everyone shares.
///
/// The poll doubles as the tick — the server advances the queue when asked
/// about it. So a client that is not looking cannot be paired, which is the
/// behaviour you want: nobody gets a match while their window is closed.
/// </summary>
public sealed class ServerQueue : IDisposable
{
    private readonly ApiClient _api;
    private readonly System.Windows.Threading.DispatcherTimer _timer;
    private readonly System.Windows.Threading.DispatcherTimer _display;
    private bool _busy;

    public QueueStatusDto? Status { get; private set; }
    public string? LastError { get; private set; }
    public bool InQueue => Status?.In_Queue == true;

    /// <summary>Raised when the queue state actually changed.</summary>
    public event Action? Changed;

    /// <summary>Raised by the display ticker. Only the counting numbers may
    /// listen — the accept button is the one control in this app with a hard
    /// deadline, and re-creating its content under the cursor is how a click on
    /// it gets lost.</summary>
    public event Action? Tick;
    /// <summary>Raised once, when a proposal has turned into a draft.</summary>
    public event Action<string>? DraftReady;

    private string? _announced;

    public ServerQueue(ApiClient api)
    {
        _api = api;
        _timer = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(2),
        };
        _timer.Tick += async (_, _) => await RefreshAsync();

        // A second, local tick that talks to nobody. It only re-renders the
        // numbers that are counting down, so the seconds move once a second
        // without asking the server four times as often.
        _display = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromMilliseconds(250),
        };
        _display.Tick += (_, _) => { if (Status is not null) Tick?.Invoke(); };
        _display.Start();
    }

    public async Task<bool> JoinAsync(double rating, string mode = "ranked_1v1")
    {
        var s = await _api.PostAsync<QueueStatusDto>("/queue",
                                                     new { rating, mode });
        if (s is null) { LastError = _api.LastError; Changed?.Invoke(); return false; }
        Apply(s);
        _timer.Start();
        return true;
    }

    public async Task LeaveAsync()
    {
        await _api.DeleteAsync("/queue");
        _timer.Stop();
        Status = null;
        _announced = null;
        Changed?.Invoke();
    }

    public async Task AcceptAsync()
    {
        var s = await _api.PostAsync<QueueStatusDto>("/queue/accept");
        if (s is null) LastError = _api.LastError; else Apply(s);
        Changed?.Invoke();
    }

    public async Task DeclineAsync()
    {
        var s = await _api.PostAsync<QueueStatusDto>("/queue/decline");
        if (s is null) LastError = _api.LastError; else Apply(s);
        Changed?.Invoke();
    }

    public async Task RefreshAsync()
    {
        if (_busy) return;
        _busy = true;
        try
        {
            var s = await _api.GetAsync<QueueStatusDto>("/queue");
            if (s is not null)
            {
                var before = Status is null ? null : Signature(Status);
                Apply(s);
                // Only when something moved. Apply() already ticked otherwise.
                if (before is null || before != Signature(s)) Changed?.Invoke();
            }
            else
            {
                LastError = _api.LastError;
                Changed?.Invoke();
            }
        }
        finally { _busy = false; }
    }

    /// <summary>What the queue screen draws, minus everything that counts down.
    /// Used to decide whether a poll is worth a redraw at all.</summary>
    private static string Signature(QueueStatusDto s) => string.Join("|",
        s.In_Queue, s.State, s.Queue_Size, s.Mode, s.Draft_Id,
        s.Penalised_Until > 0,
        s.Proposal is null ? "-" : $"{s.Proposal.Accepted_By_You}:{s.Proposal.Accepted_Count}");

    private void Apply(QueueStatusDto s)
    {
        // Stamped on arrival. Everything the view counts down is a number the
        // server sent at a known moment, so the display can keep counting
        // between polls instead of standing still for two seconds and then
        // jumping — which is what made the timer look like it was lagging.
        s.ReceivedAt = DateTime.UtcNow;
        if (s.Proposal is not null) s.Proposal.ReceivedAt = s.ReceivedAt;
        var same = Status is not null && Signature(Status) == Signature(s);
        Status = s;
        LastError = null;
        // A poll that changed nothing gets a tick, not a redraw.
        if (same) Tick?.Invoke();
        // Announced once, not on every poll: the draft id keeps being returned
        // and re-raising would fight the user for the current view.
        if (s.Draft_Id is { Length: > 0 } id && id != _announced)
        {
            _announced = id;
            _timer.Stop();
            DraftReady?.Invoke(id);
        }
    }

    public void Dispose()
    {
        _timer.Stop();
        _display.Stop();
    }
}

public sealed class QueueStatusDto
{
    public bool In_Queue { get; set; }
    public string? State { get; set; }
    public int Waited_S { get; set; }
    public int Queue_Size { get; set; }
    public ProposalDto? Proposal { get; set; }
    public string? Mode { get; set; }
    public string? Draft_Id { get; set; }
    public int Penalised_Until { get; set; }

    /// <summary>When this answer arrived, for counting on between polls.</summary>
    public DateTime ReceivedAt { get; set; } = DateTime.UtcNow;

    private double Age => Math.Max(0, (DateTime.UtcNow - ReceivedAt).TotalSeconds);

    /// <summary>How long you have waited, counted on locally.</summary>
    public int WaitedNow => Waited_S + (int)Age;

    /// <summary>
    /// Cooldown left, counted down locally.
    ///
    /// Only ever *less* than what the server said, never more: the local clock
    /// may run fast, and a countdown that claims more time than exists is the
    /// one error that costs someone a match.
    /// </summary>
    public int PenalisedNow => Math.Max(0, Penalised_Until - (int)Age);

    /// <summary>Seconds of cooldown left, or none.</summary>
    public bool Penalised() => PenalisedNow > 0;
}

public sealed class ProposalDto
{
    public bool Accepted_By_You { get; set; }
    public int Accepted_Count { get; set; }
    public int Seconds_Left { get; set; }

    /// <summary>Set from the reply this proposal arrived in.</summary>
    public DateTime ReceivedAt { get; set; } = DateTime.UtcNow;

    /// <summary>The accept window, counted down locally and never rounded up.
    /// This is the one clock in the app with a hard consequence.</summary>
    public int SecondsLeftNow => Math.Max(0, Seconds_Left -
        (int)Math.Max(0, (DateTime.UtcNow - ReceivedAt).TotalSeconds));
}
