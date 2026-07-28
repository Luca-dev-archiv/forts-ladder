using System.Windows;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// Agreeing that something did not count, and reporting the games that did.
///
/// Both halves of the same problem. A drafted series is a sequence of games,
/// and the server needs to know when each one ends: that spends the winner's
/// commander, opens the next game's commanders, and lets a Bo3 end at two wins
/// instead of running three games. The results come from this machine's own
/// game log, because that is the only place they exist.
///
/// And sometimes a game should not have counted — a crash, the wrong commander
/// loaded, the wrong map. The alternative to agreeing on that is a rated result
/// both players know is wrong. It takes both sides, deliberately: "that game
/// did not count" is exactly the claim a losing player has an interest in
/// making alone.
/// </summary>
public partial class MainWindow
{
    /// <summary>Match keys already reported for the running series, so the same
    /// game is not sent twice as the log is re-read.</summary>
    private readonly HashSet<string> _reportedGames = new();

    /// <summary>
    /// A finished match arrived. If it belongs to the drafted series, report it.
    ///
    /// The lobby id is what ties them together — it is the same id the host
    /// registered — so a game played somewhere else in the same session is not
    /// mistaken for part of this series.
    /// </summary>
    private async void MaybeReportSeriesGame(MatchRecord m)
    {
        var s = _draft.State;
        if (s is null || !s.Done || s.Voided || s.Series_Over) return;
        if (s.Lobby_Id is not { Length: > 0 } lobby) return;
        if (m.LobbyId is null || m.LobbyId.Value.ToString() != lobby) return;
        if (m.Status != MatchStatus.Decided) return;
        if (!_reportedGames.Add(m.Key)) return;

        // The log numbers sides 1 and 2; the draft calls them A and B.
        var side = m.WinnerSide == 1 ? "A" : "B";
        if (!await _draft.NoteGameAsync(s.Revealed_Through, side))
            ShowDraftError(_draft.LastError ?? "?");
    }

    // ------------------------------------------------------------------ Voiding
    private void RenderVoid(DraftStateDto s)
    {
        // Only once there is something to void: a draft still in progress is
        // left with the Leave button instead.
        if (!s.Done)
        {
            VoidBar.Visibility = Visibility.Collapsed;
            return;
        }
        VoidBar.Visibility = Visibility.Visible;

        if (s.Voided)
        {
            VoidTitle.Text = Loc.T("void.voided_series");
            VoidTitle.Foreground = (Brush)FindResource("Warn");
            VoidSub.Text = Loc.T("void.voided_series_sub");
            BtnVoidGame.Visibility = Visibility.Collapsed;
            BtnVoidSeries.Visibility = Visibility.Collapsed;
            BtnVoidWithdraw.Visibility = Visibility.Collapsed;
            return;
        }

        BtnVoidGame.Visibility = Visibility.Visible;
        BtnVoidSeries.Visibility = Visibility.Visible;
        BtnVoidGame.Content = Loc.T("void.game");

        var theirs = s.TheirVoidRequest;
        var mine = s.MyVoidRequest;
        BtnVoidWithdraw.Visibility = mine is null
            ? Visibility.Collapsed : Visibility.Visible;

        if (theirs is not null)
        {
            // Said plainly, with the reason, because agreeing is one click and
            // ought to be an informed one.
            var oppName = s.Seats.TryGetValue(s.Your_Side == "A" ? "B" : "A",
                                             out var o) ? o : Loc.T("draft.them");
            VoidTitle.Text = Loc.T("void.they_asked", oppName, Describe(theirs.Scope));
            VoidTitle.Foreground = (Brush)FindResource("Warn");
            VoidSub.Text = Loc.T("void.they_asked_sub",
                                 theirs.Reason.Length > 0 ? theirs.Reason : "—");
        }
        else if (mine is not null)
        {
            VoidTitle.Text = Loc.T("void.you_asked", Describe(mine.Scope));
            VoidTitle.Foreground = (Brush)FindResource("TextMid");
            VoidSub.Text = Loc.T("void.you_asked_sub");
        }
        else
        {
            VoidTitle.Text = Loc.T("void.title");
            VoidTitle.Foreground = (Brush)FindResource("TextMid");
            VoidSub.Text = s.Voided_Games.Count > 0
                ? Loc.T("void.sub") + "  "
                  + Loc.T("void.voided_games", string.Join(", ", s.Voided_Games))
                : Loc.T("void.sub");
        }
    }

    private string Describe(string scope) =>
        scope == "series" ? Loc.T("void.scope_series")
        : scope.StartsWith("game:") ? Loc.T("void.scope_game", scope[5..])
        : scope;

    private async void BtnVoidGame_Click(object sender, RoutedEventArgs e)
    {
        var s = _draft.State;
        if (s is null) return;
        // The game being played, which is the one anybody would mean.
        if (!await _draft.RequestVoidAsync($"game:{s.Revealed_Through}",
                                           Loc.T("void.title")))
            ShowDraftError(_draft.LastError ?? "?");
    }

    private async void BtnVoidSeries_Click(object sender, RoutedEventArgs e)
    {
        if (!await _draft.RequestVoidAsync("series", Loc.T("void.title")))
            ShowDraftError(_draft.LastError ?? "?");
    }

    private async void BtnVoidWithdraw_Click(object sender, RoutedEventArgs e)
    {
        if (!await _draft.WithdrawVoidAsync())
            ShowDraftError(_draft.LastError ?? "?");
    }
}
