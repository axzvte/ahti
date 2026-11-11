#!/usr/bin/env python3
"""Generate the current fixed-320 hardware bundle from a certified freeze."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from circuit_sources import load_profile
from fixed_roi_pipeline import check_frozen, read_json, write_json
from rtl_area import area_position_rtl, emit_area_stencil
from rtl_shared_dct import emit_shared_dct
from rtl_shared_dwt import emit_shared_dwt
from rtl_units import generate_units

PROFILES = dict(gaussian7='gaussian7_mixed_approx', sobel7='sobel7_mixed_approx',
                dct_shared='dct_shared', dwt97_shared='dwt97_shared')


def semantic(profile):
    result = json.loads(json.dumps(profile))
    result.pop('source', None)
    result.pop('baseline')
    for operation in result['ir']['operations']:
        operation.pop('source_coord', None)
        operation.pop('call_stack', None)
    return result


def generate_bundle(output, config):
    check_frozen(config)
    selected = config['input_circuits']
    if not selected or not set(selected) <= set(PROFILES.values()):
        raise ValueError('The RTL backend supports the four supplied source kernels')
    for name, profile in selected.items():
        current = load_profile(ROOT/f'configs/circuits/{name}.json')
        if semantic(profile) != semantic(current):
            raise ValueError(f'{name}: review the RTL mapping for changed C topology or operations')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    digests, targets = {}, []

    def write(relative, text):
        path = output/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
        digests[relative] = hashlib.sha256(text.encode()).hexdigest()

    write('rtl/common/evoapprox_units.sv', generate_units(ROOT))
    write('rtl/common/roi_position.sv', area_position_rtl())
    for app, name in PROFILES.items():
        if name not in selected:
            continue
        profile, plan = selected[name], config['circuits'][name]
        has_compensation = bool(plan['exact_nodes'] or plan['unit_replacements'])
        variants = ('baseline', 'flip_only', 'processed') if has_compensation else ('baseline', 'processed')
        for variant in variants:
            source_variant = 'processed' if variant == 'flip_only' else variant
            choices = plan | {'exact_nodes': [], 'unit_replacements': []} if variant == 'flip_only' else plan
            if app == 'dct_shared':
                text = emit_shared_dct(ROOT, profile, choices, source_variant, config['bbox_half_open'])
            elif app == 'dwt97_shared':
                text = emit_shared_dwt(ROOT, profile, choices, source_variant, config['bbox_half_open'])
            else:
                text = emit_area_stencil(profile, choices, app, source_variant, config['bbox_half_open'])
            if variant == 'flip_only':
                text = text.replace(f'{app}_processed', f'{app}_flip_only')
            source = f'rtl/{app}/{variant}.sv'
            write(source, text)
            dependencies = ['rtl/common/evoapprox_units.sv']
            if source_variant == 'processed':
                dependencies.append('rtl/common/roi_position.sv')
            targets.append(dict(application=app, version=variant, top=f'{app}_{variant}',
                                sources=dependencies+[source]))
    manifest = dict(schema=1, targets=targets, sha256=digests,
                    frozen_sha256=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                    scope=dict(frame_size=[320,320], dct='forward DCT; software inverse is outside area scope',
                               dwt='forward plus inverse DWT', stencils='preassembled windows supplied externally',
                               control='counters, fixed ROI comparisons and flip control included'))
    write_json(output/'manifest.json', manifest)
    print(f'Generated {len(targets)} RTL tops: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frozen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    generate_bundle(args.output, read_json(args.frozen))


if __name__ == '__main__':
    main()
