//! Edit a Podman/CRIU checkpoint archive for cross-node migration:
//! 1. Patches IP address in checkpoint/files.img (old_addr -> new_addr) using crit decode/encode.
//!    Only patches sockets bound to old_addr specifically (NOT 0.0.0.0/:: wildcard).
//! 2. Patches network.status to set the target IP (so podman assigns it on restore).
//! 3. Patches config.dump to set staticIP to the target IP.
//! Streams the tar (no full extract/repack): only checkpoint/files.img is written to temp for crit.

use std::env;
use std::fs;
use std::io::{BufReader, BufWriter, Read};
use std::path::Path;
use std::process::Command;
use std::time::Instant;

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("Usage: edit_checkpoint <checkpoint.tar> <old_addr> <new_addr> [image_name]");
        std::process::exit(1);
    }
    let tar_path = &args[1];
    let old_addr = &args[2];
    let new_addr = &args[3];
    let _image_name = args.get(4).map(String::as_str);

    if !Path::new(tar_path).exists() {
        eprintln!("Error: {} does not exist", tar_path);
        std::process::exit(1);
    }
    if old_addr.is_empty() || new_addr.is_empty() {
        eprintln!("Error: old_addr and new_addr must not be empty");
        std::process::exit(1);
    }
    if old_addr == new_addr {
        eprintln!("Error: old_addr and new_addr must be different");
        std::process::exit(1);
    }

    if let Err(e) = run(tar_path, old_addr, new_addr) {
        eprintln!("Error: {}", e);
        std::process::exit(1);
    }
}

const FILES_IMG_PATH: &str = "checkpoint/files.img";
const NETWORK_STATUS_PATH: &str = "network.status";
const CONFIG_DUMP_PATH: &str = "config.dump";

