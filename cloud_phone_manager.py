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
    create_workflow: str = "cloud-phone-managed.yml"
    managed_workflow: str = "cloud-phone-managed.yml"
    apk_url: str = DEFAULT_APK_URL
    package_name: str = DEFAULT_PACKAGE
    package_history: list[str] = field(default_factory=lambda: [DEFAULT_PACKAGE])
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
        return self._which_or_glob(
            "gh",
            [r"C:\Program Files\GitHub CLI\gh.exe"],
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

    def discover_phone(self, phone_id: str, run_id: Optional[int] = None) -> tuple[str, str]:
        candidates: list[tuple[int, str, str]] = []
        for node in self.tailscale_nodes():
            host = str(node.get("HostName") or node.get("DNSName") or "").rstrip(".")
            ips = node.get("TailscaleIPs") or []
            ip = next((x for x in ips if isinstance(x, str) and ":" not in x), "")
            if not host or not ip:
                continue
            if f"phone{phone_id}-" not in host:
                continue
            if run_id is not None and str(run_id) not in host:
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

    def set_github_secret(self, repo: str, token: str, name: str = "AVD_BACKUP_KEY") -> tuple[bool, str]:
        if not self.gh:
            return False, "未找到 GitHub CLI (gh.exe)"
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
        self.root.minsize(1000, 700)

        self.cfg = AppConfig.load()
        self.tools = LocalTools()
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.latest_run_id: Optional[int] = None
        self.latest_run_status = "-"
        self._closing = False

        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        session_token = env_token or self.tools.github_auth_token()
        self.var_token = tk.StringVar(value=session_token)
        self.var_repo = tk.StringVar(value=self.cfg.repo)
        self.var_branch = tk.StringVar(value=self.cfg.branch)
        self.var_phone = tk.StringVar(value=self.cfg.phone_id)
        self.var_create_wf = tk.StringVar(value=self.cfg.create_workflow)
        self.var_managed_wf = tk.StringVar(value=self.cfg.managed_workflow)
        self.var_apk = tk.StringVar(value=self.cfg.apk_url)
        self.var_package = tk.StringVar(value=self.cfg.package_name)
        self.var_device = tk.StringVar(value=self.cfg.last_device)
        self.var_auto = tk.BooleanVar(value=self.cfg.auto_refresh)
        self.var_interval = tk.IntVar(value=self.cfg.refresh_seconds)
        self.var_run = tk.StringVar(value="Run: -")
        self.var_node = tk.StringVar(value="Tailscale: -")
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

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._drain_queue)
        self.root.after(800, self._auto_tick)
        self.log(f"ADB: {self.tools.adb or '未找到'}")
        self.log(f"scrcpy: {self.tools.scrcpy or '未找到'}")
        self.log(f"Tailscale: {self.tools.tailscale or '未找到'}")
        self.log(f"GitHub CLI: {self.tools.gh or '未找到（仅影响一键初始化备份密钥）'}")
        self.log(f"GitHub 登录: {'已自动读取到会话凭证' if self.var_token.get().strip() else '未检测到，请在顶部填写 Token'}")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        cfg = ttk.LabelFrame(outer, text="GitHub / 云机配置", padding=8)
        cfg.pack(fill="x")
        for col in range(6):
            cfg.columnconfigure(col, weight=1 if col in (1, 3, 5) else 0)

        ttk.Label(cfg, text="GitHub Token").grid(row=0, column=0, sticky="w")
        ttk.Entry(cfg, textvariable=self.var_token, show="*", width=34).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(cfg, text="仓库").grid(row=0, column=2, sticky="w")
        ttk.Entry(cfg, textvariable=self.var_repo, width=32).grid(row=0, column=3, sticky="ew", padx=5)
        ttk.Label(cfg, text="分支").grid(row=0, column=4, sticky="w")
        ttk.Entry(cfg, textvariable=self.var_branch, width=12).grid(row=0, column=5, sticky="ew", padx=5)

        ttk.Label(cfg, text="Phone ID").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.var_phone, width=12).grid(row=1, column=1, sticky="w", padx=5, pady=(6, 0))
        ttk.Label(cfg, text="创建工作流").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.var_create_wf).grid(row=1, column=3, sticky="ew", padx=5, pady=(6, 0))
        ttk.Label(cfg, text="管理/恢复工作流").grid(row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.var_managed_wf).grid(row=1, column=5, sticky="ew", padx=5, pady=(6, 0))

        ttk.Label(cfg, text="APK URL").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(cfg, textvariable=self.var_apk).grid(row=2, column=1, columnspan=5, sticky="ew", padx=5, pady=(6, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="创建新手机", command=self.create_phone).pack(side="left", padx=3)
        ttk.Button(actions, text="初始化备份密钥", command=self.init_backup_key).pack(side="left", padx=3)
        ttk.Button(actions, text="从备份恢复", command=self.restore_phone).pack(side="left", padx=3)
        ttk.Button(actions, text="备份当前手机", command=self.request_backup).pack(side="left", padx=3)
        ttk.Button(actions, text="刷新 Run", command=self.refresh_run).pack(side="left", padx=3)
        ttk.Button(actions, text="取消 Run", command=self.cancel_run).pack(side="left", padx=3)
        ttk.Checkbutton(actions, text="自动刷新", variable=self.var_auto).pack(side="left", padx=(16, 3))
        ttk.Label(actions, text="秒").pack(side="right")
        ttk.Spinbox(actions, from_=2, to=60, textvariable=self.var_interval, width=5).pack(side="right", padx=3)

        status = ttk.LabelFrame(outer, text="云机状态", padding=8)
        status.pack(fill="x")
        top = ttk.Frame(status)
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.var_run).pack(side="left", padx=(0, 18))
        ttk.Label(top, textvariable=self.var_node).pack(side="left", padx=(0, 18))

        adbline = ttk.Frame(status)
        adbline.pack(fill="x", pady=(7, 0))
        ttk.Label(adbline, text="ADB 地址").pack(side="left")
        ttk.Entry(adbline, textvariable=self.var_device, width=28).pack(side="left", padx=5)
        ttk.Button(adbline, text="自动发现", command=self.discover_device).pack(side="left", padx=3)
        ttk.Button(adbline, text="检测手机", command=self.health_check).pack(side="left", padx=3)
        ttk.Button(adbline, text="打开 scrcpy", command=self.open_scrcpy).pack(side="left", padx=3)

        grid = ttk.Frame(status)
        grid.pack(fill="x", pady=(8, 0))
        vars_ = [
            self.var_adb,
            self.var_boot,
            self.var_model,
            self.var_app,
            self.var_app_cpu,
            self.var_app_mem,
            self.var_dev_cpu,
            self.var_dev_mem,
            self.var_host_cpu,
            self.var_host_mem,
            self.var_qemu,
        ]
        for i, var in enumerate(vars_):
            ttk.Label(grid, textvariable=var).grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=3)
            grid.columnconfigure(i % 4, weight=1)

        appbox = ttk.LabelFrame(outer, text="App 控制（默认 BICOIN，可自定义包名并自动记忆）", padding=8)
        appbox.pack(fill="x", pady=8)
        ttk.Label(appbox, text="包名").pack(side="left")
        self.package_combo = ttk.Combobox(
            appbox,
            textvariable=self.var_package,
            values=self.cfg.package_history,
            width=38,
        )
        self.package_combo.pack(side="left", padx=5)
        self.package_combo.bind("<<ComboboxSelected>>", lambda _e: self.remember_package())
        ttk.Button(appbox, text="记住包名", command=self.remember_package).pack(side="left", padx=3)
        ttk.Button(appbox, text="读取已装 App", command=self.load_packages).pack(side="left", padx=3)
        ttk.Button(appbox, text="启动 App", command=self.start_app).pack(side="left", padx=3)
        ttk.Button(appbox, text="关闭 App", command=self.stop_app).pack(side="left", padx=3)
        ttk.Button(appbox, text="重启 App", command=self.restart_app).pack(side="left", padx=3)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        logtab = ttk.Frame(notebook, padding=5)
        helptab = ttk.Frame(notebook, padding=10)
        notebook.add(logtab, text="运行日志")
        notebook.add(helptab, text="说明")

        self.log_text = tk.Text(logtab, wrap="word", height=18)
        scroll = ttk.Scrollbar(logtab, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        help_text = (
            "1. GitHub Token 只保存在当前程序内存，不写入配置文件。\n"
            "2. 创建新手机：触发 create_workflow，默认 cloud-phone-managed.yml（mode=new）。\n"
            "3. 从备份恢复：触发 managed_workflow，传 mode=restore。\n"
            "4. 备份当前手机：向仓库写入 Cloud Phone Command Issue；配套 managed workflow 会在当前 Runner 内检测命令、正常停止模拟器并保存完整 AVD。\n"
            "5. 自动发现：优先从本机 Tailscale status --json 匹配 phone_id + run_id，得到 Tailscale IPv4，然后使用 :5555。\n"
            "6. App 控制：默认 com.temperaturecoin；可输入任何合法 Android 包名，点击“记住包名”后下次仍会显示。\n"
            "7. CPU：Android CPU 是虚拟机内部值；后续 managed workflow 可增加宿主 Runner 指标接口，GUI 会继续接入。\n"
        )
        ttk.Label(helptab, text=help_text, justify="left", wraplength=1000).pack(anchor="nw")

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
        token = self.var_token.get().strip()
        if not token:
            raise RuntimeError("请填写 GitHub Token，或设置 GH_TOKEN 环境变量")
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
        self.cfg.create_workflow = self.var_create_wf.get().strip() or "cloud-phone-managed.yml"
        self.cfg.managed_workflow = self.var_managed_wf.get().strip() or "cloud-phone-managed.yml"
        self.cfg.apk_url = self.var_apk.get().strip() or DEFAULT_APK_URL
        self.cfg.package_name = self.var_package.get().strip() or DEFAULT_PACKAGE
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

    def init_backup_key(self) -> None:
        token = self.var_token.get().strip()
        repo = self.var_repo.get().strip()
        if not token:
            messagebox.showerror(APP_NAME, "请先填写 GitHub Token")
            return
        def work() -> tuple[bool, str]:
            return self.tools.set_github_secret(repo, token)
        def done(result: tuple[bool, str]) -> None:
            ok, text = result
            self.log(f"初始化备份密钥: {'成功' if ok else '失败'} {text}")
            if ok:
                messagebox.showinfo(APP_NAME, "AVD_BACKUP_KEY 已安全写入 GitHub Actions Secret。密钥不会保存到本地配置。")
        self.bg("初始化备份密钥", work, done)

    def create_phone(self) -> None:
        self.save_config()
        def work() -> str:
            api = self.api()
            wf = self.var_create_wf.get().strip()
            inputs = {"phone_id": self._phone_id(), "apk_url": self.var_apk.get().strip() or DEFAULT_APK_URL}
            notes = []
            if "managed" in wf.lower():
                inputs["mode"] = "new"
                exists = api.repo_secret_exists("AVD_BACKUP_KEY")
                if exists is False:
                    ok, detail = self.tools.set_github_secret(
                        self.var_repo.get().strip(), self.var_token.get().strip()
                    )
                    if ok:
                        notes.append("备份密钥已自动初始化")
                    else:
                        notes.append(f"备份密钥自动初始化失败（新机仍继续创建）: {detail}")
                elif exists is None:
                    notes.append("无法读取 Secret 元数据；新机继续创建，备份前请确认密钥已初始化")
            api.dispatch(wf, self.var_branch.get().strip() or "main", inputs)
            suffix = ("；" + "；".join(notes)) if notes else ""
            return "GitHub workflow 已触发" + suffix
        self.bg("创建新手机", work, lambda v: (self.log(v), self.root.after(1500, self.refresh_run)))

    def restore_phone(self) -> None:
        self.save_config()
        def work() -> str:
            self.api().dispatch(
                self.var_managed_wf.get().strip(),
                self.var_branch.get().strip() or "main",
                {"mode": "restore", "phone_id": self._phone_id()},
            )
            return "恢复工作流已触发"
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
        rid = self.latest_run_id
        def work() -> tuple[str, str]:
            ip, host = self.tools.discover_phone(phone, rid)
            if not ip and rid:
                # Fallback: finished run logs may contain TAILSCALE_ADB=x.x.x.x:5555.
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
                return
            address = f"{ip}:5555"
            self.var_device.set(address)
            self.var_node.set(f"Tailscale: {host or ip}")
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
        self.var_adb.set(f"ADB: {h.get('adb', '-')}")
        self.var_boot.set(f"Boot: {h.get('boot', '-')}")
        self.var_model.set(f"Model: {h.get('model', '-')} / Android {h.get('android', '-')}")
        self.var_app.set(f"App: {h.get('app', '-')}  PID {h.get('app_pid', '-')}")
        self.var_app_cpu.set(f"App CPU: {h.get('app_cpu', '-')}")
        self.var_app_mem.set(f"App MEM: {h.get('app_mem', '-')}")
        self.var_dev_cpu.set(f"Android CPU: {h.get('device_cpu', '-')}")
        self.var_dev_mem.set(f"Android MEM: {h.get('device_mem', '-')}")
        self.var_host_cpu.set(f"Runner CPU: {h.get('host_cpu', '-')}")
        self.var_host_mem.set(f"Runner MEM: {h.get('host_mem', '-')}")
        self.var_qemu.set(f"QEMU: {h.get('qemu', '-')}")

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
