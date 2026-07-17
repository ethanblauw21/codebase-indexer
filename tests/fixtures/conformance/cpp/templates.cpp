// Feature: a template class and a template function. Per ADR-003 §2.3 / the adapter
// docstring, template DEFINITIONS index fine, but template INSTANTIATIONS are
// invisible — an explicit-template-argument call site like `maxOf<int>(...)` is
// parsed as a `template_function` call target, which the call query does not match,
// so the call edge back to the template is silently dropped.
namespace util {

template <typename T>
class Box {
public:
    Box(T value) : value_(value) {}

    T get() const { return value_; }

private:
    T value_;
};

template <typename T>
T maxOf(T a, T b) {
    return a > b ? a : b;
}

int useTemplates() {
    Box<int> box(5);
    return maxOf<int>(box.get(), 10);
}

}  // namespace util
