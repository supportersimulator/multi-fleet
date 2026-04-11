#!/bin/bash
# Set up NATS clustering for multi-machine fleet.
# Each machine runs a local NATS server + cluster routes via SSH tunnel.
#
# Usage:
#   ./nats-cluster-setup.sh              # Generate config + print instructions
#   ./nats-cluster-setup.sh --start      # Generate config + start nats-server
#   ./nats-cluster-setup.sh --tunnels    # Set up SSH tunnels to other nodes
#
# Env vars:
#   MULTIFLEET_NODE_ID  — this node's identity (default: hostname -s)
#   NATS_PORT           — client port (default: 4222)
#   CLUSTER_PORT        — cluster gossip port (default: 6222)
#   FLEET_NODES         — comma-separated peer node IDs (default: mac1,mac2,mac3)

set -euo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
NATS_PORT="${NATS_PORT:-4222}"
CLUSTER_PORT="${CLUSTER_PORT:-6222}"
FLEET_NODES="${FLEET_NODES:-mac1,mac2,mac3}"
CONF_FILE="/tmp/fleet-nats-cluster.conf"
LOG_FILE="/tmp/fleet-nats-cluster.log"

# ── Node-to-tunnel port mapping ──
# Each remote node gets a unique local tunnel port for cluster routing.
declare -A TUNNEL_PORTS
_port=6223
IFS=',' read -ra NODES <<< "$FLEET_NODES"
for node in "${NODES[@]}"; do
    node=$(echo "$node" | xargs)  # trim whitespace
    if [[ "$node" != "$NODE_ID" ]]; then
        TUNNEL_PORTS[$node]=$_port
        ((_port++))
    fi
done

# ── Generate cluster config ──
generate_config() {
    local routes=""
    for node in "${!TUNNEL_PORTS[@]}"; do
        local tport=${TUNNEL_PORTS[$node]}
        routes+="        nats-route://127.0.0.1:${tport}  # ${node} via SSH tunnel"$'\n'
    done

    cat > "$CONF_FILE" << EOF
# Fleet NATS Cluster Config — node: ${NODE_ID}
# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

port: ${NATS_PORT}
server_name: fleet-${NODE_ID}

cluster {
    name: fleet-cluster
    port: ${CLUSTER_PORT}

    routes [
${routes}    ]
}

# Logging
log_file: "${LOG_FILE}"
debug: false
trace: false

# Limits
max_payload: 1048576  # 1MB
max_connections: 64
EOF

    echo "Config written: ${CONF_FILE}"
    echo "  Node:         ${NODE_ID}"
    echo "  Client port:  ${NATS_PORT}"
    echo "  Cluster port: ${CLUSTER_PORT}"
    echo "  Peers:        ${!TUNNEL_PORTS[*]}"
}

# ── Set up SSH tunnels ──
setup_tunnels() {
    echo "Setting up SSH tunnels for cluster routing..."
    for node in "${!TUNNEL_PORTS[@]}"; do
        local tport=${TUNNEL_PORTS[$node]}
        # Check if tunnel already running
        if lsof -i ":${tport}" >/dev/null 2>&1; then
            echo "  ${node}: tunnel already active on :${tport}"
            continue
        fi
        echo "  ${node}: ssh -fNL ${tport}:127.0.0.1:${CLUSTER_PORT} ${node}"
        ssh -fNL "${tport}:127.0.0.1:${CLUSTER_PORT}" "${node}" 2>/dev/null || \
            echo "    WARN: SSH tunnel to ${node} failed (is SSH configured?)"
    done
    echo "Tunnels ready. Cluster routes will connect on next nats-server start."
}

# ── Main ──
generate_config

case "${1:-}" in
    --start)
        if ! command -v nats-server >/dev/null 2>&1; then
            echo "ERROR: nats-server not found. Install: brew install nats-server"
            exit 1
        fi
        echo "Starting NATS server..."
        nats-server -c "$CONF_FILE" &
        echo "NATS PID: $!"
        echo "Client:  nats://127.0.0.1:${NATS_PORT}"
        echo "Cluster: nats-route://127.0.0.1:${CLUSTER_PORT}"
        ;;
    --tunnels)
        setup_tunnels
        ;;
    *)
        echo ""
        echo "Next steps:"
        echo "  1. Set up SSH tunnels:  $0 --tunnels"
        echo "  2. Start NATS server:   $0 --start"
        echo "  3. Or start manually:   nats-server -c ${CONF_FILE}"
        ;;
esac
