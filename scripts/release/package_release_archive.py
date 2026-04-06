#!/usr/bin/env python3
# Copyright (c) 2026 The BTX developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://opensource.org/license/mit/.
"""Create a canonical BTX binary archive for one release platform.

The generated archive contains the release binaries plus the fast-start and
mining helper scripts needed for a download-and-go operator flow.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SUPPORT_FILES_MANIFEST = Path(__file__).with_name("support_files.txt")


def load_support_files(manifest_path: Path = SUPPORT_FILES_MANIFEST) -> list[str]:
    support_files: list[str] = []
    for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        support_files.append(line)
    return support_files


PLATFORM_CONFIGS = {
    "linux-x86_64": {
        "triple": "x86_64-linux-gnu",
        "archive_format": "tar.gz",
        "exe_suffix": "",
    },
    "linux-arm64": {
        "triple": "aarch64-linux-gnu",
        "archive_format": "tar.gz",
        "exe_suffix": "",
    },
    "windows-x86_64": {
        "triple": "x86_64-w64-mingw32",
        "archive_format": "zip",
        "exe_suffix": ".exe",
    },
    "macos-x86_64": {
        "triple": "x86_64-apple-darwin",
        "archive_format": "tar.gz",
        "exe_suffix": "",
    },
    "macos-arm64": {
        "triple": "arm64-apple-darwin",
        "archive_format": "tar.gz",
        "exe_suffix": "",
    },
}
SUPPORT_FILES = load_support_files()
MIN_ZIP_TIMESTAMP = datetime(1980, 1, 1, tzinfo=timezone.utc)


def write_executable_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o755)


def source_date_epoch() -> int | None:
    raw_value = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_value is None or not raw_value.strip():
        return None
    try:
        epoch = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"SOURCE_DATE_EPOCH must be an integer, got: {raw_value!r}") from exc
    if epoch < 0:
        raise ValueError(f"SOURCE_DATE_EPOCH must be non-negative, got: {epoch}")
    return epoch


def normalize_release_tree_metadata(release_root: Path, epoch: int | None) -> None:
    if epoch is None:
        return
    for path in sorted([release_root, *release_root.rglob("*")]):
        os.utime(path, (epoch, epoch), follow_symlinks=False)


def iter_release_members(release_root: Path) -> list[Path]:
    return [release_root, *sorted(release_root.rglob("*"))]


def tarinfo_for_path(archive: tarfile.TarFile, path: Path, arcname: str, epoch: int | None) -> tarfile.TarInfo:
    tarinfo = archive.gettarinfo(str(path), arcname=arcname)
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = ""
    tarinfo.gname = ""
    if epoch is not None:
        tarinfo.mtime = epoch
    return tarinfo


def zip_timestamp_tuple(epoch: int | None) -> tuple[int, int, int, int, int, int]:
    if epoch is None:
        timestamp = datetime.now(timezone.utc)
    else:
        timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if timestamp < MIN_ZIP_TIMESTAMP:
        timestamp = MIN_ZIP_TIMESTAMP
    return timestamp.timetuple()[:6]


def zip_external_attributes(path: Path) -> int:
    mode = path.stat().st_mode
    return mode << 16


def render_linux_wrapper(binary_name: str) -> str:
    extra_packages = " libsqlite3-0 libzmq5" if binary_name == "btxd" else ""
    return f"""#!/bin/sh
set -eu
SELF_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REAL="$SELF_DIR/../libexec/{binary_name}.real"
if [ ! -x "$REAL" ]; then
  echo "BTX packaged binary is missing: $REAL" >&2
  exit 127
fi
if command -v ldd >/dev/null 2>&1; then
  missing="$(ldd "$REAL" 2>/dev/null | awk '/=> not found/ {{print $1}}' | tr '\\n' ' ')"
  if [ -n "$missing" ]; then
    echo "BTX {binary_name} is missing runtime libraries: $missing" >&2
    echo "Ubuntu/Debian hint: sudo apt-get install libevent-2.1-7t64 libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64{extra_packages}" >&2
    echo "General hint: install the equivalent libevent, sqlite3, and zeromq runtime packages for your distribution." >&2
    echo "The packaged binary is located at: $REAL" >&2
    exit 127
  fi
fi
exec "$REAL" "$@"
"""


def render_macos_wrapper(binary_name: str) -> str:
    return f"""#!/bin/sh
set -eu
SELF_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REAL="$SELF_DIR/../libexec/{binary_name}.real"
if [ ! -x "$REAL" ]; then
  echo "BTX packaged binary is missing: $REAL" >&2
  exit 127
