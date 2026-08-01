using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// Spectators: publishing a running match, letting people ask to watch it, and
/// answering them.
///
/// The server had all of this and the client used none of it. It read the live
/// list — which was always empty, because nothing ever published a match — and
/// it could send a request, but there was no way to learn the answer and no way
/// for a host to see one. So the whole feature was unreachable from the app.
///
/// Three pieces, and the order matters:
///
/// 1. **Publish.** When a drafted series has a lobby, the host announces it. That
///    is what puts a row in everyone else's live list. It carries the lobby id,
///    which the list deliberately does not show anybody.
/// 2. **Ask.** Anyone signed in can request a slot. Forts allows nine clients,
///    spectators included, so a full lobby is a refusal about arithmetic and
///    says so rather than reading as a judgement about the person.
/// 3. **Answer.** The host sees who is asking and admits or declines. Only then
///    does the requester get the lobby id — that is the thing that lets someone
///    in, which is why it is not in the public listing.
/// </summary>
public partial class MainWindow
{
    private string? _publishedMatchId;
    private System.Windows.Threading.DispatcherTimer? _observerPoll;

    private void InitObservers()
    {
        // One timer for both directions. Ten seconds: a spectator asking to
        // watch is not waiting on a stopwatch, and this runs all session.
        _observerPoll = new System.Windows.Threading.DispatcherTimer
        { Interval = TimeSpan.FromSeconds(10) };
        _observerPoll.Tick += async (_, _) =>
        {
            await PollObserverInboxAsync();
            await PollMyRequestsAsync();
            await HeartbeatAsync();
        };
        _observerPoll.Start();
    }

    // ------------------------------------------------------------- Publishing
    /// <summary>
    /// Announce the drafted series so it can be watched.
    ///
    /// Only the host, only once, and only with a lobby: the lobby id is what a
    /// spectator eventually needs, and an entry without one is an advert for a
    /// match nobody can join.
    /// </summary>
    private async Task PublishSeriesAsync()
    {
        var s = _draft.State;
        if (s is null || !s.Done || s.Voided) return;
        if (!s.YouHostLobby) return;
        if (s.Lobby_Id is not { Length: > 0 } lobby) return;
        if (_publishedMatchId is not null) return;

        var players = s.Seats.OrderBy(kv => kv.Key).Select(kv => kv.Value).ToList();
        var res = await _login.PublishLiveAsync(
            mode: "ranked_1v1", label: Loc.T("live.series_label", s.Plan.Count),
            players: players, slotsUsed: players.Count, lobbyId: lobby,
            // What was written into multiplayer.lua for this lobby. Anything
            // else is an invitation to a seat that does not exist.
            slotsTotal: _lobbySize);
        if (res?.Match_Id is { Length: > 0 } id)
        {
            _publishedMatchId = id;
            HandoffSub.Text = Loc.T("live.published");
            // The switch only means anything once there is a match to open or
            // close, so it appears with one.
            BtnSpectators.Visibility = Visibility.Visible;
            BtnSpectators.Content = Loc.T("live.spectators_on");
        }
        else if (_api.LastError is { Length: > 0 } err)
        {
            // Not fatal — the series is playable either way — so it is said
            // once and not retried in a loop.
            HandoffSub.Text = Loc.T("live.publish_failed", err);
            _publishedMatchId = "-";
        }
    }

    /// <summary>Keep the entry alive, and take it down when the series ends.</summary>
    private async Task HeartbeatAsync()
    {
        if (_publishedMatchId is null or "-") return;
        var s = _draft.State;
        if (s is null || s.Series_Over || s.Voided)
        {
            await _login.FinishLiveAsync(_publishedMatchId);
            _publishedMatchId = null;
            BtnSpectators.Visibility = Visibility.Collapsed;
            return;
        }
        await _login.HeartbeatLiveAsync(_publishedMatchId);
    }

    // ---------------------------------------------------------- The host side
    private async Task PollObserverInboxAsync()
    {
        if (!_api.LoggedIn) return;
        var inbox = await _login.ObserverInboxAsync();
        var pending = inbox?.Pending ?? new List<ObserverPendingDto>();
        ObserverInbox.ItemsSource = pending.Select(r => new
        {
            r.Id,
            Who = Loc.T("live.asks_to_watch", r.Who),
        }).ToList();
        ObserverInboxBar.Visibility = pending.Count == 0
            ? Visibility.Collapsed : Visibility.Visible;
    }

    private async void BtnAdmitObserver_Click(object sender, RoutedEventArgs e)
        => await AnswerAsync(sender, approve: true);

    private async void BtnDeclineObserver_Click(object sender, RoutedEventArgs e)
        => await AnswerAsync(sender, approve: false);

    private async Task AnswerAsync(object sender, bool approve)
    {
        if (sender is not Button b || b.Tag is not string id) return;
        b.IsEnabled = false;
        if (!await _login.AnswerObserverAsync(id, approve))
            AppDialog.Info(this, _api.LastError ?? "?", Loc.T("live.headline"), AppDialog.Kind.Info);
        await PollObserverInboxAsync();
    }

    // ----------------------------------------------------- The spectator side
    private async Task PollMyRequestsAsync()
    {
        if (!_api.LoggedIn) return;
        var mine = await _login.MyObserverRequestsAsync();
        var rows = (mine?.Requests ?? new List<ObserverRequestDto>())
            .Where(r => r.State != "declined" || r.Reason.Length > 0)
            .Select(r => new
            {
                r.Id,
                Title = string.Join(" vs ", r.Players),
                // The state in words, with the reason: "declined" alone invites
                // the wrong conclusion, and "no room" is arithmetic.
                Note = r.State switch
                {
                    "approved" => Loc.T("live.admitted"),
                    "declined" => Loc.T("live.declined", r.Reason),
                    _ => Loc.T("live.waiting"),
                },
                CanJoin = r.State == "approved" && r.Join_Url is { Length: > 0 },
                JoinUrl = r.Join_Url ?? "",
                Brush = BrushFor(r.State == "approved" ? "Win"
                                 : r.State == "declined" ? "Loss" : "TextMid"),
            }).ToList();
        MyRequests.ItemsSource = rows;
        MyRequestsBar.Visibility = rows.Count == 0
            ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BtnJoinAsObserver_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button b || b.Tag is not string url
            || url.Length == 0) return;
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo(url)
                { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            AppDialog.Info(this, ex.Message, Loc.T("live.headline"), AppDialog.Kind.Info);
        }
    }
}
