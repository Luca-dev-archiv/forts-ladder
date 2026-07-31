using Draw = System.Drawing;
using Forms = System.Windows.Forms;

namespace FortsLadder.Core;

/// <summary>
/// The tray menu, in the same colours as everything else.
///
/// A WinForms <c>ContextMenuStrip</c> arrives light grey with a blue highlight and
/// a 3D border. Beside a dark window with its own title bar it is the one part of
/// the program that looks like it came from a different decade — and it matters
/// more than it sounds, because once the window is closed this menu is the entire
/// interface.
///
/// The palette is the application's, copied as plain numbers rather than read out
/// of the WPF resources: this runs on the WinForms side, which knows nothing about
/// a ResourceDictionary, and two of them agreeing by construction is worth more
/// than one lookup saved. If the theme changes, both change here and in App.xaml.
/// </summary>
public sealed class DarkMenu : Forms.ProfessionalColorTable
{
    // Straight from App.xaml: BgCard, BgHover, Stroke, Accent, TextHi.
    private static readonly Draw.Color Card = Draw.Color.FromArgb(30, 34, 43);
    private static readonly Draw.Color Hover = Draw.Color.FromArgb(38, 43, 54);
    private static readonly Draw.Color Line = Draw.Color.FromArgb(42, 47, 58);
    private static readonly Draw.Color Accent = Draw.Color.FromArgb(255, 107, 44);
    public static readonly Draw.Color Text = Draw.Color.FromArgb(242, 244, 248);

    public override Draw.Color ToolStripDropDownBackground => Card;
    public override Draw.Color ImageMarginGradientBegin => Card;
    public override Draw.Color ImageMarginGradientMiddle => Card;
    public override Draw.Color ImageMarginGradientEnd => Card;
    public override Draw.Color MenuBorder => Line;
    public override Draw.Color MenuItemBorder => Accent;
    public override Draw.Color MenuItemSelected => Hover;
    public override Draw.Color MenuItemSelectedGradientBegin => Hover;
    public override Draw.Color MenuItemSelectedGradientEnd => Hover;
    public override Draw.Color MenuItemPressedGradientBegin => Card;
    public override Draw.Color MenuItemPressedGradientEnd => Card;
    public override Draw.Color SeparatorDark => Line;
    public override Draw.Color SeparatorLight => Line;
    // The tick on a checked item. Left to the default it is a blue Windows
    // check on a white square, which is the thing this class exists to avoid.
    public override Draw.Color CheckBackground => Accent;
    public override Draw.Color CheckSelectedBackground => Accent;
    public override Draw.Color CheckPressedBackground => Accent;

    /// <summary>Apply this palette to a menu, including the parts a colour table
    /// does not reach — the text colour and the little image gutter.</summary>
    public static void Apply(Forms.ContextMenuStrip menu)
    {
        menu.Renderer = new Forms.ToolStripProfessionalRenderer(new DarkMenu())
        {
            // Otherwise the operating system's own visual style paints over the
            // colour table on some builds and the menu is grey again.
            RoundedEdges = false,
        };
        menu.BackColor = Card;
        menu.ForeColor = Text;
        menu.ShowImageMargin = false;
        foreach (Forms.ToolStripItem item in menu.Items)
        {
            item.ForeColor = Text;
            item.BackColor = Card;
        }
    }
}
