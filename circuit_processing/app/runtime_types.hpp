#pragma once
#include "approximate/evoapprox_adapter.hpp"
#include "core/output_bitflip.hpp"
#include <opencv2/core.hpp>
#include <algorithm>
#include <cstdint>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace circuit_profile {
inline void demand(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error("Circuit profile: " + message);
}
inline std::string word(const cv::FileNode& node) {
    demand(node.isString(), "expected string");
    return static_cast<std::string>(node);
}
struct BaselineNode {
    bool exact = true;
    approximate::ApproxUnitId unit = approximate::ApproxUnitId::Add12se5RP;
};
inline BaselineNode unit(const std::string& name) {
    using U = approximate::ApproxUnitId;
    static const std::map<std::string,U> units{{"5RP",U::Add12se5RP},{"5QC",U::Add12se5QC},
        {"5L8",U::Add12se5L8},{"5QT",U::Add12se5QT},{"5TE",U::Add12se5TE},
        {"5PD",U::Add12se5PD},{"5PN",U::Add12se5PN},{"5SB",U::Add12se5SB},{"5Z0",U::Add12se5Z0}};
    if (name == "exact") return {};
    const auto it = units.find(name);
    demand(it != units.end(), "unknown arithmetic unit: " + name);
    return {false,it->second};
}
}
