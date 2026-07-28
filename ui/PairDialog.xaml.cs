using System.Diagnostics;
using System.Windows;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// The fallback login: log in in a browser, paste the code it shows.
///
/// Only reached when the local Discord client could not be asked. The reason it
/// failed is shown rather than hidden, because "Discord is not running" and
/// "this server has no Discord application" need different fixes and look
/// identical from behind a generic message.
/// </summary>
public partial class PairDialog : Window
{
    private readonly LoginFlow _login;

    public PairDialog(LoginFlow login, string? reason)
    {
        InitializeComponent();
        _login = login;
        Reason.Text = reason ?? "";
        Reason.Visibility = string.IsNullOrWhiteSpace(reason)
            ? Visibility.Collapsed : Visibility.Visible;
        CodeBox.Focus();
    }

    private void BtnOpen_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            Process.Start(new ProcessStartInfo(_login.BrowserLoginUrl())
            { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Error.Text = ex.Message;
        }
    }

    private async void BtnOk_Click(object sender, RoutedEventArgs e)
    {
        var code = CodeBox.Text.Trim();
        if (code.Length == 0) return;
        BtnOk.IsEnabled = false;
        var r = await _login.ClaimPairingAsync(code);
        BtnOk.IsEnabled = true;
        if (r.Ok) { DialogResult = true; return; }
        Error.Text = r.Error ?? "?";
    }

    private void BtnCancel_Click(object sender, RoutedEventArgs e)
        => DialogResult = false;
}
