using System.Windows;
using FortsLadder.Core;
using Draw = System.Drawing;
using Forms = System.Windows.Forms;

namespace FortsLadder;

/// <summary>
/// Running with the window closed.
///
/// The reason is a real one and it came from a playtest: somebody forgets to
/// start this before playing, and the evening's matches are gone. The log itself
/// is no help afterwards — Forts clears it when the game starts — so a match not
/// watched while it happened is a match that never happened as far as the ladder
/// is concerned.
///
/// So the window closing does not have to mean the program stopping. Three
/// separate choices, because they are three different amounts of trust: hide
/// instead of quit, keep reading the log while hidden, and start with Windows.
/// All off until somebody turns them on — a tool that installs itself into
/// startup and watches a game in the background because it was run once is not
/// something anybody asked for.
///
/// The icon is drawn here rather than shipped as a file, which means it can say
/// what the program is doing: lit while tracking, hollow while not. A tray icon
/// that looks the same whether or not it is working is decoration.
/// </summary>
public partial class MainWindow
{
    private Forms.NotifyIcon? _tray;
    private readonly Prefs _prefs = new();
    private bool _reallyClosing;

    /// <summary>Set when the window is hidden rather than open, so the log
    /// watcher knows whether anybody is looking.</summary>
    private bool _hidden;

    private void InitTray()
    {

        _tray = new Forms.NotifyIcon
        {
            Text = "Forts Ladder",
            Visible = true,
            Icon = MakeIcon(tracking: true),
        };
        _tray.DoubleClick += (_, _) => ShowFromTray();

        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add(Loc.T("tray.open"), null, (_, _) => ShowFromTray());
        menu.Items.Add(new Forms.ToolStripSeparator());

        _trackItem = new Forms.ToolStripMenuItem(
            Loc.T("tray.track"), null, (_, _) => ToggleTracking())
        { Checked = _prefs.Get(Prefs.TrackInBackground) };
        menu.Items.Add(_trackItem);

        _startupItem = new Forms.ToolStripMenuItem(
            Loc.T("tray.startup"), null, (_, _) => ToggleStartup())
        { Checked = Autostart.Enabled };
        menu.Items.Add(_startupItem);

        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add(Loc.T("tray.quit"), null, (_, _) => QuitForReal());
        // In the application's colours. A ContextMenuStrip left alone is light
        // grey with a blue highlight, and once the window is closed this menu is
        // the whole interface.
        DarkMenu.Apply(menu);
        _tray.ContextMenuStrip = menu;

        // An autostart entry naming a file that is no longer this one starts an
        // old build at every login — hidden, holding the single-instance lock,
        // with nothing on screen. Corrected here rather than only when the box
        // is clicked, because nobody clicks it again after updating.
        if (Autostart.PointAtThisBuild() is { Length: > 0 } was)
            _tray.ShowBalloonTip(8000, Loc.T("app.title"),
                                 Loc.T("tray.autostart_updated", was),
                                 Forms.ToolTipIcon.Info);

        // The registry is the truth about startup, not the preference file: an
        // entry somebody removed in Task Manager should not come back because a
        // JSON file still says yes.
        _prefs.Set(Prefs.StartWithWindows, Autostart.Enabled);
        UpdateTray();

        // Starting the program again is the way back to a hidden window. It is
        // what anybody tries first, and until now it only said "already
        // running" — which is true and useless when there is nothing on screen.
        SingleInstance.OnShowRequested(() => Dispatcher.Invoke(ShowFromTray));
    }

    private Forms.ToolStripMenuItem? _trackItem;
    private Forms.ToolStripMenuItem? _startupItem;

    /// <summary>
    /// A 16×16 icon drawn in code: a filled square when tracking, an outline
    /// when not.
    ///
    /// No .ico in the repository to keep in step with the palette, and — the
    /// actual point — the icon can carry the one piece of state somebody in the
    /// tray wants: is it watching or not.
    /// </summary>
    private static Draw.Icon MakeIcon(bool tracking)
    {
        using var bmp = new Draw.Bitmap(16, 16);
        using (var g = Draw.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = Draw.Drawing2D.SmoothingMode.AntiAlias;
            var accent = Draw.Color.FromArgb(255, 107, 44);        // the app's orange
            var rect = new Draw.Rectangle(2, 2, 11, 11);
            if (tracking)
                using (var b = new Draw.SolidBrush(accent)) g.FillEllipse(b, rect);
            else
                using (var p = new Draw.Pen(Draw.Color.FromArgb(167, 176, 192), 2f))
                    g.DrawEllipse(p, rect);
        }
        // GetHicon hands out an unmanaged handle. Cloning gives a managed icon
        // that owns its own copy, so destroying the handle here is correct
        // rather than a leak swapped for a crash.
        var handle = bmp.GetHicon();
        try
        {
            using var raw = Draw.Icon.FromHandle(handle);
            return (Draw.Icon)raw.Clone();
        }
        finally { DestroyIcon(handle); }
    }

