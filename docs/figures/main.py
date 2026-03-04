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
        B[Ingress Parser] --> C[Ingress]
        C --> D[Ingress Deparser]
    end

    IG --> TM[Traffic Manager]

    TM --> EG

    subgraph EG[EGRESS]
        direction LR
        E[Egress Parser] --> F[Egress]
        F --> G[Egress Deparser]
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
    A[Packet In] --> B{ARP?}
    B -- yes --> C[arp_forward]
    C --> D1[bypass_egress]
    D1 --> Z1[Packet Out]

    B -- no --> E{IPv4 + TTL?}
    E -- no --> DROP[Drop]

    E -- yes --> F[node_selector]
    F --> G{is_lb_packet?}
    G -- no --> H[client_snat]
    G -- yes --> I[forward]
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
    participant C as Client
    participant T as Switch
    participant B as Backend

    C->>T: to VIP
    T->>B: to backend
    B->>T: reply
    T->>C: from VIP
""",
    "control_plane": """
flowchart TB
    HTTP[HTTP Clients]
    HTTP --> Flask

    subgraph Switch[Tofino Switch]
        direction TB
        Flask[Flask API]
        Flask --> NM[NodeManager]
        NM --> SC[SwitchController]
        SC --> SW[bf_switchd]
    end

    style HTTP fill:#fff3e0,stroke:#f57c00
    style Switch fill:#e3f2fd,stroke:#1976d2
    style Flask fill:#c8e6c9,stroke:#388e3c
    style SW fill:#e8eaf6,stroke:#3f51b5
""",
    "testbed_topology": """
flowchart LR
    L[lakewood] -- 25G --- SW[Tofino Switch]
    SW -- 25G --- V[loveland]
    L <-. checkpoint .-> V

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
    S[Server /metrics]
    LG[Loadgen /metrics]

    S --> T1
    LG --> T2

    subgraph Tunnel[SSH Tunnel]
        direction LR
        T1[L 18081:8081]
        T2[L 19090:9090]
    end

    T1 --> C
    T2 --> C

    C[Collector]
    C --> P[Plots]

    style S fill:#e8f5e9,stroke:#388e3c
    style LG fill:#e8f5e9,stroke:#388e3c
    style Tunnel fill:#fff3e0,stroke:#f57c00
    style C fill:#e3f2fd,stroke:#1976d2
    style P fill:#e8eaf6,stroke:#3f51b5
""",
    "migration_phases": """
flowchart LR
    Q[Quiesce] --> D[Drain]
    D --> CK[Checkpoint]
    CK --> TX[Transfer]
    TX --> RM[Remove]
    RM --> RS[Restore]
    RS --> RES[Resume]
    RES --> SW[Switch Update]

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
    participant Ctrl as Control
    participant T as Tofino
    participant L as Lakewood
    participant V as Loveland

    Note over Ctrl,V: Sync + build
    Ctrl->>L: rsync + build
    Ctrl->>V: rsync
    Ctrl->>T: rsync controller

    Note over Ctrl,V: Start
    Ctrl->>T: switchd, controller, /reinitialize
    Ctrl->>L: network + container
    Ctrl->>V: network + container
    Ctrl->>L: loadgen
    Ctrl->>Ctrl: metrics collector

    Note over Ctrl,V: Migrate
    Ctrl->>L: Checkpoint
    L->>V: Transfer
    Ctrl->>V: Restore
    Ctrl->>T: /updateForward

    Note over Ctrl,V: Collect
    Ctrl->>Ctrl: plots
    Ctrl->>L: logs
""",
}

FORMATS = ["png", "svg", "pdf"]


def render(name: str, mmd_content: str) -> None:
    for fmt in FORMATS:
        out_path = OUTPUT_DIR / f"{name}.{fmt}"
        print(f"  {name}.{fmt} ...", end=" ", flush=True)
        try:
            kwargs = {}
            if fmt == "pdf":
                kwargs["pdf_fit"] = True
            mermaido.render(mmd_content.strip(), str(out_path), **kwargs)
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
