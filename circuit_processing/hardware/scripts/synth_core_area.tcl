set app     $::env(DC_APP)
set variant $::env(DC_VARIANT)
set top     "${app}_${variant}"
set rtl     $::env(DC_RTL)
set out     $::env(DC_OUT)
set period  $::env(DC_PERIOD)

file mkdir "$out/WORK"
define_design_lib WORK -path "$out/WORK"
set_app_var target_library [list $::env(DC_LIB)]
set_app_var link_library [list * $::env(DC_LIB)]

set sources [list "$rtl/common/evoapprox_units.sv"]
if {$variant eq "processed" || $variant eq "flip_only"} {
    lappend sources "$rtl/common/roi_position.sv"
}
lappend sources "$rtl/$app/$variant.sv"
if {![analyze -format sverilog -library WORK $sources]} { exit 1 }
elaborate $top -library WORK
current_design $top
if {![link]} { exit 1 }
if {![check_design]} { exit 1 }

# A virtual clock gives the combinational cores the same I/O timing budget.
set clk [get_ports -quiet clk]
if {[sizeof_collection $clk] > 0} {
    create_clock -name core_clk -period $period $clk
    set data_inputs [remove_from_collection [all_inputs] $clk]
} else {
    create_clock -name core_clk -period $period
    set data_inputs [all_inputs]
}
set io_delay [expr {0.1 * $period}]
set_input_delay -clock core_clk $io_delay $data_inputs
set_output_delay -clock core_clk $io_delay [all_outputs]

compile
if {![check_design]} { exit 1 }
report_area -hierarchy            > "$out/area.rpt"
report_timing -max_paths 5        > "$out/timing.rpt"
report_constraint -all_violators  > "$out/constraints.rpt"
write -format verilog -hierarchy -output "$out/mapped.v"
exit 0
