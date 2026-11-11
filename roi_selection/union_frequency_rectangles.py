#!/usr/bin/env python3
"""U2-Net union with per-image Otsu-RC and a frequency-threshold sweep."""
from __future__ import annotations

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path
import sys
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageOps, __version__ as pillow_version
from scipy.ndimage import label

from mask_utils import otsu_mask, mask_overlay, read_mask, sample_mask, heatmap, write_csv, write_json
from color_saliency import RC_PARAMS, compute_rc

DEFAULT_THRESHOLDS = list(range(80, 91))
METHODS = ('u2net', 'rc', 'union')


def combine(u2, rc):
    return u2 | rc, rc & ~u2


def threshold_rectangle(counts, image_count, percent):
    """Integer image counts avoid rounding the frequency before thresholding."""
    required = (percent * image_count + 99) // 100
    region = counts >= required
    ys, xs = np.nonzero(region)
    rectangle = np.zeros_like(region)
    row = dict(frequency_percent=percent, minimum_images=required,
               selected_pixels=int(region.sum()), selected_area_fraction=float(region.mean()),
               bbox_half_open=None, width=None, height=None, rectangle_pixels=None,
               rectangle_area_fraction=None, fill_ratio=None,
               component_count=int(label(region, np.ones((3, 3), dtype=np.uint8))[1]))
    if xs.size:
        left, top = int(xs.min()), int(ys.min())
        right, bottom = int(xs.max()) + 1, int(ys.max()) + 1
        rectangle[top:bottom, left:right] = True
        area = (right-left) * (bottom-top)
        row.update(bbox_half_open=[left, top, right, bottom], width=right-left,
                   height=bottom-top, rectangle_pixels=area,
                   rectangle_area_fraction=area/region.size, fill_ratio=int(region.sum())/area)
    return region, rectangle, row


