#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "$0")" && pwd)
python="${PYTHON_BIN:-python3}"
pipeline="$root/circuit_processing/scripts/fixed_roi_pipeline.py"
start="${IMAGE_START:-1}"
count="${IMAGE_COUNT:-850}"
run="${RUN_DIR:-$root/circuit_processing/results/run_$(date +%Y%m%d_%H%M%S)}"

prepare() {
    : "${ROI_RUN:?Set ROI_RUN to the completed ROI statistics directory}"
    local source="${IMAGE_DIR:-$HOME/datasets/PASCAL-S/datasets/imgs/pascal}"
    test ! -e "$run" || { echo "Use a new RUN_DIR for prepare: $run" >&2; exit 1; }
    mkdir -p "$run"
    "$python" "$pipeline" images --source "$source" --start "$start" --count "$count" --output "$run/input_320"
    "$python" "$pipeline" freeze --roi-run "$ROI_RUN" --baseline original --output "$run/original/frozen.json"
    "$python" "$pipeline" freeze --roi-run "$ROI_RUN" --baseline changed \
        --reference "$run/original/frozen.json" --output "$run/changed/frozen.json"
    for family in original changed; do
        "$python" "$root/circuit_processing/hardware/scripts/generate_rtl.py" \
            --frozen "$run/$family/frozen.json" --output "$run/$family/rtl_320"
    done
    echo "Prepared RUN_DIR=$run"
}

verify() {
    local family="$1"
    local output="$run/$family/verify_$(date +%Y%m%d_%H%M%S)"
    "$python" "$pipeline" verify --config "$run/$family/frozen.json" \
        --images "$run/input_320" --start "$start" --count "$count" --output "$output"
}

synthesize() {
    local family="$1"
    DC_AREA_BUNDLE="$run/$family/rtl_320" \
        bash "$root/circuit_processing/hardware/scripts/run_dc_core_area.sh" all
}

case "${1:-help}" in
    setup)
        "$python" -m pip install -r "$root/roi_selection/requirements.txt" -r "$root/circuit_processing/requirements.txt"
        cmake -S "$root/circuit_processing" -B "$root/circuit_processing/build" -DCMAKE_BUILD_TYPE=Release
        cmake --build "$root/circuit_processing/build" -j "${BUILD_JOBS:-4}"
        ;;
    prepare) prepare ;;
    verify-original) : "${RUN_DIR:?Set RUN_DIR to the prepared run}"; verify original ;;
    verify-changed) : "${RUN_DIR:?Set RUN_DIR to the prepared run}"; verify changed ;;
    dc-original) : "${RUN_DIR:?Set RUN_DIR to the prepared run}"; synthesize original ;;
    dc-changed) : "${RUN_DIR:?Set RUN_DIR to the prepared run}"; synthesize changed ;;
    all)
        prepare
        verify original
        verify changed
        synthesize original
        synthesize changed
        ;;
    *)
        echo 'Usage: bash run.sh [setup|prepare|verify-original|verify-changed|dc-original|dc-changed|all]'
        echo 'prepare/all require ROI_RUN; later stages require RUN_DIR.'
        echo 'IMAGE_START=1 IMAGE_COUNT=850; verification uses a new output folder each time.'
        ;;
esac
