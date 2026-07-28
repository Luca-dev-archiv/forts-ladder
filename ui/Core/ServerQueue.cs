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
    private bool _busy;

    public QueueStatusDto? Status { get; private set; }
    public string? LastError { get; private set; }
    public bool InQueue => Status?.In_Queue == true;

    public event Action? Changed;
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
            if (s is not null) Apply(s);
            else LastError = _api.LastError;
            Changed?.Invoke();
        }
        finally { _busy = false; }
    }

    private void Apply(QueueStatusDto s)
    {
        Status = s;
        LastError = null;
        // Announced once, not on every poll: the draft id keeps being returned
        // and re-raising would fight the user for the current view.
        if (s.Draft_Id is { Length: > 0 } id && id != _announced)
        {
            _announced = id;
            _timer.Stop();
            DraftReady?.Invoke(id);
        }
    }

    public void Dispose() => _timer.Stop();
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

    /// <summary>Seconds of cooldown left, or none.</summary>
    public bool Penalised() => Penalised_Until > 0;
}

public sealed class ProposalDto
{
    public bool Accepted_By_You { get; set; }
    public int Accepted_Count { get; set; }
    public int Seconds_Left { get; set; }
}
