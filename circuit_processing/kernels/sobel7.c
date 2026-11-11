#include "circuit_intrinsics.h"

/*
 * Apply [-1, -4, -5, 0, 5, 4, 1] along one axis of the row-major window.
 * Each raw paired difference has its own group and signed 12-bit operands.
 */
static int derivative(int input[49], int base, int stride, int group) {
    int outer = circuit_add(group, input[base + 6 * stride], -input[base]);
    int middle = circuit_add(group + 1, input[base + 5 * stride], -input[base + stride]);
    int inner = circuit_add(group + 2, input[base + 4 * stride], -input[base + 2 * stride]);
    return outer + 4 * middle + 5 * inner;
}

/*
 * Smooth the directional responses across the orthogonal axis. Scale the L1
 * magnitude by 512 with nearest rounding, then saturate to the pixel range.
 * The wrapper writes zero at the outer three pixels on each side.
 */
void sobel7_kernel(int input[49], double output[1]) {
    const int weights[7] = {1, 6, 15, 20, 15, 6, 1};
    int gx = 0;
    int gy = 0;
    for (int i = 0; i < 7; ++i) {
        gx = gx + weights[i] * derivative(input, 7 * i, 1, 3 * i);
        gy = gy + weights[i] * derivative(input, i, 7, 21 + 3 * i);
    }
    int magnitude = circuit_abs(gx) + circuit_abs(gy);
    output[0] = circuit_clip(circuit_round_shift(magnitude, 9), 0, 255);
}
