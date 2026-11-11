"""Emit the area-scope DWT with one scheduled, fixed-point lifting datapath."""
import hashlib

from rtl_area import area_position, area_roi_test

# The schedule and reduced widths below implement this audited C kernel.
KERNEL_SHA256 = 'd21d73ff597799f19941a7617aa0473c66d78a856191cd9808a05450474589d5'
GROUPS = {2, *range(64, 72)}
DWT_LATENCY = 1152


def _lane_adder(units, name, groups, lhs, rhs):
    """Use one physical adder per approximate implementation in this stage."""
    lines = [f'wire signed [15:0] {name}_lhs = {lhs}, {name}_rhs = {rhs};']
    choices = {unit: [lane for lane, group in enumerate(groups) if units[group] == unit]
               for unit in sorted({units[group] for group in groups} - {'exact'})}
    expression = f'{name}_lhs + {name}_rhs'
    for unit, lanes in choices.items():
        tag = f'{name}_{unit}'
        lines += [
            f'wire signed [12:0] {tag};',
            f'approx_{unit} add_{tag}(.A({name}_lhs[11:0]),.B({name}_rhs[11:0]),.Y({tag}));',
        ]
        lane_match = ' || '.join(f"lane == 2'd{lane}" for lane in lanes)
        expression = f"final_predict && ({lane_match}) ? $signed({tag}) : ({expression})"
    lines.append(f'wire signed [15:0] {name} = {expression};')
    return '\n'.join(lines)


