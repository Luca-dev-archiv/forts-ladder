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

        _draft = new ServerDraft(_api);
        _draft.Changed += RefreshDraft;
        RefreshDraft();

        _login = new LoginFlow(_api);
        InitLanguagePicker();
        _queue = new ServerQueue(_api);
        InitQueue();

        _watcher = new LogWatcher(TimeSpan.FromSeconds(2));
        _watcher.StatusChanged += (s, ok) => Dispatcher.Invoke(() => SetStatus(s, ok));
        _watcher.AccountDetected += (id, persona) =>
            Dispatcher.Invoke(() => OnAccount(id, persona));
        _watcher.MatchFinished += m => Dispatcher.Invoke(() => AddMatch(m));

        Loaded += (_, _) => LoadHistory();
        Loaded += (_, _) => _ = CheckForUpdateAsync();
        Closed += (_, _) => { _watcher.Dispose(); _draft.Dispose(); _queue.Dispose(); };
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

    // --------------------------------------------------------------- Language
    private bool _langReady;

    private void InitLanguagePicker()
    {
        LangBox.ItemsSource = Loc.Available();
        LangBox.SelectedItem = Loc.Language;
        // Set after populating, or assigning the items would fire the handler
        // and immediately claim the language had been changed.
        _langReady = true;
    }

    private void LangBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!_langReady || LangBox.SelectedItem is not string lang) return;
        if (lang == Loc.Language) return;
        Loc.Remember(lang);
        LangHint.Text = Loc.T("sidebar.language_restart");
        LangHint.Visibility = Visibility.Visible;
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
        // Deliberately NOT clearing first. LoadHistory only reads the archive
        // under desyncs/, so wiping the list threw away every match the live
        // watcher had recorded this session — including the one just played,
        // which is the one you would be looking at. The key set makes a second
        // pass idempotent, so this only ever adds what is new.
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
            // An accent bar on the left edge: a filled background alone is hard
            // to spot at a glance while a step timer is running.
            nav.BorderBrush = on ? (Brush)FindResource("Accent") : Brushes.Transparent;
            nav.BorderThickness = new Thickness(on ? 3 : 0, 0, 0, 0);
        }
        Set(ViewQueue, HeaderQueue, NavQueue, which == "queue");
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
    //
    // Server-backed. The client holds no draft rules at all: it sends a value
    // and re-reads the state. Whose turn it is, what is legal, and above all
    // what the opponent may see are decided server-side — the half that hides
    // a pick must not run on the machine it is being hidden from.
    private readonly ServerDraft _draft;

    private void NavDraft_Click(object sender, RoutedEventArgs e) => ShowView("draft");

    private async void BtnHostDraft_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureReadyAsync()) return;
        var maps = LeagueMapPool();
        var commanders = CommanderNames.Installed();
        if (maps.Count < 5 || commanders.Count < 4)
        {
            ShowDraftError(Loc.T("draft.too_little", maps.Count, commanders.Count));
            return;
        }
        var bestOf = DraftFormat.SelectedIndex switch { 0 => 1, 2 => 5, _ => 3 };
        if (!await _draft.CreateAsync(maps, commanders, bestOf))
            ShowDraftError(_draft.LastError ?? "?");
    }

    private async void BtnJoinDraft_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureReadyAsync()) return;
        var code = JoinCodeBox.Text.Trim();
        if (code.Length == 0) return;
        if (!await _draft.JoinAsync(code))
            ShowDraftError(_draft.LastError ?? "?");
    }

    private readonly LoginFlow _login;

    /// <summary>
    /// Make sure there is a server and an account, signing in if needed.
    ///
    /// Signing in happens *here*, at the moment something actually needs it,
    /// rather than being demanded up front — and it starts by asking the
    /// Discord app that is almost certainly already running. Telling someone to
    /// go and log in elsewhere is the last resort, not the first instruction.
    /// </summary>
    private async Task<bool> EnsureReadyAsync()
    {
        if (!_api.Configured)
        {
            ShowDraftError(Loc.T("draft.needs_server"));
            return false;
        }
        if (_api.LoggedIn) return true;

        SetStatus(Loc.T("login.asking_discord"));
        var r = await _login.TryDiscordAppAsync();
        if (r.Ok)
        {
            SetStatus(Loc.T("login.signed_in"), true);
            DraftErrorBar.Visibility = Visibility.Collapsed;
            return true;
        }

        // Discord could not be asked. Say why, and offer the browser route in
        // the same breath instead of leaving someone at a dead end.
        if (!r.CanRetryInBrowser)
        {
            ShowDraftError(r.Error ?? Loc.T("draft.needs_login"));
            return false;
        }
        var dlg = new PairDialog(_login, r.Error) { Owner = this };
        if (dlg.ShowDialog() == true)
        {
            SetStatus(Loc.T("login.signed_in"), true);
            DraftErrorBar.Visibility = Visibility.Collapsed;
            return true;
        }
        ShowDraftError(r.Error ?? Loc.T("draft.needs_login"));
        return false;
    }

    private void ShowDraftError(string message)
    {
        DraftError.Text = message;
        DraftErrorBar.Visibility = Visibility.Visible;
    }

    private async void Tile_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button b || b.Tag is not string id) return;
        if (!await _draft.ApplyAsync(id))
            ShowDraftError(_draft.LastError ?? "?");
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

    // -------------------------------------------------------------- Rendering
    private void RefreshDraft()
    {
        var s = _draft.State;
        var running = _draft.Active && s is not null;

        DraftSetup.Visibility = running && s!.Full
            ? Visibility.Collapsed : Visibility.Visible;
        VersusBar.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        BoardPanel.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        DraftEmpty.Visibility = running ? Visibility.Collapsed : Visibility.Visible;

        if (running && !s!.Full && _draft.JoinCode is { Length: > 0 } code)
        {
            RoomCodePanel.Visibility = Visibility.Visible;
            RoomCodeText.Text = code;
        }
        else RoomCodePanel.Visibility = Visibility.Collapsed;

        if (_draft.LastError is { Length: > 0 } err) ShowDraftError(err);
        else DraftErrorBar.Visibility = Visibility.Collapsed;

        if (!running) return;
        RenderVersus(s!);
        RenderBoard(s!);
        RenderPlan(s!);
    }

    private void RenderVersus(DraftStateDto s)
    {
        var you = s.Your_Side ?? "A";
        var opp = you == "A" ? "B" : "A";
        YouSide.Text = you;
        OppSide.Text = opp;
        YouName.Text = s.Seats.TryGetValue(you, out var yn) ? yn : "you";
        OppName.Text = s.Seats.TryGetValue(opp, out var on)
            ? on : Loc.T("draft.waiting_for_player");

        // "Locked in" is the only thing said about the opponent during a blind
        // pick — the server sends nothing more, and nothing more is needed.
        YouStatus.Text = s.Locked_In.Contains(you) ? Loc.T("draft.locked_in") : "";
        OppStatus.Text = s.Locked_In.Contains(opp) ? Loc.T("draft.locked_in") : "";

        DraftProgress.Text = Loc.T("draft.progress",
            Math.Min(s.Step_Index + 1, s.Step_Total), s.Step_Total);
        LockedLabel.Text = s.Your_Pending_Pick is { Length: > 0 } p
            ? Loc.T("draft.you_locked", s.Display(p)) : "";

        if (s.Done)
        {
            TurnBanner.Text = Loc.T("draft.finished");
            TurnBanner.Foreground = (Brush)FindResource("Win");
            TurnSub.Text = Loc.T("draft.finished_hint");
            TimerBar.Width = 0;
            return;
        }
        if (!s.Full)
        {
            TurnBanner.Text = Loc.T("draft.waiting_for_player");
            TurnBanner.Foreground = (Brush)FindResource("Warn");
            TurnSub.Text = Loc.T("draft.share_code");
            TimerBar.Width = 0;
            return;
        }

        var mine = s.YourTurn && s.Your_Pending_Pick is null;
        TurnBanner.Text = mine ? Loc.T("draft.your_turn") : Loc.T("draft.their_turn");
        TurnBanner.Foreground = (Brush)FindResource(mine ? "Accent" : "TextMid");
        TurnSub.Text = s.Action switch
        {
            "ban_map" => Loc.T("draft.sub_ban_map"),
            "pick_map" => Loc.T("draft.sub_pick_map"),
            "ban_commander" => Loc.T("draft.sub_ban_commander"),
            "pick_commander" => Loc.T("draft.sub_pick_commander"),
            _ => "",
        };

        // The bar is a fraction of the step budget rather than an animation, so
        // it can only ever show what the server reported.
        var left = s.Seconds_Left ?? 0;
        TimerBar.Width = Math.Max(0, Math.Min(200, 200 * left / 30.0));
        TimerBar.Background = (Brush)FindResource(left <= 5 ? "Loss" : "Accent");
    }

    private void RenderBoard(DraftStateDto s)
    {
        MapTiles.ItemsSource = s.Map_Pool.Select(m =>
        {
            var banned = s.Banned_Maps.Contains(m);
            var game = s.Picked_Maps.FirstOrDefault(kv => kv.Value == m).Key;
            var picked = game is not null;
            return Tile(m, m, banned, picked,
                        picked ? Loc.T("draft.game_short", game!) : null,
                        s.IsMapStep && s.YourTurn && s.Options.Contains(m),
                        s.IsBanStep);
        }).ToList();

        var inPlan = s.Plan
            .SelectMany(g => new[] { g.Commander_A, g.Commander_B })
            .Where(c => c is not null).ToHashSet();
        CommanderTiles.ItemsSource = s.Commander_Names.Keys.Select(c =>
        {
            var banned = s.Banned_Commanders.Contains(c);
            var chosen = inPlan.Contains(c) || s.Your_Pending_Pick == c;
            var note = s.Your_Pending_Pick == c ? Loc.T("draft.locked_in")
                     : inPlan.Contains(c) ? Loc.T("draft.state_picked") : null;
            return Tile(s.Display(c), c, banned, chosen, note,
                        !s.IsMapStep && s.YourTurn && s.Options.Contains(c),
                        s.IsBanStep);
        }).ToList();
    }

    private void RenderPlan(DraftStateDto s)
    {
        DraftStrike.Text = s.Neutral_Strike is { Length: > 0 } n
            ? Loc.T("draft.strike", n) : "";
        DraftFairness.Text = s.Done ? Loc.T("draft.finished_hint") : "";
        PlanList.ItemsSource = s.Plan.Select(g => new
        {
            Title = Loc.T("draft.game", g.Game, g.Map ?? "—"),
            Tag = g.Decider ? Loc.T("draft.decider")
                : g.Map_Picked_By is null ? "" : Loc.T("draft.picked_by", g.Map_Picked_By),
            Commanders = g.Commander_A is null && g.Commander_B is null
                ? Loc.T("draft.commanders_open")
                : $"{s.Display(g.Commander_A ?? "?")}  vs  {s.Display(g.Commander_B ?? "?")}",
            Accent = (Brush)FindResource(g.Decider ? "Warn" : "Stroke"),
        }).ToList();
    }

    /// <summary>
    /// One tile. Banned, picked, playable and idle have to be distinguishable
    /// at a glance — a draft step is decided in seconds.
    /// </summary>
    private object Tile(string label, string id, bool banned, bool picked,
                        string? note, bool enabled, bool isBanStep)
    {
        var bg = banned ? "#3A1F22" : picked ? "#1B3327"
               : enabled ? "#242935" : "#191C23";
        return new
        {
            Id = id,
            Label = label,
            Note = note ?? (enabled
                ? (isBanStep ? Loc.T("draft.state_ban") : Loc.T("draft.state_pick"))
                : banned ? Loc.T("draft.state_banned") : ""),
            Enabled = enabled,
            Background = new SolidColorBrush(
                (Color)ColorConverter.ConvertFromString(bg)),
            Border = (Brush)FindResource(enabled ? "Accent"
                : banned ? "Loss" : picked ? "Win" : "Stroke"),
            Thickness = new Thickness(enabled ? 2 : 1),
            Fore = (Brush)FindResource(banned || (!enabled && !picked)
                ? "TextLow" : "TextHi"),
            NoteBrush = (Brush)FindResource(banned ? "Loss"
                : picked ? "Win" : enabled ? "Accent" : "TextLow"),
        };
    }

    internal Brush BrushFor(string key) => (Brush)FindResource(key);
}

// ------------------------------------------------------------- View models
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
