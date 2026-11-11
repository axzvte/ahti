#!/usr/bin/env python3
"""Generate current 0.5-threshold U2-Net masks into an explicit result directory."""
import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from infer_u2net import infer, load_model, preprocess, resize_probability_map, select_device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path.home()/'datasets/PASCAL-S')
    parser.add_argument('--repo', type=Path, default=Path(__file__).parent/'third_party/U-2-Net')
    parser.add_argument('--weights', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--count', type=int, default=850)
    parser.add_argument('--device', choices=('auto','cpu','cuda'), default='auto')
    parser.add_argument('--skip-existing', action='store_true')
    args = parser.parse_args()
    images = args.data.expanduser().resolve()/'datasets/imgs/pascal'
    output = args.output.expanduser().resolve()
    if (args.start < 1 or args.count < 1 or output.is_relative_to(images)
            or images.is_relative_to(output)):
        raise ValueError('Invalid range or output directory')
    files = [images/f'{n}.jpg' for n in range(args.start, args.start+args.count)]
    if not all(path.is_file() for path in files):
        raise ValueError('Missing numbered input image')
    if output.exists() and not args.skip_existing:
        raise ValueError('Use a new output directory, or --skip-existing to resume')
    masks, saliency = output/'pred_masks', output/'pred_saliency'
    masks.mkdir(parents=True, exist_ok=True)
    saliency.mkdir(exist_ok=True)
    device = select_device(args.device)
    model = load_model(args.repo.expanduser(), args.weights.expanduser(), device)
    for index, path in enumerate(files, 1):
        mask_path, sal_path = masks/f'{path.stem}_mask.png', saliency/f'{path.stem}_saliency.png'
        if args.skip_existing and mask_path.is_file() and sal_path.is_file():
            continue
        with Image.open(path) as image:
            rgb = image.convert('RGB')
        probability = infer(model, preprocess(rgb), device)
        response = resize_probability_map(probability, rgb.size)
        Image.fromarray(response).save(sal_path)
        Image.fromarray((response >= round(0.5*255)).astype(np.uint8)*255).save(mask_path)
        print(f'[{index}/{len(files)}] {path.name}', flush=True)
    print(f'Masks: {masks}')


if __name__ == '__main__':
    main()
