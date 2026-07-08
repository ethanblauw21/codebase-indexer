// Feature: single base class + interface implementation. `Dog : Animal, IBark` — the
// first base-list entry (Animal) is a class → extends; the rest (IBark) are interfaces
// → implements. Also exercises an abstract method, an interface method, an override, and
// a call from an override into a sibling method.
using System;

namespace Zoo
{
    public interface IBark
    {
        string Bark();
    }

    public abstract class Animal
    {
        public abstract string Speak();
    }

    public class Dog : Animal, IBark
    {
        public override string Speak()
        {
            return Bark();
        }

        public string Bark()
        {
            return "woof";
        }
    }
}
