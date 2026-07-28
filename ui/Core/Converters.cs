using System.Globalization;
using System.Windows;
using System.Windows.Data;

namespace FortsLadder.Core;

/// <summary>
/// True becomes Visible, anything else Collapsed.
///
/// WPF ships no converter for this, and a DataTemplate cannot decide visibility
/// from a bool without one — the alternative is a second property on every view
/// model whose only job is to be a Visibility.
/// </summary>
public sealed class BoolToVisibleConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter,
                          CultureInfo culture) =>
        value is true ? Visibility.Visible : Visibility.Collapsed;

    public object ConvertBack(object? value, Type targetType, object? parameter,
                             CultureInfo culture) =>
        value is Visibility.Visible;
}