def save_mask(path, mask):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(mask.astype(np.uint8)*255).save(path)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def example(original, masks, path, methods=METHODS):
    views = [('Original', original)] + [(name, mask_overlay(original, masks[name], (255, 0, 0)))
                                      for name in methods]
    sheet = Image.new('RGB', (1120, 280), 'white')
    for index, (title, image) in enumerate(views):
        tile = ImageOps.contain(image, (270, 240), Image.Resampling.BILINEAR)
        sheet.paste(tile, (index*280+(280-tile.width)//2, 30+(240-tile.height)//2))
        ImageDraw.Draw(sheet).text((index*280+8, 10), title, fill='black')
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def run(args):
    feature = getattr(args, 'feature', 'rc')
    if feature != 'rc':
        raise ValueError('Only the accepted RC feature is supported')
    methods = METHODS
    feature_params = RC_PARAMS
    data = args.data.expanduser().resolve()
    image_dir, mask_dir = (data/args.image_dir).resolve(), (data/args.mask_dir).resolve()
    output = args.output.expanduser().resolve()
    percentages = sorted(set(args.thresholds))
    if (args.count < 1 or args.start < 1 or args.grid < 2 or args.preview_count < 0
            or not percentages or any(p < 1 or p > 100 for p in percentages)):
        raise ValueError('Invalid image range, grid, preview count or frequency percentages (1..100)')
    for source in (image_dir, mask_dir):
        if source and (output.is_relative_to(source) or source.is_relative_to(output)):
            raise ValueError('Output must be separate from images and masks')
    if output.exists():
        raise ValueError(f'Output must be a new directory: {output}')
    ids = range(args.start, args.start+args.count)
    for image_id in ids:
        for path in (image_dir/f'{image_id}.jpg', mask_dir/f'{image_id}_mask.png'):
            if not path.is_file():
                raise ValueError(f'Missing input: {path}')
    output.mkdir(parents=True, exist_ok=False)
    protocol = dict(status='running', started_utc=datetime.now(timezone.utc).isoformat(),
                    data=str(data), image_dir=str(image_dir), mask_dir=str(mask_dir),
                    count=args.count, start=args.start,
                    grid=args.grid, coordinate_mode='stretch', frequency_percentages=percentages,
                    feature=feature, feature_params=feature_params,
                    feature_binarization='per-image Otsu on native-size response; strict > threshold',
                    constant_feature_policy='empty feature mask; undefined threshold',
                    union_rule=f'per-image U2-Net OR {feature} before spatial accumulation',
                    frequency_rule='foreground image count / all selected images',
                    python=sys.version, numpy=np.__version__, pillow=pillow_version,
                    scipy=version('scipy'),
                    scikit_image=version('scikit-image'),
                    script_hashes={p: fingerprint(Path(__file__).parent/p) for p in
                                   ('union_frequency_rectangles.py', 'mask_utils.py', 'color_saliency.py')})
    write_json(output/'protocol.json', protocol)
    try:
        counts = {name: np.zeros((args.grid, args.grid), np.uint32) for name in (*methods, 'added')}
        normalized_union_sum = np.zeros((args.grid, args.grid), float)
        nonempty_union_images = 0
        records = []
        response_dir = output/f'{feature}_responses'
        response_dir.mkdir()
        for index, image_id in enumerate(ids, 1):
            image_path, mask_path = image_dir/f'{image_id}.jpg', mask_dir/f'{image_id}_mask.png'
            with Image.open(image_path) as image:
                original = image.convert('RGB')
            u2 = read_mask(mask_path)
            if u2.shape != (original.height, original.width):
                raise ValueError(f'Image/mask size mismatch: {image_id}')
            response = compute_rc(np.asarray(original, np.uint8))
            response_source, source_hash = 'computed', None
            response_path = response_dir/f'{image_id}.npz'
            np.savez_compressed(response_path, **{feature: response})
            feature_mask, feature_threshold = otsu_mask(response)
            union, added = combine(u2, feature_mask)
            native = dict(u2net=u2, union=union, added=added)
            native[feature] = feature_mask
            mapped = {}
            for name, mask in native.items():
                mapped[name], _ = sample_mask(mask, args.grid, 'stretch')
                counts[name] += mapped[name].astype(np.uint32)
                save_mask(output/'per_image_masks'/name/f'{image_id}.png', mapped[name])
            size = int(mapped['union'].sum())
            if size:
                normalized_union_sum += mapped['union']/size
                nonempty_union_images += 1
            records.append(dict(image_id=image_id, width=original.width, height=original.height,
                                **{f'{feature}_source': response_source, f'{feature}_source_sha256': source_hash},
                                image_sha256=fingerprint(image_path), u2_mask_sha256=fingerprint(mask_path),
                                **{f'{feature}_response_sha256': fingerprint(response_path),
                                   f'{feature}_otsu_threshold': feature_threshold},
                                **{f'{name}_native_area_fraction': float(mask.mean()) for name,mask in native.items()},
                                **{f'{name}_sampled_pixels': int(mask.sum()) for name,mask in mapped.items()}))
            if index <= args.preview_count:
                example(original, native, output/'examples'/f'{image_id}.png', methods)
            if index % 25 == 0 or index == args.count:
                print(f'{feature.upper()} and union: {index}/{args.count}', flush=True)
        write_csv(output/'per_image.csv', records)
        frequency_dir = output/'frequencies'
        frequency_dir.mkdir()
        for name in counts:
            np.save(frequency_dir/f'{name}_count.npy', counts[name])
            np.save(frequency_dir/f'{name}_frequency.npy', counts[name]/args.count)
            heatmap(counts[name]/args.count, f'{name}: N={args.count}, stretch').save(frequency_dir/f'{name}_frequency.png')
        np.testing.assert_array_equal(counts['union'], counts['u2net']+counts['added'])
        rectangles = []
        overview = Image.new('RGB', (1000, ((len(percentages)+4)//5)*240), 'white')
        for name in methods:
            folder = output/'thresholds'/name
            folder.mkdir(parents=True)
            for index, percent in enumerate(percentages):
                region, rectangle, row = threshold_rectangle(counts[name], args.count, percent)
                row = dict(method=name, **row)
                row['mean_union_coverage_nonempty_images'] = (
                    float(normalized_union_sum[rectangle].sum()/nonempty_union_images)
                    if row['bbox_half_open'] is not None and nonempty_union_images else None)
                row['pooled_union_coverage'] = (float(counts['union'][rectangle].sum()/counts['union'].sum())
                                               if row['bbox_half_open'] is not None and counts['union'].sum() else None)
                save_mask(folder/f'{percent:02d}_region_mask.png', region)
                save_mask(folder/f'{percent:02d}_rectangle_mask.png', rectangle)
                preview = heatmap(counts[name]/args.count, f'{name}: frequency >= {percent}%')
                if row['bbox_half_open']:
                    l,t,r,b = row['bbox_half_open']
                    scale = 420/args.grid
                    x0, y0 = 30+l*scale, 42+t*scale
                    ImageDraw.Draw(preview).rectangle((x0, y0, max(x0,30+r*scale-1),
                                                       max(y0,42+b*scale-1)),outline='red',width=2)
                else:
                    ImageDraw.Draw(preview).text((35,45), 'EMPTY: no rectangle', fill='red')
                preview.save(folder/f'{percent:02d}_preview.png')
                if name == 'union':
                    tile = preview.resize((200,220), Image.Resampling.LANCZOS)
                    x,y = (index%5)*200,(index//5)*240
                    overview.paste(tile,(x,y))
                    caption = f"{percent}%: {row['width']} x {row['height']}" if row['bbox_half_open'] else f'{percent}%: empty'
                    ImageDraw.Draw(overview).text((x+5,y+222),caption,fill='black')
                rectangles.append(row)
        overview.save(output/'union_overview.png')
        write_json(output/'rectangles.json', rectangles)
        csv_rows = [{**row,'bbox_half_open': json.dumps(row['bbox_half_open']) if row['bbox_half_open'] else None}
                    for row in rectangles]
        write_csv(output/'rectangles.csv', csv_rows)
        write_json(output/'summary.json', dict(images=args.count, thresholds=percentages, feature=feature,
                   nonempty_sampled_union_images=nonempty_union_images,
                   maximum_frequencies={name: float(count.max()/args.count) for name,count in counts.items()},
                   coordinate_mode='stretch', rectangle_coordinates='0-based half-open [left,top,right,bottom)',
                   interpretation='in-sample descriptive statistics; user selects after viewing all results'))
        protocol.update(status='completed', completed_utc=datetime.now(timezone.utc).isoformat())
        write_json(output/'protocol.json', protocol)
    except Exception as error:
        protocol.update(status='failed', error=str(error))
        write_json(output/'protocol.json', protocol)
        raise
    print(f'Completed: {output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feature', choices=('rc',), default='rc')
    parser.add_argument('--data', type=Path, default=Path.home()/'datasets/PASCAL-S')
    parser.add_argument('--image-dir', default='datasets/imgs/pascal')
    parser.add_argument('--mask-dir', default='pred_masks')
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--count', type=int, default=850)
    parser.add_argument('--grid', type=int, default=320)
    parser.add_argument('--preview-count', type=int, default=12)
    parser.add_argument('--thresholds', type=int, nargs='+', default=DEFAULT_THRESHOLDS,
                        help='Integer frequency percentages, e.g. 80 85 88 90')
    parser.add_argument('--output', type=Path, required=True)
    try:
        run(parser.parse_args())
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
