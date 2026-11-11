#ifndef CIRCUIT_INTRINSICS_H
#define CIRCUIT_INTRINSICS_H
#include <math.h>
/* These functions are exact reference semantics. The encoder recognizes names. */
static inline int circuit_add(int id, int a, int b) { (void)id; return a+b; }
static inline int circuit_sub(int id, int a, int b) { (void)id; return a-b; }
static inline int circuit_abs(int x) { return x < 0 ? -x : x; }
static inline double circuit_clip(double x, double lo, double hi) { return x<lo?lo:x>hi?hi:x; }
static inline long long circuit_round_shift(long long x, int bits) {
    if (!bits) return x;
    long long half=1LL<<(bits-1);
    return x>=0 ? (x+half)>>bits : -((-x+half)>>bits);
}
static inline double circuit_round_even(double x) {
    double lo=floor(x), rem=x-lo;
    return rem<0.5?lo:rem>0.5?lo+1:fmod(lo,2.0)==0?lo:lo+1;
}
static inline double circuit_trunc(double x) { return trunc(x); }
#endif
