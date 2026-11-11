"""Load the current C descriptors or already encoded scalar IR."""
import json
from pathlib import Path
import circuit_ir


def load_profile(path):
    path = Path(path).expanduser().resolve()
    profile = json.loads(path.read_text(encoding='utf-8'))
    if profile.get('schema') == 3:
        return circuit_ir.validate_profile(profile)
    if profile.get('schema') != 2 or profile.get('application') != 'c':
        raise ValueError('Expected a C source descriptor or schema-3 scalar IR')
    from c_kernel_encoder import encode_profile
    return circuit_ir.validate_profile(encode_profile(profile, path.parent))
