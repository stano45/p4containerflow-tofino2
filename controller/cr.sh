export SOURCE_IP="10.0.0.3"
export TARGET_IP="10.0.0.1"

curl -X POST http://127.0.0.1:5000/migrateNode \
    -H "Content-Type: application/json" \
    -d "{\"old_ipv4\":\"${SOURCE_IP}\", \"new_ipv4\":\"${TARGET_IP}\"}"
