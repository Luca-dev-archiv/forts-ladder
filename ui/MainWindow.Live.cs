using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// Live matches and tournaments — both server-backed.
///
/// Split into its own file so the recorder half of the window stays readable.
/// Everything here degrades gracefully without a server: the views explain
/// that they need one instead of showing an empty list, which would look like
/// "nothing is happening" and send people hunting for a bug that isn't there.
/// </summary>
public partial class MainWindow
{
    private readonly ApiClient _api = new();
    private string? _selectedTournament;

    private void InitServerViews()
    {
        ServerBox.Text = _api.BaseUrl ?? ApiClient.DefaultBaseUrl;
    }

    // ------------------------------------------------------------ Navigation
    private void NavLive_Click(object sender, RoutedEventArgs e)
    {
        ShowView("live");
        _ = RefreshLiveAsync();
    }

    private void NavTour_Click(object sender, RoutedEventArgs e)
    {
        ShowView("tour");
        _ = RefreshTournamentsAsync();
    }

    private void BtnConnect_Click(object sender, RoutedEventArgs e)
    {
        _api.SetBaseUrl(ServerBox.Text);
        _ = RefreshLiveAsync();
    }

    private void BtnRefreshLive_Click(object sender, RoutedEventArgs e) =>
        _ = RefreshLiveAsync();

    private void BtnRefreshTour_Click(object sender, RoutedEventArgs e) =>
        _ = RefreshTournamentsAsync();

    // ---------------------------------------------------------- Live matches
    private async Task RefreshLiveAsync()
    {
        if (!_api.Configured)
        {
            LiveList.ItemsSource = null;
            LiveEmpty.Text = Loc.T("live.offline");
            LiveEmpty.Visibility = Visibility.Visible;
            LiveSubtitle.Text = Loc.T("live.sub");
            return;
        }

        var data = await _api.GetAsync<LiveListDto>("/live");
        if (data is null)
        {
            LiveList.ItemsSource = null;
            // Name the address: "not reachable" without saying which one is
            // the least useful error message there is.
            LiveEmpty.Text = Loc.T("live.unreachable", _api.BaseUrl!)
                             + (_api.LastError is null ? "" : $"\n\n{_api.LastError}");
            LiveEmpty.Visibility = Visibility.Visible;
            return;
        }

        LiveSubtitle.Text = Loc.T("live.connected", _api.BaseUrl!);
        var rows = data.Matches.Select(m => new LiveVm(m, this)).ToList();
        LiveList.ItemsSource = rows;
        LiveEmpty.Text = Loc.T("live.empty");
        LiveEmpty.Visibility = rows.Count == 0 ? Visibility.Visible
                                               : Visibility.Collapsed;
    }

    private async void BtnObserve_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button b || b.Tag is not string id) return;
        b.IsEnabled = false;
        var ok = await _api.PostAsync($"/live/{id}/observe");
        b.Content = ok ? Loc.T("live.requested") : Loc.T("live.request");
        if (!ok && _api.LastError is not null)
            MessageBox.Show(this, _api.LastError, Loc.T("live.headline"),
                            MessageBoxButton.OK, MessageBoxImage.Information);
    }

    // ----------------------------------------------------------- Tournaments
    private async Task RefreshTournamentsAsync()
    {
        if (!_api.Configured)
        {
            TourList.ItemsSource = null;
            TourEmpty.Text = Loc.T("tour.offline");
            TourEmpty.Visibility = Visibility.Visible;
            return;
        }

        var data = await _api.GetAsync<TournamentListDto>("/tournaments");
        if (data is null)
        {
            TourList.ItemsSource = null;
            TourEmpty.Text = Loc.T("live.unreachable", _api.BaseUrl!);
            TourEmpty.Visibility = Visibility.Visible;
            return;
        }

        var rows = data.Tournaments.Select(t => new TourVm(t)).ToList();
        TourList.ItemsSource = rows;
        TourEmpty.Text = Loc.T("tour.empty");
        TourEmpty.Visibility = rows.Count == 0 ? Visibility.Visible
                                               : Visibility.Collapsed;
        TourSubtitle.Text = Loc.T("live.connected", _api.BaseUrl!);
    }

    private async void TourList_SelectionChanged(object sender,
                                                 SelectionChangedEventArgs e)
    {
        if (TourList.SelectedItem is not TourVm vm)
        {
            _selectedTournament = null;
            return;
        }
        _selectedTournament = vm.Id;
        var d = await _api.GetAsync<TournamentDetailDto>($"/tournaments/{vm.Id}");
        if (d is null) return;

        TourPlaceholder.Visibility = Visibility.Collapsed;
        TourChampion.Text = d.Champion is null ? "" : Loc.T("tour.champion", d.Champion);
        TourChampion.Visibility = d.Champion is null ? Visibility.Collapsed
                                                     : Visibility.Visible;
        BracketList.ItemsSource = d.Bracket
            .Select(r => new RoundVm(r, this)).ToList();
    }
}

