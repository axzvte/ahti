"""Bounded area-aware selection over the same typed graph for every kernel."""

from copy import deepcopy
import math

import circuit_ir


FRAME_MSE_LIMIT = 255**2 / 1000


def read_costs(document):
    """Require a complete, positive unit-area table with explicit provenance."""
    if document.get('schema') != 1 or not document.get('source'):
        raise ValueError('Expected an area cost table with schema 1 and source')
    costs = document['unit_area_um2']
    required = (circuit_ir.UNITS - {'exact'}) | {'exact32', 'exact64'}
    if set(costs) != required or any(
        type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1e9
        for value in costs.values()
    ):
        raise ValueError('Invalid or incomplete unit-area costs')
    return costs


def with_units(profile, replacements):
    """Copy the profile with static near-approximate replacements applied."""
    if not replacements:
        return profile
    result = deepcopy(profile)
    by_group = {item['node']: item for item in result['baseline']}
    seen = set()
    for replacement in replacements:
        group, unit = replacement['node'], replacement['unit']
        if group in seen or group not in by_group or by_group[group]['unit'] == 'exact':
            raise ValueError('Invalid approximate replacement group')
        if unit not in circuit_ir.UNITS - {'exact'}:
            raise ValueError('Invalid approximate replacement unit')
        seen.add(group)
        by_group[group]['unit'] = unit
    return result


