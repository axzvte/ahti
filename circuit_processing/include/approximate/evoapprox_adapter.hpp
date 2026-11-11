#pragma once

namespace approximate
{

enum class ApproxUnitId
{
    Add12se5L8,
    Add12se5PD,
    Add12se5PN,
    Add12se5QC,
    Add12se5QT,
    Add12se5RP,
    Add12se5TE,
    Add12se5SB,
    Add12se5Z0
};

int addSigned12(
    int a,
    int b,
    ApproxUnitId unit
);

}