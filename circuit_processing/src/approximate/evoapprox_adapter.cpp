#include "approximate/evoapprox_adapter.hpp"
#include "approximate/evoapprox_units.hpp"

#include <cstdint>
#include <stdexcept>

namespace approximate
{

namespace
{

std::uint64_t encodeSigned12(int value)
{
    if (value < -2048 || value > 2047)
    {
        throw std::runtime_error(
            "Value exceeds signed 12-bit range."
        );
    }

    return static_cast<std::uint64_t>(value)
        & 0xFFFULL;
}

int decodeSigned13(std::uint64_t rawValue)
{
    rawValue &= 0x1FFFULL;

    if ((rawValue & 0x1000ULL) != 0)
    {
        return static_cast<int>(rawValue) - 0x2000;
    }

    return static_cast<int>(rawValue);
}

}

int addSigned12(
    int a,
    int b,
    ApproxUnitId unit
)
{
    const std::uint64_t rawA =
        encodeSigned12(a);

    const std::uint64_t rawB =
        encodeSigned12(b);

    std::uint64_t rawResult = 0;

    switch (unit)
    {
        case ApproxUnitId::Add12se5L8:
            rawResult = add12se_5L8(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5PD:
            rawResult = add12se_5PD(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5PN:
            rawResult = add12se_5PN(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5QC:
            rawResult = add12se_5QC(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5QT:
            rawResult = add12se_5QT(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5RP:
            rawResult = add12se_5RP(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5TE:
            rawResult = add12se_5TE(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5SB:
            rawResult = add12se_5SB(rawB, rawA);
            break;

        case ApproxUnitId::Add12se5Z0:
            rawResult = add12se_5Z0(rawB, rawA);
            break;
        
        default:
            throw std::runtime_error(
                "Unknown approximate unit."
            );
    }

    return decodeSigned13(rawResult);
}

}