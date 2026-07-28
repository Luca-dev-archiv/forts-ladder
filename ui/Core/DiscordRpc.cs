using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace FortsLadder.Core;

/// <summary>
/// Log in through the Discord desktop client instead of a browser.
///
/// Discord's desktop app listens on a local named pipe (`discord-ipc-0` …
/// `-9`). An application can send `AUTHORIZE` over it, Discord shows its own
/// native "… wants to access your account" dialog, and hands back a normal
/// OAuth authorization code. No browser, no copying a pairing code, and the
/// consent prompt is Discord's own — which is exactly the prompt people should
/// be trusting rather than anything this program draws.
///
/// **The code is worthless on its own and that is the point.** Exchanging it
/// needs the client secret, and that stays on the server: a secret shipped
/// inside a downloadable .exe is not a secret, it is a published credential.
/// So this class returns the code and the server does the rest.
///
/// This is the preferred path, not the only one. Discord may not be running,
/// may be a Store/sandboxed build whose pipe is not reachable, or may refuse
/// `AUTHORIZE` if the application has not been granted RPC access. Every one
/// of those falls back to the browser flow, so the fallback is a requirement
/// rather than a nicety.
/// </summary>
public static class DiscordRpc
{
    private enum Op { Handshake = 0, Frame = 1, Close = 2, Ping = 3, Pong = 4 }

    public sealed record AuthResult(string? Code, string? Error)
    {
        public bool Ok => Code is not null;
    }

    /// <summary>
    /// Ask the local Discord client for an authorization code.
    ///
    /// `redirectUri` has to be one registered on the Discord application —
    /// Discord checks it here and again during the exchange. It is passed in
    /// rather than hardcoded because only the operator knows what they
    /// registered.
    /// </summary>
    public static async Task<AuthResult> AuthorizeAsync(
        string clientId, string redirectUri, string[]? scopes = null,
        CancellationToken ct = default)
    {
        scopes ??= new[] { "identify" };

        NamedPipeClientStream? pipe = null;
        try
        {
            pipe = await ConnectAsync(ct);
            if (pipe is null)
                return new AuthResult(null,
                    "Discord is not running, or its local interface is not "
                    + "reachable from this build.");

            await WriteAsync(pipe, Op.Handshake,
                JsonSerializer.Serialize(new { v = 1, client_id = clientId }), ct);

            var (op, payload) = await ReadAsync(pipe, ct);
            if (op == Op.Close)
                return new AuthResult(null, Reason(payload, "Discord closed the connection"));

            await WriteAsync(pipe, Op.Frame, JsonSerializer.Serialize(new
            {
                cmd = "AUTHORIZE",
                nonce = Guid.NewGuid().ToString(),
                args = new { client_id = clientId, scopes, redirect_uri = redirectUri },
            }), ct);

            // Discord is waiting for the user to press a button in its own
            // window, so this read is allowed to take a while.
            using var slow = CancellationTokenSource.CreateLinkedTokenSource(ct);
            slow.CancelAfter(TimeSpan.FromMinutes(2));

            while (true)
            {
                var (rop, body) = await ReadAsync(pipe, slow.Token);
                if (rop == Op.Close)
                    return new AuthResult(null, Reason(body, "Discord closed the connection"));
                if (rop == Op.Ping)
                {
                    await WriteAsync(pipe, Op.Pong, body, ct);
                    continue;
                }

                using var doc = JsonDocument.Parse(body);
                var root = doc.RootElement;

                if (root.TryGetProperty("evt", out var evt)
                    && evt.GetString() == "ERROR")
                    return new AuthResult(null, Reason(body, "Discord refused the request"));

                if (root.TryGetProperty("cmd", out var cmd)
                    && cmd.GetString() == "AUTHORIZE"
                    && root.TryGetProperty("data", out var data)
                    && data.TryGetProperty("code", out var code))
                    return new AuthResult(code.GetString(), null);
                // Anything else is a notification we did not subscribe to.
            }
        }
        catch (OperationCanceledException)
        {
            return new AuthResult(null, "The Discord prompt was not answered.");
        }
        catch (Exception ex)
        {
            return new AuthResult(null, ex.Message);
        }
        finally
        {
            pipe?.Dispose();
        }
    }

    /// <summary>Discord numbers its pipes; several can exist side by side.</summary>
    private static async Task<NamedPipeClientStream?> ConnectAsync(CancellationToken ct)
    {
        for (var i = 0; i < 10; i++)
        {
            var pipe = new NamedPipeClientStream(".", $"discord-ipc-{i}",
                                                 PipeDirection.InOut,
                                                 PipeOptions.Asynchronous);
            try
            {
                await pipe.ConnectAsync(300, ct);
                return pipe;
            }
            catch (Exception)
            {
                pipe.Dispose();
            }
        }
        return null;
    }

    // Frame format: 4-byte little-endian opcode, 4-byte little-endian length,
    // then UTF-8 JSON.
    private static async Task WriteAsync(Stream s, Op op, string json,
                                         CancellationToken ct)
    {
        var body = Encoding.UTF8.GetBytes(json);
        var frame = new byte[8 + body.Length];
        BitConverter.TryWriteBytes(frame.AsSpan(0, 4), (int)op);
        BitConverter.TryWriteBytes(frame.AsSpan(4, 4), body.Length);
        body.CopyTo(frame, 8);
        await s.WriteAsync(frame, ct);
        await s.FlushAsync(ct);
    }

    private static async Task<(Op, string)> ReadAsync(Stream s, CancellationToken ct)
    {
        var header = await ReadExactAsync(s, 8, ct);
        var op = (Op)BitConverter.ToInt32(header, 0);
        var length = BitConverter.ToInt32(header, 4);
        // A hostile or broken peer must not be able to ask for a huge buffer.
        if (length < 0 || length > 1 << 20)
            throw new InvalidDataException($"implausible frame length {length}");
        var body = await ReadExactAsync(s, length, ct);
        return (op, Encoding.UTF8.GetString(body));
    }

    private static async Task<byte[]> ReadExactAsync(Stream s, int count,
                                                     CancellationToken ct)
    {
        var buf = new byte[count];
        var done = 0;
        while (done < count)
        {
            var n = await s.ReadAsync(buf.AsMemory(done, count - done), ct);
            if (n == 0) throw new EndOfStreamException("Discord closed the pipe");
            done += n;
        }
        return buf;
    }

    /// <summary>Prefer Discord's own message over a generic one.</summary>
    private static string Reason(string payload, string fallback)
    {
        try
        {
            using var doc = JsonDocument.Parse(payload);
            if (doc.RootElement.TryGetProperty("data", out var d)
                && d.TryGetProperty("message", out var m)
                && m.GetString() is { Length: > 0 } msg)
                return msg;
            if (doc.RootElement.TryGetProperty("message", out var m2)
                && m2.GetString() is { Length: > 0 } msg2)
                return msg2;
        }
        catch (JsonException) { }
        return fallback;
    }
}
