"""Three-panel native-resolution comparisons with PSNR labels."""
from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

METRICS = ('baseline_global_psnr', 'processed_global_psnr', 'processed_roi_psnr')


def report_font(size):
    try:
        return ImageFont.truetype('DejaVuSans.ttf', size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def psnr_text(value):
    number = float(value)
    if math.isnan(number):
        raise ValueError('Cannot annotate an undefined PSNR')
    return 'inf dB' if number == math.inf else f'{number:.2f} dB'


def save_comparison(source, destination, record, *, processed_label='Processed (after compensation + flip)'):
    """Save exact/baseline/processed together; record values match images.csv."""
    source, destination = Path(source), Path(destination)
    images = []
    for name in ('exact', 'baseline', 'processed'):
        with Image.open(source/f'{name}.png') as image:
            images.append(image.convert('RGB'))
    width, height = images[0].size
    if any(image.size != (width, height) for image in images):
        raise ValueError('Comparison images must have identical dimensions')
    margin, panel_width = 16, max(360, width)
    image_top = 92
    canvas = Image.new('RGB', (3*panel_width+4*margin, image_top+height+92), 'white')
    draw = ImageDraw.Draw(canvas)
    font, title_font = report_font(18), report_font(20)
    image_id = int(Path(record['image']).stem)
    draw.text((margin, 16), f"{record['application']} | Image {image_id:04d}", font=title_font, fill='#282828')
    labels = ('Exact reference', 'Baseline (before processing)', processed_label)
    for index, (image, label) in enumerate(zip(images, labels)):
        left = margin+index*(panel_width+margin)
        draw.text((left, 60), label, font=font, fill='#282828')
        canvas.paste(image, (left+(panel_width-width)//2, image_top))
    footer = image_top+height+12
    draw.text((margin, footer), 'Reference for all PSNR values', font=font, fill='#505050')
    draw.text((2*margin+panel_width, footer),
              'Global PSNR: '+psnr_text(record['baseline_global_psnr']), font=font, fill='#282828')
    processed_left = 3*margin+2*panel_width
    draw.text((processed_left, footer),
              'Global PSNR: '+psnr_text(record['processed_global_psnr']), font=font, fill='#282828')
    draw.text((processed_left, footer+28),
              'ROI PSNR: '+psnr_text(record['processed_roi_psnr']), font=font, fill='#282828')
    metadata = PngInfo()
    for key in ('application', 'image', *METRICS):
        metadata.add_text(key, str(record[key]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.png.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        canvas.save(temporary, format='PNG', pnginfo=metadata)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
