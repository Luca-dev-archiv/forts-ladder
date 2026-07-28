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
        base.OnStartup(e);
    }
}
