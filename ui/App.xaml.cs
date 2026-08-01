using System.Windows;
using FortsLadder.Core;

namespace FortsLadder;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        // Language before the first window is parsed: the XAML markup
        // extension resolves at load time, so a later switch would leave
        // half the labels in the old language.
        //
        // Order: an explicit --lang, then a remembered choice, then English.
        // English is the default rather than the system language on purpose —
        // the project is English-first and the game ships in eighteen
        // languages, so a German catalog greeting whoever happens to run a
        // German Windows is the wrong default. Anyone who wants another
        // language picks it once and it is remembered.
        //     FortsLadder.exe --lang de
        var idx = Array.IndexOf(e.Args, "--lang");
        var fromArgs = idx >= 0 && idx + 1 < e.Args.Length ? e.Args[idx + 1] : null;
        Loc.Init(fromArgs ?? Loc.Preferred() ?? "en");

        // One client per machine — see SingleInstance for why, and for why a
        // refusal on its own was not enough.
        if (!SingleInstance.Claim())
        {
            // Running already: bring that one forward instead of explaining
            // that it exists. Started from the autostart entry it has no window
            // at all, and this is the only way anybody would think to look.
            if (!SingleInstance.AskRunningInstanceToShow())
                AppDialog.Info(null, ErrorCodes.Text(ErrorCodes.AlreadyRunning),
                               Loc.T("app.title"), AppDialog.Kind.Info);
            Shutdown(0);
            return;
        }
        // Hiding the last window must not end the process, or closing to the
        // tray would be an elaborate way of quitting. What ends it is the window
        // being closed for real, which MainWindow asks for on its way out.
        ShutdownMode = ShutdownMode.OnExplicitShutdown;
        //     FortsLadder.exe --tray
        // Started by the autostart entry: no window, just the tray icon and the
        // log watcher. Nobody asked for a window at login.
        StartHidden = e.Args.Contains("--tray");
        base.OnStartup(e);

        // The window is created here rather than by StartupUri in App.xaml,
        // because "start without a window" has to be a real option.
        //
        // It used to be done by hiding the window from its own Loaded handler,
        // which does not work: Loaded runs inside the showing sequence, so the
        // window appeared anyway and every login opened a full window from a
        // program that had been asked to stay in the tray. Cancelling StartupUri
        // instead is not possible either — the property throws on null.
        //
        // Constructing it is enough for everything that runs in the background:
        // the tray icon and the log watcher both live in the constructor. Only
        // what needs a visible window waits until there is one.
        var w = new MainWindow();
        MainWindow = w;
        if (StartHidden) w.StartedInTray();
        else w.Show();
    }

    /// <summary>Started from the autostart entry, so open no window.</summary>
    public static bool StartHidden { get; private set; }
}
