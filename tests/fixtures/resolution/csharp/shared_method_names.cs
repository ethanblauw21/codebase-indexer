// ADR-011 §4 resolution fixture (C#): several classes share a method NAME, so a bare-name
// resolver (ADR-021) cannot settle the call — only the receiver's TYPE says which target.
// This is the exact case receiver-type inference exists for. Ground truth in the sibling
// .resolution.json is authored from the source semantics, never copied from resolver output.
namespace Shop
{
    public class OrderRepo
    {
        public void Save() { }   // shared name: OrderRepo also defines Save
        public void Load() { }   // UNIQUE name: only OrderRepo defines Load
    }

    public class UserRepo
    {
        public void Save() { }   // shared name: collides with OrderRepo.Save
    }

    public class Service
    {
        private OrderRepo _orders;

        // ── The lift: ambiguous name, disambiguated only by the receiver type ──
        public void SaveViaField()          { _orders.Save(); }               // → OrderRepo.Save
        public void SaveViaParam(UserRepo u){ u.Save(); }                     // → UserRepo.Save
        public void SaveViaNew()            { var o = new OrderRepo(); o.Save(); }  // → OrderRepo.Save

        // ── Control: a UNIQUE name resolves with or without the hint (no lift, no regression) ──
        public void LoadUnique()            { _orders.Load(); }               // → OrderRepo.Load

        // ── Prefer-unknown (§2): must stay unresolved, in BOTH regimes ──
        public void ExternalReceiver(System.Text.StringBuilder sb) { sb.Save(); }  // external type → null
        public void ChainedReceiver()       { GetOrders().Save(); }          // chained receiver → null

        public OrderRepo GetOrders() { return _orders; }
    }
}
