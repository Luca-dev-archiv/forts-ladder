using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// The queue screen.
///
/// One state visible at a time, on purpose. A queue that shows searching,
/// found, accepted and drafting all at once is how someone misses the accept
/// window — which costs them a penalty, so the screen owes them a single
/// unmistakable thing to look at.
/// </summary>
public partial class MainWindow
{
    private readonly ServerQueue _queue;

    private List<QueueModeDto> _modes = new();
    private bool _modesReady;

    private void InitQueue()
    {
        _queue.Changed += RefreshQueue;
        _queue.Changed += RefreshModeCounts;
        // The counting numbers only. Nothing here may touch a button's content:
        // doing that on every tick is how a click on Accept gets lost.
        _queue.Tick += RefreshQueueClock;
        _queue.DraftReady += id =>
        {
            // Out of the queue first. It kept polling and counting while a
            // draft was already running, so the screen showed a search that had
            // already succeeded.
            _ = _queue.LeaveAsync();
            // Then straight to the board: making someone click through wastes
            // part of a step timer that is already running.
            _ = _draft.AdoptAsync(id);
            ShowView("draft");
        };
        RefreshQueue();
        _ = LoadModesAsync();
    }

    /// <summary>
    /// Fetch the modes the server offers. Unavailable ones are listed rather
    /// than hidden — "2v2 exists but cannot be queued yet" is information, and
    /// hiding it just prompts the question.
    /// </summary>
    private async Task LoadModesAsync()
    {
        var res = await _login.ModesAsync();
        _modes = res?.Modes ?? new List<QueueModeDto>();
        ModeBox.ItemsSource = _modes;
        var first = _modes.FirstOrDefault(m => m.Available) ?? _modes.FirstOrDefault();
        ModeBox.SelectedItem = first;
        _modesReady = true;
        ShowModeNote();
    }

    private QueueModeDto? SelectedMode => ModeBox.SelectedItem as QueueModeDto;