    [System.Runtime.InteropServices.DllImport("user32.dll",
        SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);

    /// <summary>
    /// Light this window's taskbar button until somebody looks at it.
    ///
    /// For things that arrive while the person is inside a full-screen Forts.
    /// Deliberately not <c>Activate()</c>: pulling focus out of a running match
    /// to say somebody would like to watch it is a worse interruption than the
    /// one it is reporting.
    /// </summary>
    private void FlashTaskbar()
    {
        try
        {
            var handle = new System.Windows.Interop.WindowInteropHelper(this)
                .Handle;
            if (handle == IntPtr.Zero) return;
            var info = new FLASHWINFO
            {
                cbSize = (uint)System.Runtime.InteropServices.Marshal
                    .SizeOf<FLASHWINFO>(),
                hwnd = handle,
                // Taskbar button only, until the window comes to the front.
                dwFlags = FLASHW_TRAY | FLASHW_TIMERNOFG,
                uCount = uint.MaxValue,
                dwTimeout = 0,
            };
            FlashWindowEx(ref info);
        }
        catch (Exception) { /* a missed flash is not worth a crash */ }
    }

    private const uint FLASHW_TRAY = 0x00000002;
    private const uint FLASHW_TIMERNOFG = 0x0000000C;

    [System.Runtime.InteropServices.StructLayout(
        System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct FLASHWINFO
    {
        public uint cbSize;
        public IntPtr hwnd;
        public uint dwFlags;
        public uint uCount;
        public uint dwTimeout;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool FlashWindowEx(ref FLASHWINFO pwfi);

    /// <summary>Whether the log watcher should be running right now.</summary>
    private bool ShouldTrack =>
        !_hidden || _prefs.Get(Prefs.TrackInBackground);

    private void UpdateTray()
    {
        if (_tray is null) return;
        var on = ShouldTrack;
        var old = _tray.Icon;
        _tray.Icon = MakeIcon(on);
        old?.Dispose();
        _tray.Text = "Forts Ladder — " + Loc.T(on ? "tray.on" : "tray.off");
        if (_trackItem is not null)
            _trackItem.Checked = _prefs.Get(Prefs.TrackInBackground);
        if (_startupItem is not null)
            _startupItem.Checked = Autostart.Enabled;
        // Null while the constructor is still running. It was not supposed to be
        // possible to get here that early, and then it was — and the whole client
        // failed to start rather than the tray icon looking wrong for a moment.
        if (_watcher is not null) _watcher.Paused = !on;
    }

    private void ToggleTracking()
    {
        _prefs.Set(Prefs.TrackInBackground,
                   !_prefs.Get(Prefs.TrackInBackground));
        UpdateTray();
        SyncSettingsToggles();
    }

    private void ToggleStartup()
    {
        var wanted = !Autostart.Enabled;
        if (!Autostart.Set(wanted))
            AppDialog.Info(_hidden ? null : this,
                           ErrorCodes.Text(ErrorCodes.StartupRefused),
                           Loc.T("app.title"), AppDialog.Kind.Warning);
        _prefs.Set(Prefs.StartWithWindows, Autostart.Enabled);
        UpdateTray();
        SyncSettingsToggles();
    }

    private void ShowFromTray()
    {
        _hidden = false;
        Show();
        WindowState = WindowState.Normal;
        Activate();
        UpdateTray();
        // Deferred from startup when the program came up hidden: now there is a
        // window for the question to belong to.
        _ = CheckForUpdateAsync();
    }

    /// <summary>Hide to the tray, and say so once — a window that vanishes is
    /// otherwise a program that crashed.</summary>
    private void HideToTray()
    {
        _hidden = true;
        Hide();
        UpdateTray();
        // You did it, so you know where it went; saying it every time is
        // nagging. Being *started* hidden is the other case, and it says so
        // every time — see StartedInTray.
        if (_prefs.Get("told_about_tray")) return;
        _prefs.Set("told_about_tray", true);
        SayItIsRunning();
    }

    /// <summary>
    /// Brought up by the autostart entry, with no window at all.
    ///
    /// Announced every single time, unlike hiding it yourself: nothing was
    /// clicked, there is nothing on screen, and Windows files a new tray icon
    /// behind the overflow chevron where it is not visible either. Without this
    /// the only evidence the program exists is Task Manager.
    /// </summary>
    public void StartedInTray()
    {
        _hidden = true;
        UpdateTray();
        SayItIsRunning();
    }

    private void SayItIsRunning() => _tray?.ShowBalloonTip(
        5000, Loc.T("app.title"),
        Loc.T(_prefs.Get(Prefs.TrackInBackground)
                  ? "tray.still_tracking" : "tray.still_running"),
        Forms.ToolTipIcon.Info);

    /// <summary>
    /// One handler for the three boxes.
    ///
    /// Startup is asked of Windows rather than remembered here, so a box that
    /// could not be honoured goes back to showing the truth instead of the
    /// intention.
    /// </summary>
    private void ChkBackground_Click(object sender, RoutedEventArgs e)
    {
        _prefs.Set(Prefs.CloseToTray, ChkCloseToTray.IsChecked == true);
        _prefs.Set(Prefs.TrackInBackground, ChkTrackBg.IsChecked == true);

        if ((ChkStartWindows.IsChecked == true) != Autostart.Enabled
            && !Autostart.Set(ChkStartWindows.IsChecked == true))
            AppDialog.Info(this, ErrorCodes.Text(ErrorCodes.StartupRefused),
                           Loc.T("app.title"), AppDialog.Kind.Warning);
        _prefs.Set(Prefs.StartWithWindows, Autostart.Enabled);

        UpdateTray();
        SyncSettingsToggles();
    }

    /// <summary>Put the boxes back in step with what is actually the case.</summary>
    private void SyncSettingsToggles()
    {
        if (ChkCloseToTray is null) return;
        ChkCloseToTray.IsChecked = _prefs.Get(Prefs.CloseToTray);
        ChkTrackBg.IsChecked = _prefs.Get(Prefs.TrackInBackground);
        ChkStartWindows.IsChecked = Autostart.Enabled;
    }

    private void QuitForReal()
    {
        _reallyClosing = true;
        if (_tray is not null) _tray.Visible = false;
        Application.Current.Shutdown();
    }
}
