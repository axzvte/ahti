"""Source-derived shared forward DCT datapath and sixteen-cycle scheduler."""
import hashlib
import json
from pathlib import Path
import tempfile
from rtl_emit import emit_graph
from rtl_area import area_position, area_roi_test


def core_profile(root, baseline):
    """Adapt only the encoder's double-output entry contract, retaining dct1d."""
    from c_kernel_encoder import encode_profile
    source=(root/'kernels/dct.c').read_text(encoding='utf-8')
    source+='\nvoid dct1d_forward(int input[8], double output[8]) { int w[8]; dct1d(input,w); for(int k=0;k<8;++k) output[k]=w[k]; }\n'
    descriptor=dict(schema=2,name='dct1d_rtl',application='c',source='core.c',entry='dct1d_forward',tile=[1,8],border='replicate',node_policy='marked',baseline=baseline,
        inputs=[dict(id=f'p{i}',dy=0,dx=i) for i in range(8)],outputs=[dict(dy=0,dx=i) for i in range(8)])
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory);(path/'core.c').write_text(source,encoding='utf-8')
        return encode_profile(descriptor,path)


def selected_phase(profile, core, plan):
    """Prove every invocation is a copy of the one-dimensional typed graph.

    Bind each row to the centered pixels, each column to preceding row outputs,
    then compare structural hashes including types, constants and group labels.
    Source location and call-stack metadata do not affect arithmetic identity.
    """
    if profile['ir']['tile']!=[8,8] or len(profile['ir']['inputs'])!=64:
        raise ValueError('DCT requires the original 8x8 source profile')
    if {x['node'] for x in profile['baseline']}!=set(range(32)):
        raise ValueError('DCT requires exactly the original 32 groups')
    ops=profile['ir']['operations']
    marked=[x for x in ops if 'unit_node' in x]
    cm=[x for x in core['ir']['operations'] if 'unit_node' in x]
    if len(marked)!=512 or len(cm)!=32: raise ValueError('DCT requires sixteen invocations of 32 groups')
    chosen=[i for i,x in enumerate(marked) if x['id']==plan['node']]
    if len(chosen)!=1 or marked[chosen[0]]['unit_node']!=plan['unit_node']:
        raise ValueError('DCT selected SSA site/group mismatch')
    def hashed(obj): return hashlib.sha256(json.dumps(obj,sort_keys=True).encode()).hexdigest()
    def digest_graph(operations,bindings):
        values=dict(bindings)
        for op in operations:
            args=[values[a] if isinstance(a,str) else hashed(['constant',a]) for a in op['args']]
            # The frontend inserts value-preserving assignment/cast scale1 nodes.
            if op['op']=='scale' and op['factor']==1: values[op['id']]=args[0];continue
            meta={k:v for k,v in op.items() if k not in ('id','args','source_coord','call_stack')}
            values[op['id']]=hashed([meta,args])
        return values
    initial={x['id']:hashed(['pixel',i]) for i,x in enumerate(profile['ir']['inputs'])}
    full=digest_graph(ops,initial)
    rows=[]
    for phase in range(16):
        batch=marked[phase*32:(phase+1)*32]
        if [x['unit_node'] for x in batch]!=[x['unit_node'] for x in cm]:
            raise ValueError('DCT invocation group topology changed')
        if phase<8:
            bindings={f'p{i}':hashed([dict(op='sub',integer=True,width=32,signed=True),[initial[f'p{phase*8+i}'],hashed(['constant',128])]]) for i in range(8)}
        else: bindings={f'p{i}':rows[i][phase-8] for i in range(8)}
        expected=digest_graph(core['ir']['operations'],bindings)
        if any(full[a['id']]!=expected[b['id']] for a,b in zip(batch,cm)):
            raise ValueError('DCT invocation arithmetic topology changed')
        if phase<8: rows.append([expected[x['value']] for x in core['ir']['outputs']])
    return chosen[0]//32, next(x['id'] for x in cm if x['unit_node']==plan['unit_node'])


def emit_dct(root:Path,profile:dict,plan:dict,version:str,bbox:list,*,area_scope=True)->str:
    if version not in ('baseline','processed'): raise ValueError('Unknown RTL version')
    if plan['mode']!='ir_internal': raise ValueError('DCT requires internal certified configuration')
    core=core_profile(Path(root),profile['baseline'])
    phase,site=selected_phase(profile,core,plan)
    module='dct_'+version
    arithmetic=emit_graph(core,module+'_core',[] if version=='baseline' else plan['exact_nodes'],
                          site if version=='processed' else None,plan['bit'],
                          unit_replacements=() if version=='baseline' else plan.get('unit_replacements', ()))
    if not area_scope:
        raise ValueError('Only fixed-320 area tops are supported')
    frame_ports = position_wires = ''
    roi_reg = ',block_inside' if version == 'processed' else ''
    roi_reset = 'block_inside<=0;' if version == 'processed' else ''
    roi_capture = f"   block_inside<={area_roi_test(bbox, 8)};\n" if version == 'processed' else ''
    position = area_position(8) if version == 'processed' else ''
    ready = 'active && !busy && !frame_start && rst_n' if version == 'processed' else '!busy && !frame_start && rst_n'
    position_control = position + f'assign in_ready={ready};\n'
    code=f'''// One source-derived 32-group forward core, reused for rows then columns.
module {module}(
 input wire clk,rst_n,frame_start,in_valid,
{frame_ports} input wire [511:0] samples,
 output wire in_ready, output reg out_valid,
 output reg [2047:0] coefficients
);
{position_wires}reg busy{roi_reg};reg [3:0] phase;
reg signed [31:0] data[0:63];reg signed [31:0] rows[0:63];
wire accept=in_valid && in_ready;
{position_control}wire flip_enable={"busy && block_inside && phase==4'd"+str(phase) if version=='processed' else "1'b0"};
'''
    for i in range(8):
        code+=f'wire signed [31:0] p{i}=phase<8 ? data[phase*8+{i}] : rows[{i}*8+(phase-8)];\nwire signed [31:0] y{i};\n'
    code+=f'{module}_core arithmetic(.flip_enable(flip_enable),'+','.join(f'.p{i}(p{i}),.y{i}(y{i})' for i in range(8))+');\n'
    code+=f'''integer i;
always @(posedge clk or negedge rst_n) begin
 if(!rst_n) begin busy<=0;phase<=0;{roi_reset}out_valid<=0;coefficients<=0;
  for(i=0;i<64;i=i+1) begin data[i]<=0;rows[i]<=0;end
 end else if(frame_start) begin busy<=0;phase<=0;{roi_reset}out_valid<=0;coefficients<=0;
 end else begin
  out_valid<=0;
  if(accept) begin
   busy<=1;phase<=0;
{roi_capture}   for(i=0;i<64;i=i+1) data[i]<=$signed({{24'd0,samples[i*8+:8]}})-32'sd128;
  end else if(busy) begin
'''
    for i in range(8): code+=f'   if(phase<8) rows[phase*8+{i}]<=y{i}; else coefficients[({i}*8+phase-8)*32+:32]<=y{i};\n'
    code+='''   if(phase==15) begin busy<=0;out_valid<=1;end
   else phase<=phase+1'b1;
  end
 end
end
endmodule
'''
    return arithmetic+'\n'+code

