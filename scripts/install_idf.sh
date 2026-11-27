#!/usr/bin/env bash
# Install a pinned ESP-IDF release as a local PlatformIO framework-espidf package.
#
# Why: the official platformio/espressif32 platform (7.0.1) pins framework
# package ~4.60001.0 (= ESP-IDF 6.0.1) and the PlatformIO registry publishes no
# package for the release Kleidos builds against. platformio.ini overrides the
# framework via `platform_packages = platformio/framework-espidf @ ~<version>`,
# which this script satisfies by installing the official Espressif release
# archive into the local PlatformIO package storage with a matching manifest.
#
# The version lives in ONE place here and the package version is derived from
# it, so moving to the next release is editing IDF_TAG and the three
# platform_packages pins in platformio.ini — the file used to be called
# install_idf602.sh, which meant every bump renamed the script and left stale
# references behind.
#
# Usage:  scripts/install_idf.sh              # the pinned release below
#         IDF_TAG=v6.1 IDF_SHA256=<digest> scripts/install_idf.sh
#                                             # override, e.g. to trial one
# Idempotent: skips work when the package is already installed.
#
# The archive is checked against a pinned SHA-256 before it is unpacked, the
# cached copy included, and a mismatch deletes it and stops. A tag without a
# pinned digest is refused unless IDF_SHA256 names one.

set -euo pipefail

# An install replaces files under running builds, so it runs alone, after every
# running pio job, however it was started (scripts/pio_job.py).
if [[ "${KLEIDOS_PIO_JOB_ALONE:-}" != 1 ]] && command -v python3 >/dev/null 2>&1; then
  exec "$(dirname "${BASH_SOURCE[0]}")/pio-job" -- bash "$0" "$@"
fi

# The release Kleidos builds against. v6.0.3 rather than v6.1 deliberately:
# release/v6.1 was cut in June and tagged 2026-08-27, while v6.0.3 shipped
# 2026-09-02 carrying July/August fixes that never reached the v6.1 tag —
# among them the NimBLE bond-store overflow with IRK enabled, and the
# esp_http_server Content-Length-above-UINT32_MAX rejection. Both land on
# surfaces this product exposes. Revisit when v6.1.1 ships.
IDF_TAG="${IDF_TAG:-v6.0.3}"

# SHA-256 of each release archive this script will unpack. The v6.0.3 digest
# was read on 2026-11-14 from two independent downloads of the release asset
# (the cached copy fetched on 2026-09-22 and a fresh fetch on 2026-11-14), and
# both agree with the digest GitHub publishes for the asset.
pinned_sha256() {
  case "$1" in
    v6.0.3) echo "748b12484402d8a1cb58ba68b7545d2a1f96d36820ab0145e0332c8348ba5ab7" ;;
    *) echo "" ;;
  esac
}
IDF_SHA256="${IDF_SHA256:-$(pinned_sha256 "${IDF_TAG}")}"
if [[ -z "${IDF_SHA256}" ]]; then
  echo "ERROR: no pinned SHA-256 for ESP-IDF ${IDF_TAG}; set IDF_SHA256 to the archive's digest" >&2
  exit 1
fi

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# PlatformIO package version from the IDF tag: 4.<major*10000 + minor*100 +
# patch>.0, which is the scheme the registry uses (6.0.2 -> 4.60002.0).
_v="${IDF_TAG#v}"
_major="${_v%%.*}"; _rest="${_v#*.}"
_minor="${_rest%%.*}"
if [ "${_rest}" = "${_minor}" ]; then _patch=0; else _patch="${_rest#*.}"; fi
PKG_VERSION="4.$((_major * 10000 + _minor * 100 + _patch)).0"
PKG_DIR="${HOME}/.platformio/packages/framework-espidf@${PKG_VERSION}"
ZIP_URL="https://github.com/espressif/esp-idf/releases/download/${IDF_TAG}/esp-idf-${IDF_TAG}.zip"
CACHE_ZIP="${HOME}/.platformio/.cache/tmp/esp-idf-${IDF_TAG}.zip"

