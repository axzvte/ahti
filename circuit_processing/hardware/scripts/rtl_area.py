"""Core-level area wrappers for fixed-size input frames."""
from rtl_emit import emit_graph


FRAME_SIDE = 320


def area_position_rtl():
    return f"""// Fixed square frame; coordinates are sample indices, in pixels or blocks.
module roi_position #(
  parameter integer STEP=1
)(
  input wire clk,rst_n,frame_start,advance,
  output reg active,
  output reg [$clog2({FRAME_SIDE}/STEP)-1:0] x,y
);
localparam integer SIDE={FRAME_SIDE}/STEP;
localparam [$clog2(SIDE)-1:0] LAST=SIDE-1;

always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
    active<=0; x<=0; y<=0;
  end else if(frame_start) begin
    active<=1; x<=0; y<=0;
  end else if(advance && active) begin
    if(x!=LAST) x<=x+1'b1;
    else begin
      x<=0;
      if(y!=LAST) y<=y+1'b1;
      else active<=0;
    end
  end
end
endmodule
"""


def area_position(step):
    width = (FRAME_SIDE//step-1).bit_length()
    return f"""wire active;
wire [{width-1}:0] x,y;
roi_position #(.STEP({step})) position(
 .clk(clk),.rst_n(rst_n),.frame_start(frame_start),.advance(accept),
 .active(active),.x(x),.y(y));
"""


def area_roi_test(bbox, step):
    """Convert half-open pixel bounds to indices of fully contained samples."""
    left, top, right, bottom = bbox
    left, top = (left+step-1)//step, (top+step-1)//step
    right, bottom = right//step, bottom//step
    width = (FRAME_SIDE//step-1).bit_length()
    return f"x>={width}'d{left} && x<{width}'d{right} && y>={width}'d{top} && y<{width}'d{bottom}"


def emit_area_stencil(profile, plan, app, version, bbox):
    """Wrap the same window datapath in all variants and add ROI control to C."""
    top = f'{app}_{version}'
    processed = version == 'processed'
    exact_nodes = [] if version == 'baseline' else plan['exact_nodes']
    core = emit_graph(profile, top+'_core', exact_nodes,
                      plan['node'] if processed else None, plan['bit'],
                      unit_replacements=() if version == 'baseline' else plan.get('unit_replacements', ()))
    inputs = profile['ir']['inputs']
    ports = control = ''
    enable = "1'b0"
    if processed:
        ports = ' input wire clk,rst_n,frame_start,in_valid,\n output wire in_ready,\n'
        left = -min(item['dx'] for item in inputs)
        right = max(item['dx'] for item in inputs)
        top_edge = -min(item['dy'] for item in inputs)
        bottom = max(item['dy'] for item in inputs)
        control = f"""wire accept=in_valid && in_ready;
{area_position(1)}assign in_ready=active && rst_n && !frame_start;
wire interior=x>=9'd{left} && x<9'd{FRAME_SIDE-right} &&
 y>=9'd{top_edge} && y<9'd{FRAME_SIDE-bottom};
wire flip_enable=accept && interior && ({area_roi_test(bbox, 1)});
"""
        enable = 'flip_enable'
    operands = ''.join(f" .{item['id']}($signed({{24'd0,samples[{8*i} +: 8]}})),\n"
                       for i, item in enumerate(inputs))
    return core+f"""
// Area top: preassembled windows and combinational output in every variant.
module {top}(
{ports} input wire [{8*len(inputs)-1}:0] samples,
 output wire [7:0] pixel
);
{control}wire signed [31:0] core_pixel;
{top}_core arithmetic(.flip_enable({enable}),
{operands} .y0(core_pixel));
assign pixel=core_pixel[7:0];
endmodule
"""


