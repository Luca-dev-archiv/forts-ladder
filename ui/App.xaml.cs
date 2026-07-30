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

        // One client per machine.
        //
        // Not tidiness: two clients read the same game log and both report the
        // same finished game, both write the same lobby settings, and both
        // answer the same match offer. The second one is not a second player,
        // it is a second voice claiming to be the same player.
        if (!ClaimSingleInstance())
        {
            AppDialog.Info(null, ErrorCodes.Text(ErrorCodes.AlreadyRunning), Loc.T("app.title"), AppDialog.Kind.Info);
            Shutdown(1);
            return;
        }
        base.OnStartup(e);
    }

    /// <summary>Held for the life of the process; released when it dies, however
    /// it dies — which a lock file on disk would not manage.</summary>
    private static Mutex? _only;

    private static bool ClaimSingleInstance()
    {
        try
        {
            // Local, not Global: two people on one machine under different
            // Windows accounts are two players, and each may run their own.
            _only = new Mutex(initiallyOwned: true, @"Local\FortsLadderClient",
                              out var mine);
            return mine;
        }
        catch (Exception)
        {
            // Never let the guard be the thing that stops the program starting.
            return true;
        }
    }
}
