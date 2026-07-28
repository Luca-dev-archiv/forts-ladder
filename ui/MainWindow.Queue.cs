using System.Windows;
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
    }

    private void NavQueue_Click(object sender, RoutedEventArgs e) => ShowView("queue");

    private async void BtnQueueToggle_Click(object sender, RoutedEventArgs e)
    {
        QueueError.Text = "";
        if (_queue.InQueue)
        {
            await _queue.LeaveAsync();
            return;
        }
        if (!_api.Configured) { QueueError.Text = Loc.T("draft.needs_server"); return; }
        if (!_api.LoggedIn) { QueueError.Text = Loc.T("draft.needs_login"); return; }

        // Your own open-ladder rating if the table knows it, so the first
        // pairing is not against someone hundreds of points away.
        var rating = _table.Me?.OpenRating ?? _table.Me?.UferRating ?? 1000.0;
        if (!await _queue.JoinAsync(rating))
            QueueError.Text = _queue.LastError ?? "?";
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
