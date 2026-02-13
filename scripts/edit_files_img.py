#!/usr/bin/env python3
"""
Edit a Podman checkpoint archive for cross-node migration:
  1. Replace IP address in CRIU files.img (old_addr -> new_addr).
  2. Optionally replace image ID with image name so restore on the target
     can resolve the image by name (avoids "64-byte hexadecimal" error).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


def check_crit_installed():
    if not shutil.which("crit"):
        print(
            "Error: 'crit' command not found. "
            "Please install CRIU and ensure 'crit' is in your PATH."
        )
        sys.exit(1)


def update_src_addr(file_path, old_addr, new_addr):
    try:
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file_path = temp_file.name

        # Decode the image
        subprocess.run(
            f"crit decode -i {file_path} --pretty > {temp_file_path}",
            shell=True,
            check=True,
        )

        with open(temp_file_path, "r") as file:
            data = json.load(file)

        updated = False
        addrs = []
        for entry in data.get("entries", []):
            if entry.get("type") == "INETSK":
                src_addrs = entry.get("isk", {}).get("src_addr")
                addrs.append(src_addrs)
                if old_addr in (src_addrs or []):
                    entry["isk"]["src_addr"] = [new_addr]
                    print(
                        f"Updated src_addr from {old_addr} to "
                        f"{new_addr} in {file_path}"
                    )
                    updated = True
                elif src_addrs and any(
                    a in ("::", "0.0.0.0") for a in (src_addrs or [])
                ):
                    # Container listens on 0.0.0.0 / :: (all interfaces); CRIU stores as :: or 0.0.0.0
                    entry["isk"]["src_addr"] = [new_addr]
                    print(
                        f"Updated src_addr (was {src_addrs}) to {new_addr} in {file_path}"
                    )
                    updated = True

        if not updated:
            print(
                f"Warning: could not find src_addr {old_addr} in {file_path}"
            )
            print(f"Found src_addrs: {addrs}")
            # Dump the current data to a file in /tmp for debugging
            error_dump_path = "/tmp/decoded_image.json"
            with open(error_dump_path, "w") as error_file:
                json.dump(data, error_file, indent=4)
            print(f"Decoded image dumped to {error_dump_path} for debugging.")

        with open(temp_file_path, "w") as file:
            json.dump(data, file, indent=4)

        # Encode the updated data back into the file
        subprocess.run(
            f"crit encode -i {temp_file_path} -o {file_path}",
            shell=True,
            check=True,
        )
    except Exception as e:
        print(f"An error occurred: {e}")
        # Dump the current data to a file in /tmp for debugging
        error_dump_path = "/tmp/decoded_image.json"
        with open(error_dump_path, "w") as error_file:
            json.dump(data, error_file, indent=4)
        print(f"Decoded image dumped to {error_dump_path} for debugging.")
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


# 64-char hex image ID; Podman rejects this as "named reference" on restore
IMAGE_ID_RE = re.compile(r"\b[0-9a-f]{64}\b")


def _replace_image_id_in_obj(obj, image_name):
    """Recursively replace any 64-char hex string (image ID) with image_name."""
    if isinstance(obj, dict):
        return {k: _replace_image_id_in_obj(v, image_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_image_id_in_obj(v, image_name) for v in obj]
    if isinstance(obj, str) and IMAGE_ID_RE.fullmatch(obj):
        return image_name
    return obj


def _try_patch_json_file(path, image_name):
    """If path is JSON containing a 64-char hex image ID, replace with image_name."""
    try:
        with open(path, "rb") as fp:
            raw = fp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return False
    new_data = _replace_image_id_in_obj(data, image_name)
    if new_data == data:
        return False
    with open(path, "w") as fp:
        json.dump(new_data, fp, indent=2)
    print(f"Patched image ref to {image_name!r} in {path}")
    return True


def _patch_image_id_in_text_file(path, image_name):
    """If file is small text containing a 64-char hex (image ID), replace it."""
    try:
        with open(path, "rb") as fp:
            raw = fp.read()
        if len(raw) > 100 * 1024:
            return False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return False
    except OSError:
        return False
    if "image" not in text.lower() and "Image" not in text:
        return False
    match = IMAGE_ID_RE.search(text)
    if not match:
        return False
    new_text = IMAGE_ID_RE.sub(image_name, text)
    if new_text == text:
        return False
    with open(path, "w") as fp:
        fp.write(new_text)
    print(f"Patched image ref to {image_name!r} in {path}")
    return True


def patch_image_ref_in_dir(input_dir, image_name):
    """
    Find config in the checkpoint archive and replace image ID with image name.
    Podman stores the container image as ID in the checkpoint; on restore it tries
    to parse it as a named reference and fails. Replacing with the image name
    lets restore resolve the image on the target (image must exist there).
    """
    for root, _dirs, files in os.walk(input_dir):
        for f in files:
            path = os.path.join(root, f)
            if path.endswith("files.img") or path.endswith(".img"):
                continue
            if f.endswith(".json") or f in ("container", "config", "manifest"):
                if _try_patch_json_file(path, image_name):
                    continue
            _patch_image_id_in_text_file(path, image_name)


def process_directory(input_dir, old_addr, new_addr, image_name=None):
    img_file_path = os.path.join(input_dir, "checkpoint", "files.img")
    if os.path.exists(img_file_path):
        update_src_addr(img_file_path, old_addr, new_addr)
    else:
        print(f"Error: {img_file_path} does not exist")
    if image_name:
        patch_image_ref_in_dir(input_dir, image_name)


def process_tar(tar_path, old_addr, new_addr, image_name=None):
    with tempfile.TemporaryDirectory() as temp_dir:
        with tarfile.open(tar_path, "r:") as tar:
            tar.extractall(path=temp_dir)

        process_directory(temp_dir, old_addr, new_addr, image_name=image_name)

        new_tar_path = tar_path + ".new"
        with tarfile.open(new_tar_path, "w:") as tar:
            tar.add(temp_dir, arcname="")

        shutil.move(new_tar_path, tar_path)


if __name__ == "__main__":
    check_crit_installed()

    if len(sys.argv) < 4:
        print(
            "Usage: python edit_files_img.py <input_dir_or_tar> <old_addr> <new_addr> [image_name]"
        )
        print("  image_name: optional; replace image ID with this name for restore on target")
        sys.exit(1)

    input_path = sys.argv[1]
    old_addr = sys.argv[2]
    new_addr = sys.argv[3]
    image_name = sys.argv[4] if len(sys.argv) > 4 else None

    if not os.path.exists(input_path):
        print(f"Error: {input_path} does not exist")
        sys.exit(1)

    if not old_addr or not new_addr:
        print("Error: old_addr and new_addr must not be empty")
        sys.exit(1)

    if old_addr == new_addr:
        print("Error: old_addr and new_addr must be different")
        sys.exit(1)

    if os.path.isdir(input_path):
        process_directory(input_path, old_addr, new_addr, image_name=image_name)
    elif input_path.endswith(".tar"):
        process_tar(input_path, old_addr, new_addr, image_name=image_name)
    else:
        print("Error: input must be a directory or a .tar file")
        sys.exit(1)
