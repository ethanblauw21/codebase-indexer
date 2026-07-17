// Feature: the C#-specific type kinds — enum, struct, record — each surfaced with its own
// symbol kind. Enum members (X/Y/Z) are values, not members of the symbol model, so they
// are not symbols. Struct has properties + a method that calls a private static helper.
namespace Geo
{
    public enum Axis { X, Y, Z }

    public readonly struct Point
    {
        public double X { get; }
        public double Y { get; }

        public double Magnitude()
        {
            return Dot(this);
        }

        private static double Dot(Point p)
        {
            return p.X;
        }
    }

    public record Label
    {
        public string Text { get; init; }
    }
}