# --- Platform-builder patch (idempotent, applied even when the package is
# already installed): espressif32@7.0.1's espidf.py hardcodes the single
# pre-6.0.2 bootloader linker template "bootloader.ld.in"; ESP-IDF 6.0.2 split
# it into bootloader.memory.ld.in + bootloader.sections.ld.in (the bootloader
# CMake emits "-T bootloader.memory.ld -T bootloader.sections.ld"). Patch the
# builder to preprocess whichever template set the framework ships. Obsolete
# once the platform pin moves past 7.0.1 with native 6.0.2 support.
patch_builder() {
  python3 - <<'PYEOF'
import re, sys, pathlib

path = pathlib.Path.home() / ".platformio/platforms/espressif32/builder/frameworks/espidf.py"
src = path.read_text()
MARKER = "Kleidos: IDF 6.0.2 split bootloader linker scripts"
if MARKER in src:
    print(f"builder already patched: {path}")
    sys.exit(0)

old = """        bootloader_linker_script = preprocess_linker_script(
            os.path.join(
                bootloader_src_dir,
                "main",
                "ld",
                idf_variant,
                "bootloader.ld.in",
            ),
            os.path.join(BUILD_DIR, "bootloader", "ld", "bootloader.ld"),
            [
                os.path.join(BUILD_DIR, "bootloader", "config"),
                os.path.join(FRAMEWORK_DIR, "components", "esp_system", "ld"),
            ],
        )

        # The linker script is generated before the final bootloader ELF
        env.Depends(
            os.path.join("$BUILD_DIR", "bootloader.elf"),
            bootloader_linker_script,
        )
"""
new = """        # Kleidos: IDF 6.0.2 split bootloader linker scripts
        # (bootloader.ld.in -> bootloader.memory.ld.in + bootloader.sections.ld.in);
        # preprocess whichever template set this framework version ships.
        ld_dir = os.path.join(bootloader_src_dir, "main", "ld", idf_variant)
        if os.path.isfile(os.path.join(ld_dir, "bootloader.ld.in")):
            ld_in_names = ["bootloader.ld.in"]
        else:
            ld_in_names = ["bootloader.memory.ld.in", "bootloader.sections.ld.in"]
        for ld_in_name in ld_in_names:
            bootloader_linker_script = preprocess_linker_script(
                os.path.join(ld_dir, ld_in_name),
                os.path.join(BUILD_DIR, "bootloader", "ld", ld_in_name[:-3]),
                [
                    os.path.join(BUILD_DIR, "bootloader", "config"),
                    os.path.join(FRAMEWORK_DIR, "components", "esp_system", "ld"),
                    # bootloader.sections.ld.in includes the shared
                    # bootloader.sections.common.ld from the parent ld/ dir
                    os.path.join(bootloader_src_dir, "main", "ld"),
                ],
            )

            # The linker script is generated before the final bootloader ELF
            env.Depends(
                os.path.join("$BUILD_DIR", "bootloader.elf"),
                bootloader_linker_script,
            )
"""
if old not in src:
    print("ERROR: expected builder block not found; espressif32 platform layout changed", file=sys.stderr)
    sys.exit(1)
path.write_text(src.replace(old, new, 1))
print(f"patched: {path}")
PYEOF

  # Patch 2: mbedTLS 4.1 (ESP-IDF 6.0.2) splits TF-PSA-Crypto into nested
  # sub-libraries (platform, drivers/builtin, utilities, extras) that are
  # neither direct dependencies of the ELF target nor named in the CMake link
  # fragments, so find_lib_deps() never links them (undefined
  # mbedtls_platform_zeroize etc.). Link every built mbedtls-component archive.
  python3 - <<'PYEOF'
import pathlib, sys

path = pathlib.Path.home() / ".platformio/platforms/espressif32/builder/frameworks/espidf.py"
src = path.read_text()
MARKER2 = "Kleidos: link nested TF-PSA-Crypto sub-libraries"
if MARKER2 in src:
    print(f"builder already patched (2): {path}")
    sys.exit(0)

old = """        if (
            component_config["nameOnDisk"] in implicit_lib_deps
            and component["lib"] not in result
        ):
            result.append(component["lib"])

    return result
"""
new = """        if (
            component_config["nameOnDisk"] in implicit_lib_deps
            and component["lib"] not in result
        ):
            result.append(component["lib"])

    # Kleidos: link nested TF-PSA-Crypto sub-libraries (mbedTLS 4.1 / IDF
    # 6.0.2): platform, drivers/builtin, utilities, extras are neither direct
    # ELF dependencies nor present in the link fragments; without them the
    # final link fails (e.g. undefined mbedtls_platform_zeroize).
    for component in components_map.values():
        lib = component["lib"]
        if "mbedtls" in str(lib).replace("\\\\", "/") and lib not in result:
            result.append(lib)

    return result
"""
if old not in src:
    print("ERROR: find_lib_deps block not found; espressif32 platform layout changed", file=sys.stderr)
    sys.exit(1)
path.write_text(src.replace(old, new, 1))
print(f"patched (2): {path}")
PYEOF
}
patch_builder

