"""Typed integer-valued scalar IR to synthesizable combinational SystemVerilog.

A floating declaration is lowered only when every value is provably integer
valued and the supported source bound (2**40) fits double exactly. Fractional
coefficients are rejected; this backend does not silently quantize them.
"""
import math
import re


def literal(value, width=64):
    if not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value:
        raise ValueError('Fractional constants require an explicit fixed/floating-point backend')
    value=int(value)
    if not -(1<<(width-1)) <= value < (1<<(width-1)):
        raise ValueError('RTL signed literal overflow')
    return ('-' if value<0 else '')+f"{width}'sd{abs(value)}"


def emit_graph(profile, module, exact_nodes, flip_site=None, flip_bit=0, *, unit_replacements=()):
    """Return a module with signed32 p/IR-input ports, y0.. outputs and flip_enable."""
    import circuit_ir
    from unit_policy import with_units
    profile = with_units(profile, unit_replacements)
    circuit_ir.validate_profile(profile)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',module):
        raise ValueError('Invalid RTL module name')
    ir=profile['ir']
    all_ids=[x['id'] for x in ir['inputs']+ir['operations']]
    if len(set(all_ids))!=len(all_ids) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',x) for x in all_ids):
        raise ValueError('IR identifiers must be legal distinct RTL identifiers')
    reserved={'flip_enable'}|{f'y{i}' for i in range(len(ir['outputs']))}
    if reserved.intersection(all_ids): raise ValueError('IR identifiers collide with RTL ports')
    units={x['node']:x['unit'] for x in profile['baseline']}
    exact=set(exact_nodes)
    if not exact <= set(units): raise ValueError('Unknown exact replacement group')
    if flip_site is not None:
        selected=next((x for x in ir['operations'] if x['id']==flip_site),None)
        if selected is None or 'unit_node' not in selected or not selected['integer'] or not 0<=flip_bit<=11:
            raise ValueError('Unsupported internal flip site or bit')
    ports=['input wire flip_enable']
    ports += [f"input wire signed [31:0] {x['id']}" for x in ir['inputs']]
    ports += [f'output wire signed [31:0] y{i}' for i in range(len(ir['outputs']))]
    lines=['// Generated from the typed scalar IR; do not edit arithmetic by hand.',f'module {module}(\n  '+',\n  '.join(ports)+'\n);']
    lines += ['function automatic signed [63:0] round_shift64;',
        '  input signed [63:0] v; input integer bits;',
        '  reg signed [63:0] half;',
        '  begin',
        '    if (bits==0) round_shift64=v;',
        "    else begin half=64'sd1 << (bits-1);",
        '      if (v>=0) round_shift64=(v+half) >>> bits;',
        '      else round_shift64=-(((-v)+half) >>> bits);',
        '    end', '  end','endfunction']
    known={x['id']:True for x in ir['inputs']}
    def arg(value,width):
        return f'$signed({value})' if isinstance(value,str) else literal(value,width)
    for op in ir['operations']:
        name,kind,width=op['id'],op['op'],op['width']
        # Source declarations may be double after integer-to-double casts. All
        # supported values here remain integers, so no arithmetic quantization.
        if kind=='scale': literal(op['factor'],width)
        if kind=='clamp': literal(op['min'],width); literal(op['max'],width)
        if any(not known.get(a,True) for a in op['args'] if isinstance(a,str)):
            raise ValueError('Fractional arithmetic is unsupported')
        a=arg(op['args'][0],width)
        b=arg(op['args'][1],width) if len(op['args'])>1 else None
        raw=name+'_raw' if name==flip_site else name
        group=op.get('unit_node')
        unit=units.get(group,'exact') if group not in exact else 'exact'
        lines.append(f'wire signed [{width-1}:0] {raw};')
        if kind in ('add','sub') and group is not None and unit!='exact':
            lines += [f'wire signed [31:0] {name}_lhs = {a};',
                f'wire signed [31:0] {name}_rhs = '+(f'-({b});' if kind=='sub' else f'{b};'),
                f'wire [12:0] {name}_sum;',
                f'approx_{unit} cell_{name}(.A({name}_lhs[11:0]),.B({name}_rhs[11:0]),.Y({name}_sum));',
                f'assign {raw} = $signed({name}_sum);']
        else:
            if kind=='add': expression=f'{a} + {b}'
            elif kind=='sub': expression=f'{a} - {b}'
            elif kind=='scale': expression=f'{a} * ({literal(op["factor"],width)})'
            elif kind=='abs': expression=f'({a} < {literal(0,width)}) ? -({a}) : {a}'
            elif kind=='shift': expression=f'{a} >>> {op["bits"]}'
            elif kind=='round_shift': expression=f'round_shift64({a}, {op["bits"]})'
            elif kind in ('round_even','trunc'): expression=a
            elif kind=='clamp':
                lo,hi=literal(op['min'],width),literal(op['max'],width)
                expression=f'({a} < {lo}) ? {lo} : (({a} > {hi}) ? {hi} : {a})'
            else: raise ValueError(f'Unsupported RTL operation {kind}')
            lines.append(f'assign {raw} = {expression};')
        if name==flip_site:
            lines.append(f"wire signed [{width-1}:0] {name} = {raw} ^ (flip_enable ? {width}'sd{1<<flip_bit} : {width}'sd0);")
        known[name]=True
    for i,out in enumerate(ir['outputs']):
        lines.append(f'assign y{i} = {arg(out["value"],32)};')
    lines+=['endmodule','']
    return '\n'.join(lines)
