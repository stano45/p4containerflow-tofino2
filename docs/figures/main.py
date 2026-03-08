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
DIAGRAMS_DIR = Path(__file__).parent / "diagrams"


def load(name: str) -> str:
    return (DIAGRAMS_DIR / f"{name}.mmdc").read_text()


DIAGRAMS: dict[str, str] = {
    "v1model_pipeline": load("v1model_pipeline"),
    "t2na_pipeline": load("t2na_pipeline"),
    "ingress_apply_flow": load("ingress_apply_flow"),
    "packet_flow": load("packet_flow"),
    "control_plane": load("control_plane"),
    "testbed_topology": load("testbed_topology"),
    "data_path": load("data_path"),
    "metrics_pipeline": load("metrics_pipeline"),
    "migration_phases": load("migration_phases"),
    "experiment_orchestration": load("experiment_orchestration"),
}

SLIDES_DIAGRAMS: dict[str, str] = {
    "action_profile_structure": load("action_profile_structure"),
    "action_profile_migration": load("action_profile_migration"),
    "v1model_pipeline_vert": load("v1model_pipeline_vert"),
    "migration_phases_vert": load("migration_phases_vert"),
    "data_path_vert": load("data_path_vert"),
    "dev_pipeline_model": load("dev_pipeline_model"),
    "dev_pipeline_hw": load("dev_pipeline_hw"),
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

    print(f"\nRendering {len(SLIDES_DIAGRAMS)} slides-specific diagrams to {OUTPUT_DIR}/\n")
    for name, content in SLIDES_DIAGRAMS.items():
        render(name, content)

    print(f"\nDone. Output in {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