if [[ -f "${PKG_DIR}/package.json" ]] &&
   grep -q "\"version\": \"${PKG_VERSION}\"" "${PKG_DIR}/package.json"; then
  echo "framework-espidf ${PKG_VERSION} (ESP-IDF ${IDF_TAG}) already installed at ${PKG_DIR}"
  exit 0
fi

mkdir -p "$(dirname "${CACHE_ZIP}")"
if [[ ! -f "${CACHE_ZIP}" ]]; then
  echo "Downloading ESP-IDF ${IDF_TAG} release archive (~1.9 GB)..."
  curl -fL --retry 3 -o "${CACHE_ZIP}.part" "${ZIP_URL}"
  mv "${CACHE_ZIP}.part" "${CACHE_ZIP}"
fi

# Checked every time, the cached copy included: nothing is unpacked from an
# archive whose digest is not the pinned one.
echo "Verifying the archive's SHA-256..."
ACTUAL_SHA256="$(sha256_of "${CACHE_ZIP}")"
if [[ "${ACTUAL_SHA256}" != "${IDF_SHA256}" ]]; then
  echo "ERROR: ${CACHE_ZIP} has SHA-256 ${ACTUAL_SHA256}, expected ${IDF_SHA256}; deleted" >&2
  rm -f "${CACHE_ZIP}"
  exit 1
fi

echo "Extracting to ${PKG_DIR}..."
STAGE_DIR="$(mktemp -d "${HOME}/.platformio/.cache/tmp/idf602.XXXXXX")"
trap 'rm -rf "${STAGE_DIR}"' EXIT
unzip -q "${CACHE_ZIP}" -d "${STAGE_DIR}"
SRC_ROOT="$(find "${STAGE_DIR}" -maxdepth 1 -mindepth 1 -type d | head -1)"
[[ -f "${SRC_ROOT}/components/mbedtls/CMakeLists.txt" ]] ||
  { echo "ERROR: archive layout unexpected (no components/ under ${SRC_ROOT})"; exit 1; }

# Prune what the PlatformIO build never reads (matches the official package,
# which ships without docs/examples).
rm -rf "${SRC_ROOT}/docs" "${SRC_ROOT}/examples" "${SRC_ROOT}/.github"

cat > "${SRC_ROOT}/package.json" <<EOF
{
  "name": "framework-espidf",
  "version": "${PKG_VERSION}",
  "title": "Espressif IoT Development Framework",
  "description": "Espressif IoT Development Framework. Official development framework for ESP32 chip",
  "keywords": ["framework", "esp32", "esp32c3", "esp32c6", "esp32s2", "esp32s3", "espressif"],
  "homepage": "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/",
  "license": "Apache-2.0",
  "repository": {"type": "git", "url": "https://github.com/espressif/esp-idf"}
}
EOF

# PlatformIO's package manager only treats a storage dir as satisfying an
# owned requirement ("platformio/framework-espidf") when the .piopm metadata
# marks it as such; without it the manager falls through to the registry and
# fails with UnknownPackageError.
cat > "${SRC_ROOT}/.piopm" <<EOF
{"type": "tool", "name": "framework-espidf", "version": "${PKG_VERSION}", "spec": {"owner": "platformio", "id": 8103, "name": "framework-espidf", "requirements": null, "uri": null}}
EOF

rm -rf "${PKG_DIR}"
mv "${SRC_ROOT}" "${PKG_DIR}"
echo "Installed framework-espidf ${PKG_VERSION} (ESP-IDF ${IDF_TAG}) at ${PKG_DIR}"
grep MBEDTLS_VERSION_STRING "${PKG_DIR}/components/mbedtls/mbedtls/include/mbedtls/build_info.h" | head -1
