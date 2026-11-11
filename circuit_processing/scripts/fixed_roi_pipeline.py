#!/usr/bin/env python3
"""Prepare fixed-size images, freeze current configurations, and verify PSNR."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile

import circuit_ir
import circuit_sources
import unit_policy
from comparison_images import save_comparison
from excel_report import export_psnr_workbook, export_results_directory

ROOT = Path(__file__).resolve().parents[1]
APPS = ('gaussian7_mixed_approx', 'sobel7_mixed_approx', 'dct_shared', 'dwt97_shared')
PEAK_SQUARED = 255**2


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def source_identity():
    paths = set()
    for directory, patterns in (
        ('scripts', ('*.py',)), ('app', ('*.hpp', '*.cpp')),
        ('include', ('*.hpp',)), ('src', ('*.cpp',)),
        ('kernels', ('*.h', '*.c')), ('third_party', ('*.c',)),
    ):
        for pattern in patterns:
            paths.update((ROOT/directory).rglob(pattern))
    paths.update(ROOT/'configs'/name for name in ('unit_error_bounds.json', 'nangate45_unit_costs.json'))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(
        path.read_text(encoding='utf-8').encode('utf-8')).hexdigest() for path in sorted(paths)}


def checked_sources():
    sources = source_identity()
    for relative, digest in read_json(ROOT/'configs/arithmetic_sources.json').items():
        if sources.get(relative) != digest:
            raise ValueError(f'Arithmetic source changed and requires a model review: {relative}')
    return sources


def unit_bounds():
    document = read_json(ROOT/'configs/unit_error_bounds.json')
    for relative, digest in document['sources'].items():
        actual = hashlib.sha256((ROOT/relative).read_text(encoding='utf-8').encode()).hexdigest()
        if actual != digest:
            raise ValueError(f'Re-audit the signed adder error bounds after changing {relative}')
    return document['units']


def profile_digest(selected):
    canonical = json.dumps(selected, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def decisions(plan):
    return {key: value for key, value in plan.items() if key not in ('source_coord', 'call_stack')}


def next_error_grade(profile, bounds):
    """Keep the approved one-group next-WCE-grade baseline change."""
    wce = {unit: circuit_ir._unit_bound(unit, bounds)[2] for unit in circuit_ir.UNITS-{'exact'}}
    selected = min((entry for entry in profile['baseline'] if entry['unit'] != 'exact'),
                   key=lambda entry: (wce[entry['unit']], entry['node']))
    old = selected['unit']
    higher = [unit for unit in wce if wce[unit] > wce[old]]
    if not higher:
        raise ValueError(f"{profile['name']}: no higher-error approximate unit")
    new = min(higher, key=lambda unit: (wce[unit], unit))
    return dict(node=selected['node'], previous_unit=old, unit=new,
                previous_wce=wce[old], wce=wce[new])


def plan_profiles(selected, area, total, min_bit, max_evaluations, max_candidates, *,
                  policy, grid, box):
    bounds = unit_bounds()
    floors = policy.get('minimum_roi_flip_mse_by_app', {})
    allowed = policy.get('flip_groups', {})
    if (set(floors) | set(allowed)) - selected.keys():
        raise ValueError('Policy entries must name frozen circuits')
    result = {}
    for name, profile in selected.items():
        defined = {entry['node'] for entry in profile['baseline']}
        if name in allowed and (not allowed[name] or not set(allowed[name]) <= defined):
            raise ValueError(f'{name}: invalid hardware flip groups')
        choice = unit_policy.plan(
            profile, bounds, policy['costs'], grid, box, min_bit, max_evaluations, max_candidates,
            allowed_flip_groups=allowed.get(name), minimum_roi_flip_mse=floors.get(name, 0))
        operation = next(op for op in profile['ir']['operations'] if op['id'] == choice['node'])
        choice.update(unit_node=operation['unit_node'], source_coord=operation.get('source_coord'),
                      call_stack=operation.get('call_stack', []))
        result[name] = choice
    return result


def check_frozen(config):
    if config.get('schema') != 5 or config.get('grid') != 320:
        raise ValueError('Use a newly frozen schema-5 configuration for fixed 320x320 inputs')
    if config['sources'] != checked_sources():
        raise ValueError('Frozen configuration belongs to a different source revision. Freeze again.')
    box = config['bbox_half_open']
    if (len(box) != 4 or any(type(v) is not int for v in box)
            or not 0 <= box[0] < box[2] <= 320 or not 0 <= box[1] < box[3] <= 320):
        raise ValueError('Invalid fixed ROI')
    selected = config['input_circuits']
    if not selected or profile_digest(selected) != config['input_circuits_sha256']:
        raise ValueError('Frozen input circuits changed')
    for name, profile in selected.items():
        circuit_ir.validate_profile(profile)
        if profile['name'] != name:
            raise ValueError('Circuit name differs from its key')
    area = (box[2]-box[0])*(box[3]-box[1])
    expected = plan_profiles(
        selected, area, 320**2, config['min_bit'], config['max_evaluations'], config['max_candidates'],
        policy=config['unit_policy'], grid=320, box=box)
    if {n: decisions(p) for n, p in expected.items()} != {
        n: decisions(p) for n, p in config['circuits'].items()
    }:
        raise ValueError('Frozen decisions differ from the current selector')
    return config


def freeze(args):
    import circuit_energy

    protocol = read_json(args.roi_run/'protocol.json')
    if (protocol.get('status'), protocol.get('feature'), protocol.get('coordinate_mode'),
            protocol.get('grid')) != ('completed', 'rc', 'stretch', 320):
        raise ValueError('Expected a completed RC union ROI run on a 320x320 grid')
    matches = [row for row in read_json(args.roi_run/'rectangles.json')
               if row['method'] == 'union' and row['frequency_percent'] == args.threshold]
    if len(matches) != 1 or matches[0]['bbox_half_open'] is None:
        raise ValueError('The selected threshold has no unique nonempty union rectangle')
    box = matches[0]['bbox_half_open']
    paths = args.circuit or [ROOT/f'configs/circuits/{name}.json' for name in APPS]
    loaded = [circuit_sources.load_profile(path) for path in paths]
    selected = {profile['name']: profile for profile in loaded}
    if len(selected) != len(loaded):
        raise ValueError('Circuit names must be unique')
    policy = dict(costs=read_json(ROOT/'configs/nangate45_unit_costs.json'), baseline_first=True,
                  signed_output=True, flip_groups={})
    if 'dwt97_shared' in selected:
        policy['flip_groups']['dwt97_shared'] = list(range(64, 72))
    records = {}
    if args.baseline == 'changed':
        if args.reference is None:
            raise ValueError('Changed baselines require the original frozen configuration via --reference')
        reference = check_frozen(read_json(args.reference))
        if reference['baseline_family'] != 'original' or reference['bbox_half_open'] != box:
            raise ValueError('Use the original baseline freeze with the same ROI')
        if reference['input_circuits'] != selected:
            raise ValueError('Original circuit descriptors changed between the two baseline families')
        policy['changed_baselines'] = True
        policy['minimum_roi_flip_mse_by_app'] = {
            name: reference['circuits'][name]['estimated_roi_flip_mse'] for name in selected
        }
        for name, profile in list(selected.items()):
            change = next_error_grade(profile, unit_bounds())
            selected[name] = unit_policy.with_units(profile, [dict(node=change['node'], unit=change['unit'])])
            records[name] = dict(baseline_change=change)
    area = (box[2]-box[0])*(box[3]-box[1])
    for name, profile in selected.items():
        mse = unit_policy.unflipped_mse(profile, unit_bounds(), 320, box, circuit_energy.prepare(profile))
        if mse is None or mse > unit_policy.FRAME_MSE_LIMIT:
            raise ValueError(f'{name}: the baseline itself has no 30 dB certificate')
        records.setdefault(name, {})['baseline_mse_bound'] = mse
    plans = plan_profiles(selected, area, 320**2, args.min_bit, args.max_evaluations,
                          args.max_candidates, policy=policy, grid=320, box=box)
    for name, plan in plans.items():
        compensated = bool(plan['exact_nodes'] or plan['unit_replacements'])
        flip_mse = unit_policy.study_control(plan)['mse_bound'] if compensated else plan['global_mse_bound']
        records[name].update(status='compensated' if compensated else 'zero_replacement',
                             node=plan['node'], bit=plan['bit'], exact_nodes=plan['exact_nodes'],
                             unit_replacements=plan['unit_replacements'],
                             flip_only_mse_bound=flip_mse, processed_mse_bound=plan['global_mse_bound'])
    config = dict(
        schema=5, baseline_family=args.baseline, grid=320, bbox_half_open=box,
        frequency_percent=args.threshold, roi_images=protocol['count'],
        roi_run_sha256=sha256(args.roi_run/'rectangles.json'), min_bit=args.min_bit,
        max_evaluations=args.max_evaluations, max_candidates=args.max_candidates,
        sources=checked_sources(), input_circuits=selected, input_circuits_sha256=profile_digest(selected),
        circuits=plans, unit_policy=policy,
        study=dict(applications=records, reference_sha256=sha256(args.reference) if args.reference else None))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, config)
    for name, plan in plans.items():
        lower = 10*math.log10(PEAK_SQUARED/plan['global_mse_bound'])
        print(f"{name}: {plan['node']} bit {plan['bit']}, replacements {plan['unit_replacements']}, "
              f"exact {plan['exact_nodes']}, PSNR lower bound {lower:.2f} dB", flush=True)


def project(bounds, grid, width, height):
    if (grid, width, height) != (320, 320, 320):
        raise ValueError('Resize inputs offline to 320x320 before verification')
    return bounds


def prepare_images(args):
    from PIL import Image
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Image output must be separate from the source directory')
    inputs = [source/f'{n}.jpg' for n in range(args.start, args.start+args.count)]
    if args.start < 1 or args.count < 1 or not all(p.is_file() for p in inputs):
        raise ValueError('Invalid image range or missing numbered JPG input')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for path in inputs:
        with Image.open(path) as image:
            original_size = image.size
            image.convert('RGB').resize((320, 320), Image.Resampling.BILINEAR).save(output/path.name)
        records.append(dict(image=path.name, original_size=original_size, source_sha256=sha256(path),
                            prepared_sha256=sha256(output/path.name)))
    write_json(output/'images.json', dict(start=args.start, count=args.count, grid=320,
                                          resize='RGB bilinear stretch, JPEG defaults', images=records))
    print(f'Prepared {len(inputs)} images: {output}')

def evaluate(args):
    import numpy as np
    from PIL import Image

    config = check_frozen(read_json(args.config))
    schema = config['schema']
    grid, bounds = config['grid'], config['bbox_half_open']
    selected = config['input_circuits']
    applications = args.apps or list(selected)
    if len(set(applications)) != len(applications) or any(app not in selected for app in applications):
        raise ValueError("--apps must name unique circuits present in the frozen configuration")
    policy = config.get('unit_policy', {})
    changed_baselines = policy.get('changed_baselines', False)
    study = changed_baselines
    if study:
        if schema != 5 or grid != 320:
            raise ValueError('A baseline study requires a fixed 320x320 schema-4 freeze')
        if changed_baselines:
            if set(config['study']['applications']) != set(selected):
                raise ValueError('Changed baseline metadata must cover every frozen application')
        for choice in config['circuits'].values():
            if choice['exact_nodes'] or choice['unit_replacements']:
                unit_policy.study_control(choice)
    image_dir = args.images.expanduser().resolve()
    output = args.output.resolve()
    if output.is_relative_to(image_dir) or image_dir.is_relative_to(output):
        raise ValueError("Output and input image directories must be separate")
    if not args.binary.is_file():
        raise ValueError(f"Missing compiled verifier: {args.binary}")
    if schema == 5:
        capability = subprocess.run([str(args.binary.resolve()), '--capabilities'], capture_output=True, text=True)
        try:
            features = json.loads(capability.stdout)
            supported = (capability.returncode == 0 and features.get('circuit_ir') == 1
                         and features.get('approximate_replacements') == 1)
        except (ValueError, AttributeError):
            supported = False
        if not supported:
            raise ValueError('Rebuild the native verifier for circuit IR: cmake --build circuit_processing/build -j 4')
    if args.start < 1 or args.count < 1:
        raise ValueError("Invalid image range or example count")
    inputs = [image_dir/f"{index}.jpg" for index in range(args.start, args.start+args.count)]
    for path in inputs:
        if not path.is_file():
            raise ValueError(f"Missing input image: {path}")
    output.mkdir(parents=True, exist_ok=False)
    comparisons = output/"comparisons"
    comparisons.mkdir()
    if study:
        flip_comparisons = output/'flip_only_comparisons'
        flip_comparisons.mkdir()
    rows = []
    failures = []
    with tempfile.TemporaryDirectory(dir=output) as scratch:
        scratch = Path(scratch)
        profile_paths = {}
        if schema == 5:
            for name in applications:
                profile_paths[name] = scratch/f"{name}.json"
                write_json(profile_paths[name], selected[name])
        for index, path in enumerate(inputs):
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except (OSError, ValueError) as error:
                for app in applications:
                    rows.append(dict(image=path.name, application=app, mode=config["circuits"][app]["mode"],
                                     status="failed", global_pass=False, roi_decreased=False,
                                     roi_decreased_vs_compensated=False))
                    failures.append(dict(image=path.name, application=app, error=str(error)))
                continue
            box = project(bounds, grid, width, height)
            left, top, right, bottom = box
            area, total = (right-left)*(bottom-top), width*height
            for app in applications:
                plan = config["circuits"][app]
                row = dict(image=path.name, application=app, mode=plan["mode"],
                           left=left, top=top, right=right, bottom=bottom,
                           status="failed", global_pass=False, roi_decreased=False,
                           roi_decreased_vs_compensated=False)
                rows.append(row)
                try:
                    fits = unit_policy.configuration_mse(selected[app], plan, width, height, box) <= PEAK_SQUARED/1000
                    if study and (width, height) != (grid, grid):
                        raise ValueError('The compensation study requires fixed 320x320 inputs')
                    if area == 0 or not fits:
                        raise ValueError("Projected rectangle is empty or exceeds the frozen error budget")
                    destination = scratch
                    extra = ["--circuit", str(profile_paths[app])] if schema == 5 else []
                    replacements = [str(g) for g in plan["exact_nodes"]]
                    replacements.extend(f"{entry['node']}={entry['unit']}" for entry in plan.get("unit_replacements", []))
                    subprocess.run([str(args.binary.resolve()), selected[app]["application"], str(path), str(destination),
                                    *map(str, box), str(plan["node"]), str(plan["bit"]),
                                    *replacements, *extra], check=True, capture_output=True, text=True)
                    def pixels(name):
                        with Image.open(destination/f"{name}.png") as im:
                            return np.asarray(im, dtype=np.int32)
                    reference = pixels("exact")
                    mask = np.zeros((height, width), dtype=bool)
                    mask[top:bottom, left:right] = True
                    compensated = pixels("compensated")
                    errors = {}
                    for name in ("baseline", "compensated", "processed"):
                        result = compensated if name == "compensated" else pixels(name)
                        if result.shape != (height, width) or reference.shape != result.shape:
                            raise ValueError("Verifier returned an unexpected image size")
                        delta = result-reference
                        squares = delta**2
                        errors[name] = (int(squares.sum()), int(squares[mask].sum()))
                        for label, sse, count in (("global", errors[name][0], total),
                                                  ("roi", errors[name][1], area),
                                                  ("background", errors[name][0]-errors[name][1], total-area)):
                            row[f"{name}_{label}_psnr"] = ("" if not count else "inf" if not sse
                                                          else 10*math.log10(PEAK_SQUARED*count/sse))
                        if name == "processed" and (
                            np.any(np.abs(delta[mask]) > plan["inside_error_bound"]) or
                            np.any(np.abs(delta[~mask]) > plan["outside_error_bound"]) or
                            np.any(result[~mask] != compensated[~mask])
                        ):
                            raise ValueError("Observed error exceeds its bound or the disturbance leaks outside ROI")
                        if name == "processed" and schema == 5 and errors[name][0]/total > unit_policy.configuration_mse(
                            selected[app], plan, width, height, box
                        ):
                            raise ValueError("Observed MSE exceeds the frozen frame certificate")
                    filename = f"{app}_{int(path.stem):04d}.png"
                    save_comparison(destination, comparisons/filename, row)
                    if study:
                        baseline = pixels('baseline')
                        if replacements:
                            subprocess.run([str(args.binary.resolve()), selected[app]["application"],
                                            str(path), str(destination), *map(str, box),
                                            str(plan['node']), str(plan['bit']), *extra],
                                           check=True, capture_output=True, text=True)
                        flip_only = pixels('processed')
                        if (flip_only.shape != reference.shape or np.any(pixels('baseline') != baseline)
                                or np.any(pixels('exact') != reference)
                                or np.any(flip_only[~mask] != baseline[~mask])):
                            raise ValueError('Flip-only control differs outside the ROI or changed the reference')
                        squared = (flip_only-reference)**2
                        global_sse, roi_sse = int(squared.sum()), int(squared[mask].sum())
                        row.update(
                            flip_only_global_psnr=10*math.log10(PEAK_SQUARED*total/global_sse)
                            if global_sse else 'inf',
                            flip_only_roi_psnr=10*math.log10(PEAK_SQUARED*area/roi_sse)
                            if roi_sse else 'inf',
                            baseline_global_pass=1000*errors['baseline'][0] <= PEAK_SQUARED*total,
                            flip_only_global_pass=1000*global_sse <= PEAK_SQUARED*total,
                            flip_only_roi_decreased=roi_sse > errors['baseline'][1],
                        )
                        flip_record = row | {
                            'processed_global_psnr': row['flip_only_global_psnr'],
                            'processed_roi_psnr': row['flip_only_roi_psnr'],
                        }
                        save_comparison(destination, flip_comparisons/filename, flip_record,
                                        processed_label='Processed (flip only)')
                    row.update(status="completed", global_pass=1000*errors["processed"][0] <= PEAK_SQUARED*total,
                               roi_decreased=errors["processed"][1] > errors["baseline"][1],
                               roi_decreased_vs_compensated=errors["processed"][1] > errors["compensated"][1])
                except (ValueError, OSError, subprocess.CalledProcessError) as error:
                    message = (error.stderr or str(error)).strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
                    failures.append(dict(image=path.name, application=app, error=message))
            print(f"Verified {index+1}/{len(inputs)}: {path.name}", flush=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (output/"images.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        app: dict(images=len(inputs), completed=sum(r["status"] == "completed" for r in rows if r["application"] == app),
                  global_pass=sum(r["global_pass"] for r in rows if r["application"] == app),
                  roi_decreased=sum(r["roi_decreased"] for r in rows if r["application"] == app),
                  joint_pass=sum(r["global_pass"] and r["roi_decreased"]
                                 for r in rows if r["application"] == app),
                  roi_decreased_vs_compensated=sum(r["roi_decreased_vs_compensated"] for r in rows if r["application"] == app))
        for app in applications
    }
    report_summary = dict(config_sha256=sha256(args.config), binary_sha256=sha256(args.binary),
                          applications=summary, failures=failures)
    if study:
        def study_counts(app):
            complete = [r for r in rows if r['application'] == app and r['status'] == 'completed']
            return dict(processed_mse_bound=config['circuits'][app]['global_mse_bound'],
                        baseline_global_pass=sum(r['baseline_global_pass'] for r in complete),
                        flip_only_global_pass=sum(r['flip_only_global_pass'] for r in complete),
                        flip_only_roi_decreased=sum(r['flip_only_roi_decreased'] for r in complete),
                        restored_global_pass=sum(not r['flip_only_global_pass'] and r['global_pass']
                                                 for r in complete))

        if changed_baselines:
            def measured_psnr(app):
                completed = [row for row in rows if row['application'] == app and row['status'] == 'completed']
                metrics = {}
                for version in ('baseline', 'flip_only', 'processed'):
                    for region in ('global', 'roi'):
                        key = f'{version}_{region}_psnr'
                        values = [float(row[key]) for row in completed]
                        mean = math.fsum(values)/len(values) if values else None
                        minimum = min(values) if values else None
                        metrics[key] = dict(mean='inf' if mean == math.inf else mean,
                                            minimum='inf' if minimum == math.inf else minimum)
                return metrics

            report_summary['compensation_study'] = config['study'] | {
                'applications': {app: config['study']['applications'][app] | study_counts(app) | {
                    'measured_psnr': measured_psnr(app)
                } for app in applications},
            }
    write_json(output/"summary.json", report_summary)
    report = export_psnr_workbook(rows, output/"psnr_results.xlsx")
    print(json.dumps(summary, indent=2))
    print(f"Excel report: {report}")
    print(f"Comparison images: {comparisons}")
    if study:
        print(f"Flip-only comparison images: {flip_comparisons}")
        print(json.dumps(report_summary['compensation_study'], indent=2))
    return 1 if failures or any(not r["global_pass"] for r in rows) else 0


def encode(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, circuit_sources.load_profile(args.interface))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    images = commands.add_parser('images', help='Resize numbered images offline to 320x320')
    images.add_argument('--source', type=Path, required=True)
    images.add_argument('--output', type=Path, required=True)
    images.add_argument('--start', type=int, default=1)
    images.add_argument('--count', type=int, default=850)
    images.set_defaults(action=prepare_images)
    prepare = commands.add_parser('freeze', help='Select current fixed hardware without training on images')
    prepare.add_argument('--roi-run', type=Path, required=True)
    prepare.add_argument('--baseline', choices=('original', 'changed'), default='original')
    prepare.add_argument('--reference', type=Path)
    prepare.add_argument('--circuit', type=Path, action='append', default=[])
    prepare.add_argument('--threshold', type=int, default=85)
    prepare.add_argument('--min-bit', type=int, choices=range(8), default=4)
    prepare.add_argument('--max-evaluations', type=int, choices=range(4,100001), default=512, metavar='N')
    prepare.add_argument('--max-candidates', type=int, choices=range(1,10001), default=128, metavar='N')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.set_defaults(action=freeze)
    verify = commands.add_parser('verify', help='Verify a frozen configuration on fixed-size images')
    verify.add_argument('--config', type=Path, required=True)
    verify.add_argument('--binary', type=Path, default=ROOT/'build/fixed_roi_verify')
    verify.add_argument('--images', type=Path, required=True)
    verify.add_argument('--start', type=int, default=1)
    verify.add_argument('--count', type=int, default=850)
    verify.add_argument('--apps', nargs='+')
    verify.add_argument('--output', type=Path, required=True)
    verify.set_defaults(action=evaluate)
    encoder = commands.add_parser('encode-c', help='Encode a C source descriptor as scalar IR')
    encoder.add_argument('--interface', type=Path, required=True)
    encoder.add_argument('--output', type=Path, required=True)
    encoder.set_defaults(action=encode)
    report = commands.add_parser('export-excel', help='Rebuild Excel from an existing images.csv')
    report.add_argument('--results', type=Path, required=True)
    report.set_defaults(action=lambda args: print(export_results_directory(args.results)))
    args = parser.parse_args()
    return args.action(args)


if __name__ == '__main__':
    raise SystemExit(main())
