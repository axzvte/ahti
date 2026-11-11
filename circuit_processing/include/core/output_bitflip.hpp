#pragma once

#include <cstdint>
#include <stdexcept>

namespace core {

// External payload on the signed 13-bit output; the FU itself is unchanged.
inline int flipNonSignOutputBit(int output, int bitIndex) {
    if (bitIndex < 0 || bitIndex >= 12)
        throw std::runtime_error("Bit flip requires a non-sign bit in [0, 11].");
    if (output < -4096 || output > 4095)
        throw std::runtime_error("Bit-flip input exceeds signed 13-bit output range.");
    const std::uint32_t raw = (static_cast<std::uint32_t>(output) & 0x1FFFu)
                             ^ (std::uint32_t{1} << bitIndex);
    return (raw & 0x1000u) ? static_cast<int>(raw) - 8192 : static_cast<int>(raw);
}

}
