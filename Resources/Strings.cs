using System.Globalization;
using System.Resources;

namespace BedrockCapeSync.Resources;

public static class Strings
{
    private static readonly ResourceManager ResourceManager =
        new("BedrockCapeSync.Resources.Strings", typeof(Strings).Assembly);

    private static CultureInfo _currentCulture =
        CultureInfo.CurrentUICulture.TwoLetterISOLanguageName.Equals("ru", StringComparison.OrdinalIgnoreCase)
            ? new CultureInfo("ru")
            : new CultureInfo("en");

    public static CultureInfo CurrentCulture
    {
        get => _currentCulture;
        set
        {
            _currentCulture = value;
            CultureInfo.CurrentUICulture = value;
            CultureInfo.CurrentCulture = value;
        }
    }

    public static string Get(string key)
    {
        try
        {
            string? value = ResourceManager.GetString(key, _currentCulture);
            if (!string.IsNullOrEmpty(value))
            {
                return value;
            }
        }
        catch
        {
            // Fallback
        }

        return key;
    }

    public static string Format(string key, params object[] args)
    {
        string pattern = Get(key);
        try
        {
            return string.Format(_currentCulture, pattern, args);
        }
        catch
        {
            return pattern;
        }
    }
}
