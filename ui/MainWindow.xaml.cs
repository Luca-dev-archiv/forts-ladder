using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

public partial class MainWindow : Window
{
    private readonly LogWatcher _watcher;
    private readonly IdentityStore _identity = new();
    private readonly List<MatchRecord> _matches = new();
    private readonly HashSet<string> _seenKeys = new();
    private readonly ObservableCollection<SeriesVm> _series = new();
    private Series? _selected;

    public MainWindow()
    {
        InitializeComponent();
        SeriesList.ItemsSource = _series;
        EmptyHint.Text = Loc.T("series.empty");
        TableEmpty.Text = Loc.T("table.missing");
        InitServerViews();

        _watcher = new LogWatcher(TimeSpan.FromSeconds(2));
        _watcher.StatusChanged += (s, ok) => Dispatcher.Invoke(() => SetStatus(s, ok));
        _watcher.AccountDetected += (id, persona) =>
            Dispatcher.Invoke(() => OnAccount(id, persona));
        _watcher.MatchFinished += m => Dispatcher.Invoke(() => AddMatch(m));

        Loaded += (_, _) => LoadHistory();
        Loaded += (_, _) => _ = CheckForUpdateAsync();
        Closed += (_, _) => _watcher.Dispose();
    }

    // ----------------------------------------------------------------- Updates
    /// <summary>
    /// Offer an update if there is one. Never installs on its own.
    ///
    /// Fire-and-forget on purpose: someone who opened this to record a match
    /// should not wait on a network call, and a failed check is not an error
    /// worth showing.
    /// </summary>
    private async Task CheckForUpdateAsync()
    {
        var rel = await Updater.CheckAsync();
        if (rel is null) return;

        var answer = MessageBox.Show(
            this,
            Loc.T("update.available", rel.Version.ToString(),
                  Updater.CurrentVersion().ToString(),
                  Math.Round(rel.SizeBytes / 1e6, 1)),
            Loc.T("update.title"), MessageBoxButton.YesNo,
            MessageBoxImage.Information);
        if (answer != MessageBoxResult.Yes) return;

        try
        {
            SetStatus(Loc.T("update.downloading"));
            var staged = await Updater.DownloadAndVerifyAsync(rel);
            // Only reached when the checksum matched.
            SetStatus(Loc.T("update.restarting"), true);
            Updater.ApplyAndRestart(staged);
        }
        catch (Exception ex)
        {
            // Includes a checksum mismatch, which is the case that matters:
            // say so plainly instead of retrying quietly.
            SetStatus(Loc.T("update.failed"));
            MessageBox.Show(this, ex.Message, Loc.T("update.failed"),
                            MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    // ----------------------------------------------------------------- Status
    private void SetStatus(string text, bool ok = false)
    {
        // The state arrives as a parameter, not inferred from the text: an
        // earlier version matched on a German prefix and would never have
        // turned green in English.
        StatusText.Text = text;
        StatusDot.Fill = (Brush)FindResource(ok ? "Win" : "Warn");
    }

    private void OnAccount(string steamId, string persona)
    {
        AccountName.Text = string.IsNullOrEmpty(persona) ? Loc.T("sidebar.name_unknown") : persona;
        AccountId.Text = steamId;
        RefreshUferName(steamId);

        // Ask once who owns this machine, then never again. A "no" is
        // recorded too, or the tool nags on every start and gets turned off.
        if (!_identity.WasAsked(steamId))
            AskWhoAmI(steamId, persona);
    }

    private void RefreshUferName(string steamId)
    {
        var name = _identity.UferNameFor(steamId);
        UferName.Text = name ?? Loc.T("sidebar.unassigned");
        UferName.Foreground = name is null ? (Brush)FindResource("Warn")
                                           : (Brush)FindResource("TextHi");
        RebuildSeries();
    }

    private void AskWhoAmI(string steamId, string persona)
    {
        var dlg = new IdentityDialog(steamId, persona, IdentityStore.LoadUferNames())
        { Owner = this };
        if (dlg.ShowDialog() != true) return;

        if (dlg.Skipped) { _identity.SkipDeclaration(steamId); }
        else if (!string.IsNullOrWhiteSpace(dlg.ChosenName))
        {
            try { _identity.SelfDeclare(steamId, dlg.ChosenName!); }
            catch (InvalidOperationException ex)
            {
                MessageBox.Show(this, ex.Message, Loc.T("identity.taken_title"),
                    MessageBoxButton.OK, MessageBoxImage.Warning);
            }
        }
        RefreshUferName(steamId);
    }

    private void BtnSetup_Click(object sender, RoutedEventArgs e)
    {
        var id = _watcher.CurrentAccount;
        if (id is null)
        {
            MessageBox.Show(this, Loc.T("identity.no_account"),
                Loc.T("identity.no_account_title"),
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        AskWhoAmI(id, _watcher.CurrentPersona ?? "");
    }

    // ------------------------------------------------------------------ Games
    private void LoadHistory()
    {
        // What Forts copied itself survives the next game start: the logs in
        // users/&lt;id&gt;/desyncs/. The only route to historical games.
        var forts = FortsPaths.FindFortsDir();
        if (forts is null)
        {
            SetStatus(Loc.T("status.forts_missing"));
            return;
        }
        var users = Path.Combine(forts, "users");
        if (!Directory.Exists(users)) return;

        foreach (var file in Directory.EnumerateFiles(users, "*.txt",
                     SearchOption.AllDirectories))
        {
            if (!file.Contains("desyncs", StringComparison.OrdinalIgnoreCase)) continue;
            var name = Path.GetFileName(file);
            if (!name.Contains("log", StringComparison.OrdinalIgnoreCase)) continue;
            if (name.Contains("world-dump") || name.Contains("checksum")) continue;
            try
            {
                foreach (var m in LogParser.ParseFile(file)) AddMatch(m, quiet: true);
            }
            catch (IOException) { /* one unreadable copy must not stop the rest */ }
        }
        RebuildSeries();
    }

    private void AddMatch(MatchRecord m, bool quiet = false)
    {
        // Every desync copy holds the whole session log so far — without the
        // key check the same game would appear several times in the list
        // (and later several times in the rating).
        if (!_seenKeys.Add(m.Key)) return;
        _matches.Add(m);
        if (!quiet) RebuildSeries();
    }

    private void RebuildSeries()
    {
        var selectedKey = _selected?.Matches.FirstOrDefault()?.Key;
        var grouped = Series.Group(_matches, null, _watcher.CurrentAccount);
        _series.Clear();
        foreach (var s in grouped) _series.Add(new SeriesVm(s, _identity));

        StatSeries.Text = grouped.Count.ToString();
        StatMatches.Text = grouped.Sum(s => s.Matches.Count).ToString();
        EmptyHint.Visibility = grouped.Count == 0 ? Visibility.Visible
                                                  : Visibility.Collapsed;

        if (selectedKey is not null)
        {
            var again = _series.FirstOrDefault(
                v => v.Model.Matches.Any(m => m.Key == selectedKey));
            if (again is not null) SeriesList.SelectedItem = again;
        }
    }

    private void BtnRescan_Click(object sender, RoutedEventArgs e)
    {
        _matches.Clear();
        _seenKeys.Clear();
        LoadHistory();
    }

    // ----------------------------------------------------------------- Detail
    private void SeriesList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (SeriesList.SelectedItem is not SeriesVm vm)
        {
            DetailContent.Visibility = Visibility.Collapsed;
            DetailPlaceholder.Visibility = Visibility.Visible;
            _selected = null;
            return;
        }
        _selected = vm.Model;
        DetailPlaceholder.Visibility = Visibility.Collapsed;
        DetailContent.Visibility = Visibility.Visible;

        var sides = vm.Model.Sides();
        var (wins, _) = vm.Model.Score();
        DetailTitle.Text = string.Join("   vs   ", sides.Select(kv =>
            $"{string.Join(", ", vm.Model.Names(_identity, kv.Key))} " +
            $"({wins.GetValueOrDefault(kv.Key)})"));
        DetailWhen.Text = $"{vm.Model.PlayedAt:dd.MM.yyyy HH:mm}  ·  " +
                          Loc.T("series.count", vm.Model.Matches.Count) + "  ·  " +
                          (vm.Model.IsTeam ? Loc.T("series.team") : Loc.T("series.duel"));

        GamesList.ItemsSource = vm.Model.Matches.Select(m => new GameVm(m, this)).ToList();

        var (line, warnings) = vm.Model.Report(_identity);
        ReportLine.Text = string.IsNullOrEmpty(line)
            ? Loc.T("series.no_line") : line;
        WarningsList.ItemsSource = warnings;
        BtnCopy.IsEnabled = !string.IsNullOrEmpty(line);

        var (found, missing) = vm.Model.ReplayFiles();
        BtnCollect.IsEnabled = found.Count > 0;
        var mb = found.Sum(f => f.Length) / 1e6;
        ReplayInfo.Text = found.Count == 0
            ? Loc.T("series.no_replays")
            : $"{found.Count} Replay(s), {mb:0.0} MB" +
              (missing.Count > 0 ? $"  ·  {missing.Count} fehlen" : "");
    }

    private void BtnCopy_Click(object sender, RoutedEventArgs e)
    {
        if (_selected is null) return;
        var (line, _) = _selected.Report(_identity);
        if (string.IsNullOrEmpty(line)) return;
        try
        {
            Clipboard.SetText(line);
            BtnCopy.Content = Loc.T("series.copied");
            // Reset so the button says what it does again next time.
            var t = new System.Windows.Threading.DispatcherTimer
            { Interval = TimeSpan.FromSeconds(1.6) };
            t.Tick += (_, _) => { BtnCopy.Content = Loc.T("series.copy"); t.Stop(); };
            t.Start();
        }
        catch (System.Runtime.InteropServices.ExternalException)
        {
            // Another program can be holding the clipboard.
            MessageBox.Show(this, Loc.T("series.clipboard_busy"),
                Loc.T("series.clipboard_title"), MessageBoxButton.OK,
                MessageBoxImage.Information);
        }
    }

    private void BtnCollect_Click(object sender, RoutedEventArgs e)
    {
        if (_selected is null) return;
        var stamp = _selected.PlayedAt.ToString("yyyyMMdd_HHmm");
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            $"UFER_{stamp}");
        var (n, bytes) = _selected.Collect(dir, _identity);
        ReplayInfo.Text = Loc.T("series.collected", n, bytes / 1e6, dir);
        // The folder opens so the files can be dragged straight into
        // Discord. Nothing is sent — a human does that.
        try { System.Diagnostics.Process.Start("explorer.exe", $"\"{dir}\""); }
        catch (Exception) { /* the folder is there either way */ }
    }

    // ---------------------------------------------------------------- Ranking
    private readonly RatingsTable _table = new();

    private void NavSeries_Click(object sender, RoutedEventArgs e) => ShowView(series: true);

    private void NavTable_Click(object sender, RoutedEventArgs e)
    {
        ShowView(series: false);
        ReloadTable();
    }

    private void ShowView(bool series) => ShowView(series ? "series" : "table");

    private void ShowView(string which)
    {
        void Set(UIElement view, UIElement header, Button nav, bool on)
        {
            view.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
            header.Visibility = view.Visibility;
            nav.Background = on ? (Brush)FindResource("BgHover") : Brushes.Transparent;
        }
        Set(ViewSeries, HeaderSeries, NavSeries, which == "series");
        Set(ViewTable, HeaderTable, NavTable, which == "table");
        Set(ViewDraft, HeaderDraft, NavDraft, which == "draft");
        Set(ViewLive, HeaderLive, NavLive, which == "live");
        Set(ViewTour, HeaderTour, NavTour, which == "tour");
    }

    private void ReloadTable()
    {
        _table.Reload(_watcher.CurrentAccount);
        TableList.ItemsSource = _table.Search(TableSearch.Text);
        TableEmpty.Visibility = _table.Loaded ? Visibility.Collapsed : Visibility.Visible;

        if (!_table.Loaded)
        {
            TableSubtitle.Text = Loc.T("table.missing_at", _table.Path);
            MeLine.Text = "";
            return;
        }
        TableSubtitle.Text =
            Loc.T("table.summary", _table.Players.Count, _table.EventsUsed)
            + (_table.Skipped.Count > 0
                ? "  ·  " + Loc.T("table.skipped", _table.Skipped.Count) : "");
        // Your own position is the one row everybody looks for, and in a few
        // hundred rows it is otherwise hard to find.
        var me = _table.Me;
        MeLine.Text = me is null
            ? Loc.T("table.you_unassigned")
            : Loc.T("table.you", me.Rank, me.UferText, me.OpenText);
    }

    private void BtnReloadTable_Click(object sender, RoutedEventArgs e) => ReloadTable();

    private void TableSearch_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_table.Loaded) TableList.ItemsSource = _table.Search(TableSearch.Text);
    }

    // ------------------------------------------------------------------ Draft
    private DraftEngine? _draft;
    // With nobody on the other end, one person operates both sides (hot
    // seat). For the simultaneous commander pick that means A locks in
    // first, then B — it stays blind regardless, because the result only
    // appears once both have locked in.
    private DraftSide _hotSeat = DraftSide.A;

    private void NavDraft_Click(object sender, RoutedEventArgs e) => ShowView("draft");

    private void BtnNewDraft_Click(object sender, RoutedEventArgs e)
    {
        var bestOf = DraftFormat.SelectedIndex switch { 0 => 1, 2 => 5, _ => 3 };
        var maps = LeagueMapPool();
        var commanders = CommanderNames.Installed();
        if (maps.Count < bestOf || commanders.Count < 4)
        {
            MessageBox.Show(this,
                $"Zu wenig Material: {maps.Count} Karten, {commanders.Count} Commander.\n" +
                "Ist Forts gefunden worden?", Loc.T("draft.impossible"),
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        try { _draft = new DraftEngine(maps, commanders, bestOf); }
        catch (ArgumentException ex)
        {
            MessageBox.Show(this, ex.Message, Loc.T("draft.impossible"),
                MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        _hotSeat = DraftSide.A;
        RefreshDraft();
    }

    /// <summary>
    /// The duel pool: this season's ranked maps, minus Hillfort. Falls back
    /// to the maps present in the install if the season list is unreadable —
    /// an empty draft would be worse than a rough one.
    /// </summary>
    private static List<string> LeagueMapPool()
    {
        var forts = FortsPaths.FindFortsDir();
        var pool = new List<string>();
        if (forts is not null)
        {
            var constants = Path.Combine(forts, "data", "db", "constants.lua");
            if (File.Exists(constants))
            {
                try
                {
                    var text = File.ReadAllText(constants);
                    var start = text.IndexOf("\nRankedMaps", StringComparison.Ordinal);
                    if (start > 0)
                    {
                        var seasons = System.Text.RegularExpressions.Regex.Matches(
                            text[start..], @"\[(\d+)\]\s*=\s*\{(.*?)\n\t\}",
                            System.Text.RegularExpressions.RegexOptions.Singleline);
                        if (seasons.Count > 0)
                        {
                            var last = seasons[^1];
                            foreach (System.Text.RegularExpressions.Match m in
                                     System.Text.RegularExpressions.Regex.Matches(
                                         last.Groups[2].Value, @"^\s*""([^""]+)""",
                                         System.Text.RegularExpressions.RegexOptions.Multiline))
                                pool.Add(m.Groups[1].Value);
                        }
                    }
                }
                catch (IOException) { }
            }
        }
        // Ladder rule: Hillfort is permanently banned in duels.
        pool.RemoveAll(m => m == "Hillfort");
        return pool;
    }

    private void Tile_Click(object sender, RoutedEventArgs e)
    {
        if (_draft is null || sender is not Button b || b.Tag is not string id) return;
        try
        {
            var step = _draft.Current;
            _draft.Apply(id, step?.Side ?? _hotSeat);
            if (step?.Side is null)
                _hotSeat = DraftEngine.Other(_hotSeat);   // hand over the seat
        }
        catch (InvalidOperationException ex)
        {
            DraftHint.Text = ex.Message;
            return;
        }
        RefreshDraft();
    }

    private void RefreshDraft()
    {
        if (_draft is null) return;
        var d = _draft;
        var step = d.Current;

        DraftProgress.Text = Loc.T("draft.progress",
            Math.Min(d.StepIndex + 1, d.Steps.Count), d.Steps.Count);
        if (step is null)
        {
            DraftStepText.Text = Loc.T("draft.finished");
            DraftHint.Text = Loc.T("draft.finished_hint");
        }
        else
        {
            DraftStepText.Text = step.Describe();
            DraftHint.Text = step.Side is null
                ? Loc.T("draft.hint_blind", _hotSeat)
                : Loc.T("draft.hint_click");
        }

        DraftStrike.Text = d.NeutralStrike is null ? ""
            : Loc.T("draft.strike", d.NeutralStrike);
        var bans = d.Steps.Count(s => s.Action == DraftAction.BanMap && s.Side == DraftSide.A);
        var picks = d.Steps.Count(s => s.Action == DraftAction.PickMap && s.Side == DraftSide.A);
        DraftFairness.Text = Loc.T("draft.fairness", bans, picks);

        var activeSide = step?.Side ?? _hotSeat;
        var legal = step is null ? new List<string>() : d.LegalOptions(activeSide);
        var isMapStep = step?.Action is DraftAction.BanMap or DraftAction.PickMap;
        var isBan = step?.Action is DraftAction.BanMap or DraftAction.BanCommander;

        MapTiles.ItemsSource = d.MapPool.Select(m => TileFor(
            m, m,
            banned: d.BannedMaps.Contains(m),
            picked: d.PickedMaps.Values.Contains(m),
            pickedLabel: d.PickedMaps.FirstOrDefault(kv => kv.Value == m).Key is int g
                && g > 0 ? Loc.T("draft.game_short", g) : null,
            enabled: isMapStep && legal.Contains(m),
            isBanStep: isBan)).ToList();

        var plan = d.Plan();
        CommanderTiles.ItemsSource = d.CommanderPool.Select(c => TileFor(
            CommanderNames.Display(c), c,
            banned: d.BannedCommanders.Contains(c),
            picked: plan.Any(p => p.CommanderA == c || p.CommanderB == c),
            pickedLabel: plan.FirstOrDefault(p => p.CommanderA == c || p.CommanderB == c)
                is DraftEngine.PlannedGame pg ? Loc.T("draft.game_short", pg.Game) : null,
            enabled: step?.Action is DraftAction.BanCommander
                         or DraftAction.PickCommander && legal.Contains(c),
            isBanStep: isBan)).ToList();

        PlanList.ItemsSource = plan.Select(p => new
        {
            Title = Loc.T("draft.game", p.Game, p.Map ?? "—"),
            Tag = p.Decider ? Loc.T("draft.decider")
                : p.PickedBy is null ? "" : Loc.T("draft.picked_by", p.PickedBy),
            Commanders = p.CommanderA is null && p.CommanderB is null
                ? Loc.T("draft.commanders_open")
                : $"{CommanderNames.Display(p.CommanderA ?? "?")} vs " +
                  $"{CommanderNames.Display(p.CommanderB ?? "?")}",
        }).ToList();
    }

    private object TileFor(string label, string id, bool banned, bool picked,
                           string? pickedLabel, bool enabled, bool isBanStep)
    {
        // Banned and picked have to be distinguishable at a glance — a draft
        // is decided in seconds.
        var bg = banned ? "#3A1F22" : picked ? "#1F3A2C" : enabled ? "#262B36" : "#1A1D24";
        var fg = banned || (!enabled && !picked) ? "TextLow" : "TextHi";
        return new
        {
            Id = id,
            Label = label,
            State = banned ? Loc.T("draft.state_banned") : pickedLabel ?? (enabled
                ? (isBanStep ? Loc.T("draft.state_ban") : Loc.T("draft.state_pick")) : ""),
            Enabled = enabled,
            Background = new SolidColorBrush(
                (Color)ColorConverter.ConvertFromString(bg)),
            Foreground = (Brush)FindResource(fg),
            StateBrush = banned ? (Brush)FindResource("Loss")
                       : picked ? (Brush)FindResource("Win")
                       : (Brush)FindResource("TextLow"),
        };
    }

    internal Brush BrushFor(string key) => (Brush)FindResource(key);
}

// --------------------------------------------------------------- Ansichtsdaten
public sealed class SeriesVm
{
    public Series Model { get; }
    public string Title { get; }
    public string Subtitle { get; }
    public string ScoreText { get; }
    public Brush ScoreBrush { get; }

    public SeriesVm(Series s, IdentityStore ids)
    {
        Model = s;
        var sides = s.Sides();
        var (wins, _) = s.Score();
        var own = s.LocalSide() ?? (sides.Count > 0 ? sides.Keys.Min() : 0);
        var other = sides.Keys.FirstOrDefault(x => x != own);

        Title = other == 0
            ? string.Join(", ", sides.SelectMany(kv => s.Names(ids, kv.Key)))
            : "vs " + string.Join(", ", s.Names(ids, other));
        Subtitle = $"{s.PlayedAt:dd.MM. HH:mm}  ·  " + Loc.T("series.count", s.Matches.Count) + "  ·  " +
                   string.Join(", ", s.Matches.Select(m => m.Map).Distinct());

        var w = wins.GetValueOrDefault(own);
        var l = other == 0 ? 0 : wins.GetValueOrDefault(other);
        ScoreText = $"{w}-{l}";
        ScoreBrush = w > l ? new SolidColorBrush(Color.FromRgb(0x3D, 0xD6, 0x8C))
                   : w < l ? new SolidColorBrush(Color.FromRgb(0xF0, 0x61, 0x6D))
                           : new SolidColorBrush(Color.FromRgb(0xA7, 0xB0, 0xC0));
    }
}

public sealed class GameVm
{
    public string Map { get; }
    public string Duration { get; }
    public string Result { get; }
    public string Commanders { get; }
    public Brush ResultBrush { get; }

    public GameVm(MatchRecord m, MainWindow w)
    {
        Map = m.Map ?? "?";
        Duration = m.DurationSeconds is int d ? $"{d / 60}:{d % 60:00}" : "—";
        Commanders = string.Join("   ", m.Commanders
            .OrderBy(kv => kv.Key)
            .Select(kv => $"S{kv.Key}: {kv.Value.Replace("commander-", "")}"));
        (Result, ResultBrush) = m.Status switch
        {
            MatchStatus.Decided => (Loc.T("series.result_side", m.WinnerSide), w.BrushFor("Win")),
            MatchStatus.VsAi => (Loc.T("series.result_ai"), w.BrushFor("TextMid")),
            // Undecided games are named, not hidden: an aborted game HAS no
            // result, and that has to be visible.
            _ => (Loc.T("series.result_none"), w.BrushFor("Warn")),
        };
    }
}