fn run(tar_path: &str, _old_addr: &str, new_addr: &str) -> Result<(), String> {
    let show_timing = env::var("EDIT_CHECKPOINT_TIMING").is_ok();
    let t0 = Instant::now();

    // Prefer RAM (e.g. /dev/shm) for crit temp files to minimize I/O latency
    let temp_path = Path::new("/dev/shm");
    let temp_dir = if temp_path.exists() && temp_path.is_dir() {
        tempfile::tempdir_in(temp_path).map_err(|e| e.to_string())?
    } else {
        tempfile::tempdir().map_err(|e| e.to_string())?
    };
    let temp_path = temp_dir.path();
    let files_img_in = temp_path.join("files.img.in");
    let files_img_out = temp_path.join("files.img.out");
    let decoded_path = temp_path.join("decoded.json");

    let tar_file = fs::File::open(tar_path).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(BufReader::with_capacity(256 * 1024, tar_file));
    let new_tar_path = format!("{}.new", tar_path);
    let out_file = fs::File::create(&new_tar_path).map_err(|e| e.to_string())?;
    let mut builder = tar::Builder::new(BufWriter::with_capacity(256 * 1024, out_file));

    let entries = archive.entries().map_err(|e| e.to_string())?;
    let mut found_files_img = false;

    for entry in entries {
        let mut entry = entry.map_err(|e| e.to_string())?;
        let path = entry
            .path()
            .map_err(|e| e.to_string())?
            .display()
            .to_string()
            .replace('\\', "/");
        let size_hint = entry.header().size().unwrap_or(0) as usize;
        let mut content = Vec::with_capacity(size_hint.min(64 * 1024 * 1024).max(4096));
        entry.read_to_end(&mut content).map_err(|e| e.to_string())?;

        if path == FILES_IMG_PATH {
            found_files_img = true;
            if show_timing {
                eprintln!("  tar stream:    {:>6} ms (read)", t0.elapsed().as_millis());
            }
            let t1 = Instant::now();
            fs::write(&files_img_in, &content).map_err(|e| e.to_string())?;
            let decode_status = Command::new("crit")
                .args(["decode", "-i", files_img_in.to_str().unwrap()])
                .stdout(std::process::Stdio::from(
                    fs::File::create(&decoded_path).map_err(|e| e.to_string())?,
                ))
                .status()
                .map_err(|e| e.to_string())?;
            if !decode_status.success() {
                return Err("crit decode failed".to_string());
            }
            if show_timing {
                eprintln!("  crit decode:   {:>6} ms", t1.elapsed().as_millis());
            }
            let t2 = Instant::now();
            let mut data: serde_json::Value =
                serde_json::from_reader(fs::File::open(&decoded_path).map_err(|e| e.to_string())?)
                    .map_err(|e| e.to_string())?;
            let updated = patch_files_img_json(&mut data, new_addr);
            if !updated {
                eprintln!(
                    "Note: no non-wildcard INETSK entries found in files.img (server likely uses 0.0.0.0 — OK)",
                );
            }
            // Compact JSON is smaller and faster for crit encode to read
            fs::write(
                &decoded_path,
                serde_json::to_string(&data).map_err(|e| e.to_string())?,
            )
            .map_err(|e| e.to_string())?;
            if show_timing {
                eprintln!("  json patch:    {:>6} ms", t2.elapsed().as_millis());
            }
            let t3 = Instant::now();
            let encode_status = Command::new("crit")
                .args([
                    "encode",
                    "-i",
                    decoded_path.to_str().unwrap(),
                    "-o",
                    files_img_out.to_str().unwrap(),
                ])
                .status()
                .map_err(|e| e.to_string())?;
            if !encode_status.success() {
                return Err("crit encode failed".to_string());
            }
            if show_timing {
                eprintln!("  crit encode:   {:>6} ms", t3.elapsed().as_millis());
            }
            content = fs::read(&files_img_out).map_err(|e| e.to_string())?;
            let mut new_header = entry.header().clone();
            new_header.set_size(content.len() as u64);
            new_header.set_cksum();
            builder
                .append(&new_header, content.as_slice())
                .map_err(|e| e.to_string())?;
        } else if path == NETWORK_STATUS_PATH {
            // Patch network.status: set the IP to new_addr
            let patched = patch_network_status(&content, new_addr)?;
            let mut new_header = entry.header().clone();
            new_header.set_size(patched.len() as u64);
            new_header.set_cksum();
            builder
                .append(&new_header, patched.as_slice())
                .map_err(|e| e.to_string())?;
            eprintln!("Patched network.status → {}", new_addr);
        } else if path == CONFIG_DUMP_PATH {
            // Patch config.dump: set staticIP to new_addr
            let patched = patch_config_dump(&content, new_addr)?;
            let mut new_header = entry.header().clone();
            new_header.set_size(patched.len() as u64);
            new_header.set_cksum();
            builder
                .append(&new_header, patched.as_slice())
                .map_err(|e| e.to_string())?;
            eprintln!("Patched config.dump staticIP → {}", new_addr);
        } else {
            let mut h = entry.header().clone();
            h.set_cksum();
            builder
                .append(&h, content.as_slice())
                .map_err(|e| e.to_string())?;
        }
    }

    if !found_files_img {
        return Err(format!("{} not found in archive", FILES_IMG_PATH));
    }

    builder.finish().map_err(|e| e.to_string())?;
    drop(builder);
    fs::rename(&new_tar_path, tar_path).map_err(|e| e.to_string())?;
    if show_timing {
        eprintln!(
            "  total:        {:>6} ms (stream, no full extract/repack)",
            t0.elapsed().as_millis()
        );
    }

    Ok(())
}

/// Check whether a src_addr array contains a specific (non-wildcard) address.
/// crit decode outputs src_addr as an array of integers (uint32 network order)
/// for AF_INET, but some versions may use strings.
fn is_specific_addr(addrs: &[serde_json::Value]) -> bool {
    addrs.iter().any(|a| {
        if let Some(n) = a.as_u64() {
            n != 0 // 0 = 0.0.0.0 (wildcard)
        } else if let Some(s) = a.as_str() {
            !s.is_empty() && s != "0.0.0.0" && s != "::" && s != "0"
        } else {
            false
        }
    })
}

