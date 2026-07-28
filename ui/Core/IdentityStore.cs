using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FortsLadder.Core;

/// <summary>
/// Mapping between ladder display name and SteamID64.
///
/// Reads and writes THE SAME file as ladder/identity.py
/// (data/identity.json), so the whole toolchain shares one truth — a name
/// set in the UI is the name the analysis uses.
///
/// A self-declaration is a claim, not proof: on your own machine you may
/// say you are anyone. That is fine locally; a server has to have
/// `self-declared` confirmed once.
/// </summary>
public sealed class IdentityStore
{
    public sealed class LinkDto
    {
        [JsonPropertyName("ufer_name")] public string UferName { get; set; } = "";
        [JsonPropertyName("steam_id")] public string SteamId { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "manual";
        [JsonPropertyName("confirmed")] public bool Confirmed { get; set; } = true;
        [JsonPropertyName("evidence")] public string Evidence { get; set; } = "";
        [JsonPropertyName("updated")] public string Updated { get; set; } = "";
    }

    public sealed class FileDto
    {
        [JsonPropertyName("version")] public int Version { get; set; } = 1;
        [JsonPropertyName("links")] public List<LinkDto> Links { get; set; } = new();
        [JsonPropertyName("local")] public Dictionary<string, JsonElement> Local { get; set; } = new();
        // Fields only the Python side fills are passed through on write
        // rather than dropped.
        [JsonExtensionData] public Dictionary<string, JsonElement> Extra { get; set; } = new();
    }

    private static readonly JsonSerializerOptions Opts = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private readonly string _path;
    private FileDto _data = new();

    public IdentityStore(string? path = null)
    {
        _path = path ?? DefaultPath();
        Load();
    }

    public static string DefaultPath()
    {
        // Look next to the application (repo layout), otherwise in the user
        // profile — an installed exe must not write to its own directory.
        var here = AppContext.BaseDirectory;
        for (var d = new DirectoryInfo(here); d is not null; d = d.Parent)
        {
            var cand = Path.Combine(d.FullName, "data", "identity.json");
            if (File.Exists(cand)) return cand;
            if (Directory.Exists(Path.Combine(d.FullName, "ladder")))
                return cand;
        }
        return Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "FortsLadder", "identity.json");
    }

    public void Load()
    {
        if (!File.Exists(_path)) { _data = new FileDto(); return; }
        try
        {
            _data = JsonSerializer.Deserialize<FileDto>(File.ReadAllText(_path))
                    ?? new FileDto();
        }
        catch (Exception) { _data = new FileDto(); }
    }

    public void Save()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        File.WriteAllText(_path, JsonSerializer.Serialize(_data, Opts));
    }

    private static string Norm(string s) =>
        s.Normalize(System.Text.NormalizationForm.FormKC).Trim().ToLowerInvariant();

    public string? UferNameFor(string steamId) => _data.Links
        .FirstOrDefault(l => l.SteamId == steamId && l.Confirmed)?.UferName;

    public List<string> SteamIdsFor(string uferName) => _data.Links
        .Where(l => l.Confirmed && Norm(l.UferName) == Norm(uferName))
        .Select(l => l.SteamId).ToList();

    public bool WasAsked(string steamId) =>
        UferNameFor(steamId) is not null ||
        (_data.Local.TryGetValue("skip", out var skip) &&
         skip.ValueKind == JsonValueKind.True &&
         _data.Local.TryGetValue("steam_id", out var sid) &&
         sid.GetString() == steamId);

    /// <summary>
    /// Record a self-declaration. Throws if the name already belongs to a
    /// DIFFERENT Steam ID — the case where a mix-up gets expensive. That is
    /// a league admin's call, not a dialog's.
    /// </summary>
    public void SelfDeclare(string steamId, string uferName)
    {
        var owners = SteamIdsFor(uferName);
        if (owners.Count > 0 && !owners.Contains(steamId))
            throw new InvalidOperationException(
                Loc.T("identity.name_taken", uferName, string.Join(", ", owners)));

        _data.Links.RemoveAll(l => l.SteamId == steamId);
        _data.Links.Add(new LinkDto
        {
            UferName = uferName,
            SteamId = steamId,
            Method = "self-declared",
            Confirmed = true,
            Evidence = "first-run dialog in the UI",
            Updated = DateTime.Now.ToString("yyyy-MM-dd"),
        });
        _data.Local = new Dictionary<string, JsonElement>
        {
            ["steam_id"] = JsonSerializer.SerializeToElement(steamId),
            ["ufer_name"] = JsonSerializer.SerializeToElement(uferName),
            ["asked"] = JsonSerializer.SerializeToElement(DateTime.Now.ToString("yyyy-MM-dd")),
        };
        Save();
    }

    public void SkipDeclaration(string steamId)
    {
        _data.Local = new Dictionary<string, JsonElement>
        {
            ["steam_id"] = JsonSerializer.SerializeToElement(steamId),
            ["skip"] = JsonSerializer.SerializeToElement(true),
            ["asked"] = JsonSerializer.SerializeToElement(DateTime.Now.ToString("yyyy-MM-dd")),
        };
        Save();
    }

    /// <summary>The ranking's display names, if the seed file is present.</summary>
    public static List<string> LoadUferNames()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d is not null; d = d.Parent)
        {
            var cand = Path.Combine(d.FullName, "data", "seed", "ufer.json");
            if (!File.Exists(cand)) continue;
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cand));
                return doc.RootElement.GetProperty("players").EnumerateArray()
                    .Select(p => p.GetProperty("name").GetString() ?? "")
                    .Where(s => s.Length > 0).ToList();
            }
            catch (Exception) { return new List<string>(); }
        }
        return new List<string>();
    }
}
