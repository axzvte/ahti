#!/usr/bin/env bash
set -euo pipefail
scripts_dir=$(cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(cd -- "$scripts_dir/../../.." && pwd)
: "${DC_AREA_BUNDLE:?Set DC_AREA_BUNDLE to a generated RTL directory}"
export DC_LIB="${DC_LIB:-$HOME/techlib/nangate45/stdcells.db}"
export DC_PERIOD="${DC_PERIOD:-10}"
dc_shell="${DC_SHELL:-$HOME/Synopsys/dc2016/bin/dc_shell}"
compat_lib="${DC_COMPAT_LIB:-$HOME/synopsys-compat/lib}"
test -r "$DC_LIB" || { echo "Missing DC library: $DC_LIB" >&2; exit 1; }
test -r "$dc_shell" || { echo "Missing DC launcher: $dc_shell" >&2; exit 1; }
bundle=$(realpath -- "$DC_AREA_BUNDLE")
target_list=$(python3 - "$bundle" "${1:-all}" <<'PY'
from pathlib import Path
import hashlib,json,sys
root=Path(sys.argv[1])
manifest=json.loads((root/'manifest.json').read_text())
selected=[target for target in manifest['targets']
          if sys.argv[2]=='all' or target['application']==sys.argv[2]]
if not selected:
    raise ValueError('No matching application in manifest')
for target in selected:
    for source in target['sources']:
        path=root/source
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256'][source]:
            raise ValueError(f'Generated RTL changed: {path}')
    print(target['application'],target['version'])
PY
)
export DC_LIB="$(realpath -- "$DC_LIB")"
export DC_RTL="$bundle/rtl"
dc_shell=$(realpath -s -- "$dc_shell")
mkdir -p "$project_dir/circuit_processing/results/dc_area"
run_dir=$(mktemp -d "$project_dir/circuit_processing/results/dc_area/run.XXXXXX")
cp "$bundle/manifest.json" "$run_dir/rtl_manifest.json"
echo "Results: $run_dir"
while read -r app variant; do
    out="$run_dir/${app}_${variant}"
    mkdir -p "$out"
    echo "Synthesizing ${app}_${variant}"
    (
        cd "$out"
        export DC_APP="$app" DC_VARIANT="$variant" DC_OUT="$out"
        export LD_LIBRARY_PATH="$compat_lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        bash "$dc_shell" -f "$scripts_dir/synth_core_area.tcl" >dc.log 2>&1
    ) || { tail -n 60 "$out/dc.log" >&2; exit 1; }
    grep -m 1 'Total cell area:' "$out/area.rpt"
    test -s "$out/timing.rpt"
done <<< "$target_list"
python3 "$scripts_dir/summarize_area.py" "$run_dir"
