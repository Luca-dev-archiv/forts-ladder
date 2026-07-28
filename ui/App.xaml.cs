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
        // Override with:  FortsLadder.exe --lang de
        var idx = Array.IndexOf(e.Args, "--lang");
        Loc.Init(idx >= 0 && idx + 1 < e.Args.Length ? e.Args[idx + 1] : null);
        base.OnStartup(e);
    }
}
