#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud Android Manager - GitHub Actions + Tailscale + ADB + scrcpy GUI.

Only Python's standard library is required. Designed for Windows + Tkinter.
GitHub token is session-only and is NEVER written to the config file.
"""

from __future__ import annotations

import datetime as dt
import glob
import io
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "Cloud Android Manager"
CONFIG_PATH = Path.home() / ".cloud_android_manager.json"
DEFAULT_PACKAGE = "com.temperaturecoin"
DEFAULT_APK_URL = (
    "https://github.com/usdt19908888286-bit/android-test/releases/download/"
    "cloud-phone-apk-cache/app-release.apk"
)
PKG_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
RUN_ID_RE = re.compile(r"-(\d+)$")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class AppConfig:
    repo: str = "usdt19908888286-bit/android-test"
    branch: str = "main"
    phone_id: str = "001"
    phone_name: str = "BICOIN-001"
    create_workflow: str = "cloud-phone-managed.yml"
    managed_workflow: str = "cloud-phone-managed.yml"
    apk_url: str = DEFAULT_APK_URL
    package_name: str = DEFAULT_PACKAGE
    package_history: list[str] = field(default_factory=lambda: [DEFAULT_PACKAGE])
    api_level: str = "35"
    target: str = "google_apis"
    arch: str = "x86_64"
    profile: str = "pixel_6"
    cores: str = "4"
    ram_mb: str = "8192"
    last_device: str = ""
    auto_refresh: bool = True
    refresh_seconds: int = 5

    @classmethod
    def load(cls) -> "AppConfig":
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = cls()
            for key in asdict(cfg):
                if key in data:
                    setattr(cfg, key, data[key])
            if DEFAULT_PACKAGE not in cfg.package_history:
                cfg.package_history.insert(0, DEFAULT_PACKAGE)
            return cfg
        except Exception:
            return cls()

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
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
            "User-Agent": "cloud-android-manager/1.0",
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
        self._request("POST", f"/actions/workflows/{wf}/dispatches", {"ref": ref, "inputs": inputs})

    def workflow_runs(self, workflow: str, branch: str, limit: int = 20) -> list[dict[str, Any]]:
        wf = urllib.parse.quote(workflow, safe="")
        qs = urllib.parse.urlencode(
            {"branch": branch, "event": "workflow_dispatch", "per_page": max(1, min(limit, 100))}
        )
        data = self._request("GET", f"/actions/workflows/{wf}/runs?{qs}") or {}
        return data.get("workflow_runs", [])

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
                    if name.endswith("/"):
                        continue
                    chunks.append(zf.read(name).decode("utf-8", errors="replace"))
                return "\n".join(chunks)
        except zipfile.BadZipFile:
            return data.decode("utf-8", errors="replace")

    def repo_secret_exists(self, name: str) -> Optional[bool]:
        try:
            data = self._request("GET", "/actions/secrets?per_page=100") or {}
            return any(item.get("name") == name for item in data.get("secrets", []))
        except RuntimeError:
            # Some fine-grained tokens can dispatch workflows but cannot list secret metadata.
            return None

    def user_repos(self) -> list[str]:
        url = (
            "https://api.github.com/user/repos?per_page=100&sort=updated&"
            "affiliation=owner,collaborator,organization_member"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cloud-android-manager/2.0",
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

    def workflows(self) -> list[str]:
        data = self._request("GET", "/actions/workflows?per_page=100") or {}
        return [item.get("path", "").split("/")[-1] for item in data.get("workflows", []) if item.get("path")]

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
        return self._which_or_glob(
            "adb",
            [
                r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe",
                r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_*\scrcpy-*\adb.exe",
                r"%USERPROFILE%\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_*\**\adb.exe",
            ],
        )

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

    def set_github_secret(self, repo: str, token: str, name: str = "AVD_BACKUP_KEY") -> tuple[bool, str]:
        if not self.gh:
            ok, detail = self.ensure_github_cli()
            if not ok:
                return False, detail
        key = secrets.token_urlsafe(64)
        env = os.environ.copy()
        env["GH_TOKEN"] = token
        try:
            cp = subprocess.run(
                [self.gh, "secret", "set", name, "-R", repo],
                input=key,
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
        self.root.geometry("1180x820")
        self.root.minsize(1020, 720)

        self.cfg = AppConfig.load()
        self.tools = LocalTools()
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.latest_run_id: Optional[int] = None
        self.latest_run_status = "-"
        self._closing = False

        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        session_token = env_token or self.tools.github_auth_token()
        self.var_token = tk.StringVar(value=session_token)  # internal only; never displayed/saved
        self.var_github_status = tk.StringVar(value="GitHub: 正在检查登录状态…")
        self.var_repo = tk.StringVar(value=self.cfg.repo)
        self.var_branch = tk.StringVar(value=self.cfg.branch)
        self.var_phone = tk.StringVar(value=self.cfg.phone_id)
        self.var_phone_name = tk.StringVar(value=self.cfg.phone_name)
        self.var_workflow = tk.StringVar(value=self.cfg.managed_workflow or self.cfg.create_workflow)
        self.var_create_wf = self.var_workflow
        self.var_managed_wf = self.var_workflow
        self.var_apk = tk.StringVar(value=self.cfg.apk_url)
        self.var_package = tk.StringVar(value=self.cfg.package_name)
        self.var_api_level = tk.StringVar(value=self.cfg.api_level)
        self.var_target = tk.StringVar(value=self.cfg.target)
        self.var_arch = tk.StringVar(value=self.cfg.arch)
        self.var_profile = tk.StringVar(value=self.cfg.profile)
        self.var_cores = tk.StringVar(value=self.cfg.cores)
        self.var_ram_mb = tk.StringVar(value=self.cfg.ram_mb)
        self.var_device = tk.StringVar(value=self.cfg.last_device)
        self.var_auto = tk.BooleanVar(value=self.cfg.auto_refresh)
        self.var_interval = tk.IntVar(value=self.cfg.refresh_seconds)
        self.var_run = tk.StringVar(value="Run: -")
        self.var_node = tk.StringVar(value="Tailscale: -")

        # Detailed values retained for logging and future diagnostics.
        self.var_adb = tk.StringVar(value="ADB: -")
        self.var_boot = tk.StringVar(value="Boot: -")
        self.var_model = tk.StringVar(value="Model: -")
        self.var_app = tk.StringVar(value="App: -")
        self.var_app_cpu = tk.StringVar(value="App CPU: -")
        self.var_app_mem = tk.StringVar(value="App MEM: -")
        self.var_dev_cpu = tk.StringVar(value="Android CPU: -")
        self.var_dev_mem = tk.StringVar(value="Android MEM: -")
        self.var_host_cpu = tk.StringVar(value="Runner CPU: -")
        self.var_host_mem = tk.StringVar(value="Runner MEM: -")
        self.var_qemu = tk.StringVar(value="QEMU: -")

        # Human-friendly dashboard cards.
        self.var_card_cloud = tk.StringVar(value="未连接\n等待发现云机")
        self.var_card_android = tk.StringVar(value="未检测\nAndroid 状态未知")
        self.var_card_app = tk.StringVar(value=f"未检测\n{self.var_package.get()}")
        self.var_card_runner = tk.StringVar(value="未检测\n云服务器资源未知")
        self.var_card_qemu = tk.StringVar(value="未检测\n模拟器资源未知")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._drain_queue)
        self.root.after(400, self.refresh_github_auth)
        self.root.after(800, self._auto_tick)
        self.log(f"ADB: {self.tools.adb or '未找到'}")
        self.log(f"scrcpy: {self.tools.scrcpy or '未找到'}")
        self.log(f"Tailscale: {self.tools.tailscale or '未找到'}")
        self.log(f"GitHub CLI: {self.tools.gh or '未找到'}")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        # GitHub authorization bar: no token textbox is exposed.
        auth = ttk.Frame(outer)
        auth.pack(fill="x", pady=(0, 10))
        ttk.Label(auth, text="GitHub", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Label(auth, textvariable=self.var_github_status).pack(side="left", padx=(10, 14))
        ttk.Button(auth, text="GitHub 授权登录", command=self.github_login).pack(side="left", padx=3)
        ttk.Button(auth, text="刷新登录", command=self.refresh_github_auth).pack(side="left", padx=3)
        ttk.Button(auth, text="退出登录", command=self.github_logout).pack(side="left", padx=3)
        ttk.Label(auth, text="凭证由 GitHub CLI 安全保存，本程序不显示 Token").pack(side="right")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        dashboard = ttk.Frame(notebook, padding=12)
        config = ttk.Frame(notebook, padding=12)
        apptab = ttk.Frame(notebook, padding=12)
        logtab = ttk.Frame(notebook, padding=6)
        notebook.add(dashboard, text="控制台")
        notebook.add(config, text="手机配置")
        notebook.add(apptab, text="App 控制")
        notebook.add(logtab, text="运行日志")

        # ---------------- Dashboard ----------------
        header = ttk.Frame(dashboard)
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.var_phone_name, font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.var_run).pack(side="left", padx=(16, 8))
        ttk.Label(header, textvariable=self.var_node).pack(side="left", padx=8)

        actions = ttk.Frame(dashboard)
        actions.pack(fill="x", pady=(12, 10))
        ttk.Button(actions, text="＋ 创建新手机", command=self.create_phone).pack(side="left", padx=3)
        ttk.Button(actions, text="↺ 从备份恢复", command=self.restore_phone).pack(side="left", padx=3)
        ttk.Button(actions, text="⬆ 备份当前手机", command=self.request_backup).pack(side="left", padx=3)
        ttk.Button(actions, text="▶ 打开 scrcpy", command=self.open_scrcpy).pack(side="left", padx=3)
        ttk.Button(actions, text="刷新状态", command=self.health_check).pack(side="left", padx=(14, 3))
        ttk.Button(actions, text="刷新 Run", command=self.refresh_run).pack(side="left", padx=3)
        ttk.Button(actions, text="取消 Run", command=self.cancel_run).pack(side="left", padx=3)

        cards = ttk.Frame(dashboard)
        cards.pack(fill="x", pady=(2, 12))
        for i in range(5):
            cards.columnconfigure(i, weight=1, uniform="cards")

        def add_card(col: int, title: str, variable: tk.StringVar) -> None:
            box = ttk.LabelFrame(cards, text=title, padding=(10, 12))
            box.grid(row=0, column=col, sticky="nsew", padx=4)
            ttk.Label(
                box,
                textvariable=variable,
                justify="center",
                anchor="center",
                font=("Segoe UI", 10, "bold"),
                wraplength=185,
            ).pack(fill="both", expand=True)

        add_card(0, "云机连接", self.var_card_cloud)
        add_card(1, "Android", self.var_card_android)
        add_card(2, "当前 App", self.var_card_app)
        add_card(3, "云服务器", self.var_card_runner)
        add_card(4, "QEMU 模拟器", self.var_card_qemu)

        conn = ttk.LabelFrame(dashboard, text="连接", padding=10)
        conn.pack(fill="x")
        ttk.Label(conn, text="ADB 地址").pack(side="left")
        ttk.Entry(conn, textvariable=self.var_device, width=28).pack(side="left", padx=6)
        ttk.Button(conn, text="自动发现", command=self.discover_device).pack(side="left", padx=3)
        ttk.Button(conn, text="检测手机", command=self.health_check).pack(side="left", padx=3)
        ttk.Checkbutton(conn, text="自动刷新", variable=self.var_auto).pack(side="left", padx=(18, 3))
        ttk.Spinbox(conn, from_=2, to=60, textvariable=self.var_interval, width=5).pack(side="left", padx=3)
        ttk.Label(conn, text="秒").pack(side="left")

        # ---------------- Configuration ----------------
        ghbox = ttk.LabelFrame(config, text="GitHub / 工作流", padding=10)
        ghbox.pack(fill="x")
        ghbox.columnconfigure(1, weight=1)
        ghbox.columnconfigure(3, weight=1)
        ttk.Label(ghbox, text="仓库").grid(row=0, column=0, sticky="w")
        self.repo_combo = ttk.Combobox(ghbox, textvariable=self.var_repo, width=42)
        self.repo_combo.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(ghbox, text="读取我的仓库", command=self.load_repositories).grid(row=0, column=2, padx=(0, 12))
        ttk.Label(ghbox, text="分支").grid(row=0, column=3, sticky="e")
        ttk.Entry(ghbox, textvariable=self.var_branch, width=18).grid(row=0, column=4, sticky="ew", padx=6)

        ttk.Label(ghbox, text="工作流").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.workflow_combo = ttk.Combobox(ghbox, textvariable=self.var_workflow, width=42)
        self.workflow_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(ghbox, text="读取工作流", command=self.load_workflows).grid(row=1, column=2, padx=(0, 12), pady=(8, 0))
        ttk.Button(ghbox, text="初始化备份密钥", command=self.init_backup_key).grid(row=1, column=4, sticky="e", padx=6, pady=(8, 0))

        phone = ttk.LabelFrame(config, text="手机身份", padding=10)
        phone.pack(fill="x", pady=(10, 0))
        for col in (1, 3, 5):
            phone.columnconfigure(col, weight=1)
        ttk.Label(phone, text="手机名称").grid(row=0, column=0, sticky="w")
        ttk.Entry(phone, textvariable=self.var_phone_name).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(phone, text="Phone ID").grid(row=0, column=2, sticky="w")
        ttk.Entry(phone, textvariable=self.var_phone, width=12).grid(row=0, column=3, sticky="ew", padx=6)
        ttk.Label(phone, text="默认包名").grid(row=0, column=4, sticky="w")
        ttk.Entry(phone, textvariable=self.var_package).grid(row=0, column=5, sticky="ew", padx=6)

        apkbox = ttk.LabelFrame(config, text="APK", padding=10)
        apkbox.pack(fill="x", pady=(10, 0))
        apkbox.columnconfigure(1, weight=1)
        ttk.Label(apkbox, text="APK URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(apkbox, textvariable=self.var_apk).grid(row=0, column=1, sticky="ew", padx=6)

        advanced = ttk.LabelFrame(config, text="Android / 云机规格（高级）", padding=10)
        advanced.pack(fill="x", pady=(10, 0))
        for col in (1, 3, 5, 7):
            advanced.columnconfigure(col, weight=1)
        fields = [
            ("Android API", self.var_api_level),
            ("设备 Profile", self.var_profile),
            ("CPU 核数", self.var_cores),
            ("内存 MB", self.var_ram_mb),
            ("Target", self.var_target),
            ("架构", self.var_arch),
        ]
        for i, (label, variable) in enumerate(fields):
            row = i // 4
            pair = i % 4
            col = pair * 2
            ttk.Label(advanced, text=label).grid(row=row, column=col, sticky="w", pady=4)
            ttk.Entry(advanced, textvariable=variable).grid(row=row, column=col + 1, sticky="ew", padx=6, pady=4)

        ttk.Label(
            config,
            text="所有字段都会自动记忆。恢复完整 AVD 时建议保持 API / 架构 / Profile 与备份时一致。",
        ).pack(anchor="w", pady=(10, 0))

        # ---------------- App control ----------------
        ttk.Label(apptab, text="App 控制", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            apptab,
            text="默认 BICOIN：com.temperaturecoin。可以输入任何已安装 App 包名，历史包名会记住。",
        ).pack(anchor="w", pady=(4, 12))
        appbox = ttk.Frame(apptab)
        appbox.pack(fill="x")
        ttk.Label(appbox, text="包名").pack(side="left")
        self.package_combo = ttk.Combobox(
            appbox,
            textvariable=self.var_package,
            values=self.cfg.package_history,
            width=46,
        )
        self.package_combo.pack(side="left", padx=6)
        self.package_combo.bind("<<ComboboxSelected>>", lambda _e: self.remember_package())
        ttk.Button(appbox, text="记住", command=self.remember_package).pack(side="left", padx=3)
        ttk.Button(appbox, text="读取已装 App", command=self.load_packages).pack(side="left", padx=3)
        ttk.Button(appbox, text="启动", command=self.start_app).pack(side="left", padx=(14, 3))
        ttk.Button(appbox, text="关闭", command=self.stop_app).pack(side="left", padx=3)
        ttk.Button(appbox, text="重启", command=self.restart_app).pack(side="left", padx=3)
        ttk.Button(appbox, text="打开 scrcpy", command=self.open_scrcpy).pack(side="left", padx=3)

        # ---------------- Logs ----------------
        self.log_text = tk.Text(logtab, wrap="word", height=18)
        scroll = ttk.Scrollbar(logtab, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")

    def bg(self, name: str, func: Callable[[], Any], callback: Optional[Callable[[Any], None]] = None) -> None:
        def runner() -> None:
            try:
                value = func()
                self.q.put(("ok", (name, value, callback)))
            except Exception as exc:
                self.q.put(("err", (name, str(exc))))
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
                    name, err = payload
                    self.log(f"{name}: 失败 - {err}")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_queue)

    def api(self) -> GitHubAPI:
        token = (
            os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or self.var_token.get().strip()
            or self.tools.github_auth_token()
        )
        if not token:
            raise RuntimeError("请先点击“GitHub 授权登录”完成授权")
        self.var_token.set(token)
        return GitHubAPI(self.var_repo.get(), token)

    def _phone_id(self) -> str:
        pid = self.var_phone.get().strip()
        if not pid.isdigit():
            raise ValueError("Phone ID 只能是数字")
        return pid

    def _package(self) -> str:
        pkg = self.var_package.get().strip()
        if not PKG_RE.match(pkg):
            raise ValueError("Android 包名格式无效")
        return pkg

    def save_config(self) -> None:
        self.cfg.repo = self.var_repo.get().strip()
        self.cfg.branch = self.var_branch.get().strip() or "main"
        self.cfg.phone_id = self.var_phone.get().strip() or "001"
        self.cfg.phone_name = self.var_phone_name.get().strip() or f"Phone-{self.cfg.phone_id}"
        workflow = self.var_workflow.get().strip() or "cloud-phone-managed.yml"
        self.cfg.create_workflow = workflow
        self.cfg.managed_workflow = workflow
        self.cfg.apk_url = self.var_apk.get().strip() or DEFAULT_APK_URL
        self.cfg.package_name = self.var_package.get().strip() or DEFAULT_PACKAGE
        self.cfg.api_level = self.var_api_level.get().strip() or "35"
        self.cfg.target = self.var_target.get().strip() or "google_apis"
        self.cfg.arch = self.var_arch.get().strip() or "x86_64"
        self.cfg.profile = self.var_profile.get().strip() or "pixel_6"
        self.cfg.cores = self.var_cores.get().strip() or "4"
        self.cfg.ram_mb = self.var_ram_mb.get().strip() or "8192"
        self.cfg.last_device = self.var_device.get().strip()
        self.cfg.auto_refresh = bool(self.var_auto.get())
        try:
            self.cfg.refresh_seconds = max(2, int(self.var_interval.get()))
        except Exception:
            self.cfg.refresh_seconds = 5
        self.cfg.save()

    def remember_package(self) -> None:
        try:
            pkg = self._package()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        hist = [pkg] + [x for x in self.cfg.package_history if x != pkg]
        self.cfg.package_history = hist[:30]
        self.cfg.package_name = pkg
        self.package_combo["values"] = self.cfg.package_history
        self.save_config()
        self.log(f"已记住包名: {pkg}")

    def refresh_github_auth(self) -> None:
        def work() -> tuple[str, str, bool]:
            has_cli = bool(self.tools._find_gh())
            if has_cli:
                self.tools.gh = self.tools._find_gh()
            account = self.tools.github_account() if has_cli else ""
            token = self.tools.github_auth_token() if account else ""
            return account, token, has_cli

        def done(result: tuple[str, str, bool]) -> None:
            account, token, has_cli = result
            if account and token:
                self.var_token.set(token)
                self.var_github_status.set(f"GitHub: 已登录 @{account}")
            else:
                self.var_token.set("")
                if has_cli:
                    self.var_github_status.set("GitHub: 未登录")
                else:
                    self.var_github_status.set("GitHub: 未安装 CLI（授权时自动安装）")

        self.bg("检查 GitHub 登录", work, done)

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception as exc:
            self.log(f"复制到剪贴板失败: {exc}")

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
        except Exception:
            return
        try:
            value = self.root.clipboard_get().strip()
            if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", value):
                code_var.set(value)
        except Exception:
            pass
        self.root.after(350, self._poll_github_login_code)

    def _show_github_login_dialog(self) -> None:
        existing = getattr(self, "_github_login_dialog", None)
        if existing:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        win.title("GitHub 授权登录")
        win.geometry("620x300")
        win.resizable(False, False)
        win.transient(self.root)
        self._github_login_dialog = win
        self._github_login_code_var = tk.StringVar(value="正在生成一次性验证码…")
        self._github_login_status_var = tk.StringVar(value="正在准备 GitHub CLI；不会自动打开浏览器")
        url = "https://github.com/login/device"

        body = ttk.Frame(win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="GitHub 设备授权", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            body,
            text="浏览器不会自动弹出。复制下面的地址，或由你手动点击打开，然后输入一次性验证码。",
            wraplength=570,
        ).pack(anchor="w", pady=(6, 14))

        ttk.Label(body, text="登录地址").pack(anchor="w")
        url_row = ttk.Frame(body)
        url_row.pack(fill="x", pady=(4, 10))
        url_entry = ttk.Entry(url_row)
        url_entry.insert(0, url)
        url_entry.configure(state="readonly")
        url_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(url_row, text="复制链接", command=lambda: self._copy_to_clipboard(url)).pack(side="left", padx=(6, 0))
        ttk.Button(url_row, text="手动打开", command=self._open_github_login_page).pack(side="left", padx=(6, 0))

        ttk.Label(body, text="一次性验证码").pack(anchor="w")
        code_row = ttk.Frame(body)
        code_row.pack(fill="x", pady=(4, 10))
        ttk.Label(
            code_row,
            textvariable=self._github_login_code_var,
            font=("Consolas", 16, "bold"),
        ).pack(side="left")
        ttk.Button(
            code_row,
            text="复制验证码",
            command=lambda: self._copy_to_clipboard(self._github_login_code_var.get()),
        ).pack(side="left", padx=(12, 0))

        ttk.Label(body, textvariable=self._github_login_status_var).pack(anchor="w", pady=(2, 10))
        footer = ttk.Frame(body)
        footer.pack(fill="x")
        ttk.Button(footer, text="检查登录状态", command=self.refresh_github_auth).pack(side="left")
        ttk.Button(footer, text="关闭", command=win.destroy).pack(side="right")

        self.root.after(350, self._poll_github_login_code)

    def github_login(self) -> None:
        self._show_github_login_dialog()
        status_var = getattr(self, "_github_login_status_var", None)
        if status_var:
            status_var.set("正在准备 GitHub CLI 和一次性验证码；不会自动打开浏览器")

        def work() -> tuple[bool, str]:
            return self.tools.github_login()

        def done(result: tuple[bool, str]) -> None:
            ok, detail = result
            self.log(f"GitHub 授权登录: {'成功' if ok else '未完成'} {detail}")
            status = getattr(self, "_github_login_status_var", None)
            if status:
                status.set("授权完成" if ok else detail)
            self.refresh_github_auth()
            if ok:
                self.load_repositories()

        self.bg("GitHub 授权登录", work, done)

    def github_logout(self) -> None:
        if not messagebox.askyesno(APP_NAME, "确定退出当前 GitHub 授权吗？"):
            return

        def work() -> tuple[bool, str]:
            return self.tools.github_logout()

        def done(result: tuple[bool, str]) -> None:
            self.log(result[1])
            self.refresh_github_auth()

        self.bg("退出 GitHub", work, done)

    def load_repositories(self) -> None:
        def work() -> list[str]:
            return self.api().user_repos()

        def done(items: list[str]) -> None:
            self.repo_combo["values"] = items
            self.log(f"已读取 {len(items)} 个可访问仓库")

        self.bg("读取仓库", work, done)

    def load_workflows(self) -> None:
        def work() -> list[str]:
            return self.api().workflows()

        def done(items: list[str]) -> None:
            self.workflow_combo["values"] = items
            self.log(f"已读取 {len(items)} 个工作流")

        self.bg("读取工作流", work, done)

    def _workflow_inputs(self, mode: str) -> dict[str, str]:
        phone_id = self._phone_id()
        package = self._package()
        return {
            "mode": mode,
            "phone_id": phone_id,
            "phone_name": self.var_phone_name.get().strip() or f"Phone-{phone_id}",
            "apk_url": self.var_apk.get().strip() or DEFAULT_APK_URL,
            "package_name": package,
            "api_level": self.var_api_level.get().strip() or "35",
            "target": self.var_target.get().strip() or "google_apis",
            "arch": self.var_arch.get().strip() or "x86_64",
            "profile": self.var_profile.get().strip() or "pixel_6",
            "cores": self.var_cores.get().strip() or "4",
            "ram_mb": self.var_ram_mb.get().strip() or "8192",
        }

    def init_backup_key(self) -> None:
        repo = self.var_repo.get().strip()
        token = (
            os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or self.var_token.get().strip()
            or self.tools.github_auth_token()
        )
        if not token:
            messagebox.showerror(APP_NAME, "请先点击“GitHub 授权登录”")
            return

        def work() -> tuple[bool, str]:
            return self.tools.set_github_secret(repo, token)

        def done(result: tuple[bool, str]) -> None:
            ok, text = result
            self.log(f"初始化备份密钥: {'成功' if ok else '失败'} {text}")
            if ok:
                messagebox.showinfo(APP_NAME, "备份加密密钥已写入 GitHub Actions Secret。")

        self.bg("初始化备份密钥", work, done)

    def create_phone(self) -> None:
        self.save_config()

        def work() -> str:
            api = self.api()
            wf = self.var_workflow.get().strip() or "cloud-phone-managed.yml"
            inputs = self._workflow_inputs("new")
            notes = []
            exists = api.repo_secret_exists("AVD_BACKUP_KEY")
            if exists is False:
                token = self.var_token.get().strip() or self.tools.github_auth_token()
                ok, detail = self.tools.set_github_secret(self.var_repo.get().strip(), token)
                if ok:
                    notes.append("备份密钥已自动初始化")
                else:
                    notes.append(f"备份密钥初始化失败: {detail}")
            api.dispatch(wf, self.var_branch.get().strip() or "main", inputs)
            suffix = ("；" + "；".join(notes)) if notes else ""
            return f"已创建 {inputs['phone_name']}（Phone ID {inputs['phone_id']}）" + suffix

        self.bg("创建新手机", work, lambda v: (self.log(v), self.root.after(1500, self.refresh_run)))

    def restore_phone(self) -> None:
        self.save_config()

        def work() -> str:
            wf = self.var_workflow.get().strip() or "cloud-phone-managed.yml"
            inputs = self._workflow_inputs("restore")
            self.api().dispatch(wf, self.var_branch.get().strip() or "main", inputs)
            return f"已触发恢复：{inputs['phone_name']}（Phone ID {inputs['phone_id']}）"

        self.bg("从备份恢复", work, lambda v: (self.log(v), self.root.after(1500, self.refresh_run)))

    def request_backup(self) -> None:
        if not self.latest_run_id:
            messagebox.showwarning(APP_NAME, "请先刷新 Run，确认当前云机 Run ID")
            return
        run_id = self.latest_run_id
        def work() -> str:
            issue = self.api().request_backup(self._phone_id(), run_id)
            return f"已发送备份命令到 Issue #{issue.get('number', '?')}，目标 Run {run_id}"
        self.bg("请求备份", work, self.log)

    def refresh_run(self) -> None:
        self.save_config()
        def work() -> dict[str, Any]:
            api = self.api()
            runs: list[dict[str, Any]] = []
            for wf in [self.var_managed_wf.get().strip(), self.var_create_wf.get().strip()]:
                if not wf:
                    continue
                try:
                    runs.extend(api.workflow_runs(wf, self.var_branch.get().strip() or "main", 10))
                except RuntimeError as exc:
                    if "404" not in str(exc):
                        raise
            if not runs:
                return {}
            runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            return runs[0]

        def done(run: dict[str, Any]) -> None:
            if not run:
                self.var_run.set("Run: 未找到")
                return
            self.latest_run_id = int(run["id"])
            self.latest_run_status = str(run.get("status") or "?")
            conclusion = run.get("conclusion") or ""
            sha = str(run.get("head_sha") or "")[:7]
            self.var_run.set(f"Run: {self.latest_run_id}  {self.latest_run_status}/{conclusion or '-'}  {sha}")
            self.log(f"最新 Run {self.latest_run_id}: {self.latest_run_status}/{conclusion or '-'}")
            self.discover_device()
        self.bg("刷新 Run", work, done)

    def cancel_run(self) -> None:
        if not self.latest_run_id:
            return
        rid = self.latest_run_id
        self.bg("取消 Run", lambda: (self.api().cancel(rid), f"已请求取消 Run {rid}")[1], self.log)

    def discover_device(self) -> None:
        phone = self.var_phone.get().strip() or "001"
        phone_name = self.var_phone_name.get().strip()
        rid = self.latest_run_id

        def work() -> tuple[str, str]:
            ip, host = self.tools.discover_phone(phone, rid, phone_name)
            if not ip and rid:
                # Fallback: run logs may contain TAILSCALE_ADB=x.x.x.x:5555.
                try:
                    text = self.api().run_logs_text(rid)
                    match = re.search(r"TAILSCALE_ADB=(100(?:\.\d{1,3}){3}):5555", text)
                    if match:
                        return match.group(1), "来自 GitHub 日志"
                except Exception:
                    pass
            return ip, host

        def done(data: tuple[str, str]) -> None:
            ip, host = data
            if not ip:
                self.var_node.set("Tailscale: 未发现")
                self.var_card_cloud.set("未发现\n请先刷新 Run")
                return
            address = f"{ip}:5555"
            self.var_device.set(address)
            self.var_node.set(f"Tailscale: {host or ip}")
            self.var_card_cloud.set(f"已发现\n{address}")
            self.cfg.last_device = address
            self.save_config()
            self.log(f"发现云机 {host}: {address}")
            self.health_check()

        self.bg("自动发现云机", work, done)

    def health_check(self) -> None:
        address = self.var_device.get().strip()
        if not address:
            return
        try:
            pkg = self._package()
        except Exception:
            pkg = DEFAULT_PACKAGE
        def work() -> dict[str, str]:
            h = self.tools.device_health(address, pkg)
            ip = address.split(":", 1)[0].strip()
            h.update(self.tools.runner_status(ip))
            return h
        self.bg("检测手机", work, self._apply_health)

    def _apply_health(self, h: dict[str, str]) -> None:
        adb_state = h.get("adb", "-")
        boot = h.get("boot", "-")
        model = h.get("model", "-")
        android = h.get("android", "-")
        app_state = h.get("app", "-")
        app_pid = h.get("app_pid", "-")
        app_cpu = h.get("app_cpu", "-")
        app_mem = h.get("app_mem", "-")
        host_cpu = h.get("host_cpu", "-")
        host_mem = h.get("host_mem", "-")
        qemu = h.get("qemu", "-")

        self.var_adb.set(f"ADB: {adb_state}")
        self.var_boot.set(f"Boot: {boot}")
        self.var_model.set(f"Model: {model} / Android {android}")
        self.var_app.set(f"App: {app_state}  PID {app_pid}")
        self.var_app_cpu.set(f"App CPU: {app_cpu}")
        self.var_app_mem.set(f"App MEM: {app_mem}")
        self.var_dev_cpu.set(f"Android CPU: {h.get('device_cpu', '-')}")
        self.var_dev_mem.set(f"Android MEM: {h.get('device_mem', '-')}")
        self.var_host_cpu.set(f"Runner CPU: {host_cpu}")
        self.var_host_mem.set(f"Runner MEM: {host_mem}")
        self.var_qemu.set(f"QEMU: {qemu}")

        addr = self.var_device.get().strip() or "未发现地址"
        cloud_title = "已连接" if adb_state == "已连接" else adb_state
        self.var_card_cloud.set(f"{cloud_title}\n{addr}")
        android_title = "已启动" if boot == "1" else f"Boot {boot}"
        self.var_card_android.set(f"{android_title}\nAndroid {android} · {model}")
        self.var_card_app.set(f"{app_state} · PID {app_pid}\nCPU {app_cpu} · MEM {app_mem}")
        self.var_card_runner.set(f"CPU {host_cpu}\n内存 {host_mem}")
        self.var_card_qemu.set(qemu)

    def load_packages(self) -> None:
        address = self.var_device.get().strip()
        if not address:
            return
        def done(items: list[str]) -> None:
            if not items:
                self.log("没有读取到第三方包名")
                return
            merged = list(dict.fromkeys(items + self.cfg.package_history))
            self.cfg.package_history = merged[:100]
            self.package_combo["values"] = self.cfg.package_history
            self.save_config()
            self.log("已装第三方 App: " + ", ".join(items))
        self.bg("读取已装 App", lambda: self.tools.list_third_party_packages(address), done)

    def start_app(self) -> None:
        try:
            pkg = self._package()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc)); return
        self.remember_package()
        address = self.var_device.get().strip()
        self.bg("启动 App", lambda: self.tools.start_package(address, pkg), lambda x: self._action_result("启动 App", x))

    def stop_app(self) -> None:
        try:
            pkg = self._package()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc)); return
        self.remember_package()
        address = self.var_device.get().strip()
        self.bg("关闭 App", lambda: self.tools.stop_package(address, pkg), lambda x: self._action_result("关闭 App", x))

    def restart_app(self) -> None:
        try:
            pkg = self._package()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc)); return
        address = self.var_device.get().strip()
        def work() -> tuple[bool, str]:
            self.tools.stop_package(address, pkg)
            time.sleep(0.7)
            return self.tools.start_package(address, pkg)
        self.bg("重启 App", work, lambda x: self._action_result("重启 App", x))

    def _action_result(self, name: str, result: tuple[bool, str]) -> None:
        ok, text = result
        self.log(f"{name}: {'成功' if ok else '失败'} {text[:400]}")
        self.root.after(600, self.health_check)

    def open_scrcpy(self) -> None:
        address = self.var_device.get().strip()
        self.bg("打开 scrcpy", lambda: self.tools.launch_scrcpy(address), lambda x: self._action_result("scrcpy", x))

    def _auto_tick(self) -> None:
        if self._closing:
            return
        if self.var_auto.get() and self.var_token.get().strip():
            self.refresh_run()
        try:
            seconds = max(2, int(self.var_interval.get()))
        except Exception:
            seconds = 5
        self.root.after(seconds * 1000, self._auto_tick)

    def on_close(self) -> None:
        self._closing = True
        try:
            self.save_config()
        finally:
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
