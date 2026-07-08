// Feature: base/derived classes with a virtual method overridden in the derived class.
// `class Circle : public Shape` produces an `extends` edge. C++ has no `implements`
// keyword distinct from base classes, so every base-list entry (interface or not)
// is correctly emitted as `extends` by this adapter.
namespace shapes {

class Shape {
public:
    virtual double area() const {
        return 0.0;
    }

    virtual ~Shape() {}
};

class Circle : public Shape {
public:
    Circle(double r) : radius_(r) {}

    double area() const override {
        return radius_ * radius_ * 3.14159;
    }

private:
    double radius_;
};

}  // namespace shapes
