#!/usr/bin/env python3
"""
plot_metrics.py — Generate charts from experiment CSV data.

Reads the CSV produced by the Go collector and the migration event flag file,
then generates publication-ready matplotlib charts.

Usage:
    python3 plot_metrics.py                          # defaults
    python3 plot_metrics.py --csv results/metrics.csv --migration-flag /tmp/migration_event
    python3 plot_metrics.py --output-dir results/
"""

import argparse
import os
import sys

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
except Exception as e:
    print("Plot dependencies unavailable:", e, file=sys.stderr)
    print("Install with: pip install matplotlib pandas (and Pillow if needed)", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser(description="Plot experiment metrics")
parser.add_argument(
    "--csv",
    default="results/metrics.csv",
    help="Path to collector CSV file",
)
parser.add_argument(
    "--migration-flag",
    default="/tmp/migration_event",
    help="Path to migration event flag file (from cr.sh)",
)
parser.add_argument(
    "--output-dir",
    default="results",
    help="Directory for output charts",
)
parser.add_argument(
    "--show",
    action="store_true",
    help="Show charts interactively instead of saving",
)


def load_migration_event(path: str) -> dict | None:
    """Parse the migration event flag file created by cr.sh."""
    if not os.path.exists(path):
        return None
    data = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def migration_time_ms(event: dict | None) -> int | None:
    """Extract the migration start timestamp in unix milliseconds."""
    if event and "migration_start_ns" in event:
        return int(event["migration_start_ns"]) // 1_000_000
    return None


def plot_server_metrics(df: pd.DataFrame, migration_ms: int | None, output_dir: str, show: bool):
    """Plot server-side metrics: connected peers, bytes sent, uptime."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    fig.suptitle("WebRTC Server Metrics Over Time", fontsize=14)

    t = df["t_sec"]
    peers_col = "server_connected_peers" if "server_connected_peers" in df.columns else "active_peers"
    bytes_col = "server_bytes_sent" if "server_bytes_sent" in df.columns else "bytes_sent"
    uptime_col = "server_uptime_s" if "server_uptime_s" in df.columns else "uptime_s"

    # Connected peers
    ax = axes[0]
    if peers_col in df.columns:
        ax.plot(t, pd.to_numeric(df[peers_col], errors="coerce"), "b-", linewidth=1)
    ax.set_ylabel("Connected Peers")
    ax.grid(True, alpha=0.3)

    # Bytes sent
    ax = axes[1]
    if bytes_col in df.columns:
        ax.plot(t, pd.to_numeric(df[bytes_col], errors="coerce"), "g-", linewidth=1)
    ax.set_ylabel("Total Bytes Sent")
    ax.grid(True, alpha=0.3)

    # Server uptime
    ax = axes[2]
    if uptime_col in df.columns:
        ax.plot(t, pd.to_numeric(df[uptime_col], errors="coerce"), "r-", linewidth=1)
    ax.set_ylabel("Server Uptime (s)")
    ax.set_xlabel("Experiment Time (s)")
    ax.grid(True, alpha=0.3)

    # Mark migration event
    if migration_ms is not None and "timestamp_unix_milli" in df.columns:
        t0 = df["timestamp_unix_milli"].iloc[0]
        m_sec = (migration_ms - t0) / 1000.0
        for ax in axes:
            ax.axvline(x=m_sec, color="red", linestyle="--", alpha=0.7, label="Migration")
        axes[0].legend()

    plt.tight_layout()
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, "server_metrics.png")
        plt.savefig(path, dpi=150)
        print(f"Saved: {path}")
    plt.close()


def plot_ping_rtt(df: pd.DataFrame, migration_ms: int | None, output_dir: str, show: bool):
    """Plot ping RTT to each host."""
    rtt_cols = [c for c in df.columns if c.startswith("ping_rtt_ms_") or c.startswith("ping_ms_")]
    if not rtt_cols:
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    fig.suptitle("Network RTT Over Time", fontsize=14)

    t = df["t_sec"]
    for col in rtt_cols:
        host = col.replace("ping_rtt_ms_", "").replace("ping_ms_", "").replace("_", ".")
        vals = pd.to_numeric(df[col], errors="coerce")
        ax.plot(t, vals, linewidth=1, label=host)

    if migration_ms is not None and "timestamp_unix_milli" in df.columns:
        t0 = df["timestamp_unix_milli"].iloc[0]
        m_sec = (migration_ms - t0) / 1000.0
        ax.axvline(x=m_sec, color="red", linestyle="--", alpha=0.7, label="Migration")

    ax.set_ylabel("RTT (ms)")
    ax.set_xlabel("Experiment Time (s)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, "ping_rtt.png")
        plt.savefig(path, dpi=150)
        print(f"Saved: {path}")
    plt.close()


def plot_container_stats(df: pd.DataFrame, migration_ms: int | None, output_dir: str, show: bool):
    """Plot container CPU and memory usage."""
    cpu_cols = [c for c in df.columns if c.endswith("_cpu") or (c.startswith("cpu_") and len(c) > 4)]
    if not cpu_cols:
        return

    fig, axes = plt.subplots(len(cpu_cols), 1, figsize=(12, 4 * len(cpu_cols)), sharex=True)
    if len(cpu_cols) == 1:
        axes = [axes]

    fig.suptitle("Container Resource Usage", fontsize=14)
    t = df["t_sec"]

    for i, col in enumerate(cpu_cols):
        name = col.replace("container_", "").replace("_cpu", "").replace("cpu_", "")
        ax = axes[i]

        # CPU — strip the '%' sign if present
        cpu_vals = df[col].astype(str).str.rstrip("%").str.strip()
        cpu_vals = pd.to_numeric(cpu_vals, errors="coerce")
        ax.plot(t, cpu_vals, "b-", linewidth=1, label="CPU %")
        ax.set_ylabel(f"{name}\nCPU %")
        ax.grid(True, alpha=0.3)

        if migration_ms is not None and "timestamp_unix_milli" in df.columns:
            t0 = df["timestamp_unix_milli"].iloc[0]
            m_sec = (migration_ms - t0) / 1000.0
            ax.axvline(x=m_sec, color="red", linestyle="--", alpha=0.7, label="Migration")

        ax.legend(loc="upper right")

    axes[-1].set_xlabel("Experiment Time (s)")

    plt.tight_layout()
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, "container_stats.png")
        plt.savefig(path, dpi=150)
        print(f"Saved: {path}")
    plt.close()


def plot_migration_timing(event: dict | None, output_dir: str, show: bool):
    """Bar chart of migration phase durations."""
    if event is None:
        return

    try:
        start = int(event["migration_start_ns"])
        checkpoint = int(event["checkpoint_done_ns"])
        edit = int(event["edit_done_ns"])
        restore = int(event["restore_done_ns"])
        switch_update = int(event["switch_update_done_ns"])
        end = int(event["migration_end_ns"])
    except (KeyError, ValueError):
        return

    phases = ["Checkpoint", "IP Edit", "Restore", "Switch Update"]
    durations_ms = [
        (checkpoint - start) / 1e6,
        (edit - checkpoint) / 1e6,
        (restore - edit) / 1e6,
        (switch_update - restore) / 1e6,
    ]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.barh(phases, durations_ms, color=["#4CAF50", "#2196F3", "#FF9800", "#9C27B0"])
    ax.set_xlabel("Duration (ms)")
    ax.set_title(f"Migration Phase Breakdown (total: {(end - start) / 1e6:.0f} ms)")
    ax.grid(True, axis="x", alpha=0.3)

    for bar, val in zip(bars, durations_ms):
        ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                f"{val:.0f} ms", va="center", fontsize=10)

    plt.tight_layout()
    if show:
        plt.show()
    else:
        path = os.path.join(output_dir, "migration_timing.png")
        plt.savefig(path, dpi=150)
        print(f"Saved: {path}")
    plt.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Load migration event
    event = load_migration_event(args.migration_flag)
    m_ms = migration_time_ms(event)

    # Load CSV
    if not os.path.exists(args.csv):
        print(f"CSV file not found: {args.csv}")
        print("Run the collector first, then plot.")
        sys.exit(1)

    df = pd.read_csv(args.csv)
    if df.empty:
        print("CSV is empty, nothing to plot.")
        sys.exit(1)

    # Convert to relative seconds (support both timestamp_unix_milli and elapsed_s)
    t0 = None
    if "timestamp_unix_milli" in df.columns:
        df["timestamp_unix_milli"] = pd.to_numeric(df["timestamp_unix_milli"], errors="coerce")
        t0 = float(df["timestamp_unix_milli"].iloc[0])
        df["t_sec"] = (df["timestamp_unix_milli"] - t0) / 1000.0
    elif "elapsed_s" in df.columns:
        df["t_sec"] = pd.to_numeric(df["elapsed_s"], errors="coerce")
        # No timestamp_unix_milli in CSV; migration line will be skipped
    else:
        print("CSV needs 'timestamp_unix_milli' or 'elapsed_s'")
        sys.exit(1)

    print(f"Loaded {len(df)} rows from {args.csv}")
    print(f"Duration: {df['t_sec'].iloc[-1]:.1f}s")
    if m_ms is not None and t0 is not None:
        print(f"Migration event at t={((m_ms - t0) / 1000.0):.1f}s")

    plot_server_metrics(df, m_ms, args.output_dir, args.show)
    plot_ping_rtt(df, m_ms, args.output_dir, args.show)
    plot_container_stats(df, m_ms, args.output_dir, args.show)
    plot_migration_timing(event, args.output_dir, args.show)

    print("Done.")


if __name__ == "__main__":
    main()
