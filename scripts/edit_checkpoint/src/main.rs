//! Edit a Podman/CRIU checkpoint archive for cross-node migration:
//! Patches IP address in checkpoint/files.img (old_addr -> new_addr) using crit decode/encode.

use std::env;
use std::fs;
use std::path::Path;
use std::process::Command;

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

fn run(tar_path: &str, old_addr: &str, new_addr: &str) -> Result<(), String> {
    let temp_dir = tempfile::tempdir().map_err(|e| e.to_string())?;
    let temp_path = temp_dir.path();

    // Extract tar
    let tar_file = fs::File::open(tar_path).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(tar_file);
    archive.unpack(temp_path).map_err(|e| e.to_string())?;

    let files_img = temp_path.join("checkpoint").join("files.img");
    if !files_img.exists() {
        return Err(format!("{} does not exist", files_img.display()));
    }

    // crit decode -> JSON
    let decoded_path = temp_path.join("decoded.json");
    let decode_status = Command::new("crit")
        .args(["decode", "-i", files_img.to_str().unwrap(), "--pretty"])
        .stdout(std::process::Stdio::from(
            fs::File::create(&decoded_path).map_err(|e| e.to_string())?,
        ))
        .status()
        .map_err(|e| e.to_string())?;
    if !decode_status.success() {
        return Err("crit decode failed".to_string());
    }

    // Parse and patch JSON
    let mut data: serde_json::Value =
        serde_json::from_reader(fs::File::open(&decoded_path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
    let updated = patch_files_img_json(&mut data, old_addr, new_addr);
    if !updated {
        eprintln!(
            "Warning: could not find src_addr {} in files.img (patch may be no-op)",
            old_addr
        );
    }
    fs::write(
        &decoded_path,
        serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;

    // crit encode -> files.img
    let encode_status = Command::new("crit")
        .args([
            "encode",
            "-i",
            decoded_path.to_str().unwrap(),
            "-o",
            files_img.to_str().unwrap(),
        ])
        .status()
        .map_err(|e| e.to_string())?;
    if !encode_status.success() {
        return Err("crit encode failed".to_string());
    }

    // Repack tar (overwrite original). Preserve all top-level entries (checkpoint/, manifest, etc.)
    let new_tar_path = format!("{}.new", tar_path);
    let out_file = fs::File::create(&new_tar_path).map_err(|e| e.to_string())?;
    let mut builder = tar::Builder::new(out_file);
    for entry in fs::read_dir(temp_path).map_err(|e| e.to_string())?.flatten() {
        let p = entry.path();
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if name == "decoded.json" || name.ends_with(".new") {
            continue; // skip our temp files
        }
        let name = p.strip_prefix(temp_path).unwrap_or(&p);
        if p.is_dir() {
            builder.append_dir_all(&p, temp_path).map_err(|e| e.to_string())?;
        } else {
            builder.append_path_with_name(&p, name).map_err(|e| e.to_string())?;
        }
    }
    builder.finish().map_err(|e| e.to_string())?;
    drop(builder);
    fs::rename(&new_tar_path, tar_path).map_err(|e| e.to_string())?;

    Ok(())
}

/// Patch INETSK entries' src_addr in the decoded files.img JSON. Returns true if any change was made.
fn patch_files_img_json(data: &mut serde_json::Value, old_addr: &str, new_addr: &str) -> bool {
    let entries = match data.get_mut("entries").and_then(|e| e.as_array_mut()) {
        Some(e) => e,
        None => return false,
    };
    let mut updated = false;
    for entry in entries.iter_mut() {
        if entry.get("type").and_then(|t| t.as_str()) != Some("INETSK") {
            continue;
        }
        let isk = match entry.get_mut("isk") {
            Some(i) => i,
            None => continue,
        };
        let family = isk
            .get("family")
            .and_then(|f| f.as_u64())
            .or_else(|| {
                isk.get("family")
                    .and_then(|f| f.as_str())
                    .map(|s| if s == "INET" { 2 } else if s == "INET6" { 10 } else { 0 })
            })
            .unwrap_or(2);
        if family != 2 {
            continue; // only patch IPv4 (INET = 2)
        }
        let src_addrs = isk.get("src_addr").and_then(|a| a.as_array());
        let patch = src_addrs.map_or(false, |addrs| {
            let addrs: Vec<String> = addrs
                .iter()
                .filter_map(|a| a.as_str().map(String::from))
                .collect();
            addrs.contains(&old_addr.to_string())
                || addrs.iter().any(|a| a == "0.0.0.0" || a == "::")
        });
        if patch {
            isk["src_addr"] = serde_json::json!([new_addr]);
            updated = true;
        }
    }
    updated
}
