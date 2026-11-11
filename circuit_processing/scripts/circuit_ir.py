"""Application-independent scalar IR certificates and bounded structural selection.

Intervals deliberately lose correlations. A failed certificate is not evidence of
infeasibility; structural scores are rankings, never observed image effects.
"""
from __future__ import annotations

import math
import re
import sys

LIMIT = 2**40
UNITS = {'exact', '5QC', '5QT', '5L8', '5RP', '5TE', '5PD', '5PN', '5SB', '5Z0'}
OPS = {'add': 2, 'sub': 2, 'scale': 1, 'abs': 1, 'shift': 1,
       'round_shift': 1, 'round_even': 1, 'trunc': 1, 'clamp': 1}


def _int(x, label, low=0, high=2**31-1):
    if type(x) is not int or not low <= x <= high:
        raise ValueError(f'{label} must be an integer in [{low}, {high}]')
    return x


def _number(x):
    return type(x) in (int, float) and math.isfinite(x) and abs(x) <= LIMIT


def _validate_structure(profile):
    if not isinstance(profile, dict) or profile.get('schema') != 3 or profile.get('application') != 'ir':
        raise ValueError('Expected schema 3 scalar IR profile')
    if not isinstance(profile.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', profile['name']):
        raise ValueError('Invalid circuit name')
    ir = profile.get('ir')
    if not isinstance(ir, dict) or ir.get('schema') != 1:
        raise ValueError('Expected scalar IR schema 1')
    tile = ir.get('tile')
    if not isinstance(tile, list) or len(tile) != 2:
        raise ValueError('IR tile must be [height, width]')
    for size in tile:
        _int(size, 'tile dimension', 1, 256)
    if ir.get('border') not in ('replicate', 'reflect101') or type(ir.get('interior_only')) is not bool:
        raise ValueError('Invalid border or interior_only rule')
    if ir.get('border_output', 'input') not in ('input', 'zero'):
        raise ValueError('Invalid border_output rule')
    inputs, operations, outputs = (ir.get(k) for k in ('inputs', 'operations', 'outputs'))
    if not isinstance(inputs, list) or not inputs or not isinstance(operations, list) or not isinstance(outputs, list) or not outputs:
        raise ValueError('IR requires inputs, operations and outputs lists')
    if len(operations) > 100000 or len(inputs) > 100000 or len(outputs) > 65536:
        raise ValueError('IR expansion exceeds supported budget')
    known, integral, groups = set(), {}, set()
    def identifier(value):
        if not isinstance(value, str) or not value or value == '-1' or value in known:
            raise ValueError(f'Invalid or duplicate SSA identifier: {value}')
        known.add(value)
    def operand(value):
        if isinstance(value, str):
            if value not in known:
                raise ValueError(f'Unknown or forward SSA operand: {value}')
            return integral[value]
        if not _number(value):
            raise ValueError('Operands must be prior identifiers or finite bounded numbers')
        return float(value).is_integer()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {'id', 'dx', 'dy'}:
            raise ValueError('Input requires id, dx, dy')
        identifier(item['id'])
        for axis in ('dx', 'dy'):
            _int(item[axis], 'input offset', -1024, 1024)
        integral[item['id']] = True
    for op in operations:
        if not isinstance(op, dict) or op.get('op') not in OPS:
            raise ValueError('Unsupported scalar operation')
        kind, name, args = op['op'], op.get('id'), op.get('args')
        if not isinstance(args, list) or len(args) != OPS[kind]:
            raise ValueError(f'Wrong operand count at {name}')
        arg_integer = [operand(v) for v in args]
        if type(op.get('integer')) is not bool or op.get('width') not in (32, 64) or op.get('signed') is not True:
            raise ValueError(f'Operation requires explicit signed 32/64-bit type: {name}')
        if kind == 'scale' and not _number(op.get('factor')):
            raise ValueError('Invalid scale factor')
        if kind in ('shift', 'round_shift'):
            _int(op.get('bits'), 'shift bits', 0, 63)
            if not arg_integer[0] or not op['integer']:
                raise ValueError('Shifts require integer input and output')
        if kind == 'clamp' and (not _number(op.get('min')) or not _number(op.get('max')) or op['min'] > op['max']):
            raise ValueError('Invalid clamp limits')
        natural_integer = all(arg_integer)
        if kind == 'scale':
            natural_integer &= float(op['factor']).is_integer()
        if kind == 'clamp':
            natural_integer &= float(op['min']).is_integer() and float(op['max']).is_integer()
        if op['integer'] and kind not in ('trunc', 'round_even', 'round_shift') and not natural_integer:
            raise ValueError('Fractional operation needs explicit rounding before integer assignment')
        if kind in ('trunc', 'round_even') and not op['integer']:
            raise ValueError('Rounding operations must have integer result')
        if 'unit_node' in op:
            _int(op['unit_node'], 'unit_node')
            if kind not in ('add', 'sub') or not op['integer']:
                raise ValueError('Only integer add/sub sites can have unit_node')
            groups.add(op['unit_node'])
        identifier(name)
        integral[name] = op['integer']
    seen_outputs = set()
    for item in outputs:
        if not isinstance(item, dict) or set(item) != {'value', 'dx', 'dy'}:
            raise ValueError('Output requires value, dx, dy')
        operand(item['value'])
        _int(item['dx'], 'output dx', 0, tile[1]-1)
        _int(item['dy'], 'output dy', 0, tile[0]-1)
        coord = item['dy'], item['dx']
        if coord in seen_outputs:
            raise ValueError('Duplicate output tile coordinate')
        seen_outputs.add(coord)
    if len(seen_outputs) != tile[0]*tile[1]:
        raise ValueError('Every tile output must be assigned')
    baseline = profile.get('baseline')
    if not isinstance(baseline, list) or len(baseline) != len(groups):
        raise ValueError('Baseline must assign each unit_node group once')
    assigned = set()
    for item in baseline:
        if not isinstance(item, dict) or set(item) != {'node', 'unit'}:
            raise ValueError('Invalid baseline assignment')
        node = _int(item['node'], 'baseline node')
        if node not in groups or node in assigned or not isinstance(item['unit'], str) or item['unit'] not in UNITS:
            raise ValueError('Invalid, duplicate, or unknown baseline unit')
        assigned.add(node)
    return profile


def validate_profile(profile):
    _validate_structure(profile)
    groups = {item['node'] for item in profile['baseline']}
    result = _analyze(profile, groups, {}, None)
    if result['invalid_reason']:
        raise ValueError('Exact IR cannot be certified: ' + result['invalid_reason'])
    return profile


def _unit_bound(unit, bounds):
    item = bounds[unit]
    if isinstance(item, dict):
        wce = item['wce']
        low, high = item.get('min_error', -wce), item.get('max_error', wce)
    elif isinstance(item, (tuple, list)):
        low, high = item[:2]
        wce = max(abs(low), abs(high))
    else:
        wce, low, high = item, -item, item
    if not all(_number(v) for v in (low, high, wce)) or low > high or wce < max(abs(low), abs(high)):
        raise ValueError('Invalid audited unit bounds')
    return low, high, wce


def _round_away(x):
    return math.floor(x + .5) if x >= 0 else math.ceil(x - .5)


def _analyze(profile, replacements, unit_bounds, flip, details=False):
    ir, state, ranges = profile['ir'], {}, {}
    assignments = {x['node']: x['unit'] for x in profile['baseline']}
    def result(error=math.inf, reason=None):
        return dict(error_bound=error, site_ranges=ranges, invalid_reason=reason)
    for x in ir['inputs']:
        state[x['id']] = (0, 255, 0, True)
    def operand(x):
        return state[x] if isinstance(x, str) else (x, x, 0, float(x).is_integer())
    for op in ir['operations']:
        name, kind = op['id'], op['op']
        args = [operand(x) for x in op['args']]
        lo, hi, error, integral = args[0]
        if kind in ('add', 'sub'):
            ol, oh, oe, oi = args[1]
            if kind == 'sub':
                ol, oh = -oh, -ol
            unit = assignments.get(op.get('unit_node'), 'exact')
            low, high, local = (0, 0, 0)
            if unit != 'exact' and op.get('unit_node') not in replacements:
                if min(lo, ol) < -2048 or max(hi, oh) > 2047:
                    return result(reason=f'approximate operand outside signed 12-bit range at {name}')
                low, high, local = _unit_bound(unit, unit_bounds)
            lo, hi, error = lo+ol+low, hi+oh+high, error+oe+local
        elif kind == 'scale':
            factor = op['factor']
            lo, hi, error = min(lo*factor, hi*factor), max(lo*factor, hi*factor), error*abs(factor)
        elif kind == 'abs':
            lo, hi = (0 if lo <= 0 <= hi else min(abs(lo), abs(hi))), max(abs(lo), abs(hi))
        elif kind in ('shift', 'round_shift', 'round_even', 'trunc'):
            divisor = 2**op['bits'] if kind in ('shift', 'round_shift') else 1
            rounding = math.floor if kind == 'shift' else _round_away if kind == 'round_shift' else round if kind == 'round_even' else math.trunc
            lo, hi = rounding(lo/divisor), rounding(hi/divisor)
            if error:
                error = math.ceil(error/divisor) + (0 if kind == 'shift' else 1)
        elif kind == 'clamp':
            low, high = op['min'], op['max']
            lo, hi = max(low, min(high, lo)), max(low, min(high, hi))
            error = min(error, high-low)
        # IEEE double arithmetic is used for noninteger IR values. Account for
        # both executions' local roundoff only when they can differ.
        if not op['integer']:
            eps = 8*sys.float_info.epsilon*max(abs(lo), abs(hi), 1)
            lo, hi = math.nextafter(lo-eps, -math.inf), math.nextafter(hi+eps, math.inf)
            if error:
                error += 2*eps
        ranges[name] = [lo, hi]
        if flip and name == flip[0]:
            if not op['integer'] or lo < -4096 or hi > 4095:
                return result(reason=f'flip site outside signed 13-bit integer range at {name}')
            delta = 1 << flip[1]
            lo, hi, error = lo-delta, hi+delta, error+delta
        width_limit = min(LIMIT, 2**(op['width']-1)-1) if op['integer'] else LIMIT
        lower_limit = max(-LIMIT, -2**(op['width']-1)) if op['integer'] else -LIMIT
        if not math.isfinite(lo) or not math.isfinite(hi) or lo < lower_limit or hi > width_limit:
            return result(reason=f'value/overflow range cannot be certified at {name}')
        state[name] = lo, hi, error, op['integer']
    errors = []
    for output in ir['outputs']:
        lo, hi, error, integer = operand(output['value'])
        # Final round-to-byte is monotone but can add one rounding step.
        errors.append(min(255, error + (1 if error and not integer else 0)))
    summary = result(max(errors, default=0))
    if details:
        summary.update(output_bounds=errors, state=state)
    return summary


def analyze(profile, replacements, unit_bounds, flip=None):
    """Bound maximum output error relative to this graph with all units exact.

    replacements names entire approximation groups; flip names one scalar SSA
    operation. Failure to prove range safety is returned as invalid_reason.
    """
    groups = {x['node'] for x in profile['baseline']}
    replacements = set(replacements)
    if not replacements <= groups:
        raise ValueError('Unknown replacement group')
    if flip:
        sites = {x['id'] for x in profile['ir']['operations']}
        if not isinstance(flip, (list, tuple)) or len(flip) != 2 or flip[0] not in sites:
            raise ValueError('Unknown flip site')
        _int(flip[1], 'flip bit', 0, 11)
    return _analyze(profile, replacements, unit_bounds, flip)


def _transfer_gains(profile):
    """Backward absolute linear gains; nonlinear operators use Lipschitz ranks.

    Rounding discontinuities are covered by analyze, not this ranking heuristic.
    """
    ir = profile['ir']
    count = len(ir['outputs'])
    cells = (len(ir['inputs'])+len(ir['operations']))*count
    if cells > 8_000_000:
        raise ValueError(f'Sensitivity matrix resource limit exceeded: {cells} cells; supported maximum is 8000000. This is not an infeasibility result.')
    try:
        import numpy as np
        zero = lambda: np.zeros(count, dtype=float)
    except ImportError:
        zero = lambda: [0.0]*count
    gains = {}
    for index, output in enumerate(ir['outputs']):
        name = output['value']
        if isinstance(name, str):
            gains.setdefault(name, zero())[index] += 1
    for op in reversed(ir['operations']):
        gain = gains.setdefault(op['id'], zero())
        factor = abs(op['factor']) if op['op'] == 'scale' else 2.0**(-op['bits']) if op['op'] in ('shift', 'round_shift') else 1
        for arg in op['args']:
            if isinstance(arg, str):
                target = gains.setdefault(arg, zero())
                for index in range(count):
                    target[index] += gain[index]*factor
    return gains


