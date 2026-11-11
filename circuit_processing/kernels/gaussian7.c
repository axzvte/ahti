#include "circuit_intrinsics.h"

/*
 * Separable binomial blur with weights [1, 6, 15, 20, 15, 6, 1] on each axis.
 * Inputs are a row-major 7x7 window. The wrapper retains the outer three pixels
 * on each side. Keep full precision until rounding by 64 * 64.
 */
void gaussian7_kernel(int input[49], double output[1]) {
    const int weights[7] = {1, 6, 15, 20, 15, 6, 1};
    int sum = 0;
    for (int row = 0; row < 7; ++row) {
        int base = 7 * row;
        // Mark each mirrored pair separately. Both operands are in [0, 255].
        int outer = circuit_add(3 * row, input[base], input[base + 6]);
        int middle = circuit_add(3 * row + 1, input[base + 1], input[base + 5]);
        int inner = circuit_add(3 * row + 2, input[base + 2], input[base + 4]);
        int horizontal = outer + 6 * middle + 15 * inner + 20 * input[base + 3];
        sum = sum + weights[row] * horizontal;
    }
    output[0] = circuit_clip(circuit_round_shift(sum, 12), 0, 255);
}
