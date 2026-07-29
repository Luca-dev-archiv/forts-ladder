using System.Windows;
using System.Windows.Media;
using FortsLadder.Core;

namespace FortsLadder;

/// <summary>
/// The heartbeat, and the one number it produces.
///
/// It does two jobs at once, which is why it is not just a label. Saying "I am
/// here" every twenty seconds is also what says "I am gone" when the client is
/// closed: the server drops a queue entry nobody is asking about, and before
/// this existed closing the client left an account in the queue for good —
/// still being paired, burning a whole accept window of whoever was actually
/// at their keyboard.
///
/// The count comes back from the same call, and from the queue poll when that
/// is running, so nothing is asked for twice.
/// </summary>
public partial class MainWindow
{
    private System.Windows.Threading.DispatcherTimer? _presenceTimer;

    private void InitPresence()
    {
        // Well inside the server's 45-second window, so one dropped request
        // does not make somebody look offline.
        _presenceTimer = new System.Windows.Threading.DispatcherTimer
        {
            Interval = TimeSpan.FromSeconds(20),
        };
        _presenceTimer.Tick += async (_, _) => await PingAsync();
        _presenceTimer.Start();
        _ = PingAsync();
    }

    private async Task PingAsync()
    {
        // Nothing to say while not signed in — and nobody to say it to.
        if (!_api.Configured || !_api.LoggedIn) { ShowOnline(null); return; }
        var p = await _login.PingAsync();
        ShowOnline(p?.Online);
    }

    /// <summary>
    /// Draw the count. `null` means unknown, which is not the same as zero:
    /// "0 online" while the server is unreachable is a lie about the community
    /// rather than about the connection.
    /// </summary>
    private void ShowOnline(int? online)
    {
        if (online is null)
        {
            OnlineText.Text = Loc.T("presence.unknown");
            OnlineText.Foreground = BrushFor("TextMid");
            OnlineDot.Fill = BrushFor("TextMid");
            return;
        }
        var n = online.Value;
        OnlineText.Text = n == 1 ? Loc.T("presence.one") : Loc.T("presence.many", n);
        OnlineText.Foreground = BrushFor(n > 1 ? "TextHi" : "TextMid");
        OnlineDot.Fill = BrushFor(n > 1 ? "Win" : "TextMid");
    }
}
