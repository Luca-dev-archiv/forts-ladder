using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FortsLadder.Core;

/// <summary>
/// The ranking, with both rating columns.
///
/// It is NOT computed here but by `ladder/table.py`, which writes
/// `data/ratings.json`. That is deliberate: the rating formula is verified
/// against real numbers from the spreadsheet, and a second implementation in
/// C# would be a second source of error that quietly drifts. The client
/// displays what the analysis computes.
///
/// If the file is missing the view stays empty and says how to generate it,
/// rather than inventing a ranking out of nothing.
/// </summary>
public sealed class RatingsTable
{
    public sealed class Row
    {
        [JsonPropertyName("rank")] public int Rank { get; set; }
        [JsonPropertyName("name")] public string Name { get; set; } = "";
        [JsonPropertyName("ufer_rating")] public double? UferRating { get; set; }
        [JsonPropertyName("ufer_title")] public string? UferTitle { get; set; }
        [JsonPropertyName("open_rating")] public double? OpenRating { get; set; }
        [JsonPropertyName("open_title")] public string? OpenTitle { get; set; }
        [JsonPropertyName("open_games")] public int OpenGames { get; set; }
        [JsonPropertyName("open_provisional")] public bool OpenProvisional { get; set; }
        [JsonPropertyName("steam_ids")] public List<string> SteamIds { get; set; } = new();

        // One decimal, matching the source spreadsheet — rounding 2099.4 to
        // 2099 makes the numbers look wrong next to the sheet everyone knows.
        // A dash is more honest than an invented value.
        public string UferText => UferRating is double u ? $"{u:0.0}" : "—";
        public string OpenText => OpenRating is double o
            ? $"{o:0.0}" + (OpenProvisional ? "*" : "") : "—";
        public string TitleText => UferTitle ?? OpenTitle ?? "";
        public string GamesText => OpenGames > 0 ? OpenGames.ToString() : "";
        public bool IsMe { get; set; }
    }

    public sealed class FileDto
    {
        [JsonPropertyName("players")] public List<Row> Players { get; set; } = new();
        [JsonPropertyName("events_used")] public int EventsUsed { get; set; }
        [JsonPropertyName("skipped")] public List<string> Skipped { get; set; } = new();
    }

    public List<Row> Players { get; private set; } = new();
    public int EventsUsed { get; private set; }
    public List<string> Skipped { get; private set; } = new();
    public bool Loaded { get; private set; }
    public string Path { get; }

    public RatingsTable(string? path = null)
    {
        Path = path ?? FindPath();
        Reload();
    }

    private static string FindPath()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d is not null; d = d.Parent)
        {
            var cand = System.IO.Path.Combine(d.FullName, "data", "ratings.json");
            if (File.Exists(cand)) return cand;
            if (Directory.Exists(System.IO.Path.Combine(d.FullName, "ladder")))
                return cand;
        }
        return System.IO.Path.Combine(AppContext.BaseDirectory, "data", "ratings.json");
    }

    public void Reload(string? mySteamId = null)
    {
        Loaded = false;
        Players = new List<Row>();
        if (!File.Exists(Path)) return;
        try
        {
            var dto = JsonSerializer.Deserialize<FileDto>(File.ReadAllText(Path));
            if (dto is null) return;
            Players = dto.Players;
            EventsUsed = dto.EventsUsed;
            Skipped = dto.Skipped;
            Loaded = true;
        }
        catch (Exception) { /* broken file = no table, not a crash */ }
        MarkSelf(mySteamId);
    }

    public void MarkSelf(string? mySteamId)
    {
        foreach (var r in Players)
            r.IsMe = mySteamId is not null && r.SteamIds.Contains(mySteamId);
    }

    public Row? Me => Players.FirstOrDefault(r => r.IsMe);

    public IEnumerable<Row> Search(string? query) =>
        string.IsNullOrWhiteSpace(query)
            ? Players
            : Players.Where(r => r.Name.Contains(query.Trim(),
                StringComparison.OrdinalIgnoreCase));
}