def emit_shared_dwt(root, profile, plan, version, bbox):
    """Generate a baseline or processed shared DWT top."""
    if version not in ('baseline', 'processed'):
        raise ValueError('Unknown shared DWT version')
    source = root / 'kernels/dwt97.c'
    digest = hashlib.sha256(source.read_text(encoding='utf-8').encode()).hexdigest()
    raw_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if (digest != KERNEL_SHA256 or profile['source']['sha256'] != raw_digest
            or profile['source']['entry'] != 'dwt97_kernel'):
        raise ValueError('Review the shared DWT schedule against the changed C kernel')
    ir = profile['ir']
    order = [(i//8, i%8) for i in range(64)]
    if ir['tile'] != [8, 8] or any(
        [(p['dy'], p['dx']) for p in ir[ports]] != order for ports in ('inputs', 'outputs')
    ):
        raise ValueError('Shared DWT requires row-major 8x8 ports')
    units = {item['node']: item['unit'] for item in profile['baseline']}
    if units.keys() != GROUPS or units[2] != 'exact':
        raise ValueError('Shared DWT requires exact forward lifting and the audited inverse groups')
    if plan['mode'] != 'ir_internal':
        raise ValueError('Shared DWT requires an internal flip plan')
    if version == 'processed':
        replacements = {item['node']: item['unit'] for item in plan.get('unit_replacements', ())}
        replacements.update(dict.fromkeys(plan['exact_nodes'], 'exact'))
        if not replacements.keys() <= GROUPS - {2}:
            raise ValueError('Shared DWT compensation requires a final-row lifting group')
        units.update(replacements)
    group = plan['unit_node']
    sites = [op['id'] for op in ir['operations'] if op.get('unit_node') == group]
    if (group not in range(64, 72) or len(sites) != 8 or plan['node'] not in sites
            or not 0 <= plan['bit'] <= 11):
        raise ValueError('Shared DWT requires a certified final-row lifting site')
    line = sites.index(plan['node'])
    lane = (group - 64) // 2
    mask = 1 << plan['bit']
    top = f'dwt97_shared_{version}'
    control = ''
    ready = "state == IDLE && rst_n && !frame_start"
    flip_neighbor = 'neighbor_sum'
    flip_result = 'lift_result'
    if version == 'processed':
        ready += ' && active'
        control = f"""{area_position(8)}
reg flip_tile;
always @(posedge clk or negedge rst_n) begin
  if (!rst_n) flip_tile <= 1'b0;
  else if (frame_start) flip_tile <= 1'b0;
  else if (accept) flip_tile <= {area_roi_test(bbox, 8)};
end
wire flip_site = flip_tile && final_predict && line == 3'd{line} && lane == 2'd{lane};
"""
        if group % 2:
            flip_result += f" ^ (flip_site ? 16'sd{mask} : 16'sd0)"
        else:
            flip_neighbor += f" ^ (flip_site ? 16'sd{mask} : 16'sd0)"

    neighbor = _lane_adder(units, 'neighbor_sum', range(64, 72, 2), 'neighbor_a', 'neighbor_b')
    update = _lane_adder(units, 'lift_result', range(65, 72, 2), 'target_sample',
                         'inverse ? -rounded_product : rounded_product')
    return f"""// Fixed-size 8x8 CDF 9/7. One lane per clock, one multiplier for all phases.
// Each line takes eight load, sixteen lift, eight store cycles; column passes
// also take eight scale cycles. Output is valid {DWT_LATENCY} clocks after accept.
module {top}(
 input wire clk,rst_n,frame_start,in_valid,
 output wire in_ready,
 output reg out_valid,
 input wire [511:0] samples,
 output wire [511:0] pixels
);
localparam [2:0] IDLE=0, LOAD=1, LIFT=2, SCALE=3, STORE=4;
localparam [1:0] FORWARD_ROW=0, FORWARD_COL=1, INVERSE_COL=2, INVERSE_ROW=3;
reg [2:0] state, line, offset;
reg [1:0] phase, stage, lane;
wire accept = in_valid && in_ready;
wire vertical = phase == FORWARD_COL || phase == INVERSE_COL;
wire inverse = phase == INVERSE_COL || phase == INVERSE_ROW;
wire final_predict = phase == INVERSE_ROW && stage == 0;
{control}
assign in_ready = {ready};
wire [5:0] tile_address = vertical ? {{offset,line}} : {{line,offset}};
// The four exact forward lifts bound normalized bands by 18,564 in magnitude;
// inverse scaling recovers the integer coefficient, then the lifts retrace the
// forward intermediates until the guarded final predict. 16 bits also cover
// its signed 13-bit adder results and the certified XOR bit.
reg signed [15:0] tile_mem [0:63];
reg signed [15:0] line_mem [0:7];
wire [2:0] target = {{lane,~stage[0]}};
wire [2:0] adjacent = {{lane,stage[0]}};
wire [1:0] mirror_lane = stage[0]
  ? (lane == 0 ? lane : lane-2'd1)
  : (lane == 3 ? lane : lane+2'd1);
wire [2:0] other = {{mirror_lane,stage[0]}};

function automatic signed [15:0] clip_even(input signed [15:0] value);
  clip_even = value < -16'sd512 ? -16'sd512 :
              value > 16'sd511 ? 16'sd511 : value;
endfunction
function automatic signed [15:0] clip_odd(input signed [15:0] value);
  clip_odd = value < -16'sd1024 ? -16'sd1024 :
             value > 16'sd1023 ? 16'sd1023 : value;
endfunction
wire signed [15:0] neighbor_a = final_predict ? clip_even(line_mem[adjacent]) : line_mem[adjacent];
wire signed [15:0] neighbor_b = final_predict ? clip_even(line_mem[other]) : line_mem[other];
wire signed [15:0] addressed_sample = line_mem[state == LIFT ? target : offset];
wire signed [15:0] target_sample = final_predict ? clip_odd(addressed_sample) : addressed_sample;
{neighbor}
wire signed [15:0] multiplier_input = state == SCALE ? addressed_sample : ({flip_neighbor});
wire [1:0] band = {{1'b0,line[0]}} + {{1'b0,offset[0]}};
reg signed [21:0] coefficient;
reg [4:0] shift;
always @* begin
  if (state == SCALE) begin
    if (inverse) begin
      shift = 21;
      case (band)
        0: coefficient = 22'sd793418;
        1: coefficient = 22'sd1048576;
        default: coefficient = 22'sd1385809;
      endcase
    end else begin
      shift = 14;
      case (band)
        0: coefficient = 22'sd43306;
        1: coefficient = 22'sd32768;
        default: coefficient = 22'sd24794;
      endcase
    end
  end else begin
    shift = 15;
    case (stage)
      0: coefficient = -22'sd51974;
      1: coefficient = -22'sd1736;
      2: coefficient = 22'sd28931;
      default: coefficient = 22'sd14533;
    endcase
  end
end
wire signed [37:0] product = multiplier_input * coefficient;
wire [37:0] magnitude = product[37] ? -product : product;
wire [37:0] half = inverse && state == SCALE ? 38'd1048576 :
                  state == SCALE ? 38'd8192 : 38'd16384;
wire [15:0] rounded_magnitude = (magnitude + half) >> shift;
wire signed [15:0] rounded_product = product[37] ? -rounded_magnitude : rounded_magnitude;
{update}
wire signed [15:0] lifted_sample = {flip_result};
wire [7:0] byte_output = addressed_sample < -16'sd128 ? 8'd0 :
                         addressed_sample > 16'sd127 ? 8'd255 :
                         addressed_sample[7:0] ^ 8'h80;
// On even samples the byte clamp absorbs the earlier final-predict guard.
wire signed [15:0] stored_sample = phase == INVERSE_ROW
  ? $signed({{8'd0,byte_output}}) : addressed_sample;

for (genvar i=0; i<64; i=i+1) begin : tile_storage
  wire [7:0] centered = samples[8*i +: 8] ^ 8'h80;
  always @(posedge clk) begin
    if (accept) tile_mem[i] <= $signed(centered);
    else if (state == STORE && tile_address == i && !frame_start)
      tile_mem[i] <= stored_sample;
  end
  assign pixels[8*i +: 8] = tile_mem[i][7:0];
end
for (genvar i=0; i<8; i=i+1) begin : line_storage
  always @(posedge clk) begin
    if (state == LOAD && offset == i) line_mem[i] <= tile_mem[tile_address];
    else if (state == SCALE && offset == i) line_mem[i] <= rounded_product;
    else if (state == LIFT && target == i) line_mem[i] <= lifted_sample;
  end
end

always @(posedge clk or negedge rst_n) begin
  if (!rst_n) begin
    state <= IDLE; phase <= FORWARD_ROW; line <= 0; offset <= 0;
    stage <= 0; lane <= 0; out_valid <= 0;
  end else begin
    out_valid <= 0;
    if (frame_start) state <= IDLE;
    else case (state)
      IDLE: if (accept) begin
        state <= LOAD; phase <= FORWARD_ROW; line <= 0; offset <= 0;
      end
      LOAD: begin
        offset <= offset + 1'b1;
        if (offset == 7) begin
          stage <= inverse ? 2'd3 : 2'd0;
          lane <= 0;
          state <= phase == INVERSE_COL ? SCALE : LIFT;
        end
      end
      LIFT: begin
        lane <= lane + 1'b1;
        if (lane == 3) begin
          if (inverse ? stage == 0 : stage == 3)
            state <= phase == FORWARD_COL ? SCALE : STORE;
          else stage <= inverse ? stage - 1'b1 : stage + 1'b1;
        end
      end
      SCALE: begin
        offset <= offset + 1'b1;
        if (offset == 7) state <= inverse ? LIFT : STORE;
      end
      STORE: begin
        offset <= offset + 1'b1;
        if (offset == 7) begin
          line <= line + 1'b1;
          state <= LOAD;
          if (line == 7) begin
            if (phase == INVERSE_ROW) begin
              state <= IDLE;
              out_valid <= 1;
            end else phase <= phase + 1'b1;
          end
        end
      end
      default: state <= IDLE;
    endcase
  end
end
endmodule
"""
