"""Certified RMS bound for a complete output tile with a floating linear tail."""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from circuit_ir import _validate_structure


LINEAR = {'add', 'sub', 'scale'}
MAX_COEFFICIENT_CELLS = 250_000
MAX_GRAM_PRODUCTS = 8_000_000
MAX_COEFFICIENT_BITS = 4096


def _up(value):
    return math.nextafter(value, math.inf) if value else 0.0


def _sqrt_up(value):
    if not value:
        return 0.0
    # Direct the conversion upward even when a positive rational underflows.
    return _up(math.sqrt(math.nextafter(float(value), math.inf)))


def _gram_bound(rows, frontier):
    """Bound the largest squared singular value using exact dyadic products."""
    if not frontier:
        return Fraction(0)
    small, large = sorted((len(rows), len(frontier)))
    if small * small * large > MAX_GRAM_PRODUCTS:
        return None
    denominator = max((value.denominator for row in rows for value in row.values()), default=1)
    dense = [[int(row.get(name, 0) * denominator) for name in frontier] for row in rows]
    vectors = dense if len(rows) <= len(frontier) else list(zip(*dense))
    # Both choices, A A^T and A^T A, have the same largest eigenvalue.
    row_sums = [0] * len(vectors)
    for i, left in enumerate(vectors):
        for j in range(i + 1):
            entry = abs(sum(a * b for a, b in zip(left, vectors[j])))
            row_sums[i] += entry
            if i != j:
                row_sums[j] += entry
    return Fraction(max(row_sums), denominator**2)


def _linear(op):
    return op is not None and not op['integer'] and op['op'] in LINEAR


def _value_bounds(state, name):
    lo, hi, error, _ = state[name]
    if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi
            and math.isfinite(error) and error >= 0):
        raise ValueError('Invalid analysis bounds')
    return lo, hi, error


def _range_preserved(op, state):
    """Check that the analyzer did not apply an integer-site flip here."""
    arg = op['args'][0]
    lo, hi = state[arg][:2] if isinstance(arg, str) else (arg, arg)
    if op['op'] == 'round_even':
        lo, hi = round(lo), round(hi)
    elif op['op'] == 'clamp':
        low, high = op['min'], op['max']
        lo, hi = max(low, min(high, lo)), max(low, min(high, hi))
    elif op['op'] == 'trunc':
        lo, hi = math.trunc(lo), math.trunc(hi)
    return state[op['id']][:2] == (lo, hi)


@dataclass(frozen=True)
class _Certificate:
    roots: tuple
    frontier: tuple
    tail: tuple
    wrappers: tuple
    gram_bound: Fraction

    def rms_bound(self, state):
        """Bound RMS against the all-exact graph from successful analysis state."""
        try:
            frontier_errors = {name: _value_bounds(state, name)[2] for name in self.frontier}
            for op in self.wrappers:
                _value_bounds(state, op['id'])
                if op['integer'] and not _range_preserved(op, state):
                    return None

            # At each affected arithmetic operation, bound the sum of the two
            # local rounding errors using |candidate| and |candidate| + error.
            # A binary64 operation differs from its exact result by at most
            # 2^-52 * max(1, |rounded result|), including subnormal values.
            residual = {name: 0.0 for name in self.frontier}
            affected = {name for name, error in frontier_errors.items() if error}
            for name, inputs in self.tail:
                lo, hi, error = _value_bounds(state, name)
                if not any(arg in affected for arg, _ in inputs):
                    residual[name] = 0.0
                    continue
                affected.add(name)
                candidate = max(abs(lo), abs(hi), 1)
                reference = _up(candidate + error)
                allowance = math.ldexp(_up(candidate + reference), -52)
                for arg, gain in inputs:
                    if residual[arg] and gain:
                        propagated = math.nextafter(residual[arg] * gain, math.inf)
                        allowance = _up(allowance + propagated)
                residual[name] = allowance

            count = len(self.roots)
            frontier_energy = sum(Fraction(error)**2 for error in frontier_errors.values())
            linear_rms = _sqrt_up(self.gram_bound * frontier_energy / count)
            # Clamp is nonexpansive. A pair of nearest-even conversions can
            # add at most one to the difference. A subsequent byte conversion
            # of an already rounded value through clamps is an integer clamp.
            rounding_energy = sum((Fraction(residual[root]) + 1)**2
                                  for root in self.roots if root in affected)
            total = _up(linear_rms + _sqrt_up(rounding_energy / count))
            return min(255.0, total) if math.isfinite(total) else None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


