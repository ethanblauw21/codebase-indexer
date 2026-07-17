// Feature: a struct, an enum class, and a free function that uses both. Enum members
// (Ok, Error) are NOT symbols — only the enum type itself is, per the ADR-008 symbol
// model (fields/enum-members/events excluded consistently across languages). Struct
// data members (x, y) are likewise not symbols.
namespace net {

struct Point {
    int x;
    int y;
};

enum class Status {
    Ok,
    Error
};

Status checkPoint(const Point& p) {
    if (p.x < 0) {
        return Status::Error;
    }
    return Status::Ok;
}

}  // namespace net
