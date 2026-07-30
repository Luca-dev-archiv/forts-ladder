using System.Text.RegularExpressions;
using System.Windows;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// The one dialog this program shows.
///
/// Everything used to go through <c>MessageBox.Show</c>, which draws the
/// operating system's grey dialog with a system icon — correct, and jarring
/// beside a dark window with its own title bar. It also had no room for the
/// thing that matters most in an error: the code, on its own line, selectable,
/// because a code exists to be copied into Discord.
///
/// Three shapes, one window: something to read, something to confirm, and
/// something to type. Anything more and callers start reaching for MessageBox
/// again.
/// </summary>
public partial class AppDialog : Window
{
    /// <summary>What the accent stripe says about the message.</summary>
    public enum Kind { Info, Warning, Question }

    /// <summary>What was typed, for the asking form.</summary>
    public string? Input { get; private set; }

    private AppDialog(string message, string title, Kind kind,
                      bool cancellable, string? inputPrompt, string? preset)
    {
        InitializeComponent();
        TitleText.Text = title;

        // The code is pulled out of the message and shown separately: it is the
        // one part worth copying, and buried mid-sentence it gets retyped wrong.
        var m = Regex.Match(message, @"^\[(FL-\d+)\]\s*(.*)$",
                            RegexOptions.Singleline);
        if (m.Success)
        {
            CodeBox.Text = m.Groups[1].Value + "  ·  " + Loc.T("err.list_hint");
            CodeBox.Visibility = Visibility.Visible;
            message = m.Groups[2].Value;
        }
        MessageText.Text = message;

        KindStripe.Background = (System.Windows.Media.Brush)FindResource(
            kind switch
            {
                Kind.Warning => "Loss",
                Kind.Question => "Warn",
                _ => "Accent",
            });

        BtnCancel.Visibility = cancellable ? Visibility.Visible
                                           : Visibility.Collapsed;
        if (inputPrompt is not null)
        {
            HintText.Text = inputPrompt;
            HintText.Visibility = Visibility.Visible;
            InputBox.Visibility = Visibility.Visible;
            InputBox.Text = preset ?? "";
            Loaded += (_, _) => { InputBox.Focus(); InputBox.SelectAll(); };
        }
    }

    private void BtnOk_Click(object sender, RoutedEventArgs e)
    {
        Input = InputBox.Text;
        DialogResult = true;
    }

    private void BtnCancel_Click(object sender, RoutedEventArgs e)
        => DialogResult = false;

    // ------------------------------------------------------------- The callers
    /// <summary>Something to read.</summary>
    public static void Info(Window? owner, string message, string title,
                            Kind kind = Kind.Info)
        => Make(owner, message, title, kind, false, null, null).ShowDialog();

    /// <summary>Something to confirm. False for both "no" and closing it.</summary>
    public static bool Confirm(Window? owner, string message, string title,
                               Kind kind = Kind.Question)
        => Make(owner, message, title, kind, true, null, null).ShowDialog() == true;

    /// <summary>Something to type. Null when cancelled or left empty.</summary>
    public static string? Ask(Window? owner, string prompt, string title,
                              string? preset = null)
    {
        var d = Make(owner, prompt, title, Kind.Question, true,
                     Loc.T("dialog.input_hint"), preset);
        if (d.ShowDialog() != true) return null;
        var text = (d.Input ?? "").Trim();
        return text.Length == 0 ? null : text;
    }

    private static AppDialog Make(Window? owner, string message, string title,
                                  Kind kind, bool cancellable,
                                  string? inputPrompt, string? preset)
    {
        var d = new AppDialog(message, title, kind, cancellable, inputPrompt,
                              preset);
        // Centre on the window that raised it, unless it is being raised before
        // there is one — which happens for the already-running check.
        if (owner is not null && owner.IsLoaded) d.Owner = owner;
        else d.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        return d;
    }
}