fi
if command -v otool >/dev/null 2>&1; then
  missing=""
  while IFS= read -r dep; do
    case "$dep" in
      ""|@*|/System/*|/usr/lib/*) continue ;;
    esac
    if [ ! -e "$dep" ]; then
      missing="$missing $dep"
    fi
  done <<EOF
$(otool -L "$REAL" | awk 'NR>1 {{print $1}}')
EOF
  if [ -n "$missing" ]; then
    echo "BTX {binary_name} is missing runtime libraries:$missing" >&2
    echo "Homebrew libevent is required for this native preview build." >&2
    echo "Install it with: brew install libevent" >&2
    echo "Apple Silicon default prefix: /opt/homebrew/opt/libevent/lib" >&2
    echo "Intel default prefix: /usr/local/opt/libevent/lib" >&2
    echo "The packaged binary is located at: $REAL" >&2
    exit 127
  fi
fi
exec "$REAL" "$@"
"""


def render_runtime_wrapper(platform_id: str, binary_name: str) -> str | None:
    if platform_id.startswith("linux-"):
        return render_linux_wrapper(binary_name)
    if platform_id.startswith("macos-"):
        return render_macos_wrapper(binary_name)
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="Directory where the archive will be written.")
    parser.add_argument("--version", required=True, help="Release version string, for example 29.2.")
    parser.add_argument(
        "--platform-id",
        required=True,
        choices=sorted(PLATFORM_CONFIGS.keys()),
        help="Canonical release platform id.",
    )
    parser.add_argument("--btxd", required=True, help="Path to the btxd binary for this platform.")
    parser.add_argument("--btx-cli", required=True, help="Path to the btx-cli binary for this platform.")
    parser.add_argument(
        "--source-root",
        default=str(ROOT),
        help="Repository root used to source helper scripts and docs (default: repo root).",
    )
    parser.add_argument(
        "--archive-name",
        help="Optional output filename override. Defaults to btx-<version>-<target>.<ext>.",
    )
    return parser.parse_args(argv)


def ensure_input_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {label}: {path}")
    return path


def archive_filename(version: str, platform_id: str, override: str | None) -> str:
    if override:
        return override
    config = PLATFORM_CONFIGS[platform_id]
    suffix = ".zip" if config["archive_format"] == "zip" else ".tar.gz"
    return f"btx-{version}-{config['triple']}{suffix}"


def stage_release_tree(
    *,
    version: str,
    platform_id: str,
    btxd_path: Path,
    btx_cli_path: Path,
    source_root: Path,
    temp_root: Path,
) -> tuple[Path, list[str]]:
    config = PLATFORM_CONFIGS[platform_id]
    release_root = temp_root / f"btx-{version}"
    included: list[str] = []

    bin_dir = release_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    binary_pairs = [
        (ensure_input_file(btxd_path, "btxd binary"), f"btxd{config['exe_suffix']}"),
        (ensure_input_file(btx_cli_path, "btx-cli binary"), f"btx-cli{config['exe_suffix']}"),
    ]
    for source, dest_name in binary_pairs:
        wrapper_content = render_runtime_wrapper(platform_id, dest_name)
        if wrapper_content is None:
            destination = bin_dir / dest_name
            shutil.copy2(source, destination)
            included.append(str(destination.relative_to(release_root)))
            continue

        libexec_dir = release_root / "libexec"
        libexec_dir.mkdir(parents=True, exist_ok=True)
        real_binary = libexec_dir / f"{dest_name}.real"
        shutil.copy2(source, real_binary)
        included.append(str(real_binary.relative_to(release_root)))

        wrapper_path = bin_dir / dest_name
        write_executable_text(wrapper_path, wrapper_content)
        included.append(str(wrapper_path.relative_to(release_root)))

    for relative_path in SUPPORT_FILES:
        source = ensure_input_file(source_root / relative_path, relative_path)
        destination = release_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        included.append(str(destination.relative_to(release_root)))

    return release_root, sorted(included)


def write_tar_gz(archive_path: Path, release_root: Path, *, epoch: int | None) -> None:
    with archive_path.open("wb") as raw_handle:
        gzip_kwargs = {"fileobj": raw_handle, "mode": "wb", "filename": ""}
        if epoch is not None:
            gzip_kwargs["mtime"] = epoch
        with gzip.GzipFile(**gzip_kwargs) as gzip_handle:
            with tarfile.open(fileobj=gzip_handle, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in iter_release_members(release_root):
                    arcname = str(path.relative_to(release_root.parent))
                    tarinfo = tarinfo_for_path(archive, path, arcname, epoch)
                    if path.is_file():
                        with path.open("rb") as source_handle:
                            archive.addfile(tarinfo, source_handle)
                    else:
                        archive.addfile(tarinfo)


def write_zip(archive_path: Path, release_root: Path, *, epoch: int | None) -> None:
    timestamp = zip_timestamp_tuple(epoch)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in iter_release_members(release_root):
            arcname = str(path.relative_to(release_root.parent))
            if path.is_dir():
                info = zipfile.ZipInfo(f"{arcname}/")
                info.date_time = timestamp
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = ((path.stat().st_mode | 0o040000) << 16) | 0x10
                archive.writestr(info, b"")
                continue

            info = zipfile.ZipInfo(arcname)
            info.date_time = timestamp
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = zip_external_attributes(path)
            archive.writestr(info, path.read_bytes())


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_root = Path(args.source_root).expanduser().resolve()
    archive_path = output_dir / archive_filename(args.version, args.platform_id, args.archive_name)
    epoch = source_date_epoch()

    with tempfile.TemporaryDirectory(prefix="btx-release-archive-") as temp_dir:
        temp_root = Path(temp_dir)
        release_root, included_paths = stage_release_tree(
            version=args.version,
            platform_id=args.platform_id,
            btxd_path=Path(args.btxd).expanduser().resolve(),
            btx_cli_path=Path(args.btx_cli).expanduser().resolve(),
            source_root=source_root,
            temp_root=temp_root,
        )
        normalize_release_tree_metadata(release_root, epoch)
        if PLATFORM_CONFIGS[args.platform_id]["archive_format"] == "zip":
            write_zip(archive_path, release_root, epoch=epoch)
        else:
            write_tar_gz(archive_path, release_root, epoch=epoch)

    json.dump(
        {
            "archive_path": str(archive_path),
            "archive_name": archive_path.name,
            "platform_id": args.platform_id,
            "archive_format": PLATFORM_CONFIGS[args.platform_id]["archive_format"],
            "included_paths": included_paths,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
