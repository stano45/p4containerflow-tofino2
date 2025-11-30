#!/bin/bash
# Patch the p4-examples CMakeLists.txt to include t2na_load_balancer

CMAKE_FILE="open-p4studio/pkgsrc/p4-examples/CMakeLists.txt"

if [ ! -f "$CMAKE_FILE" ]; then
    echo "ERROR: $CMAKE_FILE not found"
    exit 1
fi

# Check if already patched
if grep -q "t2na_load_balancer" "$CMAKE_FILE"; then
    echo "CMakeLists.txt already patched for t2na_load_balancer"
    exit 0
fi

echo "Patching $CMAKE_FILE to add t2na_load_balancer..."

# Add t2na_load_balancer build target after t2na_counter_true_egress_accounting
sed -i '/add_custom_target(${t} DEPENDS \$<\$<BOOL:\${TOFINO2}>:${t}-tofino2>/a\
\
# t2na_load_balancer, only tofino2\
if (TOFINO2)\
  set (t t2na_load_balancer)\
  p4_build_target(${t} ${P4_tofino2_ARCHITECTURE} "tofino2" ${CMAKE_CURRENT_SOURCE_DIR}/p4_16_programs/${t}/${t}.p4)\
endif()\
add_custom_target(${t} DEPENDS $<$<BOOL:${TOFINO2}>:${t}-tofino2>)' "$CMAKE_FILE"

# Add t2na_load_balancer to the p4-16-programs group target
sed -i '/\$<\$<BOOL:\${TOFINO2}>:t2na_counter_true_egress_accounting>/a\
  $<$<BOOL:${TOFINO2}>:t2na_load_balancer>' "$CMAKE_FILE"

echo "Patch applied successfully"
