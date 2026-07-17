// Feature: namespace-qualified free functions, including a nested namespace, with a
// call from one free function into another (both live in the same namespace, so the
// call site uses an unqualified name).
namespace net {

int square(int x) {
    return x * x;
}

int sumOfSquares(int a, int b) {
    return square(a) + square(b);
}

namespace util {

int triple(int x) {
    return x * 3;
}

}  // namespace util

}  // namespace net
