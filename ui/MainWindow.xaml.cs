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
    private System.Windows.Threading.DispatcherTimer? _lobbyFile;
    private readonly IdentityStore _identity = new();
    /// <summary>Lobbies this ladder set up — hard boundaries for the series
    /// grouping, so a new lobby is never folded into the previous series.</summary>
    private readonly LadderLobbies _ladderLobbies = new();
    /// <summary>Which series each reported game belonged to. The grouping asks
    /// this before it starts guessing from timestamps.</summary>
    private readonly DraftedGames _draftedGames = new();
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
        // Only the countdown. Anything here that rebuilt a control would put the
        // three-clicks-to-ban bug straight back.
        _draft.Tick += RefreshDraftClock;
        RefreshDraft();

        _login = new LoginFlow(_api);
        InitLanguagePicker();
        TitleVersion.Text = "v" + Updater.CurrentVersion();
        StateChanged += (_, _) => OnWindowStateChanged();
        // A dead session is not a mystery, and the client used to treat it as
        // one: every call failed with a bare 401 while it went on believing it
        // was signed in.
        _api.SignedOut += () => Dispatcher.Invoke(OnSignedOut);
        _queue = new ServerQueue(_api);
        InitQueue();
        InitObservers();
        InitPresence();

        _watcher = new LogWatcher(TimeSpan.FromSeconds(1));
        _watcher.StatusChanged += (s, ok) => Dispatcher.Invoke(() => SetStatus(s, ok));
        _watcher.AccountDetected += (id, persona) =>
            Dispatcher.Invoke(() => OnAccount(id, persona));
        _watcher.MatchFinished += m => Dispatcher.Invoke(() =>
        {
            AddMatch(m);
            // A game in the drafted lobby is a game of that series, and the
            // server needs to hear about it to open the next commanders.
            MaybeReportSeriesGame(m);
        });
        // The result as soon as the game says so, rather than when the replay
        // name turns up: the score does not need the filename, and waiting for it
        // left a finished game looking like it was still running.
        _watcher.MatchDecided += m => Dispatcher.Invoke(() => MaybeReportSeriesGame(m));
        _watcher.LobbySeen += id => Dispatcher.Invoke(() => OnLobbySeen(id));

        // lobby.dat carries the id the moment the lobby exists, which the log
        // only gets round to mentioning later.
        _lobbyFile = new System.Windows.Threading.DispatcherTimer
        { Interval = TimeSpan.FromSeconds(1) };
        _lobbyFile.Tick += (_, _) => PollLobbyFile();
        _lobbyFile.Start();

        Loaded += (_, _) => LoadHistory();
        Loaded += (_, _) => _ = CheckForUpdateAsync();
        Closed += (_, _) =>
        {
            _observerPoll?.Stop();
            _lobbyFile?.Stop();
            _watcher.Dispose(); _draft.Dispose(); _queue.Dispose();
        };
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

    // -------------------------------------------------------------- Title bar
    private void BtnMin_Click(object sender, RoutedEventArgs e)
        => WindowState = WindowState.Minimized;

    private void BtnMax_Click(object sender, RoutedEventArgs e)
        => WindowState = WindowState == WindowState.Maximized
            ? WindowState.Normal : WindowState.Maximized;

    private void BtnClose_Click(object sender, RoutedEventArgs e) => Close();

    /// <summary>
    /// Swap the maximise glyph, and pad for the taskbar.
    ///
    /// A maximised window with a custom chrome extends under the taskbar unless
    /// something compensates, which hides the bottom row of whatever is on
    /// screen.
    /// </summary>
    private void OnWindowStateChanged()
    {
        var max = WindowState == WindowState.Maximized;
        BtnMax.Content = max ? "" : "";
        BtnMax.ToolTip = Loc.T(max ? "window.restore" : "window.maximise");
        BorderThickness = new Thickness(max ? 7 : 0);
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
        // Read from `sender`, not from the sidebar box: two pickers share this
        // handler now, and hard-coding one meant changing the language in
        // Settings silently applied whatever the sidebar happened to show.
        if (!_langReady || sender is not ComboBox box) return;
        if (box.SelectedItem is not string lang || lang == Loc.Language) return;
        Loc.Remember(lang);
        LangHint.Text = Loc.T("sidebar.language_restart");
        LangHint.Visibility = Visibility.Visible;
        // Keep the other picker in step, or they disagree about what is set.
        foreach (var other in new[] { LangBox, SettingsLangBox })
            if (!ReferenceEquals(other, box) && other.ItemsSource is not null)
                other.SelectedItem = lang;
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
        // The last four digits, not all seventeen. The name above is what
        // identifies the account to a person; the tail is enough to tell two
        // Forts accounts on one PC apart, and the whole id belongs in a
        // screenshot as little as it belongs in a repository.
        AccountId.Text = steamId.Length > 4
            ? Loc.T("sidebar.steam_tail", steamId[^4..]) : steamId;
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
    /// <summary>
    /// Read every log this machine still has. Returns how many games were new.
    ///
    /// Two sources: the copies Forts leaves in <c>users/*/desyncs/</c>, which are
    /// the only record of older games, and the live <c>log.txt</c>, which holds
    /// the current session. The live one is included on purpose — the tail
    /// watcher covers it while running, but a client started after Forts was
    /// closed would otherwise never see the session that just happened.
    /// </summary>
    private int LoadHistory()
    {
        // What Forts copied itself survives the next game start: the logs in
        // users/&lt;id&gt;/desyncs/. The only route to historical games.
        var before = _matches.Count;
        var forts = FortsPaths.FindFortsDir();
        if (forts is null)
        {
            SetStatus(Loc.T("status.forts_missing"));
            return 0;
        }
        var users = Path.Combine(forts, "users");
        if (!Directory.Exists(users)) return 0;

        var files = new List<string>();
        foreach (var file in Directory.EnumerateFiles(users, "*.txt",
                     SearchOption.AllDirectories))
        {
            var name = Path.GetFileName(file);
            if (name.Contains("world-dump") || name.Contains("checksum")) continue;
            var isArchive = file.Contains("desyncs", StringComparison.OrdinalIgnoreCase)
                            && name.Contains("log", StringComparison.OrdinalIgnoreCase);
            var isLive = name.Equals("log.txt", StringComparison.OrdinalIgnoreCase);
            if (isArchive || isLive) files.Add(file);
        }
        foreach (var file in files)
        {
            try
            {
                foreach (var m in LogParser.ParseFile(file)) AddMatch(m, quiet: true);
            }
            catch (IOException) { /* one unreadable copy must not stop the rest */ }
        }
        RebuildSeries();
        return _matches.Count - before;
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
        var grouped = Series.Group(_matches, null, _watcher.CurrentAccount,
                                   _ladderLobbies.All, _draftedGames.All);
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
        // Deliberately NOT clearing first. LoadHistory reads the logs on disk,
        // so wiping the list threw away every match the live watcher had
        // recorded this session — including the one just played, which is the
        // one you would be looking at. The key set makes a second pass
        // idempotent, so this only ever adds what is new.
        //
        // And it says what it found: a rescan that silently adds nothing is
        // indistinguishable from a button that does nothing, which is exactly
        // how this one was read.
        var added = LoadHistory();
        SeriesSubtitle.Text = added > 0
            ? Loc.T("series.rescan_added", added, _matches.Count)
            : Loc.T("series.rescan_nothing", _matches.Count);
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

    /// <summary>
    /// Send this series to the ladder.
    ///
    /// The server decides whether it counts, and says why not — an unsanctioned
    /// lobby and an opponent who never opted in are both refusals, and the fix
    /// for each is completely different. So the answer is shown verbatim
    /// instead of being flattened into "failed".
    /// </summary>
    private async void BtnReport_Click(object sender, RoutedEventArgs e)
    {
        if (_selected is null) return;
        if (!await EnsureReadyAsync()) { ReportStatus.Text = DraftError.Text; return; }

        var sides = new Dictionary<string, int>();
        foreach (var p in _selected.Matches.SelectMany(m => m.Players.Values))
            if (!string.IsNullOrEmpty(p.SteamId) && p.Side > 0)
                sides[p.SteamId] = p.Side;
        if (sides.Values.Distinct().Count() != 2)
        {
            ReportStatus.Text = Loc.T("series.report_not_two_sides");
            return;
        }

        var (wins, _) = _selected.Score();
        var low = sides.Values.Min();
        var lobby = _selected.Matches.Select(m => m.LobbyId)
                                     .LastOrDefault(x => x is not null);
        var (found, _) = _selected.ReplayFiles();

        BtnReport.IsEnabled = false;
        ReportStatus.Text = Loc.T("series.reporting");
        var res = await _login.ReportSeriesAsync(
            sides, _selected.Matches.Count, wins.GetValueOrDefault(low),
            _selected.PlayedAt, lobby, found.Select(f => f.Name));
        BtnReport.IsEnabled = true;

        ReportStatus.Text = res is null
            ? Loc.T("series.report_failed", _api.LastError ?? "?")
            : res.Rated
                ? Loc.T("series.report_rated")
                : Loc.T("series.report_unrated", string.Join(" · ", res.Reasons));
        // Offered whatever the answer was: a series that counted can be wrong
        // too, and one that did not is exactly what somebody should look at.
        _lastReportedId = res?.Id;
        BtnFlag.Visibility = res is null
            ? Visibility.Collapsed : Visibility.Visible;
    }

    /// <summary>The id of the last series this client reported, so it can be
    /// handed to a human if the answer was wrong.</summary>
    private string? _lastReportedId;

    /// <summary>
    /// Ask a person to look at this series.
    ///
    /// The reason an unrated series is kept rather than dropped: "it did not
    /// count" is sometimes the software being wrong, and the player it happened
    /// to is the only one who knows. Without this they would have to find an
    /// admin on Discord and describe a match from memory.
    /// </summary>
    private async void BtnFlag_Click(object sender, RoutedEventArgs e)
    {
        if (_lastReportedId is not { Length: > 0 } id)
        {
            ReportStatus.Text = Loc.T("series.flag_needs_report");
            return;
        }
        BtnFlag.IsEnabled = false;
        var note = _selected is null ? "" : Loc.T("series.flag_note",
            string.Join(" vs ", _selected.Sides().Keys
                .Select(k => string.Join(", ", _selected.Names(_identity, k)))),
            _selected.Matches.Count);
        ReportStatus.Text = await _login.FlagResultAsync(id, note)
            ? Loc.T("series.flagged") : Loc.T("series.flag_failed",
                                              _api.LastError ?? "?");
        BtnFlag.IsEnabled = true;
    }

    private void BtnCollect_Click(object sender, RoutedEventArgs e)
    {
        if (_selected is null) return;
        var stamp = _selected.PlayedAt.ToString("yyyyMMdd_HHmm");
        var dir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            $"UFER_{stamp}");
        try
        {
            var (n, bytes) = _selected.Collect(dir, _identity);
            ReplayInfo.Text = Loc.T("series.collected", n, bytes / 1e6, dir);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Collecting the same series twice is normal — the report is
            // rewritten and existing replays are left alone. What is not normal
            // is a locked file or a read-only desktop, and that used to throw
            // out of a click handler with nothing said.
            ReplayInfo.Text = Loc.T("series.collect_failed", ex.Message);
            return;
        }
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
        Set(ViewSettings, HeaderSettings, NavSettings, which == "settings");
    }

    private void ReloadTable() => _ = ReloadTableAsync();

    /// <summary>
    /// Server first, local file second. Everyone has to read the same ranking,
    /// or it is not one — but the local file keeps the view working offline and
    /// against no server at all.
    /// </summary>
    private async Task ReloadTableAsync()
    {
        if (_api.LoggedIn)
        {
            var me = await _login.MeAsync();
            if (await _table.LoadFromServerAsync(_api, me?.Ufer_Name))
            {
                ShowTable();
                return;
            }
        }
        _table.Reload(_watcher.CurrentAccount);
        ShowTable();
    }

    private void ShowTable()
    {
        TableList.ItemsSource = _table.Search(TableSearch.Text);
        TableEmpty.Visibility = _table.Loaded ? Visibility.Collapsed : Visibility.Visible;

        if (!_table.Loaded)
        {
            TableSubtitle.Text = _api.LoggedIn
                ? Loc.T("table.missing_at", _table.Path)
                : Loc.T("table.sign_in_for_shared");
            MeLine.Text = "";
            return;
        }
        if (_table.FromServer)
        {
            TableSubtitle.Text = Loc.T("table.from_server", _table.Players.Count);
            MeLine.Text = _table.Me is { } m
                ? Loc.T("table.you", m.Rank, m.UferText, m.OpenText)
                : Loc.T("table.you_unassigned");
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

        // Leaving stays available for as long as the series is live — not
        // only during the picking. A series now holds both players out of the
        // queue, so somebody whose opponent walked off has to have a way out;
        // what changed is that it costs a cooldown, and the button says so.
        BtnCancelDraft.Visibility = running && !s!.Settled
            ? Visibility.Visible : Visibility.Collapsed;

        // Offered exactly when the series is decided and still open. Both sides
        // see it, because both are equally stuck until one of them presses it.
        BtnFinishSeries.Visibility = running && s!.Can_Conclude
            ? Visibility.Visible : Visibility.Collapsed;

        if (!running) return;
        RenderVersus(s!);
        RenderBoard(s!);
        RenderPlan(s!);
        RenderHandoff(s!);
        RenderVoid(s!);
    }

    // ----------------------------------------------------------- The handoff
    /// <summary>
    /// What to do once the draft is decided.
    ///
    /// Without this the draft ended in a plan and no game: two people with a
    /// map list and no way into a lobby. One side hosts, the log supplies the
    /// lobby id, and the other side gets a Steam join link.
    /// </summary>
    private void RenderHandoff(DraftStateDto s)
    {
        if (!s.Done || s.Cancelled)
        {
            HandoffBar.Visibility = Visibility.Collapsed;
            return;
        }
        HandoffBar.Visibility = Visibility.Visible;

        // Remembered on both sides, not just the host's: the guest groups the
        // same matches into the same series and needs the same boundary.
        if (s.Lobby_Id is { Length: > 0 } lid && ulong.TryParse(lid, out var lidNum)
            && _ladderLobbies.Add(lidNum))
            RebuildSeries();
        // Announce it so it can be watched. Nothing published a live match
        // before, so the list everyone polled was always empty.
        _ = PublishSeriesAsync();

        var haveLobby = s.Lobby_Id is { Length: > 0 };
        var claimed = s.Lobby_Host is { Length: > 0 };
        var theirs = claimed && !s.YouHostLobby;

        // Offered to whoever has not been settled as the other side's job.
        BtnHostLobby.Visibility = !haveLobby && !theirs
            ? Visibility.Visible : Visibility.Collapsed;
        BtnHostLobby.IsEnabled = !_awaitingLobby;
        BtnJoinLobby.Visibility = haveLobby && theirs
            ? Visibility.Visible : Visibility.Collapsed;
        // Starting Forts is the host's job; the other side is launched into the
        // lobby by the join link.
        BtnLaunchForts.Visibility = theirs && !haveLobby
            ? Visibility.Collapsed : Visibility.Visible;

        var oppName = s.Seats.TryGetValue(s.Your_Side == "A" ? "B" : "A",
                                          out var o) ? o : Loc.T("draft.them");
        // The password, where whoever needs it can read and copy it.
        var pw = s.Lobby_Password;
        LobbyPassword.Text = pw is { Length: > 0 }
            ? Loc.T("handoff.password", pw) : "";
        LobbyPassword.Visibility = pw is { Length: > 0 } && haveLobby
            ? Visibility.Visible : Visibility.Collapsed;
        BtnCopyPassword.Visibility = LobbyPassword.Visibility;

        if (s.Aborted)
        {
            HandoffTitle.Text = s.AbortedByYou
                ? Loc.T("handoff.aborted_you") : Loc.T("handoff.aborted_them");
            HandoffTitle.Foreground = (Brush)FindResource("Loss");
            HandoffSub.Text = s.Aborted_Reason ?? "";
            BtnHostLobby.Visibility = Visibility.Collapsed;
            BtnJoinLobby.Visibility = Visibility.Collapsed;
            BtnLaunchForts.Visibility = Visibility.Collapsed;
            return;
        }

        if (haveLobby)
        {
            // Whether *this* machine is in that lobby, which the guest could not
            // tell before: only the host knew, because only the host had asked.
            // Both have the id from the server and both have lobby.dat.
            var here = LobbySettings.CurrentLobby() is ulong cur
                       && cur.ToString() == s.Lobby_Id;
            HandoffTitle.Text = here ? Loc.T("handoff.in_lobby")
                : s.YouHostLobby ? Loc.T("handoff.you_host")
                : Loc.T("handoff.ready_to_join");
            HandoffTitle.Foreground = (Brush)FindResource(here ? "Win" : "TextHi");
            HandoffSub.Text = here
                ? Loc.T("handoff.in_lobby_sub", s.Plan.Count > 0
                        ? s.Plan[Math.Min(s.Revealed_Through, s.Plan.Count) - 1]
                            .Map ?? "?" : "?")
                : Loc.T("handoff.lobby_is", s.Lobby_Id!);
            // Nothing to join once you are in it.
            if (here) BtnJoinLobby.Visibility = Visibility.Collapsed;
            // And tell the server, which stops the join clock. Only this client
            // knows: the host sees a player connect but not which ladder account
            // it is. Sent once per series, not once per poll.
            if (here && s.Handoff.Phase == "guest" && _readyReported != s.Id)
            {
                _readyReported = s.Id;
                _ = _draft.NoteReadyAsync();
            }
        }
        else if (theirs)
        {
            // Explicitly "not yet": starting Forts before the lobby exists is
            // the mistake this replaces, and the guest had no way to tell.
            HandoffTitle.Text = Loc.T("handoff.waiting_for_lobby", oppName);
            HandoffTitle.Foreground = (Brush)FindResource("Warn");
            HandoffSub.Text = Loc.T("handoff.waiting_for_lobby_sub");
        }
        else if (_awaitingLobby)
        {
            HandoffTitle.Text = Loc.T("handoff.watching");
            HandoffSub.Text = Loc.T("handoff.watching_sub");
        }
        else
        {
            HandoffTitle.Text = Loc.T("handoff.decided");
            HandoffSub.Text = Loc.T("handoff.decided_sub");
        }

        RenderHandoffClock(s, oppName);
        RenderRestartWarning(s, oppName);
    }

    /// <summary>
    /// How long is left, and the two buttons that change it.
    ///
    /// A deadline the server keeps but nobody can see is only useful for
    /// punishing somebody. And a lobby that will not open is usually a port or a
    /// Steam problem rather than a refusal, so more time is the right answer —
    /// asked of the opponent, because it comes out of their evening.
    /// </summary>
    private void RenderHandoffClock(DraftStateDto s, string oppName)
    {
        var h = s.Handoff;
        var yours = s.ClockOnYou;

        if (h.Expired && h.Running)
        {
            HandoffClock.Text = yours
                ? Loc.T("handoff.clock_expired_you")
                : Loc.T("handoff.clock_expired_them", oppName);
            HandoffClock.Foreground = (Brush)FindResource("Loss");
            HandoffClock.Visibility = Visibility.Visible;
            // Said only to the side that is waiting: it is their cooldown that
            // has been waived, not the late side's.
            if (!yours) HandoffSub.Text = Loc.T("handoff.clock_expired_sub");
        }
        else if (h.Running)
        {
            var left = TimeSpan.FromSeconds(h.SecondsLeftNow).ToString(@"m\:ss");
            HandoffClock.Text = yours
                ? Loc.T(h.Phase == "host" ? "handoff.clock_host"
                                          : "handoff.clock_guest", left)
                : Loc.T("handoff.clock_waiting", oppName, left);
            HandoffClock.Foreground = (Brush)FindResource(
                h.SecondsLeftNow <= 30 ? "Loss" : "Warn");
            HandoffClock.Visibility = Visibility.Visible;
        }
        else HandoffClock.Visibility = Visibility.Collapsed;

        // Whoever asked cannot grant it to themselves, so exactly one of these
        // two is ever offered to a given client.
        var asked = s.Extension_Asked_By;
        var theyAsked = asked is { Length: > 0 } && asked != s.Your_Side;
        var youAsked = asked is { Length: > 0 } && asked == s.Your_Side;

        BtnGrantTime.Content = Loc.T("handoff.grant_time", oppName);
        BtnGrantTime.Visibility = theyAsked && h.Running
            ? Visibility.Visible : Visibility.Collapsed;
        BtnAskTime.Visibility = !youAsked && !theyAsked && h.Running
            ? Visibility.Visible : Visibility.Collapsed;
        if (youAsked) HandoffSub.Text = Loc.T("handoff.asked_time", oppName);
        if (theyAsked) HandoffSub.Text = Loc.T("handoff.they_asked_time", oppName);
    }

    /// <summary>
    /// Forts has not been restarted, so it is not using the ladder's settings.
    ///
    /// The commonest cause of "my opponent never sent me the password": the
    /// settings file is read while the game starts and at no other time, so a
    /// running Forts has never seen it. The host is told what to do, and the
    /// guest is told why nothing is arriving — they cannot see the other
    /// machine.
    /// </summary>
    private void RenderRestartWarning(DraftStateDto s, string oppName)
    {
        var mine = s.YouHostLobby && _restartPending;
        var theirs = !s.YouHostLobby && s.Host_Restart_Pending;
        RestartWarning.Text = mine ? ErrorCodes.Text(ErrorCodes.GameNotRestarted)
                                     + "  " + Loc.T("handoff.restart_needed")
                            : theirs ? Loc.T("handoff.restart_them", oppName)
                            : "";
        RestartWarning.Visibility = mine || theirs
            ? Visibility.Visible : Visibility.Collapsed;
        BtnCloseForts.Visibility = mine ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>Set when lobby settings were written into a running Forts.</summary>
    private bool _restartPending;

    /// <summary>
    /// Whether Forts is up right now.
    ///
    /// By process rather than by the log: the log says a game *was* running, and
    /// what matters here is whether the settings file has been read since it was
    /// written, which only a fresh start does.
    /// </summary>
    private static bool FortsIsRunning()
    {
        try
        {
            return System.Diagnostics.Process.GetProcessesByName("Forts").Length > 0;
        }
        catch (Exception) { return false; }
    }

    private async void BtnAskTime_Click(object sender, RoutedEventArgs e)
    {
        if (!await _draft.AskExtensionAsync()) ShowDraftError(_draft.LastError ?? "?");
    }

    private async void BtnGrantTime_Click(object sender, RoutedEventArgs e)
    {
        if (!await _draft.GrantExtensionAsync()) ShowDraftError(_draft.LastError ?? "?");
    }

    /// <summary>
    /// Close Forts so it can be started again and read the settings.
    ///
    /// Deliberately only closing, not restarting: the game is launched through
    /// Steam and starting it behind someone's back is not this program's
    /// business. Confirmed first, because anything unsaved in it is lost.
    /// </summary>
    private void BtnCloseForts_Click(object sender, RoutedEventArgs e)
    {
        if (MessageBox.Show(this, Loc.T("handoff.close_forts_ask"),
                            Loc.T("handoff.close_forts"),
                            MessageBoxButton.OKCancel,
                            MessageBoxImage.Warning) != MessageBoxResult.OK)
            return;
        var closed = 0;
        foreach (var p in System.Diagnostics.Process.GetProcessesByName("Forts"))
        {
            try { p.CloseMainWindow(); if (!p.WaitForExit(4000)) p.Kill(); closed++; }
            catch (Exception) { /* counted as not closed */ }
        }
        if (closed == 0)
            ShowDraftError(ErrorCodes.Text(ErrorCodes.GameNotRestarted,
                                           Loc.T("handoff.close_forts_failed")));
    }

    /// <summary>Set while waiting for a lobby to appear.</summary>
    private bool _awaitingLobby;

    /// <summary>Draft whose "I am in the lobby" has already been sent, so the
    /// poll does not send it once a second.</summary>
    private string? _readyReported;

    /// <summary>The lobby id that was already there when we started watching, so
    /// a lobby from an earlier session is not claimed as this series.</summary>
    private ulong? _lobbyBefore;

    /// <summary>Password written into the host's lobby settings, shown so it can
    /// be passed on.</summary>
    private string? _lobbyPassword;
    private int _lobbySize = 5;
    private string _lobbyName = "";

    /// <summary>
    /// What was played differently from what was agreed.
    ///
    /// Two kinds. The lobby settings are read back out of the file, because the
    /// game rewrites it when the host changes something on the host screen — so
    /// looking afterwards is the only way to know what the series was actually
    /// played under. And each finished game is compared against the drafted
    /// plan: the wrong map or the wrong commander is exactly what the draft
    /// exists to pin down.
    ///
    /// Reported, never enforced. The client cannot stop any of it, and pretending
    /// otherwise would be worse than saying plainly what happened.
    /// </summary>
    private readonly List<string> _seriesWarnings = new();

    private void CheckLobbySettings()
    {
        if (_lobbyPassword is null) return;      // we never wrote them
        foreach (var d in LobbySettings.Deviations(_lobbyName, _lobbySize,
                                                  _lobbyPassword))
        {
            var line = Loc.T("series.lobby_changed", d);
            if (!_seriesWarnings.Contains(line)) _seriesWarnings.Add(line);
        }
    }

    /// <summary>Compare one finished game against the game it was supposed to
    /// be.</summary>
    private void CheckAgainstPlan(MatchRecord m, int game)
    {
        var s = _draft.State;
        if (s is null || game < 1 || game > s.Plan.Count) return;
        var planned = s.Plan[game - 1];

        if (planned.Map is { Length: > 0 } map && m.Map is { Length: > 0 } played
            && !played.Equals(map, StringComparison.OrdinalIgnoreCase))
            Warn(Loc.T("series.plan_mismatch",
                       $"{Loc.T("draft.game_of", game, s.Plan.Count)}: "
                       + $"{map} → {played}"));

        // Only own side: the opponent's planned commander is withheld until the
        // game has been reported, so there is nothing to compare it against yet.
        var mySide = s.Your_Side == "B" ? 2 : 1;
        var mine = s.Your_Side == "B" ? planned.Commander_B : planned.Commander_A;
        if (mine is { Length: > 0 } want
            && m.Commanders.TryGetValue(mySide, out var got)
            && got != want)
            Warn(Loc.T("series.plan_mismatch",
                       $"{Loc.T("draft.game_of", game, s.Plan.Count)}: "
                       + $"{CommanderNames.Display(want)} → "
                       + CommanderNames.Display(got)));
    }

    private void Warn(string line)
    {
        if (_seriesWarnings.Contains(line)) return;
        _seriesWarnings.Add(line);
        SeriesWarnings.Text = string.Join(Environment.NewLine, _seriesWarnings);
        SeriesWarnings.Visibility = Visibility.Visible;
    }

    /// <summary>
    /// Write the league settings the host screen would otherwise be clicked
    /// into: the exact series size, sides and forts locked, a password, and a
    /// name the opponent recognises.
    ///
    /// Only works before Forts starts, because the game reads that file at
    /// startup — so this runs when the host role is claimed, not when Forts is
    /// launched. The map is not in that file and has to be picked by hand; the
    /// panel says which one rather than pretending it was set.
    /// </summary>
    private void WriteLobbySettings()
    {
        var s = _draft.State;
        if (s is null) return;
        var a = s.Seats.TryGetValue("A", out var na) ? na : "A";
        var b = s.Seats.TryGetValue("B", out var nb) ? nb : "B";
        // Players plus room to watch. Forts counts a spectator as a client, so a
        // 1v1 with two seats has nowhere for one to go — which is why every
        // observer request came back "no room". Nine is the game's hard limit.
        _lobbySize = Math.Min(9, Math.Max(2, s.Seats.Count) + 3);
        _lobbyName = $"Ladder: {a} vs {b}";
        var res = LobbySettings.Apply(_lobbyName, _lobbySize);
        _lobbyPassword = res.Password;
        // Written, but not necessarily *in effect*. Forts reads multiplayer.lua
        // while starting and at no other time, so a running game has never seen
        // this file — and the lobby it opens has the old password, or none. This
        // is the whole reason a guest sat waiting for a password that did not
        // exist.
        _restartPending = res.Ok && FortsIsRunning();
        HandoffSub.Text = !res.Ok
            ? Loc.T("handoff.settings_failed", res.Message)
            : _restartPending
                ? ErrorCodes.Text(ErrorCodes.GameNotRestarted)
                : Loc.T("handoff.settings_written", res.Password ?? "-");
    }

    private void BtnLaunchForts_Click(object sender, RoutedEventArgs e)
    {
        // If a lobby is already open and this is not the host, starting the game
        // on its own drops them into the main menu — which is what happened, and
        // reads as the button not working. The join link starts Forts *and*
        // joins, so it is the better answer whenever there is one.
        var s = _draft.State;
        if (s is not null && !s.YouHostLobby && s.Lobby_Id is { Length: > 0 })
        {
            BtnJoinLobby_Click(sender, e);
            return;
        }
        try
        {
            // Through Steam rather than the exe: Forts needs the Steam client
            // for multiplayer anyway, and this starts it if it is not running.
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo("steam://run/410900")
                { UseShellExecute = true });
        }
        catch (Exception ex) { ShowDraftError(ex.Message); }
    }

    private async void BtnHostLobby_Click(object sender, RoutedEventArgs e)
    {
        // Claimed on the server first: both clients used to offer this until one
        // pressed it, which is two people about to open the same match.
        if (!await _draft.ClaimHostAsync())
        {
            ShowDraftError(_draft.LastError ?? "?");
            return;
        }
        _lobbyBefore = LobbySettings.CurrentLobby();
        _awaitingLobby = true;
        WriteLobbySettings();
        RefreshDraft();
    }

    /// <summary>
    /// A lobby appeared in the log. Claim it for the draft if we are waiting.
    ///
    /// Only when asked for: the log reports every lobby, including one joined
    /// for a completely unrelated game, and claiming that would sanction a
    /// match nobody drafted.
    /// </summary>
    private async void OnLobbySeen(ulong lobbyId)
    {
        if (!_awaitingLobby || _draft.State is null || !_draft.State.Done) return;
        if (lobbyId == _lobbyBefore) return;   // the one from before we watched
        _awaitingLobby = false;
        // With the password: the guest cannot get in without it, and the Steam
        // link has nowhere to put it.
        // The pending restart travels with the lobby: the guest cannot see this
        // machine, and without being told they conclude the host never sent the
        // password rather than that the game never read it.
        if (!await _draft.SetLobbyAsync(lobbyId, _lobbyPassword, _restartPending))
            ShowDraftError(_draft.LastError ?? "?");
        RefreshDraft();
    }

    /// <summary>
    /// Second source for the lobby id: lobby.dat, whose first eight bytes are it.
    ///
    /// The game log mentions the lobby eventually; this file has it as soon as
    /// the lobby exists, and waiting for the log was the slowest part of the
    /// handoff. Whichever arrives first wins and the other becomes a no-op.
    /// </summary>
    private void PollLobbyFile()
    {
        var s = _draft.State;
        if (_awaitingLobby)
        {
            if (LobbySettings.CurrentLobby() is not ulong id) return;
            if (id == _lobbyBefore) return;
            OnLobbySeen(id);
            return;
        }
        // Not only while waiting for one: the guest needs to notice when it has
        // arrived in the series lobby, and that is the same file.
        if (s is null || !s.Done || s.Lobby_Id is not { Length: > 0 }) return;
        var now = LobbySettings.CurrentLobby()?.ToString();
        if (now == _lastSeenLobby) return;
        _lastSeenLobby = now;
        RenderHandoff(s);
    }

    /// <summary>Last lobby this machine was seen in, so the handoff panel is
    /// only redrawn when that actually changes.</summary>
    private string? _lastSeenLobby;

    private void BtnCopyPassword_Click(object sender, RoutedEventArgs e)
    {
        if (_draft.State?.Lobby_Password is not { Length: > 0 } pw) return;
        try
        {
            Clipboard.SetText(pw);
            BtnCopyPassword.Content = Loc.T("series.copied");
        }
        catch (System.Runtime.InteropServices.ExternalException)
        {
            // Another program can hold the clipboard; it is on screen either way.
        }
    }

    private void BtnJoinLobby_Click(object sender, RoutedEventArgs e)
    {
        var s = _draft.State;
        if (s?.Lobby_Id is not { Length: > 0 } lobby) return;
        var host = s.Lobby_Host is not null && s.Seats.TryGetValue(s.Lobby_Host, out var h)
            ? h : "";
        // Steam's own join URL, with the lobby owner's account in the third
        // field. Passing 0 there and letting Steam work it out did not join —
        // the server knows who claimed the host role, so the id comes from
        // there.
        var owner = s.Lobby_Host_Steam;
        if (string.IsNullOrEmpty(owner))
        {
            HandoffSub.Text = Loc.T("handoff.no_host_id");
            return;
        }
        try
        {
            System.Diagnostics.Process.Start(
                new System.Diagnostics.ProcessStartInfo(
                    $"steam://joinlobby/410900/{lobby}/{owner}")
                { UseShellExecute = true });
            HandoffSub.Text = Loc.T("handoff.joining", host);
        }
        catch (Exception ex) { ShowDraftError(ex.Message); }
    }

    private async void BtnCancelDraft_Click(object sender, RoutedEventArgs e)
    {
        // Asked once, because the answer is a cooldown and the other player
        // loses the whole match. Only for a queue match: two people who
        // arranged a game between themselves may also call it off.
        if (_draft.State?.Leaving_Penalised == true)
        {
            var answer = MessageBox.Show(
                this, Loc.T("draft.leave_penalty_ask"),
                Loc.T("draft.leave_penalty_title"),
                MessageBoxButton.OKCancel, MessageBoxImage.Warning);
            if (answer != MessageBoxResult.OK) return;
        }
        if (!await _draft.CancelAsync()) ShowDraftError(_draft.LastError ?? "?");
        RefreshDraft();
    }

    /// <summary>
    /// Close out a decided series.
    ///
    /// The one act that releases both players: until it happens they are bound
    /// to a board that is over, and neither may look for another match.
    /// </summary>
    private async void BtnFinishSeries_Click(object sender, RoutedEventArgs e)
    {
        BtnFinishSeries.IsEnabled = false;
        var ok = await _draft.ConcludeAsync();
        BtnFinishSeries.IsEnabled = true;
        if (!ok) { ShowDraftError(_draft.LastError ?? "?"); return; }
        RefreshDraft();
        // Straight to where the next match is found. Staying on a finished
        // board is the state this button exists to leave.
        ShowView("queue");
        _ = RefreshAccountAsync();
    }

    /// <summary>
    /// Just the countdown, on the display ticker.
    ///
    /// Kept separate from <see cref="RefreshDraft"/> because that one rebuilds
    /// the tile buttons, and rebuilding them five times a second meant a click
    /// starting on a button ended on its replacement — three presses to ban a
    /// map.
    /// </summary>
    private void RefreshDraftClock()
    {
        var s = _draft.State;
        if (s is null) return;

        // The handoff countdown runs *after* the draft is done, so it is handled
        // before the early return below. Only text is touched — rebuilding a
        // control on the display ticker is what made the map tiles unclickable.
        if (s.Handoff.Running && !s.Handoff.Expired && HandoffClock.Visibility
                == Visibility.Visible)
        {
            var oppName = s.Seats.TryGetValue(s.Your_Side == "A" ? "B" : "A",
                                              out var o) ? o : Loc.T("draft.them");
            var rest = TimeSpan.FromSeconds(s.Handoff.SecondsLeftNow)
                               .ToString(@"m\:ss");
            HandoffClock.Text = s.ClockOnYou
                ? Loc.T(s.Handoff.Phase == "host" ? "handoff.clock_host"
                                                  : "handoff.clock_guest", rest)
                : Loc.T("handoff.clock_waiting", oppName, rest);
        }

        if (s.Done || s.Cancelled || !s.Full) return;
        var left = s.SecondsLeftNow;
        TimerBar.Width = Math.Max(0, Math.Min(200, 200 * left / 30.0));
        TimerBar.Background = (Brush)FindResource(left <= 5 ? "Loss" : "Accent");
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

        // Which game of the series this step belongs to. "Step 7 of 12" says
        // nothing to a player; "Game 2 of 3" is what they are thinking in.
        var step = Loc.T("draft.progress",
                         Math.Min(s.Step_Index + 1, s.Step_Total), s.Step_Total);
        DraftProgress.Text = s.Game is int g
            ? Loc.T("draft.game_of", g, s.Plan.Count) + "  .  " + step : step;
        LockedLabel.Text = s.Your_Pending_Pick is { Length: > 0 } p
            ? Loc.T("draft.you_locked", s.Display(p)) : "";

        // Said before anything else: a board that still shows whose turn it is
        // while the other side has walked away is the worst of the three states.
        if (s.Cancelled)
        {
            TurnBanner.Text = Loc.T("draft.cancelled", s.Cancelled_By ?? "?");
            TurnBanner.Foreground = (Brush)FindResource("Warn");
            TurnSub.Text = Loc.T("draft.cancelled_sub");
            TimerBar.Width = 0;
            return;
        }
        if (s.Done)
        {
            var w = s.Wins.GetValueOrDefault(you);
            var l = s.Wins.GetValueOrDefault(opp);
            TurnBanner.Text = s.Series_Over
                ? Loc.T("draft.series_over", w, l)
                : w + l > 0 ? Loc.T("draft.series_score", w, l)
                            : Loc.T("draft.finished");
            TurnBanner.Foreground = (Brush)FindResource("Win");
            // Say why later games look empty, rather than leaving it as a
            // puzzle: the opponent's commander is withheld on purpose.
            TurnSub.Text = s.Revealed_Through <= s.Plan.Count
                ? Loc.T("draft.hidden_later") : Loc.T("draft.finished_hint");
            TimerBar.Width = 0;
            DraftProgress.Text = Loc.T("draft.game_of",
                                       Math.Min(s.Revealed_Through, s.Plan.Count),
                                       s.Plan.Count);
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
        var left = s.SecondsLeftNow;
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

        // Whose pick a commander was has to be on the tile. Both sides ended up
        // marked "chosen" in the same green after the reveal, so the opponent's
        // commander read as your own — which is the one thing this screen must
        // never be ambiguous about.
        var you = s.Your_Side ?? "A";
        var mine = new HashSet<string>();
        var theirs = new HashSet<string>();
        foreach (var g in s.Plan)
        {
            var a = you == "A" ? g.Commander_A : g.Commander_B;
            var b = you == "A" ? g.Commander_B : g.Commander_A;
            if (a is not null) mine.Add(a);
            if (b is not null) theirs.Add(b);
        }
        if (s.Your_Pending_Pick is { Length: > 0 } pending) mine.Add(pending);
        var oppName = s.Seats.TryGetValue(you == "A" ? "B" : "A", out var on2)
            ? on2 : Loc.T("draft.them");

        CommanderTiles.ItemsSource = CommanderNames
            .InGameOrder(s.Commander_Pool).Select(c =>
        {
            var banned = s.Banned_Commanders.Contains(c);
            var isMine = mine.Contains(c);
            var isTheirs = theirs.Contains(c);
            var note = s.Your_Pending_Pick == c ? Loc.T("draft.locked_in")
                     : isMine ? Loc.T("draft.picked_by_you")
                     : isTheirs ? Loc.T("draft.picked_by_them", oppName)
                     : null;
            return Tile(s.Display(c), c, banned, isMine, note,
                        !s.IsMapStep && s.YourTurn && s.Options.Contains(c),
                        s.IsBanStep, theirs: isTheirs);
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
            // Which one is being played right now, said in the row rather than
            // left to be worked out from which commanders are still hidden.
            Tag = g.Game == s.Revealed_Through && s.Done && !s.Series_Over
                    ? Loc.T("draft.now_playing")
                : s.Games_Played.Contains(g.Game) ? Loc.T("draft.game_done")
                : g.Decider ? Loc.T("draft.decider")
                : g.Map_Picked_By is null ? "" : Loc.T("draft.picked_by", g.Map_Picked_By),
            Fill = (Brush)FindResource(
                g.Game == s.Revealed_Through && s.Done && !s.Series_Over
                    ? "BgHover" : "BgPanel"),
            Thickness = g.Game == s.Revealed_Through && s.Done && !s.Series_Over
                ? new Thickness(2) : new Thickness(0, 0, 0, 2),
            // Own side first and both named: "A vs B" left the reader to work
            // out which of the two was theirs.
            Commanders = g.Commander_A is null && g.Commander_B is null
                ? Loc.T("draft.commanders_open")
                : Loc.T("draft.you_vs_them",
                        s.Display((s.Your_Side == "B" ? g.Commander_B
                                                      : g.Commander_A) ?? "?"),
                        s.Display((s.Your_Side == "B" ? g.Commander_A
                                                      : g.Commander_B) ?? "?")),
            Accent = (Brush)FindResource(
                g.Game == s.Revealed_Through && s.Done && !s.Series_Over
                    ? "Accent" : g.Decider ? "Warn" : "Stroke"),
        }).ToList();
    }

    /// <summary>
    /// One tile. Banned, picked, playable and idle have to be distinguishable
    /// at a glance — a draft step is decided in seconds.
    /// </summary>
    private object Tile(string label, string id, bool banned, bool picked,
                        string? note, bool enabled, bool isBanStep,
                        bool theirs = false)
    {
        // Four states, four looks. `theirs` exists because the opponent's
        // revealed pick used to be the same green as your own, which made it
        // read as yours.
        var bg = banned ? "#3A1F22" : picked ? "#1B3327"
               : theirs ? "#2B2233"
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
                : banned ? "Loss" : picked ? "Win"
                : theirs ? "Warn" : "Stroke"),
            Thickness = new Thickness(enabled ? 2 : 1),
            Fore = (Brush)FindResource(banned || (!enabled && !picked && !theirs)
                ? "TextLow" : "TextHi"),
            NoteBrush = (Brush)FindResource(banned ? "Loss"
                : picked ? "Win" : theirs ? "Warn"
                : enabled ? "Accent" : "TextLow"),
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

        // Both sides, own side first. Naming only the opponent read as
        // "vs Enemy" with nothing to say who that was against — and against the
        // built-in AI "Enemy" is literally the name in the log, so the line
        // said nothing at all.
        var mine = string.Join(", ", s.Names(ids, own));
        var theirs = other == 0 ? "" : string.Join(", ", s.Names(ids, other));
        if (string.IsNullOrWhiteSpace(mine)) mine = Loc.T("series.you");
        Title = string.IsNullOrWhiteSpace(theirs)
            ? mine : $"{mine}  vs  {theirs}";

        // When, then how many games, then the maps. The date was there before
        // but only in the detail pane, which needs a click to reach.
        var when = s.PlayedAt.Date == DateTime.Today
            ? Loc.T("series.today", s.PlayedAt.ToString("HH:mm"))
            : s.PlayedAt.Date == DateTime.Today.AddDays(-1)
                ? Loc.T("series.yesterday", s.PlayedAt.ToString("HH:mm"))
                : s.PlayedAt.ToString("ddd d MMM, HH:mm");
        Subtitle = $"{when}  ·  " + Loc.T("series.count", s.Matches.Count) + "  ·  " +
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
        // The game's own name for it, not the mod folder: the log says
        // "commander-da-overclocker" and the game calls that Overdrive, so
        // stripping the prefix printed something no player recognises.
        Commanders = string.Join("   ", m.Commanders
            .OrderBy(kv => kv.Key)
            .Select(kv => $"S{kv.Key}: {CommanderNames.Display(kv.Value)}"));
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
