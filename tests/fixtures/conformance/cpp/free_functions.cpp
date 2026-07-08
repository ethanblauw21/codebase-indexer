// Feature: free-function overloads. C++ FQNs carry full parameter types, so
// log(int) and log(const std::string&) are DISTINCT symbols — unlike a C#-style
// arity-only FQN, which would collapse same-arity overloads into a single key.
#include <string>

namespace diag {

void log(int code) {
}

void log(const std::string& message) {
}

void reportError(int code) {
    log(code);
    log("boom");
}

}  // namespace diag
