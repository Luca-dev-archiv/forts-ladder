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
        _queue.DraftReady += id =>
        {
            // A found match goes straight to the board. Making someone click
            // through to it wastes part of a step timer that is already running.
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

    private void NavQueue_Click(object sender, RoutedEventArgs e)
    {
        ShowView("queue");
        _ = RefreshAccountAsync();
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
        // Same route as the draft: ask Discord, fall back to the browser.
        if (!await EnsureReadyAsync())
        {
            QueueError.Text = DraftError.Text;
            return;
        }

        // Your own open-ladder rating if the table knows it, so the first
        // pairing is not against someone hundreds of points away.
        var rating = _table.Me?.OpenRating ?? _table.Me?.UferRating ?? 1000.0;
        var mode = SelectedMode?.Key ?? "ranked_1v1";
        if (!await _queue.JoinAsync(rating, mode))
            QueueError.Text = _queue.LastError ?? "?";
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
            AcceptSeconds.Text = proposal.Seconds_Left.ToString();
            BtnAccept.IsEnabled = !proposal.Accepted_By_You;
            BtnQueueToggle.Visibility = Visibility.Collapsed;
            return;
        }

        BtnQueueToggle.Visibility = Visibility.Visible;

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
            QueueSubState.Text = Loc.T("queue.penalty_sub", s.Penalised_Until);
            BtnQueueToggle.Content = Loc.T("queue.find");
            return;
        }

        if (s?.In_Queue == true)
        {
            QueueBigState.Text = Loc.T("queue.searching_title");
            QueueBigState.Foreground = (Brush)FindResource("TextHi");
            QueueSubState.Text = Loc.T("queue.searching_sub");
            QueueElapsed.Text = TimeSpan.FromSeconds(s.Waited_S).ToString(@"m\:ss");
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
