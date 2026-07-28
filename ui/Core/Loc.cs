using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows.Markup;

namespace FortsLadder.Core;

/// <summary>
/// Translations. English is the source language, everything else is a catalog.
///
/// Forts ships in eighteen languages and its community is spread across all
/// of them, so a German-only tool would be unusable for most of the people
/// it is meant for. English is therefore not a translation here — it is the
/// source, and the keys are named after meaning rather than after the German
/// sentence they replaced.
///
/// A missing key falls back to English, and a missing English entry falls
/// back to the key itself. Both are visible in the UI rather than crashing:
/// a screen that shows `draft.your_turn` is annoying, a screen that throws is
/// unusable, and an untranslated string nobody notices is worst of all.
///
/// Catalogs live in `Locales/*.json` next to the executable. Adding a
/// language means dropping in a file — no rebuild, no code change.
/// </summary>
public static class Loc
{
    private static Dictionary<string, string> _current = new();
    private static Dictionary<string, string> _fallback = new();

    public static string Language { get; private set; } = "en";

    /// <summary>Languages available, from the executable or from disk.</summary>
    public static IReadOnlyList<string> Available()
    {
        var langs = new HashSet<string>(Embedded());
        var dir = CatalogDir();
        if (Directory.Exists(dir))
        {
            foreach (var f in Directory.GetFiles(dir, "*.json"))
                if (Path.GetFileNameWithoutExtension(f) is { Length: > 0 } n)
                    langs.Add(n);
        }
        return langs.Count == 0 ? new[] { "en" } : langs.OrderBy(x => x).ToList();
    }

    private static string CatalogDir() =>
        Path.Combine(AppContext.BaseDirectory, "Locales");

    private const string ResourcePrefix = "FortsLadder.Locales.";

    private static IEnumerable<string> Embedded() =>
        typeof(Loc).Assembly.GetManifestResourceNames()
            .Where(n => n.StartsWith(ResourcePrefix, StringComparison.Ordinal)
                        && n.EndsWith(".json", StringComparison.Ordinal))
            .Select(n => n[ResourcePrefix.Length..^".json".Length]);

    /// <summary>
    /// Load one catalog. Baked into the executable, overridable on disk.
    ///
    /// Embedded because the release ships a single file — a catalog that only
    /// existed next to the exe would be missing for everyone who downloads it,
    /// and the whole UI would render as raw keys like `draft.headline`.
    ///
    /// Disk wins where present, so adding or correcting a language still means
    /// dropping in a file next to the exe, with no rebuild.
    /// </summary>
    private static Dictionary<string, string> Read(string lang)
    {
        var path = Path.Combine(CatalogDir(), lang + ".json");
        if (File.Exists(path))
        {
            try { return Parse(File.ReadAllText(path)); }
            catch (Exception) { /* fall through to the embedded copy */ }
        }
        try
        {
            using var s = typeof(Loc).Assembly
                .GetManifestResourceStream(ResourcePrefix + lang + ".json");
            if (s is null) return new Dictionary<string, string>();
            using var r = new StreamReader(s);
            return Parse(r.ReadToEnd());
        }
        catch (Exception)
        {
            // A broken catalog must not stop the program — it falls back to
            // English, which is always complete by definition.
            return new Dictionary<string, string>();
        }
    }

    private static Dictionary<string, string> Parse(string json) =>
        JsonSerializer.Deserialize<Dictionary<string, string>>(json)
        ?? new Dictionary<string, string>();

    /// <summary>
    /// Pick a language. Without an argument the system language is used, and
    /// English if there is no catalog for it.
    /// </summary>
    public static void Init(string? lang = null)
    {
        _fallback = Read("en");
        lang ??= CultureInfo.CurrentUICulture.TwoLetterISOLanguageName;
        var catalog = Read(lang);
        if (catalog.Count == 0 && lang != "en")
        {
            lang = "en";
            catalog = _fallback;
        }
        Language = lang;
        _current = catalog;
    }

    public static string T(string key)
    {
        if (_current.TryGetValue(key, out var s)) return s;
        if (_fallback.TryGetValue(key, out var e)) return e;
        return key;
    }

    /// <summary>`T("x.y", 3)` fills `{0}`, `{1}`, … in the catalog entry.</summary>
    public static string T(string key, params object[] args)
    {
        var pattern = T(key);
        try
        {
            return string.Format(CultureInfo.CurrentCulture, pattern, args);
        }
        catch (FormatException)
        {
            // A translator mistyped a placeholder. Show the raw pattern
            // rather than crash on someone else's typo.
            return pattern;
        }
    }
}

/// <summary>
/// XAML side: <c>Text="{core:T draft.headline}"</c>.
///
/// Resolved when the window is parsed, so a language change needs a restart.
/// That is a deliberate trade: live switching would mean every label becomes
/// a binding with change notification, which is a lot of machinery for
/// something people do once.
/// </summary>
[MarkupExtensionReturnType(typeof(string))]
public sealed class TExtension : MarkupExtension
{
    public string Key { get; set; } = "";

    public TExtension() { }
    public TExtension(string key) => Key = key;

    public override object ProvideValue(IServiceProvider serviceProvider) =>
        Loc.T(Key);
}
