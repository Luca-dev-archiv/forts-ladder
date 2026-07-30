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
        // `Settled` is the whole list in one field: decided, closed out, walked
        // away from, aborted or voided. A cancelled series used to fall through
        // it and take one more result.
        if (s is null || !s.Done || s.Settled || s.Series_Over) return;
        if (s.Lobby_Id is not { Length: > 0 } lobby) return;
        if (m.Status != MatchStatus.Decided) return;

        // Which games belong to this series.
        //
        // The lobby id is the strongest signal but only the *host* has one:
        // "Setting lobby" is written when hosting, so the guest's log has no
        // lobby id at all and every game of theirs was silently ignored.
        //
        // So the roster decides when there is no id: a game containing both
        // people who drafted, while their series is waiting for results, is that
        // series. Nothing else in a session looks like that.
        var sameLobby = m.LobbyId is not null
                        && m.LobbyId.Value.ToString() == lobby;
        var ids = m.Players.Values.Select(pl => pl.SteamId)
                   .Where(x => !string.IsNullOrEmpty(x)).ToHashSet();
        var mine = _watcher.CurrentAccount;
        var bothPresent = ids.Count >= 2 && mine is not null && ids.Contains(mine);
        if (!sameLobby && !bothPresent) return;
        // Keyed by the *game*, not by the slot it goes in.
        //
        // Keying by number meant an invalid game reported as game 2 blocked the
        // real game 2 for good: voiding it cleared the server's result, but this
        // client still believed game 2 had been dealt with and never sent the
        // replay. The replay of a voided game is a different game — different
        // length, different defeat times — so it is sent, while the same game
        // arriving twice (result, then replay name) still only counts once.
        if (!_reportedGames.Add(m.ReportKey)) return;

        // Compared against what was agreed *before* it is reported: the wrong
        // map or the wrong commander is exactly what the draft exists to pin
        // down, and it is only visible once the game has been played.
        CheckAgainstPlan(m, s.Revealed_Through);
        CheckLobbySettings();

        // The log numbers sides 1 and 2; the draft calls them A and B. Sent as a
        // fallback only — Forts swaps sides between games, so this mapping is
        // wrong half the time and the Steam ID below is what the server uses.
        var side = m.WinnerSide == 1 ? "A" : "B";
        // Everyone the log listed, so the server can check that the people who
        // played are the people who drafted.
        var roster = m.Players.Values
            .Select(pl => pl.SteamId)
            .Where(x => !string.IsNullOrEmpty(x)).Distinct().ToList();
        // Who won and who played what, both by Steam ID: the same person across
        // every game of the series, which a side number is not.
        var winnerSteam = m.Players.Values
            .FirstOrDefault(pl => pl.Side == m.WinnerSide)?.SteamId;
        var commanders = new Dictionary<string, string>();
        foreach (var pl in m.Players.Values)
            if (!string.IsNullOrEmpty(pl.SteamId)
                && m.Commanders.TryGetValue(pl.Side, out var used)
                && !string.IsNullOrEmpty(used))
                commanders[pl.SteamId] = used;

        if (!await _draft.NoteGameAsync(s.Revealed_Through, side, roster,
                                        m.Map, commanders, winnerSteam))
        {
            ShowDraftError(_draft.LastError ?? "?");
            return;
        }
        // Written down now, while it is known. Afterwards the grouping would
        // have to guess, and on a client that is not hosting there is no lobby
        // id in the log to guess from.
        _draftedGames.Note(m.ReportKey, s.Id);
        RebuildSeries();
    }

    /// <summary>
    /// Games the server threw out, and why.
    ///
    /// The client's own `CheckAgainstPlan` is an instant local hint and nothing
    /// more: it can only compare *this* side's commander, because the opponent's
    /// is withheld until the game is over. So the verdict comes from the server,
    /// which sees both — and it has to be shown on both machines, because the
    /// person who has to play the game again is usually the one who got it wrong
    /// and has no other way of finding out.
    /// </summary>
    private void RenderDeviations(DraftStateDto s)
    {
        foreach (var (game, why) in s.Deviations)
        {
            var reasons = string.Join("; ", why.Select(DescribeDeviation));
            Warn(ErrorCodes.Text(ErrorCodes.GameNotCounted,
                                 Loc.T("deviation.replay", game, reasons)));
        }
    }

    /// <summary>
    /// Put a commander id into words. The server sends ids because it has no
    /// Forts installation to look names up in; this machine has one.
    /// </summary>
    private static string DescribeDeviation(string raw)
    {
        // "side B: drafted commander-x-y, played commander-x-z"
        return System.Text.RegularExpressions.Regex.Replace(
            raw, @"commander-[a-z0-9-]+",
            mm => CommanderNames.Display(mm.Value));
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
        // Opened by the button, or on its own when the other side has asked for
        // something — then it is the most important thing on the screen.
        var theirAsk = s.TheirVoidRequest;
        if (theirAsk is not null) _voidOpen = true;
        VoidActions.Visibility = _voidOpen
            ? Visibility.Visible : Visibility.Collapsed;
        BtnVoidOpen.Visibility = _voidOpen
            ? Visibility.Collapsed : Visibility.Visible;

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

    private bool _voidOpen;

    private void BtnVoidOpen_Click(object sender, RoutedEventArgs e)
    {
        _voidOpen = true;
        if (_draft.State is not null) RenderVoid(_draft.State);
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
