"""Area tops for the 8x8 DCT with a shared one-dimensional datapath."""

import re

from rtl_dct import emit_dct


def emit_shared_dct(root, profile, plan, version, bbox):
    """Reuse the source-checked shared core and expose distinct module names."""
    source_version = 'processed' if version == 'flip_only' else version
    if version == 'flip_only':
        plan = plan | {'exact_nodes': [], 'unit_replacements': []}
    rtl = emit_dct(root, profile, plan, source_version, bbox, area_scope=True)
    module = f'dct_{source_version}'
    return re.sub(
        rf'\b{module}(_core)?\b',
        lambda match: f'dct_shared_{version}' + (match[1] or ''),
        rtl,
    )
