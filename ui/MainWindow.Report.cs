using System.Windows;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// Sending a finished series to the ladder, by itself.
///
/// This used to be missing entirely: the client recorded everything and reported
/// nothing, so winning a rated match changed no number anyone else could see.
///
/// It is deliberately not a button. Whether a series counts is not a decision
/// the two players make — they were both there, both agreed to be tracked, and
/// the game log already says who won. Leaving it to a click means the loser has
/// a reason not to click, and then the ladder only records the results somebody
/// liked. Both clients send the same series; the server keeps the first arrival
/// and decides for itself whether it is rated.
///
/// The one thing a player does decide is whether to ask a human to look at it.
/// See <see cref="OpenCaseAsync"/>.
/// </summary>
public partial class MainWindow
{
    /// <summary>Drafts already sent, so a poll does not send one every second.</summary>
    private readonly HashSet<string> _reportedSeries = new();

    /// <summary>Series id -> the ladder's id for the reported result, so a case
    /// can be opened against it afterwards.</summary>
    private readonly Dictionary<string, string> _resultIds = new();

    /// <summary>
    /// Send the series if it is finished and has not gone yet.
    ///
    /// Called from the draft refresh, which runs on every poll — so it has to be
    /// cheap and it has to be idempotent.
    /// </summary>
    private void MaybeReportSeries(DraftStateDto s)
    {
        if (!s.Done || !s.Series_Over || s.Voided || s.Aborted) return;
        if (!_reportedSeries.Add(s.Id)) return;
        _ = ReportSeriesAsync(s);
    }

    private async Task ReportSeriesAsync(DraftStateDto s)
    {
        // Built from the recorded games rather than from the draft's own score:
        // the ladder rates what was *played*, and the log is where that lives.
        var mine = _series.FirstOrDefault(
            v => v.Model.Matches.Any(m => _draftedGames.SeriesOf(m.ReportKey) == s.Id));
        var series = mine?.Model;
        if (series is null || series.Matches.Count == 0)
        {
            // Nothing recorded locally — the other client will have it. Allow a
            // retry rather than marking this series as done.
            _reportedSeries.Remove(s.Id);
            return;
        }

        // SteamID64 -> side number, straight out of the log, which is the form
        // the server checks against the drafted roster.
        var sides = new Dictionary<string, int>();
        foreach (var m in series.Matches)
            foreach (var p in m.Players.Values)
                if (!string.IsNullOrEmpty(p.SteamId) && p.Side > 0)
                    sides[p.SteamId] = p.Side;
        if (sides.Values.Distinct().Count() != 2)
        {
            _reportedSeries.Remove(s.Id);
            return;
        }

        var low = sides.Values.Min();
        var lowWins = series.Matches.Count(m => m.WinnerSide == low);
        var replays = series.Matches.Select(m => m.Replay)
                            .Where(r => !string.IsNullOrEmpty(r))
                            .Select(r => System.IO.Path.GetFileName(r!)).ToList();

        // The lobby id comes from the *draft*, not from the log.
        //
        // Only the host's log has one — "Setting lobby" is written when hosting —
        // so a guest reported `null`, the server could not tie the series to a
        // sanctioned lobby, and the whole thing came back unrated with FL-231.
        // The id was never missing: the server told both clients what it was.
        var lobby = ulong.TryParse(s.Lobby_Id, out var fromDraft)
            ? fromDraft
            : series.Matches.Select(m => m.LobbyId)
                            .FirstOrDefault(x => x is not null);
        var res = await _login.ReportSeriesAsync(
            sides, series.Matches.Count, lowWins,
            series.Matches[0].PlayedAt, lobby, replays, s.Id);
        if (res is null)
        {
            // Sending failed rather than being refused: let the next poll try.
            _reportedSeries.Remove(s.Id);
            ShowDraftError(_api.LastError ?? "?");
            return;
        }

        _resultIds[s.Id] = res.Id;
        // The replays go up with it. A referee reading "the wrong commander was
        // loaded in game 3" can then watch game 3, which is the difference
        // between a report and a complaint.
        _ = UploadReplaysAsync(res.Id, series);
        // Said either way. "Recorded but not rated" is information a player
        // needs — it is what they would open a case about.
        ShowDraftError(res.Rated
            ? Loc.T("report.rated")
            : ErrorCodes.Text(ErrorCodes.GameNotCounted,
                              string.Join("; ", res.Reasons)));
    }

    /// <summary>
    /// Send this series' replays to the server.
    ///
    /// Best effort and deliberately quiet: a failed upload must not make a
    /// reported series look unreported. The server keeps them for a week, caps
    /// the size and names the stored file itself, so nothing here needs to be
    /// careful on its behalf.
    /// </summary>
    private async Task UploadReplaysAsync(string resultId, Core.Series series)
    {
        var (files, _) = series.ReplayFiles();
        var game = 0;
        foreach (var f in files)
        {
            game++;
            if (!f.Exists) continue;
            await _login.UploadReplayAsync(resultId, game, f.FullName);
        }
    }

    /// <summary>
    /// Ask a referee to look at a series.
    ///
    /// The counterpart to reporting being automatic: a player cannot decide that
    /// a result does not count, but they can say something was wrong with it and
    /// have somebody with the replay in front of them decide.
    /// </summary>
    private async Task OpenCaseAsync(string resultId, string note)
    {
        if (!await _login.FlagResultAsync(resultId, note))
            AppDialog.Info(this, _api.LastError ?? "?", Loc.T("report.case_title"), AppDialog.Kind.Info);
        else
            AppDialog.Info(this, Loc.T("report.case_opened"), Loc.T("report.case_title"), AppDialog.Kind.Info);
    }
}