/// Patch INETSK entries' src_addr in the decoded files.img JSON.
/// Sockets bound to a specific IP are rewritten to 0.0.0.0 (wildcard)
/// so CRIU can bind them on any interface, avoiding "Cannot assign requested
/// address" when the restored container's IPAM-assigned IP differs.
/// Returns true if any change was made.
fn patch_files_img_json(data: &mut serde_json::Value, new_addr: &str) -> bool {
    let _ = new_addr; // new_addr not used; we always wildcard to 0.0.0.0

    let entries = match data.get_mut("entries").and_then(|e| e.as_array_mut()) {
        Some(e) => e,
        None => return false,
    };
    let mut updated = false;
    let mut count = 0u32;
    for entry in entries.iter_mut() {
        if entry.get("type").and_then(|t| t.as_str()) != Some("INETSK") {
            continue;
        }
        let isk = match entry.get_mut("isk") {
            Some(i) => i,
            None => continue,
        };
        // Check family: AF_INET = 2, crit may output as string "AF_INET" or integer 2
        let family_str = isk.get("family").and_then(|f| f.as_str()).unwrap_or("");
        let family_num = isk.get("family").and_then(|f| f.as_u64()).unwrap_or(0);
        let is_inet4 = family_str == "AF_INET" || family_str == "INET" || family_num == 2;
        if !is_inet4 {
            continue;
        }
        let src_addrs = isk.get("src_addr").and_then(|a| a.as_array());
        if src_addrs.map_or(false, |addrs| is_specific_addr(addrs)) {
            // Determine format: if the original was integer, use integer 0; otherwise "0.0.0.0"
            let was_integer = src_addrs
                .unwrap()
                .first()
                .map_or(true, |v| v.is_number());
            if was_integer {
                isk["src_addr"] = serde_json::json!([0]);
            } else {
                isk["src_addr"] = serde_json::json!(["0.0.0.0"]);
            }
            count += 1;
            updated = true;
        }
    }
    if updated {
        eprintln!("Patched {} INETSK src_addr entries → 0.0.0.0 (wildcard)", count);
    }
    updated
}

/// Patch network.status JSON: replace the IP in the "ips" array with new_addr.
fn patch_network_status(content: &[u8], new_addr: &str) -> Result<Vec<u8>, String> {
    let mut data: serde_json::Value =
        serde_json::from_slice(content).map_err(|e| format!("parse network.status: {}", e))?;

    if let Some(arr) = data.as_array_mut() {
        for entry in arr.iter_mut() {
            if let Some(ips) = entry.get_mut("ips").and_then(|v| v.as_array_mut()) {
                for ip in ips.iter_mut() {
                    if let Some(addr) = ip.get_mut("address") {
                        // address is "IP/prefix", e.g. "192.168.12.2/24"
                        let old = addr.as_str().unwrap_or("");
                        let prefix = old.split('/').nth(1).unwrap_or("24");
                        *addr = serde_json::json!(format!("{}/{}", new_addr, prefix));
                    }
                }
            }
        }
    }

    serde_json::to_vec_pretty(&data).map_err(|e| format!("serialize network.status: {}", e))
}

/// Patch config.dump JSON: replace staticIP with new_addr.
fn patch_config_dump(content: &[u8], new_addr: &str) -> Result<Vec<u8>, String> {
    let mut data: serde_json::Value =
        serde_json::from_slice(content).map_err(|e| format!("parse config.dump: {}", e))?;

    // Patch "staticIP" field
    if data.get("staticIP").is_some() {
        data["staticIP"] = serde_json::json!(new_addr);
    }

    // Also patch in the "createCommand" array if "--ip" is followed by an IP
    if let Some(cmd) = data.get_mut("createCommand").and_then(|v| v.as_array_mut()) {
        let mut i = 0;
        while i < cmd.len() {
            if cmd[i].as_str() == Some("--ip") && i + 1 < cmd.len() {
                cmd[i + 1] = serde_json::json!(new_addr);
            }
            i += 1;
        }
    }

    serde_json::to_vec(&data).map_err(|e| format!("serialize config.dump: {}", e))
}
