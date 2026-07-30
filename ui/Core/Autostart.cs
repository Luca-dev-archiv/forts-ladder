using Microsoft.Win32;

namespace FortsLadder.Core;

/// <summary>
/// Starting with Windows, if somebody asks for it.
///
/// The per-user Run key and nothing else: no service, no scheduled task, no
/// machine-wide entry. A game-community tool that needs administrator rights to
/// install itself into startup is a tool people are right to be wary of, and
/// everything here is visible and removable in Task Manager's Startup tab like
/// any other program.
///
/// It starts with `--tray`, so the answer to "it opened a window I did not ask
/// for" is that it does not.
/// </summary>
public static class Autostart
{
    private const string RunKey =
        @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "FortsLadder";

    /// <summary>The exe to launch. Empty when it cannot be determined, which is
    /// the case for a debug build running out of the SDK host.</summary>
    private static string? ExePath()
    {
        var path = Environment.ProcessPath;
        return string.IsNullOrEmpty(path) || !path.EndsWith(
                   ".exe", StringComparison.OrdinalIgnoreCase)
            ? null : path;
    }

    public static bool Enabled
    {
        get
        {
            try
            {
                using var key = Registry.CurrentUser.OpenSubKey(RunKey);
                return key?.GetValue(ValueName) is string s && s.Length > 0;
            }
            catch (Exception) { return false; }
        }
    }

    /// <summary>
    /// Turn it on or off. Returns whether the registry now says what was asked
    /// — a silent failure here would leave a checkbox lying about the machine.
    /// </summary>
    public static bool Set(bool enabled)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(RunKey);
            if (key is null) return false;
            if (!enabled)
            {
                key.DeleteValue(ValueName, throwOnMissingValue: false);
                return true;
            }
            if (ExePath() is not { Length: > 0 } exe) return false;
            // Quoted: a path with a space in it is otherwise read as a command
            // and an argument, and "C:\Program" is not a program.
            key.SetValue(ValueName, $"\"{exe}\" --tray");
            return true;
        }
        catch (Exception) { return false; }
    }
}
