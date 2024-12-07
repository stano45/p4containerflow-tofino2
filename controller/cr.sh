export SOURCE_IP="10.0.0.4"
export TARGET_IP="10.0.0.2"
export TARGET_IDX="22"

curl -X POST http://127.0.0.1:5000/update_node \
    -H "Content-Type: application/json" \
    -d "{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\", \"eport\":\"${TARGET_IDX}\"}"
