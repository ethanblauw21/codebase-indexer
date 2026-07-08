// Feature: a class with a constructor, an auto-property, and methods that call each
// other — the bread-and-butter C# type. Exercises symbols (class/constructor/property/
// method), owns edges, and an intra-class call edge.
using System;

namespace Shop.Billing
{
    public class Invoice
    {
        public Invoice(string customer)
        {
            Customer = customer;
        }

        public string Customer { get; set; }

        public decimal Total(decimal rate)
        {
            return Apply(rate);
        }

        private decimal Apply(decimal rate)
        {
            return rate;
        }
    }
}
