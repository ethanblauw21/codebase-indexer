// Feature: a namespaced class with a constructor, a member field (NOT a symbol per the
// ADR-008 symbol model), and methods that call each other within the class body.
namespace shop {

class Order {
public:
    Order(int id) : id_(id) {}

    int Id() const { return id_; }

    double Total() {
        return computeTotal();
    }

private:
    double computeTotal() {
        return 42.0;
    }

    int id_;
};

}  // namespace shop
