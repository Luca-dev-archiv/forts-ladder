namespace FortsLadder.Core;

/// <summary>
/// One client per machine — and a way back to the one that is already running.
///
/// The single-instance rule is not tidiness: two clients read the same game log
/// and both report the same finished game, both write the same lobby settings,
/// and both answer the same match offer. The second is not a second player, it
/// is a second voice claiming to be the same one.
///
/// Two things went wrong with the mutex that used to do this alone.
///
/// **Refusing was a dead end.** Started by the autostart entry the client has no
/// window, only a tray icon, and Windows files a new tray icon in the overflow
/// behind the chevron where nobody sees it. Starting the program again is what
/// anybody does next, and all it said was "already running" — true, useless, and
/// the process it was talking about was unreachable. So the second launch now
/// asks the first to show itself and leaves; the window arriving is the answer.
///
/// **A named mutex outlives its owner.** Observed, not theorised: with no client
/// running at all, `Local\FortsLadderClient` was still there, and every launch
/// from then on refused to start. A mutex proves a name was once taken, not that
/// anybody is alive behind it. What proves that is somebody listening for the
/// show request — so if nothing answers, the lock is treated as the litter it is
/// and this process takes over.
/// </summary>
public static class SingleInstance
{
    private const string MutexName = @"Local\FortsLadderClient";
    private const string ShowName = @"Local\FortsLadderShow";

    /// <summary>Held for the life of the process; released when it dies, however
    /// it dies — which a lock file on disk would not manage.</summary>
    private static Mutex? _only;
    private static EventWaitHandle? _show;

    /// <summary>
    /// True if this process should run as the client.
    ///
    /// False only when another one is demonstrably alive — which means it is
    /// listening, which means <see cref="AskRunningInstanceToShow"/> will reach
    /// it.
    /// </summary>
    public static bool Claim()
    {
        try
        {
            // Local, not Global: two people on one machine under different
            // Windows accounts are two players, and each may run their own.
            _only = new Mutex(initiallyOwned: true, MutexName, out var mine);
            if (!mine && EventWaitHandle.TryOpenExisting(ShowName, out var live))
            {
                live.Dispose();
                return false;
            }
            // Either the name was ours, or it was left behind by a client that
            // is gone. Created here rather than later so the gap between taking
            // the name and being reachable is as close to nothing as it can be.
            _show = new EventWaitHandle(false, EventResetMode.AutoReset, ShowName);
            return true;
        }
        catch (Exception)
        {
            // Never let the guard be the thing that stops the program starting.
            return true;
        }
    }

    /// <summary>
    /// Ask the running client to show its window. False if it could not be
    /// reached, which is the only case still worth a message.
    /// </summary>
    public static bool AskRunningInstanceToShow()
    {
        try
        {
            if (!EventWaitHandle.TryOpenExisting(ShowName, out var handle))
                return false;
            using (handle) return handle.Set();
        }
        catch (Exception) { return false; }
    }

    /// <summary>
    /// Listen for a later launch asking us to appear.
    ///
    /// A background thread, so it cannot keep the process alive on its own — a
    /// program that will not quit is a worse bug than the one this fixes.
    /// </summary>
    public static void OnShowRequested(Action show)
    {
        if (_show is null) return;      // claiming failed; nothing to listen on
        var t = new Thread(() =>
        {
            while (true)
            {
                try
                {
                    if (!_show.WaitOne()) continue;
                    show();
                }
                catch (Exception) { return; }
            }
        })
        { IsBackground = true, Name = "show-request" };
        t.Start();
    }
}
