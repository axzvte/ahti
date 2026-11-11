#include "circuit_intrinsics.h"

/*
 * One level of separable CDF 9/7 on each 8x8 tile, with whole-sample
 * symmetric extension. Even and odd samples stay interleaved; at the
 * right edge the last even neighbor is mirrored, and at the left edge
 * the first odd neighbor is mirrored.
 *
 * Alpha, beta, gamma, and delta are the CDF 9/7 lifting coefficients
 * rounded to signed Q15. Each lift rounds its product halfway away from
 * zero to the integer sample domain.
 */
const int dwt97_lift_q15[4] = {-51974, -1736, 28931, 14533};

int dwt97_predict_neighbor(int lane) {
    return lane == 3 ? 6 : 2*lane + 2;
}

int dwt97_update_neighbor(int lane) {
    return lane == 0 ? 1 : 2*lane - 1;
}

int dwt97_delta(int sum, int coefficient) {
    return circuit_round_shift((long long)sum * coefficient, 15);
}

void dwt97_forward_line(int line[8], int horizontal) {
    for (int stage = 0; stage < 4; ++stage) {
        for (int lane = 0; lane < 4; ++lane) {
            int odd = 2*lane + 1;
            int even = 2*lane;
            int target = stage % 2 == 0 ? odd : even;
            int adjacent = stage % 2 == 0 ? even : odd;
            int other = stage % 2 == 0 ? dwt97_predict_neighbor(lane) : dwt97_update_neighbor(lane);
            /* Mark the interior row alpha sum for lane one. Each of its
             * operands is a centered byte and fits the signed 12-bit adder.
             */
            int neighbors;
            if (horizontal && stage == 0 && lane == 1) {
                neighbors = circuit_add(2, line[adjacent], line[other]);
            } else {
                neighbors = line[adjacent] + line[other];
            }
            line[target] = line[target] + dwt97_delta(neighbors, dwt97_lift_q15[stage]);
        }
    }
}

void dwt97_inverse_line(int line[8], int final_row) {
    for (int stage = 3; stage >= final_row; --stage) {
        for (int lane = 0; lane < 4; ++lane) {
            int odd = 2*lane + 1;
            int even = 2*lane;
            int target = stage % 2 == 0 ? odd : even;
            int adjacent = stage % 2 == 0 ? even : odd;
            int other = stage % 2 == 0 ? dwt97_predict_neighbor(lane) : dwt97_update_neighbor(lane);
            int neighbors = line[adjacent] + line[other];
            line[target] = line[target] - dwt97_delta(neighbors, dwt97_lift_q15[stage]);
        }
    }
    if (final_row) {
        /*
         * Bound the final predict to the fixed-point datapath's signed
         * operand ranges. With exact lifts, even samples have already
         * reconstructed to [-128, 127], and odd samples before this
         * stage are within [-534, 534]. These limits leave headroom
         * for perturbed coefficients.
         */
        for (int c = 0; c < 8; ++c) {
            if (c % 2 == 0) {
                line[c] = (int)circuit_clip(line[c], -512, 511);
            } else {
                line[c] = (int)circuit_clip(line[c], -1024, 1023);
            }
        }
        for (int lane = 0; lane < 4; ++lane) {
            int odd = 2*lane + 1;
            int even = 2*lane;
            int neighbors = circuit_add(64 + 2*lane, line[even], line[dwt97_predict_neighbor(lane)]);
            int predict = dwt97_delta(neighbors, dwt97_lift_q15[0]);
            line[odd] = circuit_add(65 + 2*lane, line[odd], -predict);
        }
    }
}

/*
 * In the separable factorization, defer both per-axis normalization
 * factors until after the two lifting passes. The Q15 products K^2, 1,
 * and 1/K^2 correspond to LL, LH/HL, and HH for K = 1.149604398.
 * Store normalized coefficients with one fractional bit. The inverse
 * uses Q20 reciprocals of the actual Q15 factors so that rounding back
 * to the integer lifting domain recovers the original coefficient.
 */
const int dwt97_band_q15[3] = {43306, 32768, 24794};
const int dwt97_reciprocal_q20[3] = {793418, 1048576, 1385809};

void dwt97_kernel(int input[64], double output[64]) {
    int rows[64], coefficients[64], line[8];
    for (int r = 0; r < 8; ++r) {
        for (int c = 0; c < 8; ++c) line[c] = input[8*r+c] - 128;
        dwt97_forward_line(line, 1);
        for (int c = 0; c < 8; ++c) rows[8*r+c] = line[c];
    }
    for (int c = 0; c < 8; ++c) {
        for (int r = 0; r < 8; ++r) line[r] = rows[8*r+c];
        dwt97_forward_line(line, 0);
        for (int r = 0; r < 8; ++r) {
            int band = r % 2 + c % 2;
            coefficients[8*r+c] = circuit_round_shift((long long)line[r] * dwt97_band_q15[band], 14);
        }
    }
    for (int c = 0; c < 8; ++c) {
        for (int r = 0; r < 8; ++r) {
            int band = r % 2 + c % 2;
            line[r] = circuit_round_shift((long long)coefficients[8*r+c] * dwt97_reciprocal_q20[band], 21);
        }
        dwt97_inverse_line(line, 0);
        for (int r = 0; r < 8; ++r) rows[8*r+c] = line[r];
    }
    for (int r = 0; r < 8; ++r) {
        for (int c = 0; c < 8; ++c) line[c] = rows[8*r+c];
        dwt97_inverse_line(line, 1);
        for (int c = 0; c < 8; ++c) output[8*r+c] = circuit_clip(line[c] + 128, 0, 255);
    }
}