// --------------------------------------------------------------- View models
public sealed class LiveVm
{
    public string Id { get; }
    public string Title { get; }
    public string Sub { get; }
    public string Extra { get; }
    public string Slots { get; }
    public string ActionLabel { get; }
    public bool CanRequest { get; }
    public Brush SlotBrush { get; }

    public LiveVm(LiveMatchDto m, MainWindow w)
    {
        Id = m.Id;
        Title = string.Join("  vs  ", m.Players);
        var minutes = m.Running_For_S / 60;
        Sub = $"{m.Mode}  ·  " + Loc.T("live.running", $"{minutes} min");
        Extra = m.Tournament is null ? "" : Loc.T("live.tournament", m.Tournament);

        Slots = m.Free_Slots > 0 ? Loc.T("live.free_slots", m.Free_Slots)
                                 : Loc.T("live.full");
        SlotBrush = w.BrushFor(m.Free_Slots > 0 ? "TextMid" : "Loss");

        CanRequest = m.Accepting_Requests && m.Free_Slots > 0;
        // Say *why* the button is dead. "Disabled" alone makes people click
        // it again and assume the tool is broken.
        ActionLabel = m.Free_Slots <= 0 ? Loc.T("live.no_room")
                    : !m.Accepting_Requests ? Loc.T("live.closed")
                    : Loc.T("live.request");
    }
}

public sealed class TourVm
{
    public string Id { get; }
    public string Name { get; }
    public string Sub { get; }

    public TourVm(TournamentSummaryDto t)
    {
        Id = t.Id;
        Name = t.Name;
        Sub = Loc.T("tour.participants", t.Participants) + "  ·  "
              + Loc.T(t.Finished != 0 ? "tour.finished" : "tour.running");
    }
}

public sealed class RoundVm
{
    public string Round { get; }
    public List<BracketMatchVm> Matches { get; }

    public RoundVm(BracketRoundDto r, MainWindow w)
    {
        // The server sends a neutral key and translation happens here.
        // Sending finished text would put German round names into the
        // English client — which is exactly what happened first time round.
        Round = r.Round_Key switch
        {
            "final" => Loc.T("tour.round_final"),
            "semi" => Loc.T("tour.round_semi"),
            "quarter" => Loc.T("tour.round_quarter"),
            "r16" => Loc.T("tour.round_16"),
            var k when k.StartsWith("r") => Loc.T("tour.round_n", k[1..]),
            _ => r.Round,
        };
        Matches = r.Matches.Select(m => new BracketMatchVm(m, w)).ToList();
    }
}

public sealed class BracketMatchVm
{
    public string Label { get; }
    public string State { get; }
    public Brush Accent { get; }

    public BracketMatchVm(BracketMatchDto m, MainWindow w)
    {
        // Compose the label locally too: "(Freilos)" came baked in from the
        // server otherwise.
        Label = m.Bye
            ? Loc.T("tour.bye", m.A_Name ?? m.B_Name ?? Loc.T("common.none"))
            : $"{m.A_Name ?? Loc.T("common.none")} vs {m.B_Name ?? Loc.T("common.none")}"
              + (m.Score is { Count: 2 } s ? $"  {s[0]}:{s[1]}" : "");
        (State, Accent) = m.Winner is not null
            ? (m.Winner, w.BrushFor("Win"))
            : m.Ready ? (Loc.T("tour.playable"), w.BrushFor("Accent"))
            : ("", w.BrushFor("Stroke"));
    }
}
