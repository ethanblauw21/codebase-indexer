#include <string>
#include <vector>
#include "core/config.h"

namespace Core {

enum class Status { Pending, Running, Done, Failed };

struct Config {
    int maxRetries;
    float timeout;
};

typedef unsigned int uint32;
using StringVec = std::vector<std::string>;

class Logger {
public:
    explicit Logger(int level);
    void log(const std::string& msg);
    int level() const;

private:
    int _level;
};

template<typename T>
class Cache {
public:
    explicit Cache(int capacity);
    void put(const std::string& key, const T& value);
    bool get(const std::string& key, T& out) const;
    void clear();

private:
    int _capacity;
};

class DataService : public Logger {
public:
    explicit DataService(const Config& cfg);
    std::string fetch(int id);
    std::string fetch(int id, bool force);
    int count() const;

private:
    Config _cfg;
};

std::string formatStatus(Status s);

}  // namespace Core
