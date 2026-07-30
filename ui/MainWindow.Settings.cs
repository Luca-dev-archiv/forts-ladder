using System.IO;
using System.Windows;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// The settings screen.
///
/// It exists because these things were spread over three places and none of
/// them was where you would look: the server address was in the Live view, the
/// language in the sidebar, and claiming a ladder name happened in a dialog
/// that appeared once and never again — so anyone who dismissed it had no
/// route back.
///
/// It is also the one screen that has to work before anything else does, so it
/// says what is missing rather than only offering buttons.
/// </summary>
public partial class MainWindow
{
    private void NavSettings_Click(object sender, RoutedEventArgs e)
    {
        ShowView("settings");
        SettingsServerBox.Text = _api.BaseUrl ?? ApiClient.DefaultBaseUrl;
        SettingsLangBox.ItemsSource = Loc.Available();
        SettingsLangBox.SelectedItem = Loc.Language;
        SettingsVersion.Text = Loc.T("settings.version", Updater.CurrentVersion());

        var forts = FortsPaths.FindFortsDir();
        SettingsFortsPath.Text = forts ?? Loc.T("status.forts_missing");

        _ = RefreshSettingsAsync();
    }

    /// <summary>
    /// Fill in what the server knows about this account.
    ///
    /// Also the one place the Steam display name is sent: this client reads it
    /// out of the game log, and without it every list on the server can only
    /// show a 17-digit number.
    /// </summary>
    private async Task RefreshSettingsAsync()
    {
        if (!_api.LoggedIn)
        {
            SettingsAccount.Text = Loc.T("settings.not_signed_in");
            BtnSettingsLogin.Visibility = Visibility.Visible;
            BtnSettingsSteam.IsEnabled = false;
            BtnSettingsConsent.IsEnabled = false;
            BtnClaimName.IsEnabled = false;
            return;
        }
        var me = await _login.MeAsync();
        if (me is null || !me.Logged_In)
        {
            SettingsAccount.Text = Loc.T("settings.not_signed_in");
            BtnSettingsLogin.Visibility = Visibility.Visible;
            return;
        }
        BtnSettingsLogin.Visibility = Visibility.Collapsed;
        BtnSettingsSteam.IsEnabled = string.IsNullOrEmpty(me.Steam_Id);
        BtnSettingsConsent.IsEnabled = true;
        BtnClaimName.IsEnabled = true;

        // Steam by name, with the id after it: the id is the identity, but it
        // is not what a person recognises themselves by.
        var steam = string.IsNullOrEmpty(me.Steam_Id)
            ? Loc.T("queue.no_steam")
            : (_watcher.CurrentPersona is { Length: > 0 } persona
                ? $"{persona} ({me.Steam_Id})" : me.Steam_Id!);
        SettingsAccount.Text = Loc.T("settings.account_line",
            me.Discord ?? "?", me.Role ?? "?", steam,
            me.Tracking_Consent ? Loc.T("settings.tracked")
                                : Loc.T("settings.not_tracked"));
        BtnSettingsConsent.Content = me.Tracking_Consent
            ? Loc.T("settings.consent_off") : Loc.T("settings.consent");

        LadderNameBox.Text = me.Ufer_Name ?? me.Ufer_Claim ?? "";
        // One name, not two. The sidebar and the series list read the local
        // store, so it follows the server rather than drifting from it.
        AdoptServerName(me.Ufer_Name);
        NameStatus.Text = me.Ufer_Name is { Length: > 0 }
            ? Loc.T("settings.name_set", me.Ufer_Name)
            : me.Ufer_Claim is { Length: > 0 }
                ? Loc.T("settings.name_pending", me.Ufer_Claim)
                : Loc.T("settings.name_none");

        // Report the Steam display name once it differs from what the server
        // holds. Cosmetic, so it is fire-and-forget and never blocks the view.
        var persona2 = _watcher.CurrentPersona;
        if (persona2 is { Length: > 0 } && persona2 != me.Steam_Name)
            _ = _login.SetSteamNameAsync(persona2);
    }

    private async void BtnClaimName_Click(object sender, RoutedEventArgs e)
    {
        var name = LadderNameBox.Text.Trim();
        if (name.Length == 0) { NameStatus.Text = Loc.T("settings.name_none"); return; }
        if (!await EnsureReadyAsync()) { NameStatus.Text = DraftError.Text; return; }

        BtnClaimName.IsEnabled = false;
        var res = await _login.ClaimUferNameAsync(name);
        BtnClaimName.IsEnabled = true;
        NameStatus.Text = res is null
            ? Loc.T("settings.name_failed", _api.LastError ?? "?")
            : res.Applied
                ? Loc.T("settings.name_set", res.Ufer_Name ?? name)
                : Loc.T("settings.name_pending", res.Pending ?? name);
        // The sidebar shows the same name, and a stale copy of it is worse than
        // none.
        if (_watcher.CurrentAccount is { Length: > 0 } id) RefreshUferName(id);
    }

    private async void BtnSettingsLogin_Click(object sender, RoutedEventArgs e)
    {
        if (await EnsureReadyAsync()) await RefreshSettingsAsync();
        else SettingsAccount.Text = DraftError.Text;
    }

    private async void BtnSettingsConsent_Click(object sender, RoutedEventArgs e)
    {
        var me = await _login.MeAsync();
        if (me is null) return;
        var ok = me.Tracking_Consent
            ? await _login.WithdrawConsentAsync()
            : await _login.GrantConsentAsync();
        if (!ok) SettingsAccount.Text = _api.LastError ?? "?";
        await RefreshSettingsAsync();
    }

    private void BtnSettingsConnect_Click(object sender, RoutedEventArgs e)
    {
        if (!_api.SetBaseUrl(SettingsServerBox.Text))
        {
            ServerStatus.Text = _api.LastError ?? "?";
            return;
        }
        ServerStatus.Text = Loc.T("settings.server_set", _api.BaseUrl ?? "—");
        _ = RefreshSettingsAsync();
    }

    private void BtnSettingsDefault_Click(object sender, RoutedEventArgs e)
    {
        SettingsServerBox.Text = ApiClient.DefaultBaseUrl;
        BtnSettingsConnect_Click(sender, e);
    }

    private void BtnOpenFortsDir_Click(object sender, RoutedEventArgs e)
    {
        var forts = FortsPaths.FindFortsDir();
        if (forts is null || !Directory.Exists(forts)) return;
        try
        {
            System.Diagnostics.Process.Start("explorer.exe", $"\"{forts}\"");
        }
        catch (Exception ex) { SettingsFortsPath.Text = ex.Message; }
    }

    private async void BtnCheckUpdate_Click(object sender, RoutedEventArgs e)
    {
        SettingsVersion.Text = Loc.T("settings.checking");
        var rel = await Updater.CheckAsync();
        if (rel is null)
        {
            SettingsVersion.Text = Loc.T("settings.up_to_date",
                                         Updater.CurrentVersion());
            return;
        }
        SettingsVersion.Text = Loc.T("settings.update_found",
                                     rel.Version.ToString());
        await CheckForUpdateAsync();
    }
}