def prepare(profile):
    """Prepare a complete-tile certificate or return None for other graphs."""
    try:
        _validate_structure(profile)
        return _prepare(profile['ir'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _prepare(ir):
    operations = {op['id']: op for op in ir['operations']}
    whole = {item['id'] for item in ir['inputs']}
    for name, op in operations.items():
        kind = op['op']
        arg = op['args'][0]
        arg_whole = arg in whole if isinstance(arg, str) else float(arg).is_integer()
        # Encoders may declare a cast or a clamp as floating even when its
        # output is mathematically integral.
        if (op['integer'] or (arg_whole and kind == 'scale' and op['factor'] == 1)
                or (arg_whole and kind == 'clamp' and float(op['min']).is_integer()
                    and float(op['max']).is_integer())):
            whole.add(name)

    roots, wrappers = [], {}
    for output in ir['outputs']:
        name = output['value']
        seen_clamp = seen_round = False
        while name in operations:
            op = operations[name]
            kind, arg = op['op'], op['args'][0]
            if kind == 'clamp' and not seen_clamp and not seen_round:
                seen_clamp = True
            elif kind == 'round_even' and not seen_round:
                seen_round = True
            elif kind == 'scale' and op['factor'] == 1:
                pass
            elif kind == 'trunc' and arg in whole:
                pass
            else:
                break
            wrappers[name] = op
            name = arg
        if not _linear(operations.get(name)):
            return None
        roots.append(name)

    needed, frontier = set(), set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in needed or name in frontier:
            continue
        op = operations.get(name)
        if _linear(op):
            needed.add(name)
            pending.extend(arg for arg in op['args'] if isinstance(arg, str))
        else:
            frontier.add(name)
    if not frontier <= whole or len(roots) * len(frontier) > MAX_COEFFICIENT_CELLS:
        return None

    coefficients = {name: {name: Fraction(1)} for name in frontier}
    cells = len(frontier)
    tail = []
    for name, op in operations.items():
        if name not in needed:
            continue
        kind = op['op']
        weights = (op['factor'],) if kind == 'scale' else (1, -1 if kind == 'sub' else 1)
        row, inputs = {}, []
        for arg, weight in zip(op['args'], weights):
            if not isinstance(arg, str):
                continue  # An identical literal cancels in the difference.
            inputs.append((arg, abs(weight)))
            exact_weight = Fraction(weight)
            for source, value in coefficients[arg].items():
                value = row.get(source, 0) + value * exact_weight
                if max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_COEFFICIENT_BITS:
                    return None
                row[source] = value
        row = {source: value for source, value in row.items() if value}
        cells += len(row)
        if cells > MAX_COEFFICIENT_CELLS:
            return None
        coefficients[name] = row
        tail.append((name, tuple(inputs)))
    ordered_frontier = tuple(sorted(frontier))
    gram_bound = _gram_bound([coefficients[root] for root in roots], ordered_frontier)
    if gram_bound is None:
        return None
    return _Certificate(tuple(roots), ordered_frontier, tuple(tail), tuple(wrappers.values()), gram_bound)


def signed_response(profile, site):
    """Nominal signed output vector for a unit perturbation at an internal site."""
    ir = profile['ir']
    response = {}
    for op in ir['operations']:
        name, kind, args = op['id'], op['op'], op['args']
        value = response.get(args[0], 0.0)
        if kind == 'add':
            value += response.get(args[1], 0.0)
        elif kind == 'sub':
            value -= response.get(args[1], 0.0)
        elif kind == 'scale':
            value *= op['factor']
        elif kind in ('shift', 'round_shift'):
            value = math.ldexp(value, -op['bits'])
        # Treat quantization, clipping and absolute value as transparent for ranking.
        if name == site:
            value += 1
        if value:
            response[name] = value
    return tuple(response.get(out['value'], 0.0) for out in ir['outputs'])


def correlated_flip_rms(profile, replacements, unit_wce, flip, state):
    """Bound tile RMS via e = sign*coefficient + residual, with one common XOR sign."""
    ir = profile['ir']
    if len(ir['operations']) > MAX_COEFFICIENT_CELLS:
        return None
    units = {item['node']: item['unit'] for item in profile['baseline']} | replacements
    errors = {}
    zero = Fraction(0)
    empty = zero, zero
    for op in ir['operations']:
        name, kind, args = op['id'], op['op'], op['args']
        center, radius = errors.get(args[0], empty)
        if kind in ('add', 'sub'):
            other_center, other_radius = errors.get(args[1], empty)
            center += other_center if kind == 'add' else -other_center
            radius += other_radius
            radius += unit_wce[units[op['unit_node']]] if 'unit_node' in op else 0
        elif kind == 'scale':
            gain = Fraction(op['factor'])
            center *= gain
            radius *= abs(gain)
        elif kind in ('shift', 'round_shift', 'round_even', 'trunc'):
            if center or radius:
                divisor = 1 << op.get('bits', 0)
                center /= divisor
                radius = radius/divisor + (2 if kind == 'trunc' else 1)
        elif kind in ('abs', 'clamp'):
            # Both operations are nonexpansive. Drop the sign relation when
            # an input-dependent slope could change it.
            radius += abs(center)
            center = zero
            if kind == 'clamp':
                radius = min(radius, Fraction(op['max']) - Fraction(op['min']))
        else:
            return None
        if not op['integer'] and (center or radius):
            lo, hi, old_error = _value_bounds(state, name)
            magnitude = Fraction(max(1, abs(lo), abs(hi)))
            # Bound both binary64 roundoffs, using the old envelope for the
            # exact result's magnitude. Dyadic arithmetic stays exact here.
            radius += (2*magnitude + Fraction(old_error)) / (1 << 50)
        if name == flip[0]:
            center += 1 << flip[1]
        if any(value.denominator.bit_length() > MAX_COEFFICIENT_BITS for value in (center, radius)):
            return None
        if center or radius:
            errors[name] = (center, radius)

    squared = zero
    for output in ir['outputs']:
        value = output['value']
        center, radius = errors.get(value, empty)
        magnitude = abs(center) + radius
        if magnitude and not state[value][3]:
            magnitude += 1  # Final rounding to a byte.
        squared += min(255, magnitude)**2
    return _sqrt_up(squared / len(ir['outputs']))