def frame_weights(profile, width, height, box):
    """Count full flipped, full unflipped and cropped unflipped output tiles."""
    th, tw = profile['ir']['tile']
    l, t, r, b = box
    nx, rx = divmod(width, tw)
    ny, ry = divmod(height, th)
    fx = max(0, r//tw - (l+tw-1)//tw)
    fy = max(0, b//th - (t+th-1)//th)
    flipped = fx*fy
    partial = tuple((count, tuple(i for i, out in enumerate(profile['ir']['outputs'])
                                  if out['dx'] < cols and out['dy'] < rows))
                    for count, cols, rows in ((ny, rx, th), (nx, tw, ry), (1, rx, ry))
                    if count and cols and rows)
    return flipped, nx*ny-flipped, th*tw, partial, width*height


def mse_bound(weights, inside, outside):
    flipped, unflipped, pixels, partial, total = weights
    if not flipped:
        return math.inf
    outside_tile = pixels*outside['rms']**2
    # A cropped subset is also bounded by the total energy of its padded tile.
    cropped = math.fsum(count*min(outside_tile, math.fsum(outside['outputs'][i]**2 for i in visible))
                        for count, visible in partial)
    return math.fsum((flipped*pixels*inside['rms']**2,
                      unflipped*outside_tile, cropped))/total * (1+1e-12)


def configuration_mse(profile, choice, width, height, box):
    return mse_bound(frame_weights(profile, width, height, box),
                     choice['inside_certificate'], choice['outside_certificate'])


def study_control(plan):
    """Return the range-checked, over-budget flip-only control for a compensated choice."""
    if not (plan['exact_nodes'] or plan['unit_replacements']):
        raise ValueError('The compensation study requires a changed unit')
    for row in plan['selection_trace']:
        if ((row['node'], row['bit']) == (plan['node'], plan['bit'])
                and row.get('legacy_mse_bound') is not None
                and row['mse_bound'] > FRAME_MSE_LIMIT):
            return row
    raise ValueError('The selected flip lacks a range-checked over-budget control')


def _certificate(profile, exact, bounds, flip, energy):
    result = circuit_ir._analyze(profile, exact, bounds, flip, details=True)
    if result['invalid_reason']:
        return False, None
    output = result['output_bounds']
    rms = math.hypot(*output)/math.sqrt(len(output))
    method = 'per_output'
    tighter = energy.rms_bound(result['state']) if energy is not None else None
    if tighter is not None and tighter < rms:
        rms, method = tighter, 'linear_tail_energy'
    return dict(outputs=output, rms=rms, max=result['error_bound'], method=method), result['state']


def unflipped_mse(profile, bounds, grid, box, energy):
    """Return the baseline frame bound before applying any processing."""
    certificate, _ = _certificate(profile, (), bounds, None, energy)
    return mse_bound(frame_weights(profile, grid, grid, box), certificate, certificate) if certificate else None


def _baseline_first(sites, min_bit, max_candidates, max_evaluations, weights,
                    evaluate, cache, replacements, total_cost, trace, estimate=None,
                    minimum_roi_flip_mse=0):
    """Try distinct structural gains early, then improve rejected candidates in place."""
    occurrences = {}
    ordered = []
    for gain, site in sites:
        band = round(gain, 12)
        repeat = occurrences.get(band, 0)
        occurrences[band] = repeat + 1
        ordered.append((repeat, -gain, site))

    outside = evaluate({}, None, max_evaluations)
    scan_cap = min(max_candidates, max_evaluations//2 - 1)
    certified = {}
    rejected = {}
    trials = 0
    for _, negative_gain, site in sorted(ordered):
        for bit in range(11, min_bit-1, -1):
            if trials >= scan_cap:
                break
            score = -negative_gain*(1 << bit)
            if estimate is None and bit in certified and score <= certified[bit][0]:
                continue
            flip = (site, bit)
            metrics = estimate(flip) if estimate else {}
            rank_score = metrics.get('estimated_roi_flip_mse', score)
            if rank_score < minimum_roi_flip_mse and not math.isclose(
                rank_score, minimum_roi_flip_mse, rel_tol=1e-9
            ):
                continue
            inside = evaluate({}, flip, max_evaluations)
            trials += 1
            mse = mse_bound(weights, inside, outside) if inside and outside else math.inf
            if estimate:
                old_mse = None
                if inside and outside:
                    old_inside = inside | {'rms': inside['legacy_rms']}
                    old_mse = mse_bound(weights, old_inside, outside)
                trace.append(dict(node=site, bit=bit, structural_score=score, **metrics,
                                  legacy_mse_bound=old_mse,
                                  mse_bound=mse if math.isfinite(mse) else None,
                                  certified=rank_score > 0 and mse <= FRAME_MSE_LIMIT))
            if rank_score <= 0:
                continue
            if mse <= FRAME_MSE_LIMIT:
                if bit not in certified or (-rank_score, flip) < (-certified[bit][0], certified[bit][1]):
                    certified[bit] = (rank_score, flip, inside, mse)
            elif not minimum_roi_flip_mse or math.isfinite(mse):
                # A study needs a range-certified flip-only control, even when
                # that control exceeds the quality budget.
                rank = (mse, inside['rms'] if inside else math.inf, -rank_score, flip)
                if bit not in rejected or rank < rejected[bit][0]:
                    rejected[bit] = (rank, rank_score, flip)
        if trials >= scan_cap:
            break

    if certified:
        area = total_cost({})
        if estimate is None:
            for score, flip, _, mse in sorted(certified.values()):
                trace.append(dict(node=flip[0], bit=flip[1], structural_score=score,
                                  unit_area_um2=area, mse_bound=mse))
        score, flip, inside, mse = min(certified.values(), key=lambda x: (-x[0], x[1]))
        return (area, -score, flip), {}, inside, outside, mse, trials, 0, 'zero_replacement'

    best = None
    shortlist = sorted(rejected.values())
    for index, (rank, score, flip) in enumerate(shortlist):
        cap = len(cache) + (max_evaluations-len(cache))//(len(shortlist)-index)
        current, mse = {}, rank[0]
        while len(cache) < cap and mse > FRAME_MSE_LIMIT:
            for group, unit in replacements(current):
                trial = current | {group: unit}
                next_bg = evaluate(trial, None, cap)
                if next_bg is None:
                    continue
                next_fg = evaluate(trial, flip, cap)
                if next_fg is None:
                    continue
                next_mse = mse_bound(weights, next_fg, next_bg)
                if next_mse < mse:
                    current, mse, inside, background = trial, next_mse, next_fg, next_bg
                    break
            else:
                break
        if mse <= FRAME_MSE_LIMIT:
            area = total_cost(current)
            diagnostics = (estimate(flip) if estimate else {'structural_score': score})
            trace.append(dict(node=flip[0], bit=flip[1], **diagnostics,
                              unit_area_um2=area, mse_bound=mse, replacement_count=len(current)))
            key = (len(current), area, -score, flip)
            if best is None or key < best[0]:
                best = key, current, inside, background, mse

    if best is None:
        raise ValueError('No certified configuration within the baseline-first search budget')
    key, current, inside, background, mse = best
    return key[1:], current, inside, background, mse, trials, 0, 'compensated'


def plan(profile, bounds, costs, grid, box, min_bit=4, max_evaluations=512, max_candidates=128,
         *, allowed_flip_groups=None, minimum_roi_flip_mse=0):
    """Select a signed-output flip on the baseline before considering compensation."""
    if (type(minimum_roi_flip_mse) not in (int, float) or not math.isfinite(minimum_roi_flip_mse)
            or minimum_roi_flip_mse < 0):
        raise ValueError('ROI disturbance floor must be finite and nonnegative')
    import circuit_energy
    circuit_ir.validate_profile(profile)
    costs = read_costs(costs)
    weights = frame_weights(profile, grid, grid, box)
    if not weights[0]:
        raise ValueError('No complete tile is inside the frozen ROI')
    energy = circuit_energy.prepare(profile)
    baseline = {x['node']: x['unit'] for x in profile['baseline']}
    groups = sorted(g for g, unit in baseline.items() if unit != 'exact')
    widths = {}
    for op in profile['ir']['operations']:
        if 'unit_node' in op:
            group = op['unit_node']
            widths[group] = max(widths.get(group, 0), op['width'])

    def cost(group, unit):
        return costs[f'exact{widths[group]}' if unit == 'exact' else unit]

    def total_cost(assignment):
        return math.fsum(cost(group, assignment.get(group, unit))
                         for group, unit in sorted(baseline.items()))

    def wce(unit):
        return 0 if unit == 'exact' else circuit_ir._unit_bound(unit, bounds)[2]

    audited_wce = {unit: wce(unit) for unit in circuit_ir.UNITS}
    options = {g: [unit for unit in sorted(circuit_ir.UNITS)
                   if wce(unit) <= wce(baseline[g])] for g in groups}
    trace = []
    cache = {}

    def evaluate(assignment, flip, cap):
        key = (tuple(sorted(assignment.items())), flip)
        if key in cache:
            return cache[key]
        if len(cache) >= cap:
            return None
        exact = [g for g, unit in assignment.items() if unit == 'exact']
        changes = [dict(node=g, unit=unit) for g, unit in assignment.items()
                   if unit != 'exact' and unit != baseline[g]]
        effective = with_units(profile, changes)
        cert, state = _certificate(effective, exact, bounds, flip, energy)
        if cert and flip:
            rms = cert['rms']
            correlated = circuit_energy.correlated_flip_rms(
                profile, assignment, audited_wce, flip, state)
            cert.update(legacy_rms=rms, correlated_rms=correlated)
            if correlated is not None and correlated < rms:
                cert.update(rms=correlated, method='correlated_flip')
        cache[key] = cert
        return cert

    gains = circuit_ir._transfer_gains(profile)
    influence = dict.fromkeys(groups, 0.0)
    sites = []
    for op in profile['ir']['operations']:
        group = op.get('unit_node')
        if group is None:
            continue
        values = gains[op['id']]
        gain = math.hypot(*(float(x) for x in values))/math.sqrt(len(values))
        if group in influence:
            influence[group] += gain
        if (gain > 0 and op['integer']
                and (allowed_flip_groups is None or group in allowed_flip_groups)):
            sites.append((gain, op['id']))
    sites.sort(key=lambda item: (-item[0], item[1]))

    responses = {}

    def estimate(flip):
        site, bit = flip
        if site not in responses:
            responses[site] = circuit_energy.signed_response(profile, site)
        squares = [min(255.0, abs(value)*(1 << bit))**2 for value in responses[site]]
        total = math.fsum(squares)
        concentration = math.fsum(value**2 for value in squares)
        return dict(estimated_roi_flip_mse=total/len(squares),
                    effective_output_pixels=total**2/concentration if concentration else 0.0)

    def replacements(current):
        moves = []
        for group in groups:
            original = current.get(group, baseline[group])
            for unit in options[group]:
                reduction = wce(original) - wce(unit)
                if reduction > 0 and influence[group] > 0:
                    delta = cost(group, unit) - cost(group, original)
                    efficiency = influence[group]*reduction/(1+max(0, delta))
                    moves.append((-efficiency, delta, group, unit))
        return [(group, unit) for _, _, group, unit in sorted(moves)]

    key, final, inside, outside, mse, trials, strength_floor, stage = _baseline_first(
        sites, min_bit, max_candidates, max_evaluations, weights, evaluate, cache,
        replacements, total_cost, trace, estimate,
        minimum_roi_flip_mse)
    method = f"baseline_first_{stage}_signed_v2"
    site, bit = key[2]
    diagnostics = estimate((site, bit))
    score = math.hypot(*gains[site])/math.sqrt(len(gains[site]))*(1 << bit)
    return dict(mode='ir_internal', node=site, bit=bit, **diagnostics,
                exact_nodes=[g for g in groups if final.get(g) == 'exact'],
                unit_replacements=[dict(node=g, unit=final[g]) for g in groups
                                   if g in final and final[g] not in ('exact', baseline[g])],
                inside_error_bound=max(inside['max'], outside['max']),
                outside_error_bound=outside['max'],
                inside_certificate=inside, outside_certificate=outside,
                global_mse_bound=mse,
                structural_score=score, structural_score_floor=strength_floor,
                unit_area_um2=key[0], baseline_unit_area_um2=total_cost({}),
                analysis_evaluations=len(cache), candidate_count_studied=trials,
                selection_method=method, selection_trace=trace,
                roi_mse_lower_bound=0, roi_effect='requires_image_verification',
                optimality_claim=False, search_complete=False)
