using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace FortsLadder.Core;

/// <summary>
/// Checks GitHub Releases for a newer build and installs it on request.
///
/// An updater is a code execution channel: whatever it downloads runs on the
/// user's machine with their privileges. So three rules hold here and are the
/// reason this class is longer than a download would need to be.
///
///   1. **The hash is verified before anything is executed.** The release
///      publishes `SHA256SUMS.txt`; the download has to match the entry for
///      its filename. Without this, anyone able to tamper with the transfer
///      gets code execution on every install.
///   2. **Nothing installs itself.** The check only reports. Installing is a
///      separate call that the user triggers after seeing the version.
///   3. **Only our own executable is ever replaced**, by a swap script that
///      does nothing else and deletes itself.
///
/// A running .exe cannot overwrite itself on Windows, which is why the swap
/// happens from a small script after the process exits.
/// </summary>
public sealed class Updater
{
    public const string Owner = "Luca-dev-archiv";
    public const string Repo = "forts-ladder";

    private static readonly HttpClient Http = CreateClient();

    private static HttpClient CreateClient()
    {
        var c = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };
        // GitHub rejects requests without a User-Agent.
        c.DefaultRequestHeaders.UserAgent.Add(
            new ProductInfoHeaderValue("FortsLadder", CurrentVersion().ToString()));
        c.DefaultRequestHeaders.Accept.Add(
            new MediaTypeWithQualityHeaderValue("application/vnd.github+json"));
        return c;
    }

    public sealed record Release(string Tag, Version Version, string Notes,
                                 string AssetUrl, string AssetName,
                                 string ChecksumUrl, long SizeBytes);

    /// <summary>
    /// The running version. A local build reports 0.0.0, which is lower than
    /// any release — so a hand-built exe is always offered the update rather
    /// than being mistaken for one.
    /// </summary>
    public static Version CurrentVersion()
    {
        var raw = Assembly.GetEntryAssembly()?
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion ?? "0.0.0";
        // Strip "+buildmetadata" and any "-prerelease" suffix before parsing.
        var core = raw.Split('+')[0].Split('-')[0];
        return Version.TryParse(core, out var v) ? v : new Version(0, 0, 0);
    }

    /// <summary>
    /// Ask GitHub for the newest release. Returns null when already current,
    /// or when the answer cannot be trusted — never throws at the caller.
    /// </summary>
    public static async Task<Release?> CheckAsync(CancellationToken ct = default)
    {
        try
        {
            var url = $"https://api.github.com/repos/{Owner}/{Repo}/releases/latest";
            using var doc = JsonDocument.Parse(await Http.GetStringAsync(url, ct));
            var root = doc.RootElement;

            if (root.TryGetProperty("draft", out var d) && d.GetBoolean()) return null;
            if (root.TryGetProperty("prerelease", out var p) && p.GetBoolean()) return null;

            var tag = root.GetProperty("tag_name").GetString() ?? "";
            if (!Version.TryParse(tag.TrimStart('v', 'V').Split('-')[0], out var ver))
                return null;
            if (ver <= CurrentVersion()) return null;

            string? assetUrl = null, assetName = null, sumsUrl = null;
            long size = 0;
            foreach (var a in root.GetProperty("assets").EnumerateArray())
            {
                var name = a.GetProperty("name").GetString() ?? "";
                var dl = a.GetProperty("browser_download_url").GetString() ?? "";
                if (name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
                {
                    assetUrl = dl;
                    assetName = name;
                    size = a.TryGetProperty("size", out var s) ? s.GetInt64() : 0;
                }
                else if (name.Equals("SHA256SUMS.txt", StringComparison.OrdinalIgnoreCase))
                    sumsUrl = dl;
            }
            // No checksum published means the download cannot be verified, and
            // an unverifiable update is not offered at all.
            if (assetUrl is null || assetName is null || sumsUrl is null) return null;

            return new Release(tag, ver,
                root.TryGetProperty("body", out var b) ? b.GetString() ?? "" : "",
                assetUrl, assetName, sumsUrl, size);
        }
        catch (Exception)
        {
            // Offline, rate-limited, or the repository is private (the API
            // answers 404 without a token). None of that is worth interrupting
            // someone who only wanted to record a match.
            return null;
        }
    }

    /// <summary>
    /// Download, verify, and stage the swap. Returns the staged file, or throws
    /// with a reason the caller can show.
    /// </summary>
    public static async Task<string> DownloadAndVerifyAsync(
        Release rel, IProgress<double>? progress = null,
        CancellationToken ct = default)
    {
        var expected = await FetchExpectedHashAsync(rel, ct);

        var dir = Path.Combine(Path.GetTempPath(), "FortsLadderUpdate");
        Directory.CreateDirectory(dir);
        var staged = Path.Combine(dir, rel.AssetName);

        using (var resp = await Http.GetAsync(
                   rel.AssetUrl, HttpCompletionOption.ResponseHeadersRead, ct))
        {
            resp.EnsureSuccessStatusCode();
            var total = resp.Content.Headers.ContentLength ?? rel.SizeBytes;
            await using var src = await resp.Content.ReadAsStreamAsync(ct);
            await using var dst = File.Create(staged);
            var buffer = new byte[81920];
            long done = 0;
            int read;
            while ((read = await src.ReadAsync(buffer, ct)) > 0)
            {
                await dst.WriteAsync(buffer.AsMemory(0, read), ct);
                done += read;
                if (total > 0) progress?.Report((double)done / total);
            }
        }

        var actual = Sha256OfFile(staged);
        if (!actual.Equals(expected, StringComparison.OrdinalIgnoreCase))
        {
            // Delete it. A file that failed verification must not be left
            // lying around where a later run could pick it up.
            TryDelete(staged);
            throw new InvalidOperationException(
                $"checksum mismatch: expected {expected}, got {actual}. " +
                "The download was not used.");
        }
        return staged;
    }

    private static async Task<string> FetchExpectedHashAsync(
        Release rel, CancellationToken ct)
    {
        var text = await Http.GetStringAsync(rel.ChecksumUrl, ct);
        // Standard `sha256sum` format: "<hash>  <filename>".
        foreach (var line in text.Split('\n'))
        {
            var parts = line.Trim().Split((char[]?)null,
                                          StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length >= 2 &&
                Path.GetFileName(parts[^1].TrimStart('*'))
                    .Equals(rel.AssetName, StringComparison.OrdinalIgnoreCase))
                return parts[0];
        }
        throw new InvalidOperationException(
            $"SHA256SUMS.txt has no entry for {rel.AssetName} — refusing to " +
            "install something that cannot be checked.");
    }

    public static string Sha256OfFile(string path)
    {
        using var sha = SHA256.Create();
        using var fs = File.OpenRead(path);
        return Convert.ToHexString(sha.ComputeHash(fs));
    }

    /// <summary>
    /// Replace the running executable with the staged one and restart.
    ///
    /// Windows will not let a running image be overwritten, so a one-shot
    /// script waits for this process to exit, moves the file, starts it again
    /// and removes itself. It touches nothing else.
    /// </summary>
    public static void ApplyAndRestart(string stagedExe)
    {
        var current = Environment.ProcessPath
            ?? throw new InvalidOperationException("cannot locate the running exe");
        var pid = Environment.ProcessId;
        var script = Path.Combine(Path.GetTempPath(), "FortsLadderUpdate",
                                  "swap.cmd");

        // Quoted paths throughout: "Program Files" and the like are normal.
        File.WriteAllText(script, $"""
            @echo off
            rem Written by FortsLadder's updater. Waits for the app to close,
            rem swaps in the downloaded build, restarts it, then deletes itself.
            :wait
            tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
            if not errorlevel 1 (
                ping -n 2 127.0.0.1 >nul
                goto wait
            )
            move /Y "{stagedExe}" "{current}" >nul
            if errorlevel 1 (
                echo Update failed: could not replace "{current}".
                echo The downloaded build is still at "{stagedExe}".
                pause
                exit /b 1
            )
            start "" "{current}"
            del "%~f0"
            """);

        Process.Start(new ProcessStartInfo("cmd.exe", $"/c \"{script}\"")
        {
            CreateNoWindow = true,
            UseShellExecute = false,
        });
        System.Windows.Application.Current.Shutdown();
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); } catch (IOException) { }
    }
}
