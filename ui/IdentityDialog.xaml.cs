using System.Windows;
using System.Windows.Controls;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// First-run dialog. Asks exactly once who owns this machine.
///
/// Suggestions while typing are explicitly fine here — a HUMAN decides. Only
/// an exact hit is linked automatically: the ranking contains short names
/// nested inside longer ones (`Rin`, `Rinaldo`), and a similarity match
/// would merge two careers.
/// </summary>
public partial class IdentityDialog : Window
{
    private readonly List<string> _names;
    private bool _updating;

    public string? ChosenName { get; private set; }
    public bool Skipped { get; private set; }

    public IdentityDialog(string steamId, string persona, List<string> uferNames)
    {
        InitializeComponent();
        _names = uferNames;
        SteamIdText.Text = steamId;
        PersonaText.Text = string.IsNullOrEmpty(persona)
            ? Loc.T("identity.persona_missing") : persona;

        // If the Steam name appears in the ranking exactly like that, it is
        // the likeliest answer — but the user still has to confirm it.
        var exact = _names.FirstOrDefault(
            n => string.Equals(n, persona, StringComparison.OrdinalIgnoreCase));
        if (exact is not null) NameBox.Text = exact;
        NameBox.Focus();
        NameBox.CaretIndex = NameBox.Text.Length;
        UpdateHint();
    }

    private void NameBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_updating) return;
        UpdateHint();
    }

    private void UpdateHint()
    {
        var text = NameBox.Text.Trim();
        BtnOk.IsEnabled = text.Length > 0;

        if (_names.Count == 0)
        {
            HintText.Text = Loc.T("identity.no_list");
            Suggestions.Visibility = Visibility.Collapsed;
            return;
        }
        if (text.Length == 0)
        {
            HintText.Text = Loc.T("identity.count", _names.Count);
            Suggestions.Visibility = Visibility.Collapsed;
            return;
        }
        if (_names.Any(n => string.Equals(n, text, StringComparison.OrdinalIgnoreCase)))
        {
            HintText.Text = Loc.T("identity.exact");
            Suggestions.Visibility = Visibility.Collapsed;
            return;
        }

        var near = _names
            .Where(n => n.Contains(text, StringComparison.OrdinalIgnoreCase))
            .Take(6).ToList();
        Suggestions.ItemsSource = near;
        Suggestions.Visibility = near.Count > 0 ? Visibility.Visible
                                                : Visibility.Collapsed;
        HintText.Text = near.Count > 0
            ? Loc.T("identity.did_you_mean")
            : Loc.T("identity.unlisted");
    }

    private void Suggestions_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Suggestions.SelectedItem is not string pick) return;
        _updating = true;
        NameBox.Text = pick;
        NameBox.CaretIndex = pick.Length;
        _updating = false;
        Suggestions.Visibility = Visibility.Collapsed;
        UpdateHint();
    }

    private void BtnOk_Click(object sender, RoutedEventArgs e)
    {
        var text = NameBox.Text.Trim();
        if (text.Length == 0) return;
        ChosenName = text;
        DialogResult = true;
    }

    private void BtnSkip_Click(object sender, RoutedEventArgs e)
    {
        Skipped = true;
        DialogResult = true;
    }

    private void BtnLater_Click(object sender, RoutedEventArgs e)
    {
        // Record nothing — the next start asks again.
        DialogResult = false;
    }
}
