#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud Android Manager - GitHub Actions + Tailscale + ADB + scrcpy GUI.

Only Python's standard library is required. Designed for Windows + Tkinter.
GitHub token is session-only and is NEVER written to the config file.
"""

from __future__ import annotations

import base64
import datetime as dt
import glob
import io
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "Cloud Android Manager"
CONFIG_PATH = Path.home() / ".cloud_android_manager.json"
GLOBAL_SECRET_PATH = Path.home() / ".cloud_android_manager_secrets.json"
DEFAULT_PACKAGE = "com.temperaturecoin"
DEFAULT_APK_URL = (
    "https://github.com/usdt19908888286-bit/android-test/releases/download/"
    "cloud-phone-apk-cache/app-release.apk"
)
PKG_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
RUN_ID_RE = re.compile(r"-(\d+)$")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BUILTIN_WORKFLOW_PATH = ".github/workflows/cloud-phone-managed.yml"
BUILTIN_MANAGED_WORKFLOW = 'name: Managed Android Cloud Phone\nrun-name: ${{ inputs.phone_name || \'Managed Phone\' }} · ${{ inputs.phone_id || \'001\' }} / ${{ inputs.mode || \'new\' }}\n\non:\n  workflow_dispatch:\n    inputs:\n      mode:\n        description: Create a new phone or restore the latest encrypted backup\n        required: true\n        default: new\n        type: choice\n        options:\n          - new\n          - restore\n      phone_id:\n        description: Logical phone ID (digits only, e.g. 001)\n        required: true\n        default: \'001\'\n        type: string\n      phone_name:\n        description: Friendly phone name shown in GitHub and the local GUI\n        required: false\n        default: \'BICOIN-001\'\n        type: string\n      package_name:\n        description: Android package used for GUI monitoring/launch only; it is not auto-installed\n        required: false\n        default: \'com.temperaturecoin\'\n        type: string\n      apk_url:\n        description: Optional APK reference only; managed phone creation does not auto-install APKs\n        required: false\n        default: \'https://github.com/usdt19908888286-bit/android-test/releases/download/cloud-phone-apk-cache/app-release.apk\'\n        type: string\n      api_level:\n        description: Android API level\n        required: false\n        default: \'35\'\n        type: string\n      target:\n        description: Android system image target\n        required: false\n        default: \'google_apis\'\n        type: string\n      arch:\n        description: Android emulator architecture\n        required: false\n        default: \'x86_64\'\n        type: string\n      profile:\n        description: Android device profile\n        required: false\n        default: \'pixel_6\'\n        type: string\n      cores:\n        description: Emulator CPU cores\n        required: false\n        default: \'4\'\n        type: string\n      ram_mb:\n        description: Emulator RAM in MiB\n        required: false\n        default: \'8192\'\n        type: string\n      device_manufacturer:\n        description: Optional Android Build.MANUFACTURER value for emulator compatibility testing\n        required: false\n        default: \'\'\n        type: string\n      device_brand:\n        description: Optional Android Build.BRAND value for emulator compatibility testing\n        required: false\n        default: \'\'\n        type: string\n      device_model:\n        description: Optional Android Build.MODEL value for emulator compatibility testing\n        required: false\n        default: \'\'\n        type: string\n\npermissions:\n  contents: read\n  actions: read\n  issues: write\n\nconcurrency:\n  group: managed-android-phone-${{ inputs.phone_id || \'001\' }}\n  cancel-in-progress: true\n\nenv:\n  DISPLAY: :99\n  APK_URL: ${{ inputs.apk_url || \'https://github.com/usdt19908888286-bit/android-test/releases/download/cloud-phone-apk-cache/app-release.apk\' }}\n  PHONE_ID: ${{ inputs.phone_id || \'001\' }}\n  PHONE_NAME: ${{ inputs.phone_name || \'BICOIN-001\' }}\n  PACKAGE_NAME: ${{ inputs.package_name || \'com.temperaturecoin\' }}\n  AVD_NAME: managed-phone-${{ inputs.phone_id || \'001\' }}\n  DEVICE_MANUFACTURER: ${{ inputs.device_manufacturer || \'\' }}\n  DEVICE_BRAND: ${{ inputs.device_brand || \'\' }}\n  DEVICE_MODEL: ${{ inputs.device_model || \'\' }}\n\njobs:\n  phone:\n    runs-on: ubuntu-latest\n    timeout-minutes: 330\n    env:\n      AVD_BACKUP_KEY: ${{ secrets.AVD_BACKUP_KEY }}\n    steps:\n      - name: Checkout\n        uses: actions/checkout@v4\n\n      - name: Validate inputs and backup encryption key\n        shell: bash\n        run: |\n          set -euo pipefail\n          case "$PHONE_ID" in\n            \'\'|*[!0-9]*) echo \'::error::phone_id must contain digits only\'; exit 1 ;;\n          esac\n          printf \'%s\\n\' "${PACKAGE_NAME:-}" | grep -Eq \'^[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+$\' || { echo \'::error::package_name is invalid\'; exit 1; }\n          printf \'%s\\n\' "${DEVICE_MANUFACTURER:-}" | grep -Eq \'^[A-Za-z0-9._+ -]*$\' || { echo \'::error::device_manufacturer contains unsupported characters\'; exit 1; }\n          printf \'%s\\n\' "${DEVICE_BRAND:-}" | grep -Eq \'^[A-Za-z0-9._+-]*$\' || { echo \'::error::device_brand contains unsupported characters\'; exit 1; }\n          printf \'%s\\n\' "${DEVICE_MODEL:-}" | grep -Eq \'^[A-Za-z0-9._+() -]*$\' || { echo \'::error::device_model contains unsupported characters\'; exit 1; }\n          if [ -n "${DEVICE_MODEL:-}" ] && { [ -z "${DEVICE_MANUFACTURER:-}" ] || [ -z "${DEVICE_BRAND:-}" ]; }; then\n            echo \'::error::device_model requires device_manufacturer and device_brand\'\n            exit 1\n          fi\n          if [ "${{ inputs.mode }}" = \'restore\' ] && [ -z "${AVD_BACKUP_KEY:-}" ]; then\n            echo \'::error::AVD_BACKUP_KEY repository secret is required to restore an encrypted backup.\'\n            exit 1\n          fi\n          if [ -z "${AVD_BACKUP_KEY:-}" ]; then\n            echo \'::warning::AVD_BACKUP_KEY is not initialized yet. New phone may run, but backup is disabled until the secret is created.\'\n          fi\n          echo "MANAGED_PHONE_MODE=${{ inputs.mode }}"\n\n      - name: Prepare phone identity\n        id: identity\n        shell: bash\n        run: |\n          set -euo pipefail\n          SLUG=$(printf \'%s\' "${PHONE_NAME:-}" | tr \'[:upper:]\' \'[:lower:]\' | sed -E \'s/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//\' | cut -c1-35)\n          [ -n "$SLUG" ] || SLUG="managed-phone${PHONE_ID}"\n          echo "ts_hostname=${SLUG}-${GITHUB_RUN_ID}" >> "$GITHUB_OUTPUT"\n          echo "PHONE_DISPLAY_NAME=${PHONE_NAME}"\n\n      - name: Join Tailscale\n        uses: tailscale/github-action@v4\n        with:\n          oauth-client-id: ${{ secrets.TS_API_CLIENT_ID }}\n          oauth-secret: ${{ secrets.TS_API_CLIENT_SECRET }}\n          tags: tag:github-phone\n          hostname: ${{ steps.identity.outputs.ts_hostname }}\n          version: latest\n\n      - name: Enable KVM and install runtime dependencies\n        shell: bash\n        run: |\n          set -euo pipefail\n          echo \'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"\' | sudo tee /etc/udev/rules.d/99-kvm4all.rules\n          sudo udevadm control --reload-rules\n          sudo udevadm trigger --name-match=kvm\n          test -e /dev/kvm\n          sudo apt-get update -qq\n          sudo apt-get install -y xvfb netcat-openbsd curl unzip libpulse0 zstd openssl jq\n          Xvfb :99 -screen 0 1280x720x24 >/tmp/xvfb.log 2>&1 &\n          sleep 2\n\n      - name: Verify direct Internet\n        shell: bash\n        run: |\n          set -euo pipefail\n          curl -fsS --connect-timeout 12 --max-time 25 https://www.cloudflare.com/cdn-cgi/trace -o /dev/null\n          echo \'DIRECT_NETWORK_OK\'\n\n      - name: Restore latest encrypted AVD backup\n        if: inputs.mode == \'restore\'\n        id: cache-restore\n        uses: actions/cache/restore@v5\n        with:\n          path: /tmp/managed-avd-backup\n          key: managed-avd-${{ inputs.phone_id }}-restore-${{ github.run_id }}\n          restore-keys: |\n            managed-avd-${{ inputs.phone_id }}-\n\n      - name: Decrypt restored AVD\n        if: inputs.mode == \'restore\'\n        shell: bash\n        run: |\n          set -euo pipefail\n          [ -n "${{ steps.cache-restore.outputs.cache-matched-key }}" ] || {\n            echo \'::error::No encrypted AVD backup exists for this phone_id.\'\n            exit 1\n          }\n          ENC_FILE=$(find /tmp/managed-avd-backup -maxdepth 1 -type f -name \'*.enc\' | head -n1)\n          [ -n "$ENC_FILE" ] && [ -s "$ENC_FILE" ]\n          mkdir -p "$HOME/.android"\n          openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \\\n            -pass env:AVD_BACKUP_KEY -in "$ENC_FILE" -out /tmp/managed-avd.tar.zst\n          zstd -d -q -c /tmp/managed-avd.tar.zst | tar -xf - -C "$HOME/.android"\n          rm -f /tmp/managed-avd.tar.zst\n          test -f "$HOME/.android/avd/${AVD_NAME}.ini"\n          test -d "$HOME/.android/avd/${AVD_NAME}.avd"\n          echo "RESTORED_CACHE_KEY=${{ steps.cache-restore.outputs.cache-matched-key }}"\n          echo \'MANAGED_AVD_DECRYPT_RESTORE_OK\'\n\n      - name: Prepare runner status service\n        shell: bash\n        run: |\n          cat >/tmp/runner_status_server.py <<\'PY\'\n          import http.server, json, os, subprocess, time\n\n          RUN_ID = os.environ.get(\'GITHUB_RUN_ID\', \'\')\n          PHONE_ID = os.environ.get(\'PHONE_ID\', \'\')\n\n          def cpu_sample():\n              def snap():\n                  with open(\'/proc/stat\', \'r\', encoding=\'utf-8\') as f:\n                      vals = [int(x) for x in f.readline().split()[1:]]\n                  idle = vals[3] + (vals[4] if len(vals) > 4 else 0)\n                  return sum(vals), idle\n              a_total, a_idle = snap(); time.sleep(0.20); b_total, b_idle = snap()\n              total = max(1, b_total - a_total)\n              return round(100.0 * (1.0 - (b_idle - a_idle) / total), 1)\n\n          def mem_sample():\n              vals = {}\n              with open(\'/proc/meminfo\', \'r\', encoding=\'utf-8\') as f:\n                  for line in f:\n                      k, v = line.split(\':\', 1)\n                      vals[k] = int(v.strip().split()[0])\n              total = vals.get(\'MemTotal\', 0)\n              avail = vals.get(\'MemAvailable\', 0)\n              used = max(0, total - avail)\n              pct = round(100.0 * used / total, 1) if total else 0\n              return total, used, avail, pct\n\n          def qemu_sample():\n              cp = subprocess.run(\n                  [\'ps\', \'-C\', \'qemu-system-x86_64\', \'-o\', \'%cpu=,%mem=,rss=,pid=\', \'--sort=-%cpu\'],\n                  capture_output=True, text=True\n              )\n              line = next((x.strip() for x in cp.stdout.splitlines() if x.strip()), \'\')\n              if not line:\n                  return {\'cpu_percent\': 0.0, \'mem_percent\': 0.0, \'rss_kib\': 0, \'pid\': None}\n              parts = line.split()\n              try:\n                  return {\n                      \'cpu_percent\': float(parts[0]),\n                      \'mem_percent\': float(parts[1]),\n                      \'rss_kib\': int(parts[2]),\n                      \'pid\': int(parts[3]),\n                  }\n              except Exception:\n                  return {\'raw\': line}\n\n          class Handler(http.server.BaseHTTPRequestHandler):\n              def do_GET(self):\n                  if self.path not in (\'/\', \'/status\'):\n                      self.send_response(404); self.end_headers(); return\n                  total, used, avail, pct = mem_sample()\n                  load = os.getloadavg()\n                  data = {\n                      \'ok\': True,\n                      \'run_id\': RUN_ID,\n                      \'phone_id\': PHONE_ID,\n                      \'cpu_percent\': cpu_sample(),\n                      \'load1\': round(load[0], 2),\n                      \'load5\': round(load[1], 2),\n                      \'load15\': round(load[2], 2),\n                      \'mem_total_kib\': total,\n                      \'mem_used_kib\': used,\n                      \'mem_available_kib\': avail,\n                      \'mem_percent\': pct,\n                      \'qemu\': qemu_sample(),\n                  }\n                  body = json.dumps(data).encode()\n                  self.send_response(200)\n                  self.send_header(\'Content-Type\', \'application/json\')\n                  self.send_header(\'Content-Length\', str(len(body)))\n                  self.end_headers(); self.wfile.write(body)\n              def log_message(self, *args):\n                  pass\n\n          http.server.ThreadingHTTPServer((\'127.0.0.1\', 8787), Handler).serve_forever()\n          PY\n          nohup python3 /tmp/runner_status_server.py >/tmp/runner-status.log 2>&1 &\n          sleep 1\n          curl -fsS http://127.0.0.1:8787/status >/tmp/runner-status-probe.json\n          sudo -E tailscale serve --bg --yes --tcp=8787 tcp://127.0.0.1:8787\n          echo \'RUNNER_STATUS_READY=8787\'\n\n      - name: Prepare managed Android bootstrap script\n        shell: bash\n        run: |\n          cat >/tmp/managed-phone.sh <<\'SCRIPT\'\n          #!/usr/bin/env bash\n          set -euo pipefail\n\n          wait_android_boot() {\n            adb wait-for-device\n            for _ in $(seq 1 180); do\n              [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d \'\\r\')" = \'1\' ] && return 0\n              sleep 1\n            done\n            return 1\n          }\n\n          echo \'MANAGED_AVD_BOOT_WAIT\'\n          wait_android_boot\n          echo \'MANAGED_AVD_BOOT_COMPLETE\'\n\n          # Optional Build identity for emulator compatibility testing. Android Emulator\n          # does not allow arbitrary ro.product.* values through -prop, so use the\n          # writable-system/remount development path and verify after reboot.\n          if [ -n "${DEVICE_MODEL:-}" ]; then\n            echo "DEVICE_IDENTITY_TARGET=${DEVICE_MANUFACTURER}|${DEVICE_BRAND}|${DEVICE_MODEL}"\n            IDENTITY_READY=0\n            if adb root >/tmp/adb-root.log 2>&1; then\n              adb wait-for-device\n              sleep 2\n              if adb remount >/tmp/adb-remount.log 2>&1; then\n                IDENTITY_READY=1\n              else\n                adb disable-verity >/tmp/adb-disable-verity.log 2>&1 || true\n                adb reboot || true\n                if wait_android_boot; then\n                  adb root >/tmp/adb-root2.log 2>&1 || true\n                  adb wait-for-device\n                  sleep 2\n                  if adb remount >/tmp/adb-remount2.log 2>&1; then\n                    IDENTITY_READY=1\n                  fi\n                fi\n              fi\n            fi\n\n            if [ "$IDENTITY_READY" = \'1\' ]; then\n              cat >/tmp/patch-device-identity.sh <<\'DEVICEPATCH\'\n          #!/system/bin/sh\n          set -eu\n          patch_prop() {\n            key="$1"\n            value="$2"\n            file="$3"\n            [ -f "$file" ] || return 0\n            if grep -q "^${key}=" "$file" 2>/dev/null; then\n              sed -i "s|^${key}=.*|${key}=${value}|g" "$file"\n            fi\n          }\n          for file in             /system/build.prop /system/system/build.prop             /product/build.prop /system/product/build.prop             /vendor/build.prop /system/vendor/build.prop             /odm/build.prop /system/odm/build.prop             /system_ext/build.prop /system/system_ext/build.prop; do\n            for suffix in \'\' \'.system\' \'.product\' \'.vendor\' \'.odm\' \'.system_ext\'; do\n              patch_prop "ro.product${suffix}.manufacturer" "$TARGET_MANUFACTURER" "$file"\n              patch_prop "ro.product${suffix}.brand" "$TARGET_BRAND" "$file"\n              patch_prop "ro.product${suffix}.model" "$TARGET_MODEL" "$file"\n            done\n          done\n          sync\n          DEVICEPATCH\n              adb push /tmp/patch-device-identity.sh /data/local/tmp/patch-device-identity.sh >/dev/null\n              adb shell chmod 700 /data/local/tmp/patch-device-identity.sh\n              if adb shell "TARGET_MANUFACTURER=\'${DEVICE_MANUFACTURER}\' TARGET_BRAND=\'${DEVICE_BRAND}\' TARGET_MODEL=\'${DEVICE_MODEL}\' sh /data/local/tmp/patch-device-identity.sh"; then\n                adb reboot || true\n                wait_android_boot\n              fi\n            else\n              echo \'::warning::Could not remount emulator system; continuing with stock AVD identity.\'\n            fi\n\n            ACTUAL_MANUFACTURER=$(adb shell getprop ro.product.manufacturer 2>/dev/null | tr -d \'\\r\')\n            ACTUAL_BRAND=$(adb shell getprop ro.product.brand 2>/dev/null | tr -d \'\\r\')\n            ACTUAL_MODEL=$(adb shell getprop ro.product.model 2>/dev/null | tr -d \'\\r\')\n            echo "DEVICE_IDENTITY_ACTUAL=${ACTUAL_MANUFACTURER}|${ACTUAL_BRAND}|${ACTUAL_MODEL}"\n            if [ "$ACTUAL_MODEL" = "$DEVICE_MODEL" ]; then\n              echo \'DEVICE_REAL_MODEL_APPLIED\'\n            else\n              echo "::warning::Requested model \'${DEVICE_MODEL}\', but Android reports \'${ACTUAL_MODEL}\'."\n            fi\n          fi\n\n          # Fresh Android often needs a short settling period before first app launch.\n          sleep 12\n          adb shell settings put global window_animation_scale 0 || true\n          adb shell settings put global transition_animation_scale 0 || true\n          adb shell settings put global animator_duration_scale 0 || true\n          adb shell settings put global disable_window_blurs 1 || true\n          adb shell settings put system peak_refresh_rate 60.0 || true\n          adb shell settings put system min_refresh_rate 60.0 || true\n          adb shell wm size 720x1600 || true\n          adb shell wm density 280 || true\n\n          # Phone creation/restoration is intentionally APK-neutral. The GUI may\n          # remember a package name for monitoring, but this workflow never downloads,\n          # installs, or auto-launches an APK.\n          DEFAULT_PACKAGE="$PACKAGE_NAME"\n          if adb shell pm path "$DEFAULT_PACKAGE" >/dev/null 2>&1; then\n            echo "MONITORED_APP_PRESENT=$DEFAULT_PACKAGE"\n          else\n            echo "MONITORED_APP_NOT_INSTALLED=$DEFAULT_PACKAGE"\n          fi\n          echo \'MANAGED_PHONE_APK_NEUTRAL\'\n\n          adb shell pm disable-user --user 0 com.android.vending >/dev/null 2>&1 || true\n          adb shell settings put global auto_update_system_apps 0 || true\n\n          echo \'ADB_TCPIP_BOOTSTRAP_START\'\n          adb tcpip 5555 >/tmp/adb-tcpip.log 2>&1 || { cat /tmp/adb-tcpip.log; exit 1; }\n          for _ in $(seq 1 30); do\n            nc -z 127.0.0.1 5555 && break\n            sleep 1\n          done\n          nc -z 127.0.0.1 5555\n          echo \'ADB_TCPIP_5555_READY\'\n\n          TS_IPV4=$(tailscale ip -4 | head -n1)\n          [ -n "$TS_IPV4" ]\n          sudo -E tailscale serve --bg --yes --tcp=5555 tcp://127.0.0.1:5555\n          echo \'MANAGED_CLOUD_PHONE_READY\'\n          echo "TAILSCALE_ADB=${TS_IPV4}:5555"\n          echo "RUNNER_STATUS=http://${TS_IPV4}:8787/status"\n          echo "SCRCPY_CONNECT=adb connect ${TS_IPV4}:5555 && scrcpy -s ${TS_IPV4}:5555"\n\n          COMMAND_TITLE="Cloud Phone Command ${PHONE_ID}"\n          for _ in $(seq 1 1800); do\n            ISSUE_LINE=$(gh api "/repos/${GITHUB_REPOSITORY}/issues?state=open&per_page=100" \\\n              --jq ".[] | select(.pull_request == null) | select(.title == \\"${COMMAND_TITLE}\\") | [.number,.body] | @tsv" \\\n              2>/dev/null | head -n1 || true)\n            if [ -n "$ISSUE_LINE" ]; then\n              ISSUE_NO=${ISSUE_LINE%%$\'\\t\'*}\n              ISSUE_BODY=${ISSUE_LINE#*$\'\\t\'}\n              CMD_JSON=$(printf \'%s\\n\' "$ISSUE_BODY" | grep -m1 \'^{\' || true)\n              if [ -n "$CMD_JSON" ]; then\n                CMD=$(jq -r \'.command // ""\' <<<"$CMD_JSON" 2>/dev/null || true)\n                TARGET_RUN=$(jq -r \'.run_id // 0\' <<<"$CMD_JSON" 2>/dev/null || true)\n                TARGET_PHONE=$(jq -r \'.phone_id // ""\' <<<"$CMD_JSON" 2>/dev/null || true)\n                if [ "$CMD" = \'backup\' ] && [ "$TARGET_RUN" = "$GITHUB_RUN_ID" ] && [ "$TARGET_PHONE" = "$PHONE_ID" ]; then\n                  if [ -z "${AVD_BACKUP_KEY:-}" ]; then\n                    echo \'BACKUP_REQUEST_REJECTED_NO_KEY\'\n                  else\n                    echo "BACKUP_REQUEST_RECEIVED issue=${ISSUE_NO}"\n                    printf \'%s\' "$ISSUE_NO" >/tmp/managed-backup-issue-number\n                    touch /tmp/managed-backup-requested\n                    adb shell sync || true\n                    exit 0\n                  fi\n                fi\n              fi\n            fi\n            sleep 10\n          done\n\n          # End-of-life safety backup when the 5-hour session naturally expires.\n          if [ -n "${AVD_BACKUP_KEY:-}" ]; then\n            echo \'SESSION_TIMEOUT_AUTO_BACKUP\'\n            touch /tmp/managed-backup-requested\n            adb shell sync || true\n          else\n            echo \'SESSION_TIMEOUT_AUTO_BACKUP_SKIPPED_NO_KEY\'\n          fi\n          SCRIPT\n          chmod +x /tmp/managed-phone.sh\n          bash -n /tmp/managed-phone.sh\n          echo \'MANAGED_BOOTSTRAP_SCRIPT_READY\'\n\n      - name: Start managed Android phone\n        id: android\n        uses: reactivecircus/android-emulator-runner@v2\n        env:\n          PHONE_MODE: ${{ inputs.mode }}\n          GH_TOKEN: ${{ github.token }}\n        with:\n          api-level: ${{ inputs.api_level }}\n          target: ${{ inputs.target }}\n          arch: ${{ inputs.arch }}\n          profile: ${{ inputs.profile }}\n          avd-name: managed-phone-${{ inputs.phone_id || \'001\' }}\n          cores: ${{ inputs.cores }}\n          ram-size: ${{ format(\'{0}M\', inputs.ram_mb) }}\n          force-avd-creation: ${{ inputs.mode == \'new\' }}\n          disable-animations: false\n          emulator-options: >-\n            -no-snapshot\n            ${{ inputs.device_model != \'\' && \'-writable-system\' || \'\' }}\n            -gpu swiftshader_indirect\n            -noaudio\n            -no-boot-anim\n            -camera-back none\n            -camera-front none\n          script: bash /tmp/managed-phone.sh\n\n      - name: Pack and encrypt full AVD backup\n        id: pack\n        if: always()\n        shell: bash\n        run: |\n          set -euo pipefail\n          if [ ! -f /tmp/managed-backup-requested ]; then\n            echo \'should_save=false\' >> "$GITHUB_OUTPUT"\n            echo \'NO_BACKUP_REQUEST\'\n            exit 0\n          fi\n          test -d "$HOME/.android/avd/${AVD_NAME}.avd"\n          mkdir -p /tmp/managed-avd-backup\n          rm -f /tmp/managed-avd-backup/*\n          tar --sparse -cf - -C "$HOME/.android" avd | zstd -T0 -3 -q -o /tmp/managed-avd.tar.zst\n          openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \\\n            -pass env:AVD_BACKUP_KEY \\\n            -in /tmp/managed-avd.tar.zst \\\n            -out "/tmp/managed-avd-backup/phone-${PHONE_ID}-run-${GITHUB_RUN_ID}.tar.zst.enc"\n          rm -f /tmp/managed-avd.tar.zst\n          BYTES=$(stat -c \'%s\' /tmp/managed-avd-backup/*.enc)\n          echo "ENCRYPTED_BACKUP_BYTES=$BYTES"\n          echo \'should_save=true\' >> "$GITHUB_OUTPUT"\n          echo \'FULL_AVD_ENCRYPTED_BACKUP_READY\'\n\n      - name: Save encrypted AVD backup to GitHub Actions Cache\n        if: steps.pack.outputs.should_save == \'true\'\n        uses: actions/cache/save@v5\n        with:\n          path: /tmp/managed-avd-backup\n          key: managed-avd-${{ inputs.phone_id }}-${{ github.run_id }}\n\n      - name: Close consumed backup command\n        if: steps.pack.outputs.should_save == \'true\'\n        env:\n          GH_TOKEN: ${{ github.token }}\n        shell: bash\n        run: |\n          set -euo pipefail\n          if [ -s /tmp/managed-backup-issue-number ]; then\n            ISSUE_NO=$(cat /tmp/managed-backup-issue-number)\n            gh issue close "$ISSUE_NO" --repo "$GITHUB_REPOSITORY" --comment "Backup saved successfully from run $GITHUB_RUN_ID." || true\n          fi\n          echo \'MANAGED_BACKUP_SAVED\'\n'

REAL_DEVICE_CATALOG: list[dict[str, str]] = [{'name': 'Samsung Galaxy S25 Ultra (SM-S938B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S938B',
  'resolution': '1440x3120'},
 {'name': 'Samsung Galaxy S25+ (SM-S936B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S936B',
  'resolution': '1440x3120'},
 {'name': 'Samsung Galaxy S25 (SM-S931B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S931B',
  'resolution': '1080x2340'},
 {'name': 'Samsung Galaxy S24 Ultra (SM-S928B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S928B',
  'resolution': '1440x3120'},
 {'name': 'Samsung Galaxy S24+ (SM-S926B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S926B',
  'resolution': '1440x3120'},
 {'name': 'Samsung Galaxy S24 (SM-S921B)',
  'manufacturer': 'samsung',
  'brand': 'samsung',
  'model': 'SM-S921B',
  'resolution': '1080x2340'},
 {'name': 'Google Pixel 9 Pro XL',
  'manufacturer': 'Google',
  'brand': 'google',
  'model': 'Pixel 9 Pro XL',
  'resolution': '1344x2992'},
 {'name': 'Google Pixel 9 Pro',
  'manufacturer': 'Google',
  'brand': 'google',
  'model': 'Pixel 9 Pro',
  'resolution': '1280x2856'},
 {'name': 'Google Pixel 9',
  'manufacturer': 'Google',
  'brand': 'google',
  'model': 'Pixel 9',
  'resolution': '1080x2424'},
 {'name': 'Google Pixel 8 Pro',
  'manufacturer': 'Google',
  'brand': 'google',
  'model': 'Pixel 8 Pro',
  'resolution': '1344x2992'},
 {'name': 'Google Pixel 8',
  'manufacturer': 'Google',
  'brand': 'google',
  'model': 'Pixel 8',
  'resolution': '1080x2400'},
 {'name': 'Xiaomi 14 (23127PN0CG)',
  'manufacturer': 'Xiaomi',
  'brand': 'Xiaomi',
  'model': '23127PN0CG',
  'resolution': '1200x2670'},
 {'name': 'Xiaomi 14 Ultra (24030PN60G)',
  'manufacturer': 'Xiaomi',
  'brand': 'Xiaomi',
  'model': '24030PN60G',
  'resolution': '1440x3200'},
 {'name': 'Xiaomi 13 (2211133G)',
  'manufacturer': 'Xiaomi',
  'brand': 'Xiaomi',
  'model': '2211133G',
  'resolution': '1080x2400'},
 {'name': 'OnePlus 13 (CPH2653)',
  'manufacturer': 'OnePlus',
  'brand': 'OnePlus',
  'model': 'CPH2653',
  'resolution': '1440x3168'},
 {'name': 'OnePlus 12 (CPH2581)',
  'manufacturer': 'OnePlus',
  'brand': 'OnePlus',
  'model': 'CPH2581',
  'resolution': '1440x3168'},
 {'name': 'OnePlus 11 (CPH2449)',
  'manufacturer': 'OnePlus',
  'brand': 'OnePlus',
  'model': 'CPH2449',
  'resolution': '1440x3216'}]

class GlobalSecretStore:
    """Persist global credentials with Windows DPAPI (current-user scope)."""

    def __init__(self, path: Path = GLOBAL_SECRET_PATH):
        self.path = Path(path)

    @staticmethod
    def _protect(value: str) -> str:
        if os.name != "nt":
            raise RuntimeError("全局 Secret 安全保存目前仅支持 Windows DPAPI")
        if not value:
            return ""
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        raw = value.encode("utf-8")
        buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "Cloud Android Manager",
            None,
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI 加密失败")
        try:
            encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return base64.b64encode(encrypted).decode("ascii")
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(out_blob.pbData)

    @staticmethod
    def _unprotect(value: str) -> str:
        if not value:
            return ""
        if os.name != "nt":
            return ""
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte)),
            ]

        encrypted = base64.b64decode(value.encode("ascii"), validate=True)
        buf = ctypes.create_string_buffer(encrypted, len(encrypted))
        in_blob = DATA_BLOB(len(encrypted), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        out_blob = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0x01,
            ctypes.byref(out_blob),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI 解密失败")
        try:
            plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return plain.decode("utf-8")
        finally:
            if out_blob.pbData:
                kernel32.LocalFree(out_blob.pbData)

    def load(self) -> tuple[str, str]:
        try:
            if not self.path.is_file():
                return "", ""
            data = json.loads(self.path.read_text(encoding="utf-8"))
            client_id = self._unprotect(str(data.get("ts_api_client_id") or ""))
            client_secret = self._unprotect(str(data.get("ts_api_client_secret") or ""))
            return client_id, client_secret
        except Exception:
            return "", ""

    def save(self, client_id: str, client_secret: str) -> None:
        client_id = str(client_id or "").strip()
        client_secret = str(client_secret or "").strip()
        if not client_id or not client_secret:
            raise ValueError("TS_API_CLIENT_ID 和 TS_API_CLIENT_SECRET 都不能为空")
        payload = {
            "version": 1,
            "ts_api_client_id": self._protect(client_id),
            "ts_api_client_secret": self._protect(client_secret),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except TypeError:
            if self.path.exists():
                self.path.unlink()

    def configured(self) -> bool:
        client_id, client_secret = self.load()
        return bool(client_id and client_secret)


@dataclass
class AppConfig:
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    repo: str = "usdt19908888286-bit/android-test"
    branch: str = "main"
    phone_id: str = "001"
    phone_name: str = "BICOIN-001"
    apk_url: str = DEFAULT_APK_URL
    package_name: str = DEFAULT_PACKAGE
    local_apk_path: str = ""
    manual_install_command: str = ""
    package_history: list[str] = field(default_factory=lambda: [DEFAULT_PACKAGE])
    api_level: str = "35"
    target: str = "google_apis"
    arch: str = "x86_64"
    profile: str = "pixel_6"
    cores: str = "4"
    ram_mb: str = "8192"
    device_preset: str = ""
    device_manufacturer: str = ""
    device_brand: str = ""
    device_model: str = ""
    device_native_resolution: str = ""
    last_device: str = ""
    last_run_id: int = 0
    last_run_status: str = "-"
    last_node: str = ""
    auto_refresh: bool = True
    refresh_seconds: int = 8

    # Automatic phone rotation. Multiple rules can coexist, for example:
    # ["time:04:00", "time:12:30", "hours:4"]. The earliest upcoming
    # occurrence wins. Legacy single-rule fields are retained for migration.
    auto_rotate: bool = False
    rotate_rules: list[str] = field(default_factory=list)
    rotate_mode: str = "interval"  # legacy: interval | daily
    rotate_interval_hours: int = 4  # legacy
    rotate_daily_time: str = "04:00"  # legacy
    rotate_next_ts: float = 0.0
    rotate_last_ts: float = 0.0
    rotation_phase: str = ""  # "" | waiting_backup
    rotation_run_id: int = 0
    rotation_started_ts: float = 0.0
    rotation_last_error: str = ""

    # Safe local ADB automation tasks. Each task is stored as a dict so old
    # config files stay forward-compatible when fields are added later.
    adb_tasks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        cfg = cls()
        valid = set(asdict(cfg))
        for key, value in data.items():
            if key in valid:
                setattr(cfg, key, value)
        if not cfg.profile_id:
            cfg.profile_id = uuid.uuid4().hex
        if not isinstance(cfg.package_history, list):
            cfg.package_history = [DEFAULT_PACKAGE]
        if cfg.package_name and cfg.package_name not in cfg.package_history:
            cfg.package_history.insert(0, cfg.package_name)
        if DEFAULT_PACKAGE not in cfg.package_history:
            cfg.package_history.append(DEFAULT_PACKAGE)

        if cfg.rotate_mode not in ("interval", "daily"):
            cfg.rotate_mode = "interval"
        try:
            cfg.rotate_interval_hours = max(1, min(168, int(cfg.rotate_interval_hours)))
        except Exception:
            cfg.rotate_interval_hours = 4
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(cfg.rotate_daily_time)):
            cfg.rotate_daily_time = "04:00"

        raw_rules = cfg.rotate_rules if isinstance(cfg.rotate_rules, list) else []
        clean_rules: list[str] = []
        for raw in raw_rules:
            rule = str(raw).strip().lower()
            if re.fullmatch(r"time:(?:[01]\d|2[0-3]):[0-5]\d", rule):
                if rule not in clean_rules:
                    clean_rules.append(rule)
                continue
            match = re.fullmatch(r"hours:(\d{1,3})", rule)
            if match:
                hours = int(match.group(1))
                if 1 <= hours <= 168:
                    normalized = f"hours:{hours}"
                    if normalized not in clean_rules:
                        clean_rules.append(normalized)

        # Migrate the previous single-rule scheduler automatically.
        if not clean_rules:
            if cfg.rotate_mode == "daily":
                clean_rules = [f"time:{cfg.rotate_daily_time}"]
            else:
                clean_rules = [f"hours:{cfg.rotate_interval_hours}"]
        cfg.rotate_rules = clean_rules

        raw_tasks = cfg.adb_tasks if isinstance(cfg.adb_tasks, list) else []
        clean_tasks: list[dict[str, Any]] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                continue
            command = str(raw_task.get("command") or "").strip()
            if not command:
                continue
            trigger = str(raw_task.get("trigger") or "startup").strip().lower()
            if trigger not in ("startup", "daily", "interval"):
                trigger = "startup"
            value = str(raw_task.get("value") or "").strip()
            if trigger == "daily" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                value = "04:00"
            if trigger == "interval":
                try:
                    value = str(max(1, min(168, int(value or "4"))))
                except Exception:
                    value = "4"
            clean_tasks.append(
                {
                    "id": str(raw_task.get("id") or uuid.uuid4().hex),
                    "name": str(raw_task.get("name") or "ADB 任务").strip() or "ADB 任务",
                    "command": command,
                    "trigger": trigger,
                    "value": value,
                    "enabled": bool(raw_task.get("enabled", True)),
                    "next_ts": float(raw_task.get("next_ts") or 0.0),
                    "last_ts": float(raw_task.get("last_ts") or 0.0),
                    "last_run_id": int(raw_task.get("last_run_id") or 0),
                }
            )
        cfg.adb_tasks = clean_tasks
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProfileStore:
    profiles: list[AppConfig] = field(default_factory=list)
    active_id: str = ""

    @classmethod
    def load(cls) -> "ProfileStore":
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            profiles = [AppConfig.from_dict(x) for x in data["profiles"] if isinstance(x, dict)]
            active = str(data.get("active_profile") or "")
        elif isinstance(data, dict) and data:
            # Migrate the old one-phone config into the first card.
            profiles = [AppConfig.from_dict(data)]
            active = profiles[0].profile_id
        else:
            profiles = [AppConfig()]
            active = profiles[0].profile_id

        if profiles and not any(x.profile_id == active for x in profiles):
            active = profiles[0].profile_id
        return cls(profiles=profiles, active_id=active)

    def get(self, profile_id: str) -> Optional[AppConfig]:
        return next((x for x in self.profiles if x.profile_id == profile_id), None)

    def active(self) -> AppConfig:
        if not self.profiles:
            cfg = AppConfig()
            self.profiles.append(cfg)
            self.active_id = cfg.profile_id
            return cfg
        return self.get(self.active_id) or self.profiles[0]

    def upsert(self, cfg: AppConfig) -> None:
        old = self.get(cfg.profile_id)
        if old is None:
            self.profiles.append(cfg)
        else:
            idx = self.profiles.index(old)
            self.profiles[idx] = cfg
        self.active_id = cfg.profile_id
        self.save()

    def add(self, cfg: AppConfig) -> None:
        self.upsert(cfg)

    def remove(self, profile_id: str) -> None:
        self.profiles = [x for x in self.profiles if x.profile_id != profile_id]
        if self.active_id == profile_id:
            self.active_id = self.profiles[0].profile_id if self.profiles else ""
        self.save()

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    "version": 3,
                    "active_profile": self.active_id,
                    "profiles": [x.to_dict() for x in self.profiles],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class GitHubAPI:
    def __init__(self, repo: str, token: str):
        self.repo = repo.strip().strip("/")
        self.token = token.strip()
        if "/" not in self.repo:
            raise ValueError("仓库格式应为 owner/repo")

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        raw: bool = False,
    ) -> Any:
        url = f"https://api.github.com/repos/{self.repo}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cloud-android-manager/3.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if raw:
                    return body
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(detail).get("message", detail)
            except Exception:
                msg = detail
            raise RuntimeError(f"GitHub API {exc.code}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API 网络错误: {exc.reason}") from exc

    def dispatch(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        wf = urllib.parse.quote(workflow, safe="")
        # A workflow created by the Contents API can take a few seconds to be indexed.
        for attempt in range(7):
            try:
                self._request("POST", f"/actions/workflows/{wf}/dispatches", {"ref": ref, "inputs": inputs})
                return
            except RuntimeError as exc:
                if "GitHub API 404" not in str(exc) or attempt >= 6:
                    raise
                time.sleep(2)

    def workflow_runs(self, workflow: str, branch: str, limit: int = 30) -> list[dict[str, Any]]:
        wf = urllib.parse.quote(workflow, safe="")
        qs = urllib.parse.urlencode(
            {"branch": branch, "event": "workflow_dispatch", "per_page": max(1, min(limit, 100))}
        )
        data = self._request("GET", f"/actions/workflows/{wf}/runs?{qs}") or {}
        return data.get("workflow_runs", [])

    def latest_phone_run(self, phone_name: str, phone_id: str, branch: str) -> dict[str, Any]:
        try:
            runs = self.workflow_runs(Path(BUILTIN_WORKFLOW_PATH).name, branch, 50)
        except RuntimeError as exc:
            if "GitHub API 404" in str(exc):
                return {}
            raise
        name = phone_name.strip().lower()
        pid_marker = f"· {phone_id} /"
        for run in runs:
            title = str(run.get("display_title") or run.get("name") or "")
            low = title.lower()
            if pid_marker in title and (not name or name in low):
                return run
        for run in runs:
            title = str(run.get("display_title") or run.get("name") or "")
            if pid_marker in title:
                return run
        return {}

    def run(self, run_id: int) -> dict[str, Any]:
        return self._request("GET", f"/actions/runs/{run_id}") or {}

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/actions/runs/{run_id}/jobs?per_page=100") or {}
        return data.get("jobs", [])

    def cancel(self, run_id: int) -> None:
        self._request("POST", f"/actions/runs/{run_id}/cancel")

    def run_logs_text(self, run_id: int) -> str:
        data = self._request("GET", f"/actions/runs/{run_id}/logs", raw=True)
        if not data:
            return ""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                chunks = []
                for name in zf.namelist():
                    if not name.endswith("/"):
                        chunks.append(zf.read(name).decode("utf-8", errors="replace"))
                return "\n".join(chunks)
        except zipfile.BadZipFile:
            return data.decode("utf-8", errors="replace")

    def repo_secret_names(self) -> Optional[set[str]]:
        try:
            data = self._request("GET", "/actions/secrets?per_page=100") or {}
            return {str(item.get("name")) for item in data.get("secrets", []) if item.get("name")}
        except RuntimeError:
            return None

    def repo_secret_exists(self, name: str) -> Optional[bool]:
        names = self.repo_secret_names()
        return None if names is None else name in names

    def user_repos(self) -> list[str]:
        url = (
            "https://api.github.com/user/repos?per_page=100&sort=updated&"
            "affiliation=owner,collaborator,organization_member"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cloud-android-manager/3.0",
            "Authorization": f"Bearer {self.token}",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [item.get("full_name", "") for item in data if item.get("full_name")]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc


    def create_user_repository(self, name: str, private: bool = True) -> dict[str, Any]:
        """Create a repository for the authenticated GitHub user and initialize main/default branch."""
        repo_name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo_name):
            raise ValueError("仓库名只能包含字母、数字、点、下划线和短横线")
        if not self.token:
            raise RuntimeError("请先完成 GitHub 授权登录")
        url = "https://api.github.com/user/repos"
        payload = {
            "name": repo_name,
            "private": bool(private),
            "auto_init": True,
            "description": "Cloud Android Manager repository",
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cloud-android-manager/3.0",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result if isinstance(result, dict) else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(detail).get("message", detail)
            except Exception:
                msg = detail
            raise RuntimeError(f"GitHub API {exc.code}: {msg}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API 网络错误: {exc.reason}") from exc

    def delete_repository(self) -> None:
        """Permanently delete this repository using the current GitHub authorization."""
        self._request("DELETE", "")

    def repository_file(self, path: str, ref: str = "") -> Optional[dict[str, Any]]:
        safe = urllib.parse.quote(path, safe="/")
        qs = "?" + urllib.parse.urlencode({"ref": ref}) if ref else ""
        try:
            value = self._request("GET", f"/contents/{safe}{qs}")
            return value if isinstance(value, dict) else None
        except RuntimeError as exc:
            if "GitHub API 404" in str(exc):
                return None
            raise

    def ensure_builtin_workflow(self, branch: str) -> str:
        path = BUILTIN_WORKFLOW_PATH
        desired = BUILTIN_MANAGED_WORKFLOW.replace("\r\n", "\n").rstrip() + "\n"
        current = self.repository_file(path, branch)
        sha = ""
        if current:
            sha = str(current.get("sha") or "")
            encoded = str(current.get("content") or "").replace("\n", "")
            try:
                existing = base64.b64decode(encoded).decode("utf-8").replace("\r\n", "\n").rstrip() + "\n"
            except Exception:
                existing = ""
            if existing == desired:
                return "内置工作流已是最新"
        safe = urllib.parse.quote(path, safe="/")
        payload: dict[str, Any] = {
            "message": "chore: sync cloud phone managed workflow",
            "content": base64.b64encode(desired.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        self._request("PUT", f"/contents/{safe}", payload)
        return "已更新内置工作流" if sha else "已上传内置工作流"

    def open_issues(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/issues?state=open&per_page=100") or []
        return [x for x in data if "pull_request" not in x]

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        return self._request("POST", "/issues", {"title": title, "body": body}) or {}

    def edit_issue(self, number: int, body: str, state: str = "open") -> dict[str, Any]:
        return self._request("PATCH", f"/issues/{number}", {"body": body, "state": state}) or {}

    def request_backup(self, phone_id: str, run_id: int) -> dict[str, Any]:
        title = f"Cloud Phone Command {phone_id}"
        body_obj = {
            "version": 1,
            "command": "backup",
            "phone_id": phone_id,
            "run_id": run_id,
            "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        body = "CLOUD_PHONE_COMMAND_V1\n```json\n" + json.dumps(body_obj, ensure_ascii=False) + "\n```\n"
        for issue in self.open_issues():
            if issue.get("title") == title:
                return self.edit_issue(int(issue["number"]), body)
        return self.create_issue(title, body)


class LocalTools:
    def __init__(self):
        self.adb = self._find_adb()
        self.scrcpy = self._find_scrcpy()
        self.tailscale = self._find_tailscale()
        self.gh = self._find_gh()

    @staticmethod
    def _which_or_glob(name: str, patterns: list[str]) -> str:
        found = shutil.which(name)
        if found:
            return found
        for pattern in patterns:
            matches = glob.glob(os.path.expandvars(pattern), recursive=True)
            for item in matches:
                if os.path.isfile(item):
                    return item
        return ""

    def _find_adb(self) -> str:
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        app_dir = Path(__file__).resolve().parent
        preferred = [
            app_dir / "platform-tools" / "adb.exe",
            local_appdata / "CloudAndroidManager" / "platform-tools" / "adb.exe",
        ]
        for item in preferred:
            if item.is_file():
                return str(item)
        return self._which_or_glob(
            "adb",
            [
                r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
                r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_*\scrcpy-*\adb.exe",
                r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_*\**\adb.exe",
            ],
        )

    @staticmethod
    def _managed_adb_path() -> Path:
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return local_appdata / "CloudAndroidManager" / "platform-tools" / "adb.exe"

    def ensure_managed_adb(self) -> tuple[bool, str]:
        """Prepare a program-owned Google Platform Tools copy under LocalAppData."""
        if os.name != "nt":
            self.adb = self._find_adb()
            return (bool(self.adb), self.adb or "当前系统未找到 adb")

        target = self._managed_adb_path()
        if target.is_file():
            self.adb = str(target)
            return True, str(target)

        base = target.parent.parent
        base.mkdir(parents=True, exist_ok=True)
        archive = base / "platform-tools-latest-windows.zip"
        staging = base / (".platform-tools-" + uuid.uuid4().hex)
        url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
        previous_adb = self.adb
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CloudAndroidManager/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, archive.open("wb") as out:
                shutil.copyfileobj(resp, out)
            if archive.stat().st_size < 1_000_000:
                raise RuntimeError("Google Platform Tools 下载文件异常")

            staging.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(staging)
            source = staging / "platform-tools"
            source_adb = source / "adb.exe"
            if not source_adb.is_file():
                raise RuntimeError("Platform Tools 压缩包中未找到 adb.exe")

            if target.parent.exists():
                shutil.rmtree(target.parent, ignore_errors=True)
            shutil.move(str(source), str(target.parent))
            if not target.is_file():
                raise RuntimeError("ADB 安装完成但目标文件不存在")
            self.adb = str(target)
            return True, str(target)
        except Exception as exc:
            # Existing Android Studio/scrcpy ADB remains usable as a fallback.
            if previous_adb and Path(previous_adb).is_file():
                self.adb = previous_adb
                return True, f"程序自有 ADB 准备失败，暂用现有 ADB：{previous_adb}；原因：{exc}"
            return False, f"自动准备 Google Platform Tools 失败：{exc}"
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                archive.unlink(missing_ok=True)
            except Exception:
                pass

    def _find_scrcpy(self) -> str:
        return self._which_or_glob(
            "scrcpy",
            [
                r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_*\**\scrcpy.exe",
                r"C:\Program Files\scrcpy\scrcpy.exe",
            ],
        )

    def _find_tailscale(self) -> str:
        return self._which_or_glob(
            "tailscale",
            [r"C:\Program Files\Tailscale\tailscale.exe"],
        )

    def _find_gh(self) -> str:
        local_bin = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CloudAndroidManager" / "bin"
        return self._which_or_glob(
            "gh",
            [
                r"C:\Program Files\GitHub CLI\gh.exe",
                r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe",
                str(local_bin / "**" / "gh.exe"),
            ],
        )

    @staticmethod
    def run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        try:
            cp = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            return cp.returncode, cp.stdout.strip(), cp.stderr.strip()
        except subprocess.TimeoutExpired:
            return 124, "", "命令超时"
        except Exception as exc:
            return 125, "", str(exc)

    @staticmethod
    def port_open(host: str, port: int = 5555, timeout: float = 1.5) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def tailscale_nodes(self) -> list[dict[str, Any]]:
        if not self.tailscale:
            return []
        code, out, _ = self.run([self.tailscale, "status", "--json"], timeout=10)
        if code != 0 or not out:
            return []
        try:
            obj = json.loads(out)
        except json.JSONDecodeError:
            return []
        nodes: list[dict[str, Any]] = []
        if isinstance(obj.get("Self"), dict):
            nodes.append(obj["Self"])
        peer = obj.get("Peer", {})
        if isinstance(peer, dict):
            nodes.extend(v for v in peer.values() if isinstance(v, dict))
        return nodes

    def discover_phone(
        self, phone_id: str, run_id: Optional[int] = None, phone_name: str = ""
    ) -> tuple[str, str]:
        candidates: list[tuple[int, str, str]] = []
        slug = re.sub(r"[^a-z0-9-]+", "-", phone_name.lower()).strip("-")
        for node in self.tailscale_nodes():
            host = str(node.get("HostName") or node.get("DNSName") or "").rstrip(".")
            ips = node.get("TailscaleIPs") or []
            ip = next((x for x in ips if isinstance(x, str) and ":" not in x), "")
            if not host or not ip:
                continue
            if run_id is not None:
                if str(run_id) not in host:
                    continue
            else:
                id_match = f"phone{phone_id}-" in host
                name_match = bool(slug and slug in host.lower())
                if not (id_match or name_match):
                    continue
            match = RUN_ID_RE.search(host)
            score = int(match.group(1)) if match else 0
            candidates.append((score, ip, host))
        if not candidates:
            return "", ""
        candidates.sort(reverse=True)
        _, ip, host = candidates[0]
        return ip, host

    def adb_cmd(self, device: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
        if not self.adb:
            return 127, "", "未找到 adb.exe"
        cmd = [self.adb]
        if device:
            cmd += ["-s", device]
        cmd += args
        return self.run(cmd, timeout)

    def adb_connect(self, address: str) -> tuple[bool, str]:
        if not self.adb:
            return False, "未找到 adb.exe"
        code, out, err = self.run([self.adb, "connect", address], timeout=15)
        text = (out + " " + err).strip()
        ok = code == 0 and ("connected" in text.lower() or "already connected" in text.lower())
        return ok, text

    def device_health(self, address: str, package: str) -> dict[str, str]:
        result: dict[str, str] = {
            "adb": "未连接",
            "boot": "?",
            "model": "?",
            "android": "?",
            "package": package,
            "app": "未知",
            "app_pid": "-",
            "app_cpu": "-",
            "app_mem": "-",
            "device_cpu": "-",
            "device_mem": "-",
        }
        host = address.split(":", 1)[0].strip()
        if not host or not self.port_open(host, 5555):
            return result
        ok, msg = self.adb_connect(address)
        if not ok:
            result["adb"] = msg or "ADB 握手失败"
            return result
        result["adb"] = "已连接"

        def shell(*parts: str, timeout: int = 20) -> str:
            code, out, _ = self.adb_cmd(address, ["shell", *parts], timeout=timeout)
            return out.strip() if code == 0 else ""

        result["boot"] = shell("getprop", "sys.boot_completed") or "?"
        result["model"] = shell("getprop", "ro.product.model") or "?"
        result["android"] = shell("getprop", "ro.build.version.release") or "?"
        pid = shell("pidof", package) if PKG_RE.match(package) else ""
        if pid:
            result["app"] = "运行中"
            result["app_pid"] = pid.split()[0]
        else:
            installed = shell("pm", "path", package) if PKG_RE.match(package) else ""
            result["app"] = "已安装/未运行" if installed else "未安装"

        cpuinfo = shell("dumpsys", "cpuinfo", timeout=20)
        if cpuinfo:
            lines = [ln.strip() for ln in cpuinfo.splitlines() if package in ln]
            if lines:
                m = re.search(r"([0-9.]+)%", lines[0])
                result["app_cpu"] = (m.group(1) + "%") if m else lines[0][:80]
            total = next((ln.strip() for ln in cpuinfo.splitlines() if "TOTAL" in ln), "")
            if total:
                result["device_cpu"] = total[:100]

        if PKG_RE.match(package):
            meminfo = shell("dumpsys", "meminfo", package, timeout=25)
            m = re.search(r"TOTAL PSS:\s*([0-9,]+)", meminfo)
            if not m:
                m = re.search(r"TOTAL\s+([0-9,]+)", meminfo)
            if m:
                try:
                    kib = int(m.group(1).replace(",", ""))
                    result["app_mem"] = f"{kib / 1024:.1f} MiB"
                except ValueError:
                    pass

        top = shell("top", "-b", "-n", "1", "-m", "5", timeout=20)
        if top:
            cpu_line = next((ln.strip() for ln in top.splitlines() if "%cpu" in ln.lower()), "")
            mem_line = next((ln.strip() for ln in top.splitlines() if ln.strip().startswith("Mem:")), "")
            if cpu_line:
                result["device_cpu"] = cpu_line
            if mem_line:
                result["device_mem"] = mem_line
        return result

    def list_third_party_packages(self, address: str) -> list[str]:
        ok, _ = self.adb_connect(address)
        if not ok:
            return []
        code, out, _ = self.adb_cmd(address, ["shell", "pm", "list", "packages", "-3"])
        if code != 0:
            return []
        return sorted(x.removeprefix("package:").strip() for x in out.splitlines() if x.strip())

    def start_package(self, address: str, package: str) -> tuple[bool, str]:
        if not PKG_RE.match(package):
            return False, "包名格式无效"
        ok, msg = self.adb_connect(address)
        if not ok:
            return False, msg
        code, out, err = self.adb_cmd(
            address,
            ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=20,
        )
        return code == 0, (out or err)

    def stop_package(self, address: str, package: str) -> tuple[bool, str]:
        if not PKG_RE.match(package):
            return False, "包名格式无效"
        ok, msg = self.adb_connect(address)
        if not ok:
            return False, msg
        code, out, err = self.adb_cmd(address, ["shell", "am", "force-stop", package])
        return code == 0, (out or err or "已强制停止")

    @staticmethod
    def _safe_adb_args(command: str, address: str, package: str = "") -> tuple[list[str], str]:
        rendered = str(command or "").strip()
        rendered = rendered.replace("{device}", address).replace("{package}", package)
        if not rendered:
            raise ValueError("ADB 命令不能为空")
        try:
            tokens = shlex.split(rendered, posix=False)
        except ValueError as exc:
            raise ValueError(f"ADB 命令格式错误: {exc}") from exc
        tokens = [x[1:-1] if len(x) >= 2 and x[0] == x[-1] == '"' else x for x in tokens]
        if not tokens:
            raise ValueError("ADB 命令不能为空")

        first = tokens[0].replace("\\", "/").split("/")[-1].lower()
        if first in ("adb", "adb.exe"):
            tokens = tokens[1:]
        if len(tokens) >= 2 and tokens[0] == "-s":
            # Tasks belong to one phone profile, so always use that profile's
            # current ADB address even if the saved text contains an old IP.
            tokens = tokens[2:]
        if not tokens:
            raise ValueError("ADB 命令缺少参数")

        top = tokens[0].lower()
        if top in {
            "install", "install-multiple", "install-multi-package", "push", "pull",
            "root", "unroot", "remount", "sync", "reboot", "backup", "restore",
            "forward", "reverse", "tcpip",
        }:
            raise ValueError(f"该 ADB 操作不允许自动执行: {top}")
        if top in ("get-state", "wait-for-device"):
            return tokens, rendered
        if top != "shell":
            raise ValueError("自动任务目前只允许安全的 adb shell / get-state / wait-for-device 命令")
        if len(tokens) < 2:
            raise ValueError("adb shell 后缺少命令")

        program = tokens[1].lower()
        allowed_shell = {"dumpsys", "getprop", "settings", "input", "am", "monkey", "wm", "svc", "pm", "cmd"}
        if program not in allowed_shell:
            raise ValueError(f"该 shell 命令不在自动执行白名单中: {program}")

        if program == "pm":
            if len(tokens) < 3 or tokens[2].lower() not in {
                "list", "path", "dump", "resolve-activity", "query-activities", "query-services", "query-receivers"
            }:
                raise ValueError("自动任务中的 pm 只允许查询类命令")
        if program == "cmd":
            if len(tokens) < 3:
                raise ValueError("cmd 后缺少服务名")
            service = tokens[2].lower()
            if service == "deviceidle":
                if len(tokens) < 4 or tokens[3].lower() not in {"disable", "enable", "whitelist"}:
                    raise ValueError("cmd deviceidle 只允许 disable / enable / whitelist")
            elif service == "notification":
                if len(tokens) < 4 or tokens[3].lower() not in {"allow_listener", "disallow_listener"}:
                    raise ValueError("cmd notification 只允许 allow_listener / disallow_listener")
            else:
                raise ValueError("自动任务中的 cmd 目前只允许 deviceidle / notification")
        if program == "settings":
            if len(tokens) < 3 or tokens[2].lower() not in {"get", "put", "delete", "list"}:
                raise ValueError("settings 只允许 get / put / delete / list")
        if program == "input":
            if len(tokens) < 3 or tokens[2].lower() not in {"keyevent", "tap", "swipe", "text", "motionevent", "keycombination"}:
                raise ValueError("input 子命令不受支持")
        if program == "am":
            if len(tokens) < 3 or tokens[2].lower() not in {"start", "startservice", "start-foreground-service", "force-stop", "broadcast"}:
                raise ValueError("am 只允许 start / service / force-stop / broadcast")
        return tokens, rendered

    def run_safe_adb_command(self, address: str, command: str, package: str = "") -> tuple[bool, str]:
        if not self.adb:
            return False, "未找到 adb.exe"
        if not address:
            return False, "这台手机还没有可用的 ADB 地址"
        try:
            args, rendered = self._safe_adb_args(command, address, package)
        except ValueError as exc:
            return False, str(exc)
        ok, detail = self.adb_connect(address)
        if not ok:
            return False, detail
        code, out, err = self.adb_cmd(address, args, timeout=60)
        text = (out or err or "命令执行完成").strip()
        return code == 0, f"{rendered}\n{text}".strip()

    def github_auth_token(self) -> str:
        """Read the already-authorized gh token into process memory only."""
        if not self.gh:
            return ""
        try:
            cp = subprocess.run(
                [self.gh, "auth", "token"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if cp.returncode == 0:
                return cp.stdout.strip()
        except Exception:
            pass
        return ""

    def ensure_github_cli(self) -> tuple[bool, str]:
        """Ensure GitHub CLI exists. Prefer winget; fall back to official portable ZIP."""
        self.gh = self._find_gh()
        if self.gh:
            return True, self.gh
        if os.name != "nt":
            return False, "未找到 GitHub CLI；当前自动安装仅支持 Windows"

        winget = shutil.which("winget")
        if winget:
            try:
                cp = subprocess.run(
                    [
                        winget,
                        "install",
                        "--id",
                        "GitHub.cli",
                        "-e",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--silent",
                        "--disable-interactivity",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                    creationflags=CREATE_NO_WINDOW,
                )
                self.gh = self._find_gh()
                if self.gh:
                    return True, "GitHub CLI 已通过 winget 自动安装"
                winget_detail = (cp.stdout + "\n" + cp.stderr).strip()
            except Exception as exc:
                winget_detail = str(exc)
        else:
            winget_detail = "系统未找到 winget"

        # Portable fallback: download the official GitHub CLI ZIP into the user's LocalAppData.
        try:
            api_req = urllib.request.Request(
                "https://api.github.com/repos/cli/cli/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "cloud-android-manager/2.0",
                },
            )
            with urllib.request.urlopen(api_req, timeout=30) as resp:
                release = json.loads(resp.read().decode("utf-8"))

            machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or "AMD64").upper()
            arch = "arm64" if "ARM64" in machine else "amd64"
            suffix = f"windows_{arch}.zip"
            asset_url = ""
            asset_name = ""
            for asset in release.get("assets", []):
                name = str(asset.get("name") or "")
                if name.lower().endswith(suffix):
                    asset_url = str(asset.get("browser_download_url") or "")
                    asset_name = name
                    break
            if not asset_url:
                return False, f"winget 安装失败，且未找到官方 {suffix} 便携包：{winget_detail}"

            base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CloudAndroidManager" / "bin"
            base.mkdir(parents=True, exist_ok=True)
            zip_path = base / asset_name
            dl_req = urllib.request.Request(
                asset_url,
                headers={"User-Agent": "cloud-android-manager/2.0"},
            )
            with urllib.request.urlopen(dl_req, timeout=120) as resp, zip_path.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(base)
            try:
                zip_path.unlink()
            except OSError:
                pass

            self.gh = self._find_gh()
            if self.gh:
                return True, "GitHub CLI 已从 GitHub 官方 Release 自动安装为便携版"
            return False, f"GitHub CLI 下载完成但未找到 gh.exe；winget 信息：{winget_detail}"
        except Exception as exc:
            return False, f"GitHub CLI 自动安装失败：{exc}；winget 信息：{winget_detail}"

    def github_account(self) -> str:
        if not self.gh:
            return ""
        code, out, _ = self.run([self.gh, "api", "user", "--jq", ".login"], timeout=15)
        return out.strip() if code == 0 else ""

    def github_login(self) -> tuple[bool, str]:
        """Run GitHub device authorization without opening a browser automatically.

        GitHub CLI generates the one-time code and copies it to the clipboard. The
        GUI shows https://github.com/login/device and lets the user decide whether
        to open/copy it.
        """
        installed_now = False
        if not self.gh:
            ok, detail = self.ensure_github_cli()
            if not ok:
                return False, detail
            installed_now = True
        try:
            # gh normally opens the browser during --web login. Route that request
            # into a no-op batch file instead; the GUI exposes the login URL.
            helper_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CloudAndroidManager"
            helper_dir.mkdir(parents=True, exist_ok=True)
            helper = helper_dir / "no_browser.cmd"
            helper.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

            env = os.environ.copy()
            env["GH_BROWSER"] = f'cmd.exe /d /c "{helper}"'
            cp = subprocess.run(
                [
                    self.gh,
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--git-protocol",
                    "https",
                    "--web",
                    "--clipboard",
                ],
                input="\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            account = self.github_account()
            if cp.returncode == 0 and account:
                prefix = "GitHub CLI 已自动安装；" if installed_now else ""
                return True, prefix + f"已登录 @{account}"
            return False, "GitHub 授权未完成或已取消"
        except subprocess.TimeoutExpired:
            return False, "GitHub 授权超时"
        except Exception as exc:
            return False, str(exc)

    def github_logout(self) -> tuple[bool, str]:
        if not self.gh:
            return False, "未找到 GitHub CLI (gh.exe)"
        code, out, err = self.run(
            [self.gh, "auth", "logout", "--hostname", "github.com"], timeout=30
        )
        return code == 0, out or err or "已退出 GitHub"

    def set_github_secret(
        self,
        repo: str,
        token: str,
        name: str = "AVD_BACKUP_KEY",
        value: Optional[str] = None,
    ) -> tuple[bool, str]:
        if not self.gh:
            ok, detail = self.ensure_github_cli()
            if not ok:
                return False, detail
        secret_value = value if value is not None else secrets.token_urlsafe(64)
        if not secret_value:
            return False, f"{name} 的值为空"
        env = os.environ.copy()
        env["GH_TOKEN"] = token
        try:
            cp = subprocess.run(
                [self.gh, "secret", "set", name, "-R", repo],
                input=secret_value,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            text = (cp.stdout + " " + cp.stderr).strip()
            return cp.returncode == 0, text or (f"{name} 已写入 GitHub Actions Secret" if cp.returncode == 0 else "设置失败")
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def runner_status(ip: str) -> dict[str, str]:
        out = {"host_cpu": "-", "host_mem": "-", "qemu": "-"}
        if not ip:
            return out
        try:
            req = urllib.request.Request(f"http://{ip}:8787/status", headers={"User-Agent": "cloud-android-manager/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            out["host_cpu"] = f"{obj.get('cpu_percent', '-')}%  load {obj.get('load1', '-')}/{obj.get('load5', '-')}"
            used = obj.get("mem_used_kib", 0) or 0
            total = obj.get("mem_total_kib", 0) or 0
            out["host_mem"] = f"{obj.get('mem_percent', '-')}%  {used/1048576:.2f}/{total/1048576:.2f} GiB" if total else "-"
            q = obj.get("qemu") or {}
            if isinstance(q, dict):
                out["qemu"] = f"CPU {q.get('cpu_percent', '-')}% / MEM {q.get('mem_percent', '-')}% / RSS {(q.get('rss_kib', 0) or 0)/1024:.0f} MiB"
        except Exception:
            pass
        return out

    def launch_scrcpy(self, address: str) -> tuple[bool, str]:
        if not self.scrcpy:
            return False, "未找到 scrcpy.exe"
        ok, msg = self.adb_connect(address)
        if not ok:
            return False, msg
        try:
            subprocess.Popen(
                [self.scrcpy, "-s", address],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, "scrcpy 已启动"
        except Exception as exc:
            return False, str(exc)


class CloudPhoneGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1260x860")
        self.root.minsize(1040, 700)

        self.store = ProfileStore.load()
        self.tools = LocalTools()
        self.global_secrets = GlobalSecretStore()
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._closing = False
        self._refreshing: set[str] = set()
        self._last_auto: dict[str, float] = {}
        self._last_rotation_poll: dict[str, float] = {}
        self._rotating: set[str] = set()
        self._adb_task_running: set[str] = set()
        self.card_vars: dict[str, dict[str, tk.StringVar]] = {}
        self.repo_cache: list[str] = []

        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        self.var_token = tk.StringVar(value=env_token or self.tools.github_auth_token())
        self.var_github_status = tk.StringVar(value="GitHub: 正在检查登录状态…")
        self.var_tailscale_status = tk.StringVar(
            value="Tailscale: 全局已配置" if self.global_secrets.configured() else "Tailscale: 全局未配置"
        )

        self._build_ui()
        self._render_cards()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._drain_queue)
        self.root.after(350, self.refresh_github_auth)
        self.root.after(1500, self._auto_tick)
        self.log(f"已加载 {len(self.store.profiles)} 台手机配置")

    # ------------------------- base UI -------------------------
    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Cloud Android Manager", font=("Segoe UI", 17, "bold")).pack(side="left")
        ttk.Label(top, text="多仓库 · 多手机", font=("Segoe UI", 10)).pack(side="left", padx=(10, 20))
        ttk.Label(top, textvariable=self.var_github_status).pack(side="left", padx=(0, 8))
        ttk.Button(top, text="GitHub 授权登录", command=self.github_login).pack(side="left", padx=3)
        ttk.Button(top, text="刷新登录", command=self.refresh_github_auth).pack(side="left", padx=3)
        ttk.Label(top, textvariable=self.var_tailscale_status).pack(side="left", padx=(12, 4))
        ttk.Button(top, text="⚙ 全局设置", command=self.open_global_settings).pack(side="left", padx=3)
        ttk.Button(top, text="刷新全部", command=self.refresh_all).pack(side="right", padx=3)
        ttk.Button(top, text="＋ 添加手机", command=lambda: self.open_settings(None)).pack(side="right", padx=3)

        cards_shell = ttk.Frame(outer)
        cards_shell.pack(fill="both", expand=True)
        self.cards_canvas = tk.Canvas(cards_shell, highlightthickness=0)
        self.cards_scroll = ttk.Scrollbar(cards_shell, orient="vertical", command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=self.cards_scroll.set)
        self.cards_scroll.pack(side="right", fill="y")
        self.cards_canvas.pack(side="left", fill="both", expand=True)
        self.cards_inner = ttk.Frame(self.cards_canvas)
        self._cards_window = self.cards_canvas.create_window((0, 0), window=self.cards_inner, anchor="nw")
        self.cards_inner.bind(
            "<Configure>",
            lambda _e: self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox("all")),
        )
        self.cards_canvas.bind(
            "<Configure>",
            lambda e: self.cards_canvas.itemconfigure(self._cards_window, width=e.width),
        )

        logs = ttk.LabelFrame(outer, text="全局日志", padding=5)
        logs.pack(fill="x", pady=(10, 0))
        self.log_text = tk.Text(logs, wrap="word", height=7)
        log_scroll = ttk.Scrollbar(logs, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def _render_cards(self) -> None:
        for child in self.cards_inner.winfo_children():
            child.destroy()
        self.card_vars.clear()

        if not self.store.profiles:
            empty = ttk.Frame(self.cards_inner, padding=40)
            empty.pack(fill="both", expand=True)
            ttk.Label(empty, text="还没有手机", font=("Segoe UI", 16, "bold")).pack(pady=(40, 8))
            ttk.Label(empty, text="点击右上角“＋ 添加手机”，绑定一个 GitHub 仓库并设置手机参数。 ").pack()
            ttk.Button(empty, text="＋ 添加手机", command=lambda: self.open_settings(None)).pack(pady=16)
            return

        for cfg in self.store.profiles:
            self._build_phone_card(cfg)

    def _build_phone_card(self, cfg: AppConfig) -> None:
        vars_ = {
            "run": tk.StringVar(value=self._initial_run_text(cfg)),
            "rotation": tk.StringVar(value=self._rotation_text(cfg)),
            "adb": tk.StringVar(value=cfg.last_device or "未发现 ADB 地址"),
            "server": tk.StringVar(value="CPU -\n内存 -"),
            "android": tk.StringVar(value=f"目标 {cfg.device_preset or cfg.device_model or '默认 AVD'}\n等待检测"),
            "app": tk.StringVar(value=f"{cfg.package_name}\n等待检测"),
            "repo": tk.StringVar(value=f"{cfg.repo} · {cfg.branch}"),
        }
        self.card_vars[cfg.profile_id] = vars_

        card = ttk.LabelFrame(
            self.cards_inner,
            text=f"{cfg.phone_name}   ·   ID {cfg.phone_id}",
            padding=(12, 9),
        )
        card.pack(fill="x", padx=3, pady=5)

        # Header: only the state and the two controls people need constantly.
        head = ttk.Frame(card)
        head.pack(fill="x")
        ttk.Label(head, textvariable=vars_["run"], font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(head, text="⚙", width=3, command=lambda pid=cfg.profile_id: self.open_settings(pid)).pack(side="right")
        ttk.Button(head, text="↻", width=3, command=lambda pid=cfg.profile_id: self.refresh_profile(pid)).pack(side="right", padx=(0, 4))

        # Compact status strip. Avoid nested LabelFrames: borders around every metric
        # made the dashboard look like a monitoring console instead of a phone manager.
        status = ttk.Frame(card)
        status.pack(fill="x", pady=(9, 7))
        for i in range(3):
            status.columnconfigure(i, weight=1, uniform="phone_status")

        def status_item(col: int, title: str, variable: tk.StringVar) -> None:
            box = ttk.Frame(status, padding=(7, 3))
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 6, 0))
            ttk.Label(box, text=title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(box, textvariable=variable, justify="left", wraplength=330).pack(anchor="w", pady=(2, 0))

        status_item(0, "连接", vars_["adb"])
        status_item(1, "Android", vars_["android"])
        status_item(2, "当前 App", vars_["app"])

        # Primary actions: six only. Everything else is deliberately hidden by default.
        primary = ttk.Frame(card)
        primary.pack(fill="x", pady=(3, 0))
        ttk.Button(primary, text="▶ 启动", command=lambda pid=cfg.profile_id: self.start_profile(pid)).pack(side="left", padx=(0, 5))
        ttk.Button(primary, text="打开 scrcpy", command=lambda pid=cfg.profile_id: self.open_scrcpy_profile(pid)).pack(side="left", padx=5)
        ttk.Button(primary, text="备份", command=lambda pid=cfg.profile_id: self.backup_profile(pid)).pack(side="left", padx=5)
        ttk.Button(primary, text="启动 App", command=lambda pid=cfg.profile_id: self.start_app_profile(pid)).pack(side="left", padx=5)
        ttk.Button(primary, text="关闭 App", command=lambda pid=cfg.profile_id: self.stop_app_profile(pid)).pack(side="left", padx=5)
        ttk.Button(primary, text="停止", command=lambda pid=cfg.profile_id: self.cancel_profile(pid)).pack(side="left", padx=5)

        details = ttk.Frame(card)
        details_visible = tk.BooleanVar(value=False)
        more_text = tk.StringVar(value="更多 ▾")

        def toggle_details() -> None:
            if details_visible.get():
                details.pack_forget()
                details_visible.set(False)
                more_text.set("更多 ▾")
            else:
                details.pack(fill="x", pady=(9, 0))
                details_visible.set(True)
                more_text.set("收起 ▴")

        ttk.Button(primary, textvariable=more_text, command=toggle_details).pack(side="right")

        ttk.Separator(details, orient="horizontal").pack(fill="x", pady=(0, 8))
        meta = ttk.Frame(details)
        meta.pack(fill="x")
        meta.columnconfigure(0, weight=2)
        meta.columnconfigure(1, weight=1)
        meta.columnconfigure(2, weight=2)

        def detail_item(col: int, title: str, variable: tk.StringVar) -> None:
            box = ttk.Frame(meta, padding=(5, 2))
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            ttk.Label(box, text=title, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(box, textvariable=variable, justify="left", wraplength=360).pack(anchor="w", pady=(2, 0))

        detail_item(0, "仓库", vars_["repo"])
        detail_item(1, "云服务器", vars_["server"])
        detail_item(2, "自动换机", vars_["rotation"])

        secondary = ttk.Frame(details)
        secondary.pack(fill="x", pady=(8, 0))
        ttk.Button(secondary, text="恢复备份", command=lambda pid=cfg.profile_id: self.restore_profile(pid)).pack(side="left", padx=(0, 5))
        ttk.Button(secondary, text="立即换机", command=lambda pid=cfg.profile_id: self.rotate_profile(pid)).pack(side="left", padx=5)
        ttk.Button(secondary, text="APK 安装命令", command=lambda pid=cfg.profile_id: self.apk_command_profile(pid)).pack(side="left", padx=5)
        ttk.Button(secondary, text="ADB 任务", command=lambda pid=cfg.profile_id: self.open_adb_tasks(pid)).pack(side="left", padx=5)
        ttk.Button(secondary, text="刷新状态", command=lambda pid=cfg.profile_id: self.refresh_profile(pid)).pack(side="right", padx=(5, 0))

    def _initial_run_text(self, cfg: AppConfig) -> str:
        if cfg.last_run_id:
            return f"Run {cfg.last_run_id} · {cfg.last_run_status}"
        return "未启动"

    @staticmethod
    def _format_local_ts(value: float) -> str:
        if not value:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))

    def _next_rotation_ts(self, cfg: AppConfig, now_ts: Optional[float] = None) -> float:
        now_ts = float(now_ts or time.time())
        candidates: list[float] = []
        now_local = dt.datetime.fromtimestamp(now_ts)

        rules = list(cfg.rotate_rules or [])
        if not rules:
            rules = [f"time:{cfg.rotate_daily_time}"] if cfg.rotate_mode == "daily" else [f"hours:{cfg.rotate_interval_hours}"]

        for rule in rules:
            if rule.startswith("time:"):
                value = rule.split(":", 1)[1]
                match = re.fullmatch(r"(\d{2}):(\d{2})", value)
                if not match:
                    continue
                hour, minute = int(match.group(1)), int(match.group(2))
                target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target.timestamp() <= now_ts + 1:
                    target += dt.timedelta(days=1)
                candidates.append(target.timestamp())
                continue

            match = re.fullmatch(r"hours:(\d{1,3})", rule)
            if match:
                hours = int(match.group(1))
                if 1 <= hours <= 168:
                    candidates.append(now_ts + hours * 3600)

        return min(candidates) if candidates else now_ts + 4 * 3600

    def _rotation_text(self, cfg: AppConfig) -> str:
        if cfg.rotation_phase == "waiting_backup" and cfg.rotation_run_id:
            return f"换机中 · 等待旧 Run {cfg.rotation_run_id} 备份完成"
        if cfg.auto_rotate:
            labels: list[str] = []
            times: list[str] = []
            intervals: list[int] = []
            for rule in cfg.rotate_rules or []:
                if rule.startswith("time:"):
                    times.append(rule.split(":", 1)[1])
                else:
                    match = re.fullmatch(r"hours:(\d{1,3})", rule)
                    if match:
                        intervals.append(int(match.group(1)))
            if times:
                labels.append("每天 " + "/".join(times))
            if intervals:
                labels.extend(f"每 {hours} 小时" for hours in intervals)
            rule_text = " + ".join(labels) if labels else "未配置规则"
            if not cfg.rotate_next_ts:
                return f"自动换机 · {rule_text} · 等待计算时间"
            return f"自动换机 · {rule_text} · 下次 {self._format_local_ts(cfg.rotate_next_ts)}"
        if cfg.rotation_last_error:
            return f"自动换机关闭 · 上次错误: {cfg.rotation_last_error[:70]}"
        return "自动换机关闭"

    def _reschedule_rotation(self, cfg: AppConfig, now_ts: Optional[float] = None) -> None:
        cfg.rotate_next_ts = self._next_rotation_ts(cfg, now_ts) if cfg.auto_rotate else 0.0
        self._set_card(cfg.profile_id, "rotation", self._rotation_text(cfg))

    def _set_card(self, profile_id: str, key: str, value: str) -> None:
        vars_ = self.card_vars.get(profile_id)
        if vars_ and key in vars_:
            vars_[key].set(value)

    # ------------------------- background / logging -------------------------
    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")

    def bg(
        self,
        name: str,
        func: Callable[[], Any],
        callback: Optional[Callable[[Any], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        def runner() -> None:
            try:
                value = func()
                self.q.put(("ok", (name, value, callback)))
            except Exception as exc:
                self.q.put(("err", (name, str(exc), error_callback)))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    name, value, callback = payload
                    if callback:
                        callback(value)
                    else:
                        self.log(f"{name}: 完成")
                else:
                    name, err, error_callback = payload
                    self.log(f"{name}: 失败 - {err}")
                    if error_callback:
                        error_callback(err)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_queue)

    # ------------------------- GitHub auth -------------------------
    def _token(self) -> str:
        token = (
            os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or self.var_token.get().strip()
            or self.tools.github_auth_token()
        )
        if not token:
            raise RuntimeError("请先完成 GitHub 授权登录")
        self.var_token.set(token)
        return token

    def api_for(self, cfg: AppConfig) -> GitHubAPI:
        return GitHubAPI(cfg.repo, self._token())

    def refresh_github_auth(self) -> None:
        def work() -> tuple[str, str, bool]:
            found = self.tools._find_gh()
            if found:
                self.tools.gh = found
            account = self.tools.github_account() if found else ""
            token = self.tools.github_auth_token() if account else ""
            return account, token, bool(found)

        def done(result: tuple[str, str, bool]) -> None:
            account, token, has_cli = result
            if account and token:
                self.var_token.set(token)
                self.var_github_status.set(f"GitHub: 已登录 @{account}")
            elif has_cli:
                self.var_token.set("")
                self.var_github_status.set("GitHub: 未登录")
            else:
                self.var_token.set("")
                self.var_github_status.set("GitHub: CLI 未安装（登录时自动安装）")

        self.bg("检查 GitHub 登录", work, done)

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as exc:
            self.log(f"复制失败: {exc}")

    def _open_github_login_page(self) -> None:
        url = "https://github.com/login/device"
        try:
            if os.name == "nt":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", url])
        except Exception as exc:
            self.log(f"打开登录页面失败: {exc}")

    def _poll_github_login_code(self) -> None:
        win = getattr(self, "_github_login_dialog", None)
        code_var = getattr(self, "_github_login_code_var", None)
        if not win or not code_var:
            return
        try:
            if not win.winfo_exists():
                return
            value = self.root.clipboard_get().strip()
            if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", value):
                code_var.set(value)
        except Exception:
            pass
        self.root.after(350, self._poll_github_login_code)

    def _show_github_login_dialog(self) -> None:
        old = getattr(self, "_github_login_dialog", None)
        try:
            if old and old.winfo_exists():
                old.lift()
                return
        except Exception:
            pass

        win = tk.Toplevel(self.root)
        win.title("GitHub 授权登录")
        win.geometry("620x300")
        win.resizable(False, False)
        win.transient(self.root)
        self._github_login_dialog = win
        self._github_login_code_var = tk.StringVar(value="正在生成验证码…")
        self._github_login_status_var = tk.StringVar(value="不会自动打开浏览器")
        url = "https://github.com/login/device"

        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="GitHub 设备授权", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(body, text="浏览器不会自动弹出。复制链接或主动点击“手动打开”，然后输入下面的验证码。", wraplength=570).pack(anchor="w", pady=(6, 14))
        ttk.Label(body, text="登录地址").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(4, 10))
        url_entry = ttk.Entry(row)
        url_entry.insert(0, url)
        url_entry.configure(state="readonly")
        url_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="复制链接", command=lambda: self._copy_to_clipboard(url)).pack(side="left", padx=5)
        ttk.Button(row, text="手动打开", command=self._open_github_login_page).pack(side="left")
        ttk.Label(body, text="一次性验证码").pack(anchor="w")
        code_row = ttk.Frame(body)
        code_row.pack(fill="x", pady=(4, 10))
        ttk.Label(code_row, textvariable=self._github_login_code_var, font=("Consolas", 16, "bold")).pack(side="left")
        ttk.Button(code_row, text="复制验证码", command=lambda: self._copy_to_clipboard(self._github_login_code_var.get())).pack(side="left", padx=12)
        ttk.Label(body, textvariable=self._github_login_status_var).pack(anchor="w")
        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="检查登录状态", command=self.refresh_github_auth).pack(side="left")
        ttk.Button(footer, text="关闭", command=win.destroy).pack(side="right")
        self.root.after(350, self._poll_github_login_code)

    def github_login(self) -> None:
        self._show_github_login_dialog()
        status = getattr(self, "_github_login_status_var", None)
        if status:
            status.set("正在准备 GitHub CLI 和验证码；不会自动打开浏览器")

        def done(result: tuple[bool, str]) -> None:
            ok, detail = result
            self.log(f"GitHub 登录: {detail}")
            status_var = getattr(self, "_github_login_status_var", None)
            if status_var:
                status_var.set("授权完成" if ok else detail)
            self.refresh_github_auth()

        self.bg("GitHub 授权登录", self.tools.github_login, done)

    def github_logout(self) -> None:
        if not messagebox.askyesno(APP_NAME, "确定退出 GitHub 登录吗？"):
            return
        self.bg("退出 GitHub", self.tools.github_logout, lambda r: (self.log(r[1]), self.refresh_github_auth()))

    # ------------------------- settings / profiles -------------------------
    def _fetch_repositories(self) -> list[str]:
        return GitHubAPI("placeholder/repository", self._token()).user_repos()

    @staticmethod
    def _quote_cmd_arg(value: str) -> str:
        value = str(value or "")
        return '"' + value.replace('"', '') + '"'

    def _build_apk_install_command(self, cfg: AppConfig) -> str:
        adb = self.tools.adb or str(self.tools._managed_adb_path())
        device = (cfg.last_device or "").strip() or "<ADB地址>"
        apk = (cfg.local_apk_path or "").strip() or "<APK路径>"
        template = (cfg.manual_install_command or "").strip()
        if template:
            try:
                return template.format(adb=adb, device=device, apk=apk)
            except Exception:
                return template
        q_adb = self._quote_cmd_arg(adb)
        q_device = self._quote_cmd_arg(device)
        q_apk = self._quote_cmd_arg(apk)
        if os.name == "nt":
            # Windows PowerShell 5.1 treats a quoted executable path as a string.
            # The call operator (&) is required; && is not available in PS 5.1.
            return (
                f"& {q_adb} connect {q_device}; "
                f"if ($LASTEXITCODE -eq 0) {{ & {q_adb} -s {q_device} install -r -g {q_apk} }}"
            )
        return f"{q_adb} connect {q_device} && {q_adb} -s {q_device} install -r -g {q_apk}"

    def _refresh_tailscale_global_status(self) -> None:
        self.var_tailscale_status.set(
            "Tailscale: 全局已配置" if self.global_secrets.configured() else "Tailscale: 全局未配置"
        )

    def open_global_settings(self) -> None:
        client_id, client_secret = self.global_secrets.load()
        win = tk.Toplevel(self.root)
        win.title("全局设置")
        win.geometry("620x330")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        v_client_id = tk.StringVar(value=client_id)
        v_client_secret = tk.StringVar(value=client_secret)
        v_show = tk.BooleanVar(value=False)
        v_status = tk.StringVar(
            value="已安全保存，可自动补到新/现有仓库" if client_id and client_secret else "尚未配置 Tailscale OAuth"
        )

        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="Tailscale OAuth（全局一次设置）", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            frame,
            text="保存后，程序在创建仓库或启动手机前发现仓库缺少 Secret 时，会自动写入。凭据使用 Windows DPAPI 当前用户加密保存。",
            wraplength=570,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 14))

        ttk.Label(frame, text="TS_API_CLIENT_ID").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=v_client_id).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0))
        ttk.Label(frame, text="TS_API_CLIENT_SECRET").grid(row=3, column=0, sticky="w", pady=(10, 0))
        secret_entry = ttk.Entry(frame, textvariable=v_client_secret, show="•")
        secret_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))

        def toggle_secret() -> None:
            secret_entry.configure(show="" if v_show.get() else "•")

        ttk.Checkbutton(frame, text="显示", variable=v_show, command=toggle_secret).grid(
            row=3, column=2, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Label(frame, textvariable=v_status, wraplength=570).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(14, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(22, 0))

        def save_global() -> None:
            try:
                self.global_secrets.save(v_client_id.get(), v_client_secret.get())
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"保存全局 Tailscale 凭据失败：{exc}", parent=win)
                return
            self._refresh_tailscale_global_status()
            self.log("全局 Tailscale OAuth 已使用 Windows DPAPI 安全保存")
            messagebox.showinfo(
                APP_NAME,
                "已保存。以后创建仓库或启动手机时，会自动补齐缺少的 Tailscale GitHub Secrets。",
                parent=win,
            )
            win.destroy()

        def clear_global() -> None:
            if not messagebox.askyesno(APP_NAME, "清除本机保存的全局 Tailscale 凭据？", parent=win):
                return
            self.global_secrets.clear()
            self._refresh_tailscale_global_status()
            self.log("已清除本机全局 Tailscale OAuth")
            win.destroy()

        ttk.Button(buttons, text="清除本机凭据", command=clear_global).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=save_global).pack(side="right", padx=(0, 8))
        win.bind("<Control-s>", lambda _e: save_global())

    def _sync_tailscale_repo_secrets(
        self,
        repo: str,
        token: str,
        secret_names: set[str],
        require_configured: bool = True,
    ) -> list[str]:
        names = set(secret_names or set())
        missing = [x for x in ("TS_API_CLIENT_ID", "TS_API_CLIENT_SECRET") if x not in names]
        if not missing:
            return []
        client_id, client_secret = self.global_secrets.load()
        if not client_id or not client_secret:
            message = "目标仓库缺少 GitHub Secrets: " + ", ".join(missing) + "；请先在顶部“全局设置”填写一次 Tailscale OAuth"
            if require_configured:
                raise RuntimeError(message)
            return [message]

        values = {
            "TS_API_CLIENT_ID": client_id,
            "TS_API_CLIENT_SECRET": client_secret,
        }
        written: list[str] = []
        for name in missing:
            ok, detail = self.tools.set_github_secret(repo, token, name, values[name])
            if not ok:
                raise RuntimeError(f"自动写入 {name} 失败: {detail}")
            written.append(name)
        return ["已从全局设置自动补齐 Tailscale Secrets: " + ", ".join(written)] if written else []

    def open_settings(self, profile_id: Optional[str]) -> None:
        original = self.store.get(profile_id) if profile_id else None
        if original:
            draft = AppConfig.from_dict(original.to_dict())
        else:
            base_repo = self.store.profiles[0].repo if self.store.profiles else "usdt19908888286-bit/android-test"
            draft = AppConfig(repo=base_repo, phone_name=f"BICOIN-{len(self.store.profiles)+1:03d}", phone_id=f"{len(self.store.profiles)+1:03d}")

        win = tk.Toplevel(self.root)
        win.title("手机设置" if original else "添加手机")
        win.geometry("880x760")
        win.minsize(780, 620)
        win.transient(self.root)

        v_repo = tk.StringVar(value=draft.repo)
        v_branch = tk.StringVar(value=draft.branch)
        v_name = tk.StringVar(value=draft.phone_name)
        v_id = tk.StringVar(value=draft.phone_id)
        v_pkg = tk.StringVar(value=draft.package_name)
        v_apk = tk.StringVar(value=draft.apk_url)
        v_local_apk = tk.StringVar(value=draft.local_apk_path)
        v_install_template = tk.StringVar(value=draft.manual_install_command)
        v_install_preview = tk.StringVar(value="")
        v_api = tk.StringVar(value=draft.api_level)
        v_target = tk.StringVar(value=draft.target)
        v_arch = tk.StringVar(value=draft.arch)
        v_profile = tk.StringVar(value=draft.profile)
        v_cores = tk.StringVar(value=draft.cores)
        v_ram = tk.StringVar(value=draft.ram_mb)
        v_device_preset = tk.StringVar(value=draft.device_preset)
        v_device_manufacturer = tk.StringVar(value=draft.device_manufacturer)
        v_device_brand = tk.StringVar(value=draft.device_brand)
        v_device_model = tk.StringVar(value=draft.device_model)
        v_device_native = tk.StringVar(value=draft.device_native_resolution)
        v_auto = tk.BooleanVar(value=draft.auto_refresh)
        v_interval = tk.IntVar(value=draft.refresh_seconds)
        v_rotate = tk.BooleanVar(value=draft.auto_rotate)
        rotation_rules = list(draft.rotate_rules or [])
        v_rule_type = tk.StringVar(value="每天时间点")
        v_rule_value = tk.StringVar(value="04:00")
        v_repo_status = tk.StringVar(value="内置工作流将在启动/恢复前自动同步到目标仓库")
        v_rotate_status = tk.StringVar(value=self._rotation_text(draft))

        footer = ttk.Frame(win, padding=(14, 10))
        footer.pack(side="bottom", fill="x")
        ttk.Separator(win, orient="horizontal").pack(side="bottom", fill="x")

        content_shell = ttk.Frame(win)
        content_shell.pack(side="top", fill="both", expand=True)
        settings_canvas = tk.Canvas(content_shell, highlightthickness=0)
        settings_scroll = ttk.Scrollbar(content_shell, orient="vertical", command=settings_canvas.yview)
        settings_canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right", fill="y")
        settings_canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(settings_canvas, padding=14)
        body_window = settings_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind(
            "<Configure>",
            lambda _e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")),
        )
        settings_canvas.bind(
            "<Configure>",
            lambda e: settings_canvas.itemconfigure(body_window, width=e.width),
        )

        repo_box = ttk.LabelFrame(body, text="GitHub 仓库", padding=10)
        repo_box.pack(fill="x")
        repo_box.columnconfigure(1, weight=1)
        ttk.Label(repo_box, text="仓库").grid(row=0, column=0, sticky="w")
        repo_combo = ttk.Combobox(repo_box, textvariable=v_repo, values=self.repo_cache)
        repo_combo.grid(row=0, column=1, sticky="ew", padx=6)

        def load_repos() -> None:
            def done(items: list[str]) -> None:
                self.repo_cache = items
                repo_combo["values"] = items
                v_repo_status.set(f"已读取 {len(items)} 个仓库")
            self.bg("读取 GitHub 仓库", self._fetch_repositories, done, lambda e: v_repo_status.set(e))

        def create_repo_dialog() -> None:
            try:
                token = self._token()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=win)
                return

            dlg = tk.Toplevel(win)
            dlg.title("创建 GitHub 仓库")
            dlg.geometry("470x245")
            dlg.resizable(False, False)
            dlg.transient(win)
            dlg.grab_set()

            v_new_name = tk.StringVar()
            v_visibility = tk.StringVar(value="私有")
            v_create_status = tk.StringVar(value="创建后会自动初始化分支并上传内置 workflow")
            frame = ttk.Frame(dlg, padding=16)
            frame.pack(fill="both", expand=True)
            frame.columnconfigure(1, weight=1)
            ttk.Label(frame, text="仓库名").grid(row=0, column=0, sticky="w")
            name_entry = ttk.Entry(frame, textvariable=v_new_name)
            name_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))
            ttk.Label(frame, text="可见性").grid(row=1, column=0, sticky="w", pady=(12, 0))
            ttk.Combobox(
                frame,
                textvariable=v_visibility,
                values=["私有", "公开"],
                state="readonly",
                width=12,
            ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(12, 0))
            ttk.Label(frame, textvariable=v_create_status, wraplength=430).grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(14, 0)
            )
            buttons = ttk.Frame(frame)
            buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(20, 0))
            ttk.Button(buttons, text="取消", command=dlg.destroy).pack(side="right")

            def submit_create() -> None:
                name = v_new_name.get().strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
                    messagebox.showerror(APP_NAME, "仓库名只能包含字母、数字、点、下划线和短横线", parent=dlg)
                    return
                private = v_visibility.get() != "公开"
                create_button.state(["disabled"])
                v_create_status.set("正在 GitHub 创建仓库…")

                def work() -> tuple[str, str, list[str]]:
                    bootstrap = GitHubAPI("placeholder/repository", token)
                    result = bootstrap.create_user_repository(name, private)
                    full_name = str(result.get("full_name") or "").strip()
                    branch = str(result.get("default_branch") or "main").strip() or "main"
                    if not full_name:
                        raise RuntimeError("GitHub 已响应，但没有返回仓库名称")
                    notes = [f"已创建 {'私有' if private else '公开'}仓库 {full_name}"]
                    api = GitHubAPI(full_name, token)
                    workflow_note = ""
                    for attempt in range(5):
                        try:
                            workflow_note = api.ensure_builtin_workflow(branch)
                            break
                        except Exception as exc:
                            workflow_note = f"workflow 暂未同步: {exc}"
                            if attempt < 4:
                                time.sleep(1.5)
                    notes.append(workflow_note)
                    names = api.repo_secret_names()
                    if names is not None:
                        if "AVD_BACKUP_KEY" not in names:
                            ok, detail = self.tools.set_github_secret(full_name, token, "AVD_BACKUP_KEY")
                            notes.append("已初始化 AVD_BACKUP_KEY" if ok else f"备份密钥稍后初始化: {detail}")
                        notes.extend(
                            self._sync_tailscale_repo_secrets(
                                full_name,
                                token,
                                names,
                                require_configured=False,
                            )
                        )
                    return full_name, branch, notes

                def done(result: tuple[str, str, list[str]]) -> None:
                    full_name, branch, notes = result
                    if full_name not in self.repo_cache:
                        self.repo_cache.insert(0, full_name)
                    repo_combo["values"] = self.repo_cache
                    v_repo.set(full_name)
                    v_branch.set(branch)
                    v_repo_status.set("；".join(notes))
                    self.log("；".join(notes))
                    dlg.destroy()

                def failed(err: str) -> None:
                    create_button.state(["!disabled"])
                    v_create_status.set(f"创建失败：{err}")

                self.bg("创建 GitHub 仓库", work, done, failed)

            create_button = ttk.Button(buttons, text="创建仓库", command=submit_create)
            create_button.pack(side="right", padx=(0, 8))
            name_entry.focus_set()
            dlg.bind("<Return>", lambda _e: submit_create())

        def delete_selected_repo() -> None:
            target = v_repo.get().strip().strip("/")
            if "/" not in target:
                messagebox.showerror(APP_NAME, "请先选择要删除的 owner/repo 仓库", parent=win)
                return
            refs = [p.phone_name for p in self.store.profiles if p.repo.lower() == target.lower()]
            ref_text = f"\n\n当前有 {len(refs)} 个本地手机配置引用这个仓库。删除仓库不会自动删除这些手机配置。" if refs else ""
            if not messagebox.askyesno(
                APP_NAME,
                f"永久删除 GitHub 仓库：\n{target}\n\n此操作不可撤销，Actions、Cache、备份和仓库内容都会一起删除。{ref_text}",
                parent=win,
            ):
                return
            typed = simpledialog.askstring(
                "确认删除仓库",
                f"请输入完整仓库名确认删除：\n{target}",
                parent=win,
            )
            if typed is None:
                return
            if typed.strip() != target:
                messagebox.showerror(APP_NAME, "输入的仓库名不一致，已取消删除。", parent=win)
                return
            try:
                token = self._token()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=win)
                return
            v_repo_status.set(f"正在永久删除 {target}…")

            def work() -> str:
                try:
                    GitHubAPI(target, token).delete_repository()
                except RuntimeError as exc:
                    text = str(exc)
                    if "GitHub API 403" in text:
                        raise RuntimeError(
                            "当前 GitHub 授权没有删除仓库权限，或你不是该仓库管理员。需要带仓库删除权限的 GitHub 授权。"
                        ) from exc
                    raise
                return target

            def done(deleted: str) -> None:
                self.repo_cache = [x for x in self.repo_cache if x.lower() != deleted.lower()]
                repo_combo["values"] = self.repo_cache
                v_repo.set("")
                v_repo_status.set(f"已永久删除 {deleted}；请为这台手机重新选择仓库后保存")
                self.log(f"已永久删除 GitHub 仓库: {deleted}")

            self.bg("删除 GitHub 仓库", work, done, lambda err: v_repo_status.set(f"删除失败：{err}"))

        ttk.Button(repo_box, text="读取我的仓库", command=load_repos).grid(row=0, column=2, padx=(5, 2))
        ttk.Button(repo_box, text="＋ 创建仓库", command=create_repo_dialog).grid(row=0, column=3, padx=2)
        ttk.Button(repo_box, text="删除仓库", command=delete_selected_repo).grid(row=0, column=4, padx=(2, 5))
        ttk.Label(repo_box, text="分支").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(repo_box, textvariable=v_branch).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(repo_box, textvariable=v_repo_status, wraplength=800).grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))

        identity = ttk.LabelFrame(body, text="手机身份 / App", padding=10)
        identity.pack(fill="x", pady=(10, 0))
        for c in (1, 3):
            identity.columnconfigure(c, weight=1)
        ttk.Label(identity, text="手机名称").grid(row=0, column=0, sticky="w")
        ttk.Entry(identity, textvariable=v_name).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(identity, text="Phone ID").grid(row=0, column=2, sticky="w")
        ttk.Entry(identity, textvariable=v_id).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Label(identity, text="监控 / App 包名").grid(row=1, column=0, sticky="w", pady=(8, 0))
        pkg_combo = ttk.Combobox(identity, textvariable=v_pkg, values=draft.package_history)
        pkg_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(identity, text="APK URL（仅参考，不自动安装）").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(identity, textvariable=v_apk).grid(row=2, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 0))

        def read_apps() -> None:
            device = original.last_device if original else ""
            if not device:
                messagebox.showinfo(APP_NAME, "这台手机还没有可用的 ADB 地址。")
                return
            self.bg(
                "读取已安装 App",
                lambda: self.tools.list_third_party_packages(device),
                lambda items: pkg_combo.configure(values=items),
            )

        def refresh_install_preview(*_args) -> None:
            temp = AppConfig.from_dict(draft.to_dict())
            temp.local_apk_path = v_local_apk.get().strip()
            temp.manual_install_command = v_install_template.get().strip()
            if original:
                temp.last_device = original.last_device
            v_install_preview.set(self._build_apk_install_command(temp))

        def choose_local_apk() -> None:
            path = filedialog.askopenfilename(
                parent=win,
                title="选择本地 APK",
                filetypes=[("Android APK", "*.apk"), ("所有文件", "*.*")],
            )
            if not path:
                return
            v_local_apk.set(path)
            refresh_install_preview()

        def copy_install_command() -> None:
            refresh_install_preview()
            command = v_install_preview.get().strip()
            if not v_local_apk.get().strip():
                messagebox.showinfo(APP_NAME, "请先选择本地 APK。", parent=win)
                return
            self._copy_to_clipboard(command)
            messagebox.showinfo(APP_NAME, "安装命令已复制。程序不会自动执行该命令。", parent=win)

        ttk.Button(identity, text="读取当前手机已装 App", command=read_apps).grid(row=1, column=2, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(identity, text="本地 APK").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(identity, textvariable=v_local_apk).grid(row=3, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(identity, text="选择 APK", command=choose_local_apk).grid(row=3, column=3, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(identity, text="安装命令模板").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(identity, textvariable=v_install_template).grid(row=4, column=1, columnspan=3, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(identity, text="可留空自动生成；自定义时可用 {adb}、{device}、{apk} 占位符。").grid(row=5, column=1, columnspan=3, sticky="w", padx=6, pady=(4, 0))
        ttk.Label(identity, text="命令预览").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(identity, textvariable=v_install_preview, state="readonly").grid(row=6, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(identity, text="复制安装命令", command=copy_install_command).grid(row=6, column=3, sticky="w", padx=6, pady=(8, 0))
        v_local_apk.trace_add("write", refresh_install_preview)
        v_install_template.trace_add("write", refresh_install_preview)
        refresh_install_preview()

        spec = ttk.LabelFrame(body, text="Android / 云机规格", padding=10)
        spec.pack(fill="x", pady=(10, 0))
        for c in (1, 3, 5):
            spec.columnconfigure(c, weight=1)
        fields = [
            ("Android API", v_api), ("Profile", v_profile), ("CPU 核数", v_cores),
            ("内存 MB", v_ram), ("Target", v_target), ("架构", v_arch),
        ]
        for i, (label, var) in enumerate(fields):
            row, pair = divmod(i, 3)
            col = pair * 2
            ttk.Label(spec, text=label).grid(row=row, column=col, sticky="w", pady=4)
            ttk.Entry(spec, textvariable=var).grid(row=row, column=col+1, sticky="ew", padx=6, pady=4)
        ttk.Checkbutton(spec, text="自动监控", variable=v_auto).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(spec, from_=3, to=120, textvariable=v_interval, width=7).grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Label(spec, text="秒").grid(row=2, column=1, sticky="e", pady=(8, 0))

        model_box = ttk.LabelFrame(body, text="真实手机型号 / Android Build 身份", padding=10)
        model_box.pack(fill="x", pady=(10, 0))
        model_box.columnconfigure(1, weight=1)
        model_box.columnconfigure(3, weight=1)
        preset_names = [item["name"] for item in REAL_DEVICE_CATALOG]
        ttk.Label(model_box, text="机型预设").grid(row=0, column=0, sticky="w")
        preset_combo = ttk.Combobox(model_box, textvariable=v_device_preset, values=preset_names, state="readonly")
        preset_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6)

        def apply_device_entry(item: dict[str, str]) -> None:
            v_device_preset.set(item["name"])
            v_device_manufacturer.set(item["manufacturer"])
            v_device_brand.set(item["brand"])
            v_device_model.set(item["model"])
            v_device_native.set(item.get("resolution", ""))

        def apply_selected_device(_event=None) -> None:
            name = v_device_preset.get()
            item = next((x for x in REAL_DEVICE_CATALOG if x["name"] == name), None)
            if item:
                apply_device_entry(item)

        def random_real_device() -> None:
            apply_device_entry(secrets.choice(REAL_DEVICE_CATALOG))

        def clear_device_identity() -> None:
            v_device_preset.set("")
            v_device_manufacturer.set("")
            v_device_brand.set("")
            v_device_model.set("")
            v_device_native.set("")

        preset_combo.bind("<<ComboboxSelected>>", apply_selected_device)
        ttk.Button(model_box, text="🎲 随机真实机型", command=random_real_device).grid(row=0, column=3, sticky="w", padx=(6, 0))
        ttk.Button(model_box, text="使用默认 AVD", command=clear_device_identity).grid(row=0, column=4, sticky="w", padx=(6, 0))
        ttk.Label(model_box, text="Manufacturer").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(model_box, textvariable=v_device_manufacturer).grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(model_box, text="Brand").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(model_box, textvariable=v_device_brand).grid(row=1, column=3, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(model_box, text="Build.MODEL").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(model_box, textvariable=v_device_model).grid(row=2, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(model_box, text="原生分辨率参考").grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(model_box, textvariable=v_device_native, state="readonly").grid(row=2, column=3, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(
            model_box,
            text="随机按钮只从内置真实量产机型库选择。云机仍按当前 720×1600 渲染以控制资源占用；这里只改变 Emulator 的 Android Build 身份，不改变硬件级证明。",
            wraplength=820,
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))

        rotation = ttk.LabelFrame(body, text="定时自动换机", padding=10)
        rotation.pack(fill="x", pady=(10, 0))
        rotation.columnconfigure(0, weight=1)
        ttk.Checkbutton(rotation, text="启用定时自动换机", variable=v_rotate).grid(row=0, column=0, sticky="w")

        rules_box = ttk.Frame(rotation)
        rules_box.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        rules_box.columnconfigure(0, weight=1)
        rules_list = tk.Listbox(rules_box, height=4, exportselection=False)
        rules_list.grid(row=0, column=0, columnspan=5, sticky="ew")

        def rule_label(rule: str) -> str:
            if rule.startswith("time:"):
                return f"每天 {rule.split(':', 1)[1]}"
            match = re.fullmatch(r"hours:(\d{1,3})", rule)
            return f"每 {int(match.group(1))} 小时" if match else rule

        def render_rules() -> None:
            rules_list.delete(0, "end")
            for item in rotation_rules:
                rules_list.insert("end", rule_label(item))
            preview = AppConfig.from_dict({
                **draft.to_dict(),
                "auto_rotate": bool(v_rotate.get()),
                "rotate_rules": list(rotation_rules),
            })
            preview.rotate_next_ts = self._next_rotation_ts(preview) if preview.auto_rotate and preview.rotate_rules else 0.0
            v_rotate_status.set(self._rotation_text(preview))

        def add_rule() -> None:
            kind = v_rule_type.get()
            value = v_rule_value.get().strip()
            if kind == "每天时间点":
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    messagebox.showerror(APP_NAME, "时间格式必须是 HH:MM，例如 04:30")
                    return
                rule = f"time:{value}"
            else:
                try:
                    hours = int(value)
                except Exception:
                    messagebox.showerror(APP_NAME, "间隔小时必须是 1 到 168 的整数")
                    return
                if not 1 <= hours <= 168:
                    messagebox.showerror(APP_NAME, "间隔小时必须是 1 到 168")
                    return
                rule = f"hours:{hours}"
            if rule not in rotation_rules:
                rotation_rules.append(rule)
            render_rules()

        def remove_rule() -> None:
            selected = list(rules_list.curselection())
            if not selected:
                return
            rotation_rules.pop(int(selected[0]))
            render_rules()

        def rule_type_changed(_event=None) -> None:
            v_rule_value.set("04:00" if v_rule_type.get() == "每天时间点" else "4")

        type_combo = ttk.Combobox(
            rules_box,
            textvariable=v_rule_type,
            values=["每天时间点", "每 N 小时"],
            state="readonly",
            width=14,
        )
        type_combo.grid(row=1, column=0, sticky="w", pady=(7, 0))
        type_combo.bind("<<ComboboxSelected>>", rule_type_changed)
        ttk.Entry(rules_box, textvariable=v_rule_value, width=12).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(7, 0))
        ttk.Button(rules_box, text="＋ 添加规则", command=add_rule).grid(row=1, column=2, padx=(6, 0), pady=(7, 0))
        ttk.Button(rules_box, text="删除选中", command=remove_rule).grid(row=1, column=3, padx=(6, 0), pady=(7, 0))

        ttk.Label(
            rotation,
            text="可以同时添加多个每天时间点和小时规则；程序每次选择最近到期的一条执行。执行顺序：备份旧机 → 确认完整 AVD 已保存 → 恢复新 Runner。",
            wraplength=760,
        ).grid(row=2, column=0, sticky="w", pady=(6, 2))
        ttk.Label(rotation, textvariable=v_rotate_status, wraplength=760).grid(row=3, column=0, sticky="w", pady=(2, 0))
        render_rules()

        note = ttk.LabelFrame(body, text="仓库部署", padding=10)
        note.pack(fill="x", pady=(10, 0))
        ttk.Label(
            note,
            text=(
                f"工作流已经内置在程序中。目标仓库缺少 {BUILTIN_WORKFLOW_PATH} 时会自动上传，"
                "版本不同时会自动更新。备份密钥 AVD_BACKUP_KEY 可自动创建。\n"
                "Tailscale OAuth 可以在主界面“全局设置”里只填写一次；程序使用 Windows DPAPI 加密保存在本机，"
                "以后创建仓库或启动手机时会自动把缺少的 TS_API_CLIENT_ID / TS_API_CLIENT_SECRET 写入目标仓库。"
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

        def collect() -> AppConfig:
            repo = v_repo.get().strip().strip("/")
            branch = v_branch.get().strip() or "main"
            phone_id = v_id.get().strip()
            phone_name = v_name.get().strip()
            package = v_pkg.get().strip()
            if "/" not in repo:
                raise ValueError("仓库格式应为 owner/repo")
            if not phone_id.isdigit():
                raise ValueError("Phone ID 只能是数字")
            if not phone_name:
                raise ValueError("手机名称不能为空")
            if not PKG_RE.match(package):
                raise ValueError("Android 包名格式无效")
            if not v_cores.get().strip().isdigit() or int(v_cores.get()) < 1:
                raise ValueError("CPU 核数必须是正整数")
            if not v_ram.get().strip().isdigit() or int(v_ram.get()) < 1024:
                raise ValueError("内存至少 1024 MB")

            manufacturer = v_device_manufacturer.get().strip()
            brand = v_device_brand.get().strip()
            model = v_device_model.get().strip()
            if model:
                if not manufacturer or not brand:
                    raise ValueError("设置手机型号时 Manufacturer 和 Brand 不能为空")
                if not re.fullmatch(r"[A-Za-z0-9._+ -]+", manufacturer):
                    raise ValueError("Manufacturer 含不支持的字符")
                if not re.fullmatch(r"[A-Za-z0-9._+-]+", brand):
                    raise ValueError("Brand 含不支持的字符")
                if not re.fullmatch(r"[A-Za-z0-9._+() -]+", model):
                    raise ValueError("Build.MODEL 含不支持的字符")

            clean_rules: list[str] = []
            for raw_rule in rotation_rules:
                rule = str(raw_rule).strip().lower()
                if re.fullmatch(r"time:(?:[01]\d|2[0-3]):[0-5]\d", rule):
                    if rule not in clean_rules:
                        clean_rules.append(rule)
                    continue
                match = re.fullmatch(r"hours:(\d{1,3})", rule)
                if match and 1 <= int(match.group(1)) <= 168:
                    normalized = f"hours:{int(match.group(1))}"
                    if normalized not in clean_rules:
                        clean_rules.append(normalized)
                    continue
                raise ValueError(f"无效换机规则: {raw_rule}")
            if bool(v_rotate.get()) and not clean_rules:
                raise ValueError("启用自动换机时至少添加一条换机规则")

            for other in self.store.profiles:
                if other.profile_id != draft.profile_id and other.repo.lower() == repo.lower() and other.phone_id == phone_id:
                    raise ValueError(f"同一个仓库内 Phone ID {phone_id} 已经存在")

            identity_changed = (draft.repo.lower(), draft.phone_id) != (repo.lower(), phone_id)
            if identity_changed and draft.rotation_phase:
                raise ValueError("当前正在自动换机，完成后再修改仓库或 Phone ID")
            schedule_changed = (
                draft.auto_rotate != bool(v_rotate.get())
                or list(draft.rotate_rules or []) != clean_rules
            )

            draft.repo = repo
            draft.branch = branch
            draft.phone_id = phone_id
            draft.phone_name = phone_name
            draft.package_name = package
            draft.apk_url = v_apk.get().strip() or DEFAULT_APK_URL
            draft.local_apk_path = v_local_apk.get().strip()
            draft.manual_install_command = v_install_template.get().strip()
            draft.api_level = v_api.get().strip() or "35"
            draft.target = v_target.get().strip() or "google_apis"
            draft.arch = v_arch.get().strip() or "x86_64"
            draft.profile = v_profile.get().strip() or "pixel_6"
            draft.cores = v_cores.get().strip() or "4"
            draft.ram_mb = v_ram.get().strip() or "8192"
            draft.device_preset = v_device_preset.get().strip() if model else ""
            draft.device_manufacturer = manufacturer if model else ""
            draft.device_brand = brand if model else ""
            draft.device_model = model
            draft.device_native_resolution = v_device_native.get().strip() if model else ""
            draft.auto_refresh = bool(v_auto.get())
            try:
                draft.refresh_seconds = max(3, int(v_interval.get()))
            except Exception:
                draft.refresh_seconds = 8
            draft.auto_rotate = bool(v_rotate.get())
            draft.rotate_rules = clean_rules
            # Keep old fields in sync so older configuration files/versions can still read the first rule.
            if clean_rules:
                first_rule = clean_rules[0]
                if first_rule.startswith("time:"):
                    draft.rotate_mode = "daily"
                    draft.rotate_daily_time = first_rule.split(":", 1)[1]
                else:
                    draft.rotate_mode = "interval"
                    match = re.fullmatch(r"hours:(\d{1,3})", first_rule)
                    draft.rotate_interval_hours = int(match.group(1)) if match else 4
            draft.package_history = [package] + [x for x in draft.package_history if x != package]
            draft.package_history = draft.package_history[:40]

            if identity_changed:
                draft.last_device = ""
                draft.last_node = ""
                draft.last_run_id = 0
                draft.last_run_status = "-"
                draft.rotation_phase = ""
                draft.rotation_run_id = 0
                draft.rotation_started_ts = 0.0
                draft.rotation_last_error = ""
                draft.rotate_last_ts = 0.0
                draft.rotate_next_ts = 0.0

            if not draft.auto_rotate:
                draft.rotate_next_ts = 0.0
            elif schedule_changed or not draft.rotate_next_ts or draft.rotate_next_ts <= time.time():
                draft.rotate_next_ts = self._next_rotation_ts(draft)
            return draft

        def check_repo() -> None:
            try:
                temp = collect()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            v_repo_status.set("正在同步内置 workflow 并检查 GitHub Secrets…")
            self.bg(
                "准备仓库",
                lambda: self._prepare_repository(temp),
                lambda notes: v_repo_status.set("；".join(notes)),
                lambda err: v_repo_status.set(err),
            )

        def save() -> None:
            try:
                cfg = collect()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            self.store.upsert(cfg)
            self._render_cards()
            self.log(f"已保存手机配置: {cfg.phone_name} · {cfg.repo} · ID {cfg.phone_id}")
            win.destroy()

        def delete() -> None:
            if not original:
                return
            if not messagebox.askyesno(APP_NAME, f"删除手机配置“{original.phone_name}”？\n不会删除 GitHub 上已有的备份。"):
                return
            self.store.remove(original.profile_id)
            self._render_cards()
            self.log(f"已删除手机配置: {original.phone_name}")
            win.destroy()

        ttk.Button(footer, text="检查 / 部署仓库", command=check_repo).pack(side="left")
        if original:
            ttk.Button(footer, text="删除此手机", command=delete).pack(side="left", padx=8)
        ttk.Button(footer, text="关闭", command=win.destroy).pack(side="right")
        ttk.Button(footer, text="保存", command=save).pack(side="right", padx=8)
        ttk.Label(footer, text="Ctrl+S 保存").pack(side="right", padx=(0, 10))
        win.bind("<Control-s>", lambda _e: save())
        win.bind("<Control-S>", lambda _e: save())

    # ------------------------- repository / workflow -------------------------
    def _prepare_repository(self, cfg: AppConfig) -> list[str]:
        api = self.api_for(cfg)
        token = self._token()
        notes = [api.ensure_builtin_workflow(cfg.branch)]
        secret_names = api.repo_secret_names()
        if secret_names is not None:
            notes.extend(self._sync_tailscale_repo_secrets(cfg.repo, token, secret_names, require_configured=True))
            if "AVD_BACKUP_KEY" not in secret_names:
                ok, detail = self.tools.set_github_secret(cfg.repo, token, "AVD_BACKUP_KEY")
                if not ok:
                    raise RuntimeError("初始化 AVD_BACKUP_KEY 失败: " + detail)
                notes.append("已初始化备份加密密钥")
        else:
            notes.append("当前授权无法列出 Secret 元数据，跳过 Secret 完整性检查")
        return notes

    def _workflow_inputs(self, cfg: AppConfig, mode: str) -> dict[str, str]:
        return {
            "mode": mode,
            "phone_id": cfg.phone_id,
            "phone_name": cfg.phone_name,
            "apk_url": cfg.apk_url or DEFAULT_APK_URL,
            "package_name": cfg.package_name or DEFAULT_PACKAGE,
            "api_level": cfg.api_level or "35",
            "target": cfg.target or "google_apis",
            "arch": cfg.arch or "x86_64",
            "profile": cfg.profile or "pixel_6",
            "cores": cfg.cores or "4",
            "ram_mb": cfg.ram_mb or "8192",
            "device_manufacturer": cfg.device_manufacturer or "",
            "device_brand": cfg.device_brand or "",
            "device_model": cfg.device_model or "",
        }

    # ------------------------- cloud phone actions -------------------------
    def start_profile(self, profile_id: str) -> None:
        self._dispatch_profile(profile_id, "new")

    def restore_profile(self, profile_id: str) -> None:
        self._dispatch_profile(profile_id, "restore")

    def _dispatch_profile(self, profile_id: str, mode: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return
        action = "恢复备份" if mode == "restore" else "新建 / 启动"
        self._set_card(profile_id, "run", f"{action}：正在准备仓库…")

        def work() -> list[str]:
            notes = self._prepare_repository(cfg)
            api = self.api_for(cfg)
            api.dispatch(Path(BUILTIN_WORKFLOW_PATH).name, cfg.branch, self._workflow_inputs(cfg, mode))
            notes.append("GitHub workflow 已触发")
            return notes

        def done(notes: list[str]) -> None:
            self._set_card(profile_id, "run", f"{action}：已触发，等待 Runner")
            self.log(f"{cfg.phone_name}: " + "；".join(notes))
            self.root.after(2200, lambda: self.refresh_profile(profile_id))

        self.bg(
            f"{cfg.phone_name} {action}",
            work,
            done,
            lambda err: self._set_card(profile_id, "run", f"{action}失败：{err}"),
        )

    def backup_profile(self, profile_id: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return
        self._set_card(profile_id, "run", "正在发送备份请求…")

        def work() -> tuple[int, int]:
            api = self.api_for(cfg)
            run = api.latest_phone_run(cfg.phone_name, cfg.phone_id, cfg.branch)
            if not run:
                raise RuntimeError("没有找到这台手机的 GitHub Run")
            if str(run.get("status")) != "in_progress":
                raise RuntimeError("当前云机 Runner 已不在运行，无法从运行中实例触发完整备份")
            rid = int(run["id"])
            # Ensure the encryption key exists, without regenerating an existing one.
            names = api.repo_secret_names()
            if names is not None and "AVD_BACKUP_KEY" not in names:
                ok, detail = self.tools.set_github_secret(cfg.repo, self._token(), "AVD_BACKUP_KEY")
                if not ok:
                    raise RuntimeError(detail)
            issue = api.request_backup(cfg.phone_id, rid)
            return rid, int(issue.get("number") or 0)

        def done(result: tuple[int, int]) -> None:
            rid, issue_no = result
            cfg.last_run_id = rid
            self.store.save()
            self._set_card(profile_id, "run", f"备份请求已发送 · Run {rid} · Issue #{issue_no}")
            self.log(f"{cfg.phone_name}: 备份请求已发送，Runner 会停止 Emulator 后保存完整 AVD")
            self.root.after(3000, lambda: self.refresh_profile(profile_id))

        self.bg(
            f"{cfg.phone_name} 备份",
            work,
            done,
            lambda err: self._set_card(profile_id, "run", f"备份失败：{err}"),
        )

    def rotate_profile(self, profile_id: str, automatic: bool = False) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return
        if cfg.rotation_phase or profile_id in self._rotating:
            if not automatic:
                messagebox.showinfo(APP_NAME, "这台手机已经在换机流程中。")
            return

        self._rotating.add(profile_id)
        self._set_card(profile_id, "rotation", "正在开始换机…")
        self._set_card(profile_id, "run", "换机：正在检查旧手机…")

        def work() -> dict[str, Any]:
            self._prepare_repository(cfg)
            api = self.api_for(cfg)
            run = api.latest_phone_run(cfg.phone_name, cfg.phone_id, cfg.branch)
            if run and str(run.get("status")) == "in_progress":
                rid = int(run["id"])
                names = api.repo_secret_names()
                if names is not None and "AVD_BACKUP_KEY" not in names:
                    ok, detail = self.tools.set_github_secret(cfg.repo, self._token(), "AVD_BACKUP_KEY")
                    if not ok:
                        raise RuntimeError(detail)
                issue = api.request_backup(cfg.phone_id, rid)
                return {
                    "state": "waiting_backup",
                    "run_id": rid,
                    "issue": int(issue.get("number") or 0),
                }

            # If the old phone is already offline, restore directly from the latest
            # encrypted cache. This also covers the natural 5-hour auto-backup case.
            api.dispatch(
                Path(BUILTIN_WORKFLOW_PATH).name,
                cfg.branch,
                self._workflow_inputs(cfg, "restore"),
            )
            return {"state": "restore_dispatched", "run_id": 0, "issue": 0}

        def done(result: dict[str, Any]) -> None:
            self._rotating.discard(profile_id)
            cfg.rotation_last_error = ""
            now_ts = time.time()
            if result.get("state") == "waiting_backup":
                cfg.rotation_phase = "waiting_backup"
                cfg.rotation_run_id = int(result.get("run_id") or 0)
                cfg.rotation_started_ts = now_ts
                issue_no = int(result.get("issue") or 0)
                self._set_card(
                    profile_id,
                    "rotation",
                    f"换机中 · 旧 Run {cfg.rotation_run_id} 正在备份 · Issue #{issue_no}",
                )
                self._set_card(profile_id, "run", f"换机：等待旧 Run {cfg.rotation_run_id} 完成备份")
                self.log(
                    f"{cfg.phone_name}: 换机已开始，先完整备份旧 Run {cfg.rotation_run_id}，备份成功后自动恢复新 Runner"
                )
            else:
                cfg.rotation_phase = ""
                cfg.rotation_run_id = 0
                cfg.rotation_started_ts = 0.0
                cfg.rotate_last_ts = now_ts
                self._reschedule_rotation(cfg, now_ts)
                self._set_card(profile_id, "run", "换机：旧机已离线，已直接从最新备份启动新 Runner")
                self.log(f"{cfg.phone_name}: 旧机已离线，已直接触发最新备份恢复")
                self.root.after(2200, lambda: self.refresh_profile(profile_id))
            self.store.save()

        def failed(err: str) -> None:
            self._rotating.discard(profile_id)
            cfg.rotation_last_error = err
            if cfg.auto_rotate:
                cfg.rotate_next_ts = time.time() + 1800
            self.store.save()
            self._set_card(profile_id, "rotation", self._rotation_text(cfg))
            self._set_card(profile_id, "run", f"换机启动失败：{err}")

        self.bg(
            f"{cfg.phone_name} {'自动' if automatic else '立即'}换机",
            work,
            done,
            failed,
        )

    def _poll_rotation(self, cfg: AppConfig) -> None:
        profile_id = cfg.profile_id
        if cfg.rotation_phase != "waiting_backup" or not cfg.rotation_run_id:
            return
        if profile_id in self._rotating:
            return

        now_ts = time.time()
        last = self._last_rotation_poll.get(profile_id, 0.0)
        if now_ts - last < 12:
            return
        self._last_rotation_poll[profile_id] = now_ts
        self._rotating.add(profile_id)

        old_run_id = cfg.rotation_run_id

        def work() -> dict[str, Any]:
            api = self.api_for(cfg)
            run = api.run(old_run_id)
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            if status != "completed":
                return {"state": "waiting", "status": status}
            if conclusion != "success":
                return {"state": "failed", "error": f"旧 Run {old_run_id} 备份流程结束为 {conclusion or 'unknown'}"}

            logs = api.run_logs_text(old_run_id)
            if "MANAGED_BACKUP_SAVED" not in logs:
                return {"state": "failed", "error": f"旧 Run {old_run_id} 已结束，但日志未确认 MANAGED_BACKUP_SAVED"}

            api.dispatch(
                Path(BUILTIN_WORKFLOW_PATH).name,
                cfg.branch,
                self._workflow_inputs(cfg, "restore"),
            )
            return {"state": "restored"}

        def done(result: dict[str, Any]) -> None:
            self._rotating.discard(profile_id)
            state = str(result.get("state") or "")
            if state == "waiting":
                self._set_card(profile_id, "rotation", f"换机中 · 等待旧 Run {old_run_id} 保存完整备份")
                return

            if state == "failed":
                cfg.rotation_phase = ""
                cfg.rotation_run_id = 0
                cfg.rotation_started_ts = 0.0
                cfg.rotation_last_error = str(result.get("error") or "换机失败")
                if cfg.auto_rotate:
                    cfg.rotate_next_ts = time.time() + 1800
                self.store.save()
                self._set_card(profile_id, "rotation", self._rotation_text(cfg))
                self._set_card(profile_id, "run", f"换机失败：{cfg.rotation_last_error}")
                self.log(f"{cfg.phone_name}: {cfg.rotation_last_error}")
                return

            now_done = time.time()
            cfg.rotation_phase = ""
            cfg.rotation_run_id = 0
            cfg.rotation_started_ts = 0.0
            cfg.rotation_last_error = ""
            cfg.rotate_last_ts = now_done
            self._reschedule_rotation(cfg, now_done)
            self.store.save()
            self._set_card(profile_id, "rotation", self._rotation_text(cfg))
            self._set_card(profile_id, "run", "旧机备份成功 · 新 Runner 已从备份启动")
            self.log(f"{cfg.phone_name}: 旧机完整备份已保存，新的 Runner 已从该备份恢复启动")
            self.root.after(2200, lambda: self.refresh_profile(profile_id))

        def failed(err: str) -> None:
            # Keep waiting_backup state on transient GitHub/network failures. The
            # next scheduler poll will retry instead of losing the in-flight rotation.
            self._rotating.discard(profile_id)
            cfg.rotation_last_error = err
            self.store.save()
            self._set_card(profile_id, "rotation", f"换机中 · 查询暂时失败，稍后重试：{err[:80]}")

        self.bg(f"检查 {cfg.phone_name} 换机备份", work, done, failed)

    def cancel_profile(self, profile_id: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return

        def work() -> int:
            api = self.api_for(cfg)
            run = api.latest_phone_run(cfg.phone_name, cfg.phone_id, cfg.branch)
            if not run:
                raise RuntimeError("没有找到可停止的 Run")
            rid = int(run["id"])
            if str(run.get("status")) != "in_progress":
                raise RuntimeError("Run 当前不是运行中状态")
            api.cancel(rid)
            return rid

        self.bg(
            f"{cfg.phone_name} 停止云机",
            work,
            lambda rid: (self._set_card(profile_id, "run", f"已请求停止 Run {rid}"), self.root.after(1800, lambda: self.refresh_profile(profile_id))),
            lambda err: self._set_card(profile_id, "run", f"停止失败：{err}"),
        )

    # ------------------------- monitoring -------------------------
    def refresh_profile(self, profile_id: str, quiet: bool = False) -> None:
        cfg = self.store.get(profile_id)
        if not cfg or profile_id in self._refreshing:
            return
        self._refreshing.add(profile_id)
        if not quiet:
            self._set_card(profile_id, "run", "正在刷新状态…")

        def work() -> dict[str, Any]:
            api = self.api_for(cfg)
            run = api.latest_phone_run(cfg.phone_name, cfg.phone_id, cfg.branch)
            result: dict[str, Any] = {"run": run, "ip": "", "host": "", "health": {}, "runner": {}}
            if not run:
                return result
            rid = int(run["id"])
            ip, host = self.tools.discover_phone(cfg.phone_id, rid, cfg.phone_name)
            result["ip"] = ip
            result["host"] = host
            if ip:
                result["runner"] = self.tools.runner_status(ip)
                address = f"{ip}:5555"
                if self.tools.port_open(ip, 5555, 1.2):
                    result["health"] = self.tools.device_health(address, cfg.package_name)
            return result

        def done(result: dict[str, Any]) -> None:
            self._refreshing.discard(profile_id)
            run = result.get("run") or {}
            if not run:
                cfg.last_run_id = 0
                cfg.last_run_status = "-"
                cfg.last_device = ""
                cfg.last_node = ""
                self._set_card(profile_id, "run", "未找到 Run")
                self._set_card(profile_id, "adb", "未发现 ADB 地址")
                self._set_card(profile_id, "server", "CPU -\n内存 -")
                self._set_card(profile_id, "android", "未运行")
                self._set_card(profile_id, "app", f"{cfg.package_name}\n未检测")
                self.store.save()
                return

            rid = int(run["id"])
            status = str(run.get("status") or "?")
            conclusion = str(run.get("conclusion") or "")
            cfg.last_run_id = rid
            cfg.last_run_status = f"{status}/{conclusion or '-'}"
            ip = str(result.get("ip") or "")
            host = str(result.get("host") or "")
            health = result.get("health") or {}
            runner = result.get("runner") or {}

            if ip:
                cfg.last_device = f"{ip}:5555"
                cfg.last_node = host
            elif status != "in_progress":
                cfg.last_device = ""
                cfg.last_node = ""

            boot = str(health.get("boot") or "")
            adb = str(health.get("adb") or "")
            if status == "queued":
                state = "排队中"
            elif status == "in_progress" and boot == "1" and adb == "已连接":
                state = "运行中"
            elif status == "in_progress":
                state = "启动中"
            elif conclusion == "success":
                state = "已结束"
            elif conclusion == "cancelled":
                state = "已停止"
            elif conclusion == "failure":
                state = "失败"
            else:
                state = status
            self._set_card(profile_id, "run", f"{state} · Run {rid}")

            if ip:
                adb_text = cfg.last_device
                if not health:
                    adb_text += "\nADB 5555 尚未就绪"
                elif adb:
                    adb_text += f"\n{adb}"
                self._set_card(profile_id, "adb", adb_text)
            else:
                self._set_card(profile_id, "adb", "Tailscale 节点尚未发现" if status == "in_progress" else "云机不在线")

            host_cpu = str(runner.get("host_cpu") or "-")
            host_mem = str(runner.get("host_mem") or "-")
            self._set_card(profile_id, "server", f"CPU {host_cpu}\n内存 {host_mem}")

            if health:
                model = str(health.get("model") or "-")
                android = str(health.get("android") or "-")
                device_cpu = str(health.get("device_cpu") or "-")
                self._set_card(profile_id, "android", f"Android {android} · {model}\nBoot {boot or '-'} · CPU {device_cpu[:55]}")
                app_state = str(health.get("app") or "-")
                app_cpu = str(health.get("app_cpu") or "-")
                app_mem = str(health.get("app_mem") or "-")
                self._set_card(profile_id, "app", f"{cfg.package_name}\n{app_state} · CPU {app_cpu} · MEM {app_mem}")
            else:
                self._set_card(profile_id, "android", "等待 Android / ADB 就绪" if status == "in_progress" else "未运行")
                self._set_card(profile_id, "app", f"{cfg.package_name}\n等待检测" if status == "in_progress" else "未运行")

            if status == "in_progress" and boot == "1" and adb == "已连接":
                self._run_startup_adb_tasks(cfg, rid)

            self.store.save()

        def failed(err: str) -> None:
            self._refreshing.discard(profile_id)
            if not quiet:
                self._set_card(profile_id, "run", f"刷新失败：{err}")

        self.bg(f"刷新 {cfg.phone_name}", work, done, failed)

    def refresh_all(self) -> None:
        for cfg in list(self.store.profiles):
            self.refresh_profile(cfg.profile_id)

    @staticmethod
    def _adb_task_trigger_text(task: dict[str, Any]) -> str:
        trigger = str(task.get("trigger") or "startup")
        value = str(task.get("value") or "")
        if trigger == "daily":
            return f"每天 {value or '04:00'}"
        if trigger == "interval":
            return f"每 {value or '4'} 小时"
        return "每次云机启动后"

    def _adb_task_next_ts(self, task: dict[str, Any], now_ts: Optional[float] = None) -> float:
        now_ts = float(now_ts or time.time())
        trigger = str(task.get("trigger") or "startup")
        if trigger == "daily":
            value = str(task.get("value") or "04:00")
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                value = "04:00"
            hour, minute = (int(x) for x in value.split(":", 1))
            now_dt = dt.datetime.fromtimestamp(now_ts)
            target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= now_ts:
                target += dt.timedelta(days=1)
            return target.timestamp()
        if trigger == "interval":
            try:
                hours = max(1, min(168, int(task.get("value") or 4)))
            except Exception:
                hours = 4
            base = float(task.get("last_ts") or 0.0)
            if base <= 0 or base > now_ts:
                base = now_ts
            candidate = base + hours * 3600
            if candidate <= now_ts:
                candidate = now_ts + hours * 3600
            return candidate
        return 0.0

    def _run_adb_task(self, cfg: AppConfig, task: dict[str, Any], reason: str = "manual") -> None:
        task_id = str(task.get("id") or "")
        key = f"{cfg.profile_id}:{task_id}"
        if not task_id or key in self._adb_task_running:
            return
        if not cfg.last_device:
            self.log(f"{cfg.phone_name} · ADB 任务未执行：手机还没有可用的 ADB 地址")
            return

        name = str(task.get("name") or "ADB 任务")
        command = str(task.get("command") or "").strip()
        self._adb_task_running.add(key)

        def work() -> tuple[bool, str]:
            return self.tools.run_safe_adb_command(cfg.last_device, command, cfg.package_name)

        def done(result: tuple[bool, str]) -> None:
            self._adb_task_running.discard(key)
            ok, detail = result
            now = time.time()
            task["last_ts"] = now
            if reason == "startup" and ok:
                task["last_run_id"] = int(cfg.last_run_id or 0)
            if reason != "manual" and str(task.get("trigger")) in ("daily", "interval"):
                task["next_ts"] = self._adb_task_next_ts(task, now) if ok else now + 300
            self.store.upsert(cfg)
            compact = str(detail or "").replace("\r", " ").replace("\n", " · ")
            if len(compact) > 420:
                compact = compact[:417] + "..."
            self.log(f"{cfg.phone_name} · ADB任务[{name}] {'成功' if ok else '失败'} · {compact}")

        def failed(err: str) -> None:
            self._adb_task_running.discard(key)
            task["last_ts"] = time.time()
            if reason != "manual" and str(task.get("trigger")) in ("daily", "interval"):
                task["next_ts"] = time.time() + 300
            self.store.upsert(cfg)
            self.log(f"{cfg.phone_name} · ADB任务[{name}] 执行异常: {err}")

        self.bg(f"ADB任务 {cfg.phone_name} · {name}", work, done, failed)

    def _run_startup_adb_tasks(self, cfg: AppConfig, run_id: int) -> None:
        sequence_key = f"{cfg.profile_id}:startup-sequence:{run_id}"
        if sequence_key in self._adb_task_running or not cfg.last_device:
            return

        now = time.time()
        pending: list[dict[str, Any]] = []
        for task in list(cfg.adb_tasks or []):
            if not bool(task.get("enabled", True)):
                continue
            if str(task.get("trigger") or "startup") != "startup":
                continue
            if int(task.get("last_run_id") or 0) == int(run_id):
                continue
            if now - float(task.get("last_ts") or 0.0) < 30:
                continue
            pending.append(task)
        if not pending:
            return

        self._adb_task_running.add(sequence_key)
        device = cfg.last_device
        package = cfg.package_name

        def work() -> list[tuple[str, bool, str, float]]:
            results: list[tuple[str, bool, str, float]] = []
            # The list order is the execution order. Keep startup commands serial so
            # power/permission setup finishes before app launch and HOME navigation.
            for task in pending:
                task_id = str(task.get("id") or "")
                command = str(task.get("command") or "").strip()
                ok, detail = self.tools.run_safe_adb_command(device, command, package)
                results.append((task_id, ok, detail, time.time()))
                time.sleep(0.6)
            return results

        def done(results: list[tuple[str, bool, str, float]]) -> None:
            self._adb_task_running.discard(sequence_key)
            by_id = {str(task.get("id") or ""): task for task in cfg.adb_tasks or []}
            for task_id, ok, detail, finished_ts in results:
                task = by_id.get(task_id)
                if not task:
                    continue
                task["last_ts"] = finished_ts
                if ok:
                    task["last_run_id"] = int(run_id)
                compact = str(detail or "").replace("\r", " ").replace("\n", " · ")
                if len(compact) > 360:
                    compact = compact[:357] + "..."
                self.log(
                    f"{cfg.phone_name} · 启动ADB[{task.get('name','ADB 任务')}] "
                    f"{'成功' if ok else '失败'} · {compact}"
                )
            self.store.upsert(cfg)

        def failed(err: str) -> None:
            self._adb_task_running.discard(sequence_key)
            self.log(f"{cfg.phone_name} · 启动 ADB 任务序列异常: {err}")

        self.bg(f"启动ADB序列 {cfg.phone_name}", work, done, failed)

    def open_adb_tasks(self, profile_id: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title(f"ADB 自动任务 · {cfg.phone_name}")
        dlg.geometry("940x620")
        dlg.minsize(820, 520)
        dlg.transient(self.root)

        outer = ttk.Frame(dlg, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=2)
        outer.columnconfigure(1, weight=3)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="支持立即执行、每次新云机启动后、每天固定时间、每 N 小时。{device} 自动替换当前 ADB 地址，{package} 自动替换当前选择的 App 包名。启动任务严格按列表从上到下串行执行。",
            wraplength=880,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        left = ttk.LabelFrame(outer, text="任务列表", padding=8)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        task_list = tk.Listbox(left, exportselection=False)
        task_list.grid(row=0, column=0, sticky="nsew")
        task_scroll = ttk.Scrollbar(left, orient="vertical", command=task_list.yview)
        task_scroll.grid(row=0, column=1, sticky="ns")
        task_list.configure(yscrollcommand=task_scroll.set)

        right = ttk.LabelFrame(outer, text="任务设置", padding=10)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)

        v_name = tk.StringVar(value="App 后台联网白名单")
        v_command = tk.StringVar(value="adb -s {device} shell dumpsys deviceidle whitelist +{package}")
        v_trigger = tk.StringVar(value="每次云机启动后")
        v_value = tk.StringVar(value="")
        v_enabled = tk.BooleanVar(value=True)
        v_status = tk.StringVar(value=f"当前 App: {cfg.package_name} · ADB: {cfg.last_device or '未连接'}")

        ttk.Label(right, text="任务名").grid(row=0, column=0, sticky="w")
        ttk.Entry(right, textvariable=v_name).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(right, text="ADB 命令").grid(row=1, column=0, sticky="nw", pady=(10, 0))
        command_entry = ttk.Entry(right, textvariable=v_command)
        command_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))
        ttk.Label(
            right,
            text="示例：adb -s {device} shell dumpsys deviceidle whitelist +{package}",
            wraplength=500,
        ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        ttk.Label(right, text="触发方式").grid(row=3, column=0, sticky="w", pady=(12, 0))
        trigger_combo = ttk.Combobox(
            right,
            textvariable=v_trigger,
            values=["每次云机启动后", "每天固定时间", "每 N 小时"],
            state="readonly",
        )
        trigger_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(12, 0))
        ttk.Label(right, text="时间 / 小时").grid(row=4, column=0, sticky="w", pady=(8, 0))
        value_entry = ttk.Entry(right, textvariable=v_value)
        value_entry.grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(right, text="启用自动执行", variable=v_enabled).grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(right, textvariable=v_status, wraplength=500).grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))

        tasks = cfg.adb_tasks

        def trigger_code() -> tuple[str, str]:
            label = v_trigger.get()
            value = v_value.get().strip()
            if label == "每天固定时间":
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    raise ValueError("每天固定时间必须是 HH:MM，例如 04:30")
                return "daily", value
            if label == "每 N 小时":
                try:
                    hours = int(value)
                except Exception as exc:
                    raise ValueError("小时必须是 1 到 168 的整数") from exc
                if not 1 <= hours <= 168:
                    raise ValueError("小时必须是 1 到 168")
                return "interval", str(hours)
            return "startup", ""

        def render_tasks(select_id: str = "") -> None:
            task_list.delete(0, "end")
            selected_index = -1
            for i, task in enumerate(tasks):
                state = "✓" if bool(task.get("enabled", True)) else "×"
                next_text = ""
                if str(task.get("trigger")) in ("daily", "interval") and float(task.get("next_ts") or 0):
                    next_text = f" · 下次 {self._format_local_ts(float(task.get('next_ts') or 0))}"
                task_list.insert("end", f"{state} {task.get('name','ADB 任务')} · {self._adb_task_trigger_text(task)}{next_text}")
                if select_id and str(task.get("id")) == select_id:
                    selected_index = i
            if tasks:
                idx = selected_index if selected_index >= 0 else 0
                task_list.selection_set(idx)
                task_list.see(idx)
                load_selected()

        def load_selected(_event=None) -> None:
            selected = task_list.curselection()
            if not selected:
                return
            task = tasks[int(selected[0])]
            v_name.set(str(task.get("name") or "ADB 任务"))
            v_command.set(str(task.get("command") or ""))
            trigger = str(task.get("trigger") or "startup")
            v_trigger.set({"daily": "每天固定时间", "interval": "每 N 小时"}.get(trigger, "每次云机启动后"))
            v_value.set(str(task.get("value") or ""))
            v_enabled.set(bool(task.get("enabled", True)))
            last = float(task.get("last_ts") or 0)
            v_status.set(
                f"当前 App: {cfg.package_name} · ADB: {cfg.last_device or '未连接'}"
                + (f" · 上次执行 {self._format_local_ts(last)}" if last else "")
            )

        def validate_command(command: str) -> None:
            probe_address = cfg.last_device or "100.64.0.1:5555"
            self.tools._safe_adb_args(command, probe_address, cfg.package_name)

        def save_current() -> Optional[dict[str, Any]]:
            selected = task_list.curselection()
            if not selected:
                messagebox.showinfo(APP_NAME, "请先选择一条任务，或点“新增任务”。", parent=dlg)
                return None
            task = tasks[int(selected[0])]
            name = v_name.get().strip() or "ADB 任务"
            command = v_command.get().strip()
            try:
                validate_command(command)
                trigger, value = trigger_code()
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=dlg)
                return None
            task["name"] = name
            task["command"] = command
            task["trigger"] = trigger
            task["value"] = value
            task["enabled"] = bool(v_enabled.get())
            if trigger in ("daily", "interval"):
                task["next_ts"] = self._adb_task_next_ts(task)
            else:
                task["next_ts"] = 0.0
            self.store.upsert(cfg)
            render_tasks(str(task.get("id")))
            v_status.set("任务已保存")
            return task

        def add_task() -> None:
            task = {
                "id": uuid.uuid4().hex,
                "name": "App 后台联网白名单",
                "command": "adb -s {device} shell dumpsys deviceidle whitelist +{package}",
                "trigger": "startup",
                "value": "",
                "enabled": True,
                "next_ts": 0.0,
                "last_ts": 0.0,
                "last_run_id": 0,
            }
            tasks.append(task)
            self.store.upsert(cfg)
            render_tasks(task["id"])

        def delete_task() -> None:
            selected = task_list.curselection()
            if not selected:
                return
            idx = int(selected[0])
            task = tasks[idx]
            if not messagebox.askyesno(APP_NAME, f"删除 ADB 任务“{task.get('name','ADB 任务')}”？", parent=dlg):
                return
            tasks.pop(idx)
            self.store.upsert(cfg)
            render_tasks()

        def run_now() -> None:
            task = save_current()
            if not task:
                return
            if not cfg.last_device:
                messagebox.showinfo(APP_NAME, "这台手机还没有可用的 ADB 地址。", parent=dlg)
                return
            self._run_adb_task(cfg, task, "manual")
            v_status.set("已提交立即执行；结果会写入主窗口日志")

        def trigger_changed(_event=None) -> None:
            label = v_trigger.get()
            if label == "每天固定时间" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", v_value.get().strip()):
                v_value.set("04:00")
            elif label == "每 N 小时" and not v_value.get().strip().isdigit():
                v_value.set("4")
            elif label == "每次云机启动后":
                v_value.set("")

        task_list.bind("<<ListboxSelect>>", load_selected)
        trigger_combo.bind("<<ComboboxSelected>>", trigger_changed)

        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(bottom, text="＋ 新增任务", command=add_task).pack(side="left")
        ttk.Button(bottom, text="删除选中", command=delete_task).pack(side="left", padx=6)
        ttk.Button(bottom, text="保存任务", command=save_current).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="立即执行", command=run_now).pack(side="right")
        ttk.Button(bottom, text="关闭", command=dlg.destroy).pack(side="right", padx=6)

        render_tasks()
        if not tasks:
            add_task()

    def _auto_tick(self) -> None:
        if self._closing:
            return
        now = time.time()
        store_changed = False
        has_github = bool(self.var_token.get().strip() or self.tools.github_auth_token())

        for cfg in list(self.store.profiles):
            # Normal status monitoring remains independent for every phone.
            if cfg.auto_refresh:
                last = self._last_auto.get(cfg.profile_id, 0.0)
                if now - last >= max(3, cfg.refresh_seconds):
                    self._last_auto[cfg.profile_id] = now
                    self.refresh_profile(cfg.profile_id, quiet=True)

            # Timed ADB tasks are local-GUI schedules. If the phone is temporarily
            # offline, retry soon instead of discarding the scheduled occurrence.
            for task in list(cfg.adb_tasks or []):
                if not bool(task.get("enabled", True)):
                    continue
                trigger = str(task.get("trigger") or "startup")
                if trigger not in ("daily", "interval"):
                    continue
                if not float(task.get("next_ts") or 0.0):
                    task["next_ts"] = self._adb_task_next_ts(task, now)
                    store_changed = True
                if now >= float(task.get("next_ts") or 0.0):
                    if cfg.last_device:
                        self._run_adb_task(cfg, task, "schedule")
                    else:
                        task["next_ts"] = now + 60
                        store_changed = True

            # A rotation already in progress must be resumed even if the user later
            # disables the schedule; otherwise an old phone could be backed up and
            # never have its replacement started.
            if cfg.rotation_phase == "waiting_backup":
                if has_github:
                    self._poll_rotation(cfg)
                continue

            if not cfg.auto_rotate:
                continue
            if not cfg.rotate_next_ts:
                cfg.rotate_next_ts = self._next_rotation_ts(cfg, now)
                self._set_card(cfg.profile_id, "rotation", self._rotation_text(cfg))
                store_changed = True
                continue
            if now >= cfg.rotate_next_ts and has_github:
                self.rotate_profile(cfg.profile_id, automatic=True)

        if store_changed:
            self.store.save()
        self.root.after(2000, self._auto_tick)

    # ------------------------- local App/scrcpy actions -------------------------
    def apk_command_profile(self, profile_id: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg:
            return
        path = (cfg.local_apk_path or "").strip()
        if not path or not Path(path).is_file():
            selected = filedialog.askopenfilename(
                parent=self.root,
                title=f"为 {cfg.phone_name} 选择 APK",
                filetypes=[("Android APK", "*.apk"), ("所有文件", "*.*")],
            )
            if not selected:
                return
            cfg.local_apk_path = selected
            self.store.upsert(cfg)

        def show_command_dialog() -> None:
            command = self._build_apk_install_command(cfg)
            dlg = tk.Toplevel(self.root)
            dlg.title(f"APK 安装命令 · {cfg.phone_name}")
            dlg.geometry("860x320")
            dlg.minsize(720, 280)
            dlg.transient(self.root)
            frame = ttk.Frame(dlg, padding=14)
            frame.pack(fill="both", expand=True)
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=f"APK: {cfg.local_apk_path}", wraplength=800).grid(row=0, column=0, sticky="w")
            ttk.Label(
                frame,
                text=f"程序 ADB: {self.tools.adb or self.tools._managed_adb_path()}",
                wraplength=800,
            ).grid(row=1, column=0, sticky="w", pady=(6, 0))
            ttk.Label(
                frame,
                text=(
                    "下面是 Windows PowerShell 命令，可直接粘贴执行。带引号的 exe 路径前已经自动加 &；"
                    "兼容 Windows PowerShell 5.1，不使用 &&。"
                ),
                wraplength=800,
            ).grid(row=2, column=0, sticky="w", pady=(10, 4))
            command_box = tk.Text(frame, height=6, wrap="word")
            command_box.grid(row=3, column=0, sticky="nsew")
            frame.rowconfigure(3, weight=1)
            command_box.insert("1.0", command)
            command_box.configure(state="disabled")
            buttons = ttk.Frame(frame)
            buttons.grid(row=4, column=0, sticky="e", pady=(10, 0))
            ttk.Button(buttons, text="关闭", command=dlg.destroy).pack(side="right")
            ttk.Button(
                buttons,
                text="复制 PowerShell 安装命令",
                command=lambda: self._copy_to_clipboard(command),
            ).pack(side="right", padx=(0, 8))

        managed_adb = self.tools._managed_adb_path()
        if managed_adb.is_file():
            self.tools.adb = str(managed_adb)
            show_command_dialog()
            return

        self.log("正在准备程序自有 Google Platform Tools / adb.exe…")

        def adb_ready(result: tuple[bool, str]) -> None:
            ok, detail = result
            if not ok:
                messagebox.showerror(APP_NAME, detail, parent=self.root)
                return
            self.log(f"ADB 已准备：{detail}")
            show_command_dialog()

        self.bg(
            "准备程序自有 ADB",
            self.tools.ensure_managed_adb,
            adb_ready,
            lambda err: messagebox.showerror(APP_NAME, f"准备 ADB 失败：{err}", parent=self.root),
        )

    def open_scrcpy_profile(self, profile_id: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg or not cfg.last_device:
            messagebox.showinfo(APP_NAME, "还没有可用的 ADB 地址，请先刷新这台手机。")
            return
        self.bg(
            f"{cfg.phone_name} scrcpy",
            lambda: self.tools.launch_scrcpy(cfg.last_device),
            lambda r: self.log(f"{cfg.phone_name}: {r[1]}"),
        )

    def _choose_app_and_execute(self, profile_id: str, action: str) -> None:
        cfg = self.store.get(profile_id)
        if not cfg or not cfg.last_device:
            messagebox.showinfo(APP_NAME, "手机还没有连接。")
            return

        action_label = "启动" if action == "start" else "关闭"
        dlg = tk.Toplevel(self.root)
        dlg.title(f"{action_label} App · {cfg.phone_name}")
        dlg.geometry("620x520")
        dlg.minsize(520, 420)
        dlg.transient(self.root)

        search_var = tk.StringVar()
        status_var = tk.StringVar(value="正在读取这台手机已安装的第三方 App…")
        all_packages: list[str] = []
        visible_packages: list[str] = []

        frame = ttk.Frame(dlg, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, text=f"ADB: {cfg.last_device}").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(frame, textvariable=search_var)
        search_entry.grid(row=1, column=0, sticky="ew", pady=(10, 8))

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        app_list = tk.Listbox(list_frame, exportselection=False)
        app_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=app_list.yview)
        app_list.configure(yscrollcommand=app_scroll.set)
        app_list.grid(row=0, column=0, sticky="nsew")
        app_scroll.grid(row=0, column=1, sticky="ns")

        ttk.Label(frame, textvariable=status_var).grid(row=3, column=0, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=dlg.destroy).pack(side="right")

        def render_list(*_args) -> None:
            term = search_var.get().strip().lower()
            visible_packages[:] = [p for p in all_packages if not term or term in p.lower()]
            app_list.delete(0, "end")
            for package in visible_packages:
                app_list.insert("end", package)
            status_var.set(f"已读取 {len(all_packages)} 个第三方 App · 当前显示 {len(visible_packages)} 个")
            if not visible_packages:
                return
            preferred = cfg.package_name if cfg.package_name in visible_packages else visible_packages[0]
            try:
                idx = visible_packages.index(preferred)
            except ValueError:
                idx = 0
            app_list.selection_clear(0, "end")
            app_list.selection_set(idx)
            app_list.see(idx)

        def execute_selected() -> None:
            selected = app_list.curselection()
            if not selected:
                messagebox.showinfo(APP_NAME, "请先选择一个 App。", parent=dlg)
                return
            package = app_list.get(int(selected[0])).strip()
            if not PKG_RE.match(package):
                messagebox.showerror(APP_NAME, "选择的包名格式无效。", parent=dlg)
                return

            cfg.package_name = package
            cfg.package_history = [package] + [x for x in cfg.package_history if x != package]
            cfg.package_history = cfg.package_history[:40]
            self.store.upsert(cfg)
            self._set_card(profile_id, "app", f"{package}\n准备{action_label}…")
            dlg.destroy()

            fn = self.tools.start_package if action == "start" else self.tools.stop_package

            def done(result: tuple[bool, str]) -> None:
                ok, detail = result
                prefix = "成功" if ok else "失败"
                self.log(f"{cfg.phone_name} · {action_label} {package} {prefix}: {detail}")
                self.root.after(800, lambda: self.refresh_profile(profile_id))

            self.bg(
                f"{cfg.phone_name} {action_label} {package}",
                lambda: fn(cfg.last_device, package),
                done,
            )

        execute_button = ttk.Button(buttons, text=f"{action_label}选中 App", command=execute_selected)
        execute_button.pack(side="right", padx=(0, 8))
        app_list.bind("<Double-Button-1>", lambda _e: execute_selected())
        search_var.trace_add("write", render_list)
        search_entry.focus_set()

        def loaded(items: list[str]) -> None:
            all_packages[:] = sorted(dict.fromkeys(items))
            render_list()
            if not all_packages:
                status_var.set("这台手机没有检测到第三方 App。")
                execute_button.state(["disabled"])

        def failed(err: str) -> None:
            status_var.set(f"读取 App 失败：{err}")
            execute_button.state(["disabled"])

        self.bg(
            f"读取 {cfg.phone_name} 已安装 App",
            lambda: self.tools.list_third_party_packages(cfg.last_device),
            loaded,
            failed,
        )

    def start_app_profile(self, profile_id: str) -> None:
        self._choose_app_and_execute(profile_id, "start")

    def stop_app_profile(self, profile_id: str) -> None:
        self._choose_app_and_execute(profile_id, "stop")

    def on_close(self) -> None:
        self._closing = True
        try:
            self.store.save()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    CloudPhoneGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
