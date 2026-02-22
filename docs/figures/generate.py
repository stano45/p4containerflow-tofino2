"""
Generate Mermaid diagrams for the technical report.

Renders each diagram to PNG, SVG, and PDF using mermaid-py (mermaid.ink API)
and cairosvg for SVG-to-PDF conversion.

Usage:
    cd docs/figures
    uv run generate.py
"""

from pathlib import Path

import cairosvg
import mermaid as md
from mermaid.graph import Graph

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
}


def render(name: str, mmd_content: str) -> None:
    script = mmd_content.strip()
    graph = Graph(name, script)
    diagram = md.Mermaid(graph)

    svg_path = OUTPUT_DIR / f"{name}.svg"
    png_path = OUTPUT_DIR / f"{name}.png"
    pdf_path = OUTPUT_DIR / f"{name}.pdf"

    print(f"  {name}.svg ...", end=" ", flush=True)
    diagram.to_svg(svg_path)
    svg_ok = svg_path.read_text().strip().startswith("<")
    print("OK" if svg_ok else "FAILED (invalid response)")

    print(f"  {name}.png ...", end=" ", flush=True)
    diagram.to_png(png_path)
    print("OK")

    print(f"  {name}.pdf ...", end=" ", flush=True)
    svg_content = svg_path.read_text()
    if not svg_content.strip().startswith("<"):
        print(f"SKIPPED (SVG response was not valid: {svg_content[:80]})")
        return
    try:
        cairosvg.svg2pdf(
            bytestring=svg_content.encode("utf-8"), write_to=str(pdf_path)
        )
        print("OK")
    except Exception as e:
        print(f"FAILED ({e})")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Rendering {len(DIAGRAMS)} diagrams to {OUTPUT_DIR}/\n")
    for name, content in DIAGRAMS.items():
        render(name, content)

    print(f"\nDone. Output in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
