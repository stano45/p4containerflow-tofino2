"""
Generate Mermaid diagrams for the technical and experiment reports.

Usage:
    cd docs/figures
    uv run mermaido install   # first time only
    uv run python main.py
"""

import mermaido
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "out"

DIAGRAMS: dict[str, str] = {
    "v1model_pipeline": """
flowchart LR
    A[Packet In] --> B[Parser]
    B --> C[Verify\nChecksum]
    C --> D[Ingress]
    D --> E[Egress]
    E --> F[Compute\nChecksum]
    F --> G[Deparser]
    G --> H[Packet Out]

    style A fill:#e8f5e9,stroke:#388e3c
    style H fill:#e8f5e9,stroke:#388e3c
""",
    "t2na_pipeline": """
flowchart TB
    A[Packet In] --> IG

    subgraph IG[INGRESS]
        direction LR
        B[IngressParser\n\nparse headers\nchecksum verify] --> C[Ingress\n\nmatch-action\ntables]
        C --> D[IngressDeparser\n\nchecksum compute\nemit headers]
    end

    IG --> TM[Traffic Manager\n\nqueuing, replication,\nscheduling]

    TM --> EG

    subgraph EG[EGRESS]
        direction LR
        E[EgressParser\n\nparse headers\nchecksum verify] --> F[Egress\n\nmatch-action\ntables]
        F --> G[EgressDeparser\n\nchecksum compute\nemit headers]
    end

    EG --> H[Packet Out]

    style A fill:#e8f5e9,stroke:#388e3c
    style H fill:#e8f5e9,stroke:#388e3c
    style TM fill:#fff3e0,stroke:#f57c00
    style IG fill:#e3f2fd,stroke:#1976d2
    style EG fill:#fce4ec,stroke:#c62828
""",
    "ingress_apply_flow": """
flowchart TD
    A[Packet In] --> B{ARP valid?}
    B -- yes --> C[arp_forward]
    C --> D1[bypass_egress]
    D1 --> Z1[Packet Out]

    B -- no --> E{IPv4 valid\n&& TTL >= 1?}
    E -- no --> DROP[Drop]

    E -- yes --> F[node_selector\n\nActionSelector: hash 5-tuple\nrewrite dst to backend IP]
    F --> G{is_lb_packet\n== false?}
    G -- "no: server reply" --> H[client_snat\n\nrewrite src IP to VIP]
    G -- "yes" --> I[forward\n\nset egress port]
    H --> I
    I --> D2[bypass_egress]
    D2 --> Z2[Packet Out]

    style A fill:#e8f5e9,stroke:#388e3c
    style Z1 fill:#e8f5e9,stroke:#388e3c
    style Z2 fill:#e8f5e9,stroke:#388e3c
    style DROP fill:#ffcdd2,stroke:#c62828
""",
    "packet_flow": """
sequenceDiagram
    participant C as Client (10.0.0.100:54321)
    participant T as Tofino Pipeline
    participant B as Backend (10.0.0.2:8080)

    Note over C,B: Client-to-Server (request)
    C->>T: src 10.0.0.100:54321, dst 10.0.0.10 (VIP):8080
    Note over T: node_selector: match VIP,<br/>hash(5-tuple) → member 0,<br/>rewrite dst → 10.0.0.2,<br/>is_lb_packet = true
    Note over T: client_snat: SKIPPED
    Note over T: forward: dst 10.0.0.2 → port 140
    Note over T: deparser: recompute checksums
    T->>B: src 10.0.0.100:54321, dst 10.0.0.2:8080

    Note over C,B: Server-to-Client (response)
    B->>T: src 10.0.0.2:8080, dst 10.0.0.100:54321
    Note over T: node_selector: NO MATCH,<br/>is_lb_packet = false
    Note over T: client_snat: match src_port 8080,<br/>rewrite src → 10.0.0.10 (VIP)
    Note over T: forward: dst 10.0.0.100 → port 148
    Note over T: deparser: recompute checksums
    T->>C: src 10.0.0.10 (VIP):8080, dst 10.0.0.100:54321
""",
    "control_plane": """
flowchart LR
    HTTP["HTTP Clients\n(experiment scripts, curl)"]
    HTTP -- "POST /migrateNode\nPOST /updateForward\nPOST /cleanup\nPOST /reinitialize" --> Flask

    subgraph Switch[Tofino Switch]
        direction LR
        Flask["Flask API\nport 5000"]
        Flask --> NM["NodeManager\n\nnodes{}, lb_nodes{}\nmigrateNode()\nupdateForward()"]
        NM --> SC["SwitchController\n(bf_switch_controller)\n\ngRPC to bf_switchd\n127.0.0.1:50052"]
        SC --> SW["bf_switchd\n\nP4 pipeline (ASIC)\nnode_selector\naction_selector\nforward / arp_forward\nclient_snat"]
    end

    style HTTP fill:#fff3e0,stroke:#f57c00
    style Switch fill:#e3f2fd,stroke:#1976d2
    style Flask fill:#c8e6c9,stroke:#388e3c
    style SW fill:#e8eaf6,stroke:#3f51b5
""",
    "testbed_topology": """
flowchart LR
    L["Dell R740\n(lakewood)"] -- "NFP 25G\nPort 2/0" --- SW["Tofino Switch\nWedge100BF-32X"]
    SW -- "NFP 25G\nPort 3/0" --- V["Dell R740\n(loveland)"]
    L <-. "25G DAC · checkpoint" .-> V

    style L fill:#c8e6c9,stroke:#388e3c
    style V fill:#c8e6c9,stroke:#388e3c
    style SW fill:#e3f2fd,stroke:#1976d2
""",
    "data_path": """
flowchart LR
    LG["Load Generator"] --> MS["macvlan-shim"] --> SW["P4 Switch"] --> C["Server Container"]

    style LG fill:#fff3e0,stroke:#f57c00
    style MS fill:#fff3e0,stroke:#f57c00
    style SW fill:#e3f2fd,stroke:#1976d2
    style C fill:#c8e6c9,stroke:#388e3c
""",
    "metrics_pipeline": """
flowchart TB
    S["Server :8081\n/metrics"]
    LG["Loadgen :9090\n/metrics"]

    S -- "macvlan-shim\n192.168.12.2" --> T1
    LG -- "localhost\nlakewood" --> T2

    subgraph Tunnel["SSH Tunnel (lakewood)"]
        direction LR
        T1["-L 18081:192.168.12.2:8081"]
        T2["-L 19090:localhost:9090"]
    end

    T1 --> C
    T2 --> C

    C["Collector (local)\n\nscrape every 1s\nwrite metrics.csv\ncheck migration_flag"]

    C --> P["plot_metrics.py\n\n12 PDF + PNG plots"]

    style S fill:#e8f5e9,stroke:#388e3c
    style LG fill:#e8f5e9,stroke:#388e3c
    style Tunnel fill:#fff3e0,stroke:#f57c00
    style C fill:#e3f2fd,stroke:#1976d2
    style P fill:#e8eaf6,stroke:#3f51b5
""",
    "migration_phases": """
flowchart LR
    Q["Quiesce\n(SIGUSR2)"] --> D["Drain\nTCP queue"]
    D --> CK["Checkpoint\n(CRIU dump)\n~1550 ms"]
    CK --> TX["Transfer\n(socat 25G)\n~430 ms"]
    TX --> RM["Remove\nsource container"]
    RM --> RS["Restore\n(CRIU restore +\nmacvlan fix)\n~3270 ms"]
    RS --> RES["Resume\n(SIGUSR2)"]
    RES --> SW["Switch Update\n(/updateForward)\n~30 ms"]

    style CK fill:#ffcdd2,stroke:#c62828
    style TX fill:#fff3e0,stroke:#f57c00
    style RS fill:#ffcdd2,stroke:#c62828
    style SW fill:#c8e6c9,stroke:#388e3c
    style Q fill:#e3f2fd,stroke:#1976d2
    style D fill:#e3f2fd,stroke:#1976d2
    style RM fill:#e3f2fd,stroke:#1976d2
    style RES fill:#e3f2fd,stroke:#1976d2
""",
    "experiment_orchestration": """
sequenceDiagram
    participant Ctrl as Control Machine
    participant T as Tofino Switch
    participant L as Lakewood (Server 1)
    participant V as Loveland (Server 2)

    Note over Ctrl,V: Step 1: Connectivity + sync
    Ctrl->>L: SSH check + rsync scripts
    Ctrl->>V: SSH check + rsync scripts
    Ctrl->>T: SSH check + rsync controller

    Note over Ctrl,V: Step 2: Build container images
    Ctrl->>L: Build server image
    L->>V: Sync image (podman save/load)

    Note over Ctrl,V: Step 3-4: Start infrastructure
    Ctrl->>T: Ensure switchd running
    Ctrl->>T: Ensure controller running
    Ctrl->>T: POST /reinitialize

    Note over Ctrl,V: Step 5-6: Setup experiment
    Ctrl->>L: Create network + container
    Ctrl->>V: Create network + container

    Note over Ctrl,V: Step 7: Start measurement
    Ctrl->>L: Start load generator
    Ctrl->>Ctrl: Start metrics collector (via SSH tunnel)

    Note over Ctrl,V: Step 8: Steady-state wait
    Ctrl->>L: Health check server
    Ctrl->>Ctrl: Wait for steady-state

    Note over Ctrl,V: Step 9: CRIU migration
    Ctrl->>L: Checkpoint container
    L->>V: Transfer checkpoint (direct link)
    Ctrl->>V: Restore container
    Ctrl->>T: POST /updateForward

    Note over Ctrl,V: Step 10-11: Collect results
    Ctrl->>Ctrl: Wait post-migration
    Ctrl->>Ctrl: Stop collector, generate plots
    Ctrl->>L: Collect logs
""",
}

FORMATS = ["png", "pdf"]


def render(name: str, mmd_content: str) -> None:
    for fmt in FORMATS:
        out_path = OUTPUT_DIR / f"{name}.{fmt}"
        print(f"  {name}.{fmt} ...", end=" ", flush=True)
        try:
            mermaido.render(mmd_content.strip(), str(out_path), fmt=fmt)
            print("OK")
        except Exception as e:
            print(f"FAILED ({e!s:.120})")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Rendering {len(DIAGRAMS)} diagrams to {OUTPUT_DIR}/\n")
    for name, content in DIAGRAMS.items():
        render(name, content)

    print(f"\nDone. Output in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
