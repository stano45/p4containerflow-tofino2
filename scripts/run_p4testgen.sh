PROGRAM_NAME="t2na_load_balancer"
PROGRAM_DIR="/home/vagrant/sde/bf-sde-9.13.4/pkgsrc/p4-examples/p4_16_programs/$PROGRAM_NAME"
STRATEGY="GREEDY_STATEMENT_SEARCH"
# STRATEGY="RANDOM_BACKTRACK" 
# STRATEGY="RANDOM_STATEMENT_SEARCH"
MAX_TESTS=200

p4testgen --target tofino2 \
          --arch t2na \
          --std p4-16 \
          -I p4_include \
          --test-backend PTF \
          --seed 1000 \
          --max-tests $MAX_TESTS \
          --out-dir $PROGRAM_DIR \
          -I $PROGRAM_DIR/common $PROGRAM_DIR/$PROGRAM_NAME.p4 \
          --path-selection $STRATEGY \
          --track-coverage STATEMENTS