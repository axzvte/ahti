#!/usr/bin/env python3
"""Summarize mapped areas using each application's own baseline."""
import csv
import json
from pathlib import Path
import re
import sys


def summarize(directory):
    manifest = json.loads((directory/'rtl_manifest.json').read_text())
    applications = list(dict.fromkeys(target['application'] for target in manifest['targets']))
    rows = []
    for app in applications:
        if not (directory/f'{app}_baseline/area.rpt').is_file():
            continue
        def area(variant):
            path = directory/f'{app}_{variant}/area.rpt'
            return float(re.search(r'Total cell area:\s+([\d.]+)', path.read_text())[1])
        baseline, processed = area('baseline'), area('processed')
        flip = area('flip_only') if (directory/f'{app}_flip_only/area.rpt').is_file() else processed
        rows.append(dict(application=app, baseline_area=baseline, flip_only_area=flip, processed_area=processed,
                         flip_overhead=flip-baseline, compensation_overhead=processed-flip,
                         total_overhead=processed-baseline, total_overhead_percent=100*(processed-baseline)/baseline))
    if not rows:
        raise ValueError('No completed area reports found')
    path = directory/'area_summary.csv'
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(path.read_text(), end='')
    print(f'Area summary: {path}')


if __name__ == '__main__':
    summarize(Path(sys.argv[1]))