    private void ModeBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_modesReady) ShowModeNote();
    }

    private void ShowModeNote()
    {
        var m = SelectedMode;
        var blocked = m is not null && !m.Available;
        ModeNote.Text = blocked ? Loc.T("queue.mode_unavailable") : "";
        ModeNote.Visibility = blocked ? Visibility.Visible : Visibility.Collapsed;
        BtnQueueToggle.IsEnabled = m is null || m.Available || _queue.InQueue;
    }

    /// <summary>
    /// Put the live searcher counts on the mode picker.
    ///
    /// They arrive with every queue poll, so this is free — and the picker
    /// previously showed a number fetched once at startup, which meant "0
    /// waiting" for the rest of the session.
    /// </summary>
    private void RefreshModeCounts()
    {
        var waiting = _queue.Status?.Waiting;
        if (waiting is null || _modes.Count == 0) return;
        var changed = false;
        foreach (var m in _modes)
        {
            // A mode the server did not mention has nobody in it. Treating a
            // missing key as "no news" is what kept "1 waiting" on screen after
            // the last searcher left, because an empty queue is dropped from
            // the dictionary entirely.
            var n = waiting.TryGetValue(m.Key, out var v) ? v : 0;
            if (m.Waiting == n) continue;
            m.Waiting = n;
            changed = true;
        }
        // Rebuilt only when a number actually moved: reassigning the source
        // closes the drop-down under the cursor.
        if (!changed) return;
        var keep = ModeBox.SelectedItem;
        ModeBox.ItemsSource = null;
        ModeBox.ItemsSource = _modes;
        ModeBox.SelectedItem = keep;
    }

    /// <summary>The two numbers that count, and nothing else.</summary>
    private void RefreshQueueClock()
    {
        var s = _queue.Status;
        if (s is null) return;
        if (s.Proposal is { } p) AcceptSeconds.Text = p.SecondsLeftNow.ToString();
        else if (s.In_Queue)
            QueueElapsed.Text = TimeSpan.FromSeconds(s.WaitedNow).ToString(@"m\:ss");
    }

    private void NavQueue_Click(object sender, RoutedEventArgs e)
    {
        ShowView("queue");
        _ = RefreshAccountAsync();
        // Asked on arrival. The screen was showing whatever it last heard, which
        // after a series was closed out was "you are still in a series".
        _ = _queue.RefreshNowAsync();
    }

    // ---------------------------------------------------------------- Account
    /// <summary>
    /// Show what is still missing before results can count, with the fix next
    /// to it. Being told "not allowed" without being told what is absent is a
    /// dead end.
    /// </summary>
    private async Task RefreshAccountAsync()
    {
        if (!_api.LoggedIn)
        {
            AccountLine.Text = Loc.T("queue.not_signed_in");
            BtnLinkSteam.Visibility = Visibility.Collapsed;
            BtnConsent.Visibility = Visibility.Collapsed;
            return;
        }
        var me = await _login.MeAsync();
        if (me is null || !me.Logged_In)
        {
            AccountLine.Text = Loc.T("queue.not_signed_in");
            BtnLinkSteam.Visibility = Visibility.Collapsed;
            BtnConsent.Visibility = Visibility.Collapsed;
            return;
        }

        var missing = new List<string>();
        if (string.IsNullOrEmpty(me.Steam_Id)) missing.Add(Loc.T("queue.no_steam"));
        if (!me.Tracking_Consent) missing.Add(Loc.T("queue.no_consent"));

        AccountLine.Text = missing.Count == 0
            ? Loc.T("queue.account_ready", me.Discord ?? "?")
            : Loc.T("queue.account_missing", me.Discord ?? "?",
                    string.Join(" · ", missing));

        BtnLinkSteam.Visibility = string.IsNullOrEmpty(me.Steam_Id)
            ? Visibility.Visible : Visibility.Collapsed;
        BtnConsent.Visibility = me.Tracking_Consent
            ? Visibility.Collapsed : Visibility.Visible;

        // Only an admin sees this, and only while the server still has no pool.
        // Once it is set, the button would just be a way to overwrite it by
        // accident.
        var admin = me.Role is "Admin" or "Owner";
        var pools = await _login.PoolsAsync();
        BtnPublishPools.Visibility = admin && pools?.Configured == false
            ? Visibility.Visible : Visibility.Collapsed;
        if (pools?.Configured == false && !admin)
            QueueError.Text = Loc.T("queue.no_pools_yet");

        // Managing accounts and building brackets happens in the browser, so
        // this is a shortcut rather than a screen. Hidden for everyone who
        // would only find a refusal behind it.
        _managePath = admin ? "/admin"
            : me.Grants.Contains("tournament_host") || me.Grants.Contains("referee")
                ? "/manage/tournaments" : null;
        BtnManage.Visibility = _managePath is null
            ? Visibility.Collapsed : Visibility.Visible;
    }

    /// <summary>Which management page this account may open, if any.</summary>
    private string? _managePath;

    private async void BtnPublishPools_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureReadyAsync()) return;
        var maps = LeagueMapPool();
        var commanders = CommanderNames.Installed();
        if (maps.Count < 5 || commanders.Count < 4)
        {
            QueueError.Text = Loc.T("draft.too_little", maps.Count, commanders.Count);
            return;
        }
        QueueError.Text = await _login.PublishPoolsAsync(maps, commanders)
            ? Loc.T("queue.pools_published", maps.Count, commanders.Count)
            : _api.LastError ?? "?";
        await RefreshAccountAsync();
        await LoadModesAsync();
    }

    private async void BtnLinkSteam_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureReadyAsync()) return;
        QueueError.Text = Loc.T("queue.steam_waiting");
        var ok = await _login.LinkSteamAsync();
        QueueError.Text = ok ? "" : Loc.T("queue.steam_failed");
        await RefreshAccountAsync();
    }

    private async void BtnConsent_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureReadyAsync()) return;
        if (!await _login.GrantConsentAsync())
            QueueError.Text = _api.LastError ?? "?";
        await RefreshAccountAsync();
    }

    /// <summary>
    /// Whether people may ask to watch. Off is a real answer, so it is a switch
    /// rather than something buried in a rule.
    ///
    /// Only meaningful for a published match — a ranked series is closed to
    /// spectators regardless, which the label says.
    /// </summary>
    private async void BtnSpectators_Click(object sender, RoutedEventArgs e)
    {
        if (_publishedMatchId is null or "-")
        {
            QueueError.Text = Loc.T("live.nothing_published");
            return;
        }
        _spectatorsWelcome = !_spectatorsWelcome;
        if (!await _login.SetSpectatorsAllowedAsync(_publishedMatchId,
                                                   _spectatorsWelcome))
            QueueError.Text = _api.LastError ?? "?";
        BtnSpectators.Content = Loc.T(_spectatorsWelcome
            ? "live.spectators_on" : "live.spectators_off");
    }

    /// <summary>Whether this host is currently taking requests.</summary>
    private bool _spectatorsWelcome = true;

    private void BtnWebsite_Click(object sender, RoutedEventArgs e)
        => OpenInBrowser("/");

    private void BtnManage_Click(object sender, RoutedEventArgs e)
        => OpenInBrowser(_managePath ?? "/");

    private void OpenInBrowser(string path)
    {
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo(_login.WebsiteUrl(path))
                { UseShellExecute = true });
        }
        catch (Exception ex) { QueueError.Text = ex.Message; }
    }

    private async void BtnQueueToggle_Click(object sender, RoutedEventArgs e)
    {
        QueueError.Text = "";
        if (_queue.InQueue)
        {
            await _queue.LeaveAsync();
            return;
        }
        // Say something immediately. Three round trips run before the queue is
        // actually joined — login check, account, pools — and a button that sits
        // there for two seconds reads as a button that did not register.
        BtnQueueToggle.IsEnabled = false;
        QueueBigState.Text = Loc.T("queue.joining");
        // Same route as the draft: ask Discord, fall back to the browser.
        if (!await EnsureReadyAsync())
        {
            QueueError.Text = DraftError.Text;
            // Both, or the screen keeps saying "JOINING…" over a dead button.
            BtnQueueToggle.IsEnabled = true;
            RefreshQueue();
            return;
        }

        BtnQueueToggle.IsEnabled = true;
        // Your own open-ladder rating if the table knows it, so the first
        // pairing is not against someone hundreds of points away.
        var rating = _table.Me?.OpenRating ?? _table.Me?.UferRating ?? 1000.0;
        var mode = SelectedMode?.Key ?? "ranked_1v1";
        if (!await _queue.JoinAsync(rating, mode))
        {
            QueueError.Text = _queue.LastError ?? "?";
            // A refused join leaves the hand-set label behind, and the reason
            // for the refusal — an unfinished series, a cooldown — is the thing
            // the screen should be showing instead.
            RefreshQueue();
        }
        await RefreshAccountAsync();
    }

    private async void BtnAccept_Click(object sender, RoutedEventArgs e)
        => await _queue.AcceptAsync();

    private async void BtnDecline_Click(object sender, RoutedEventArgs e)
        => await _queue.DeclineAsync();

    private void RefreshQueue()
    {
        var s = _queue.Status;
        var proposal = s?.Proposal;

        // Free: the poll carries it, and while somebody is queueing it is the
        // freshest number there is.
        //
        // Zero is treated as "not answered" rather than as a count, because you
        // are always counted yourself — so a real reply is never zero, and a
        // server too old to send the field would otherwise have the client
        // announce that nobody is online.
        if (s is { Online: > 0 }) ShowOnline(s.Online);

        AcceptPanel.Visibility = proposal is not null && !s!.Penalised()
            ? Visibility.Visible : Visibility.Collapsed;
        SearchStats.Visibility = s?.In_Queue == true && proposal is null
            ? Visibility.Visible : Visibility.Collapsed;
        BtnGoToDraft.Visibility = s?.Draft_Id is { Length: > 0 }
            ? Visibility.Visible : Visibility.Collapsed;

        QueueError.Text = _queue.LastError ?? QueueError.Text;

        if (proposal is not null)
        {
            QueueBigState.Text = Loc.T("queue.found_title");
            QueueBigState.Foreground = (Brush)FindResource("Accent");
            QueueSubState.Text = proposal.Accepted_By_You
                ? Loc.T("queue.found_waiting")
                : Loc.T("queue.found_sub");
            AcceptCount.Text = Loc.T("queue.accepted_count",
                                     proposal.Accepted_Count, 2);
            AcceptSeconds.Text = proposal.SecondsLeftNow.ToString();
            BtnAccept.IsEnabled = !proposal.Accepted_By_You;
            BtnQueueToggle.Visibility = Visibility.Collapsed;
            return;
        }

        BtnQueueToggle.Visibility = Visibility.Visible;

        // A series of yours that is not finished. It is the reason the queue
        // will refuse you, so it is what the screen says — with the way back to
        // the board rather than a dead "Find match".
        if (s?.Blocked_By_Series is { Length: > 0 })
        {
            QueueBigState.Text = Loc.T("queue.in_series_title");
            QueueBigState.Foreground = (Brush)FindResource("Win");
            QueueSubState.Text = Loc.T("queue.in_series_sub");
            BtnQueueToggle.Visibility = Visibility.Collapsed;
            return;
        }

        if (s?.Draft_Id is { Length: > 0 })
        {
            QueueBigState.Text = Loc.T("queue.drafting_title");
            QueueBigState.Foreground = (Brush)FindResource("Win");
            QueueSubState.Text = Loc.T("queue.drafting_sub");
            BtnQueueToggle.Content = Loc.T("queue.find");
            return;
        }

        if (s?.Penalised() == true)
        {
            // Say how long and why. A cooldown with no explanation reads as the
            // tool being broken.
            QueueBigState.Text = Loc.T("queue.penalty_title");
            QueueBigState.Foreground = (Brush)FindResource("Loss");
            QueueSubState.Text = Loc.T("queue.penalty_sub", s.PenalisedNow);
            BtnQueueToggle.Content = Loc.T("queue.find");
            return;
        }

        if (s?.In_Queue == true)
        {
            QueueBigState.Text = Loc.T("queue.searching_title");
            QueueBigState.Foreground = (Brush)FindResource("TextHi");
            QueueSubState.Text = Loc.T("queue.searching_sub");
            QueueElapsed.Text = TimeSpan.FromSeconds(s.WaitedNow).ToString(@"m\:ss");
            QueueSize.Text = s.Queue_Size.ToString();
            BtnQueueToggle.Content = Loc.T("queue.cancel");
            return;
        }

        QueueBigState.Text = Loc.T("queue.idle_title");
        QueueBigState.Foreground = (Brush)FindResource("TextHi");
        QueueSubState.Text = Loc.T("queue.idle_sub");
        BtnQueueToggle.Content = Loc.T("queue.find");
    }
}
