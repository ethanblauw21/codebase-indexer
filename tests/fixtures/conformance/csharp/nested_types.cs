// Feature: a nested type. The inner type's FQN uses the `+` separator (Outer+Inner, CLR
// convention). Methods are owned by their immediate enclosing type; the outer type is not
// recorded as owning the nested type. Intra-type call edge (Compute -> Seed); `new Inner()`
// is object creation, not a call.
namespace Widgets
{
    public class Outer
    {
        public class Inner
        {
            public int Compute()
            {
                return Seed();
            }

            private int Seed()
            {
                return 42;
            }
        }

        public Inner Make()
        {
            return new Inner();
        }
    }
}
