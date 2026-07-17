// ADR-011 §4 resolution fixture (C++): mirrors the C# case — several classes share a method
// NAME, so only the receiver's TYPE disambiguates the call. Receiver reached via -> and .
// Ground truth in the sibling .resolution.json is authored from the source semantics.
namespace shop {

class OrderRepo {
public:
    void Save();   // shared name
    void Load();   // UNIQUE name: only OrderRepo defines Load
};

class UserRepo {
public:
    void Save();   // shared name: collides with OrderRepo::Save
};

class Service {
    OrderRepo* orders_;
public:
    // ── The lift: ambiguous name, disambiguated only by the receiver type ──
    void saveViaField()            { orders_->Save(); }        // → OrderRepo::Save
    void saveViaParam(UserRepo* u) { u->Save(); }              // → UserRepo::Save
    void saveViaLocal()            { OrderRepo o; o.Save(); }  // → OrderRepo::Save

    // ── Control: a UNIQUE name resolves with or without the hint ──
    void loadUnique()              { orders_->Load(); }        // → OrderRepo::Load

    // ── Prefer-unknown (§2): must stay unresolved, in BOTH regimes ──
    void chainedReceiver()         { get()->Save(); }          // chained receiver → null

    OrderRepo* get();
};

}  // namespace shop
