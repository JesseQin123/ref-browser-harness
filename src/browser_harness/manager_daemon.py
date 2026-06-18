"""Browser lifecycle manager for browser-harness manager mode."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from . import admin, auth, context


BU_API = "https://api.browser-use.com/api/v3"
MAC_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


@dataclass
class BrowserLease:
    browser_id: str
    run_id: str
    owner_agent_id: str
    backend: str
    profile_kind: str
    harness_daemon_name: str
    runtime_dir: str
    tmp_dir: str
    download_dir: str
    artifact_dir: str
    profile_dir: str
    cdp_url: str | None = None
    cdp_ws: str | None = None
    local_process_id: int | None = None
    local_debug_port: int | None = None
    cloud_browser_id: str | None = None
    cloud_live_url: str | None = None
    allowed_agents: list[str] = field(default_factory=list)
    active_execution: dict | None = None
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    last_used_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    @classmethod
    def from_json(cls, data: dict) -> "BrowserLease":
        return cls(**data)

    def binding(self) -> dict:
        return {
            "browser_id": self.browser_id,
            "bu_name": self.harness_daemon_name,
            "runtime_dir": self.runtime_dir,
            "tmp_dir": self.tmp_dir,
            "download_dir": self.download_dir,
            "artifact_dir": self.artifact_dir,
            "cdp_url": self.cdp_url,
            "cdp_ws": self.cdp_ws,
        }


class Manager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.leases: dict[str, BrowserLease] = {}
        self.active_by_agent: dict[str, str] = {}
        self.next_seq = 0
        self._load()

    def _load(self):
        try:
            data = json.loads((self.root / "registry.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self.next_seq = int(data.get("next_seq") or 0)
        self.active_by_agent = dict(data.get("active_by_agent") or {})
        for item in data.get("leases") or []:
            lease = BrowserLease.from_json(item)
            lease.active_execution = None
            self.leases[lease.browser_id] = lease

    def _persist(self):
        data = {
            "next_seq": self.next_seq,
            "active_by_agent": self.active_by_agent,
            "leases": [asdict(v) for v in self.leases.values()],
        }
        tmp = self.root / "registry.json.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.root / "registry.json")

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        try:
            if op == "status":
                return self.status(req)
            if op == "list":
                return self.list(req)
            if op == "new":
                return self.new(req)
            if op == "switch":
                return self.switch(req)
            if op == "close":
                return self.close(req)
            if op == "lock":
                return self.lock(req)
            if op == "unlock":
                return self.unlock(req)
            return error("unknown-op", f"unknown op {op!r}", [])
        except Exception as e:
            return error("manager-error", str(e), [])

    def status(self, req: dict) -> dict:
        with self._lock:
            key = agent_key(req)
            browser_id = self.active_by_agent.get(key)
            if not browser_id:
                return {"ok": True, "ready": False, "state": "no-active-browser", "safe_actions": ["browser_new", "browser_switch"]}
            lease = self.leases.get(browser_id)
            if not lease:
                return {"ok": True, "ready": False, "state": "stale-binding", "safe_actions": ["browser_new", "browser_switch"]}
            return ready_public(lease)

    def list(self, req: dict) -> dict:
        with self._lock:
            run_id, agent_id = run_agent(req)
            browsers = []
            for lease in self.leases.values():
                if lease.run_id != run_id:
                    continue
                if lease.owner_agent_id != agent_id and agent_id not in lease.allowed_agents:
                    continue
                browsers.append({
                    "browser_id": lease.browser_id,
                    "backend": lease.backend,
                    "owned_by_this_agent": lease.owner_agent_id == agent_id,
                    "shared": len(lease.allowed_agents) > 1,
                    "state": "busy" if lease.active_execution else "ready",
                    **({"live_url": lease.cloud_live_url} if lease.cloud_live_url else {}),
                })
            return {"ok": True, "browsers": browsers}

    def new(self, req: dict) -> dict:
        run_id, agent_id = run_agent(req)
        backend = req.get("backend") or "managed"
        if backend not in {"managed", "cloud"}:
            return error("unsupported-backend", f"unsupported backend {backend!r}", ["browser_new"])
        with self._lock:
            lease = self._allocate_lease(run_id, agent_id, backend, req.get("profile") or "clean")
        try:
            if backend == "cloud":
                start_cloud_backend(lease, req.get("proxy_country"))
            else:
                start_managed_backend(lease)
        except auth.CloudAuthRequired as e:
            cleanup_backend(lease)
            return error("cloud-auth-required", str(e), ["browser-harness auth login"])
        except Exception as e:
            cleanup_backend(lease)
            return error("browser-start-failed", str(e), ["browser_new"])
        with self._lock:
            self.leases[lease.browser_id] = lease
            self.active_by_agent[agent_key_parts(run_id, agent_id)] = lease.browser_id
            self._persist()
        return ready_response(lease)

    def switch(self, req: dict) -> dict:
        with self._lock:
            run_id, agent_id = run_agent(req)
            browser_id = req.get("browser_id")
            if not browser_id:
                return error("bad-request", "browser_id is required", ["browser_list", "browser_new"])
            lease = self.leases.get(browser_id)
            if not lease:
                return error("not-found", "browser id not found", ["browser_list", "browser_new"])
            if lease.run_id != run_id:
                return error("forbidden", "browser belongs to another run", ["browser_list", "browser_new"])
            if agent_id not in lease.allowed_agents:
                lease.allowed_agents.append(agent_id)
            lease.last_used_at_ms = int(time.time() * 1000)
            self.active_by_agent[agent_key_parts(run_id, agent_id)] = browser_id
            self._persist()
            return ready_response(lease)

    def close(self, req: dict) -> dict:
        cleanup = None
        with self._lock:
            run_id, agent_id = run_agent(req)
            key = agent_key_parts(run_id, agent_id)
            browser_id = req.get("browser_id") or self.active_by_agent.get(key)
            if not browser_id:
                return {"ok": True, "ready": False, "state": "no-active-browser"}
            lease = self.leases.get(browser_id)
            if not lease:
                self.active_by_agent.pop(key, None)
                self._persist()
                return {"ok": True, "ready": False, "state": "stale-binding"}
            if lease.run_id != run_id or agent_id not in lease.allowed_agents:
                return error("forbidden", "browser belongs to another run or agent", ["browser_list"])
            if agent_id in lease.allowed_agents:
                lease.allowed_agents.remove(agent_id)
            if lease.owner_agent_id == agent_id or not lease.allowed_agents:
                cleanup = lease
                self.leases.pop(browser_id, None)
                self.active_by_agent = {k: v for k, v in self.active_by_agent.items() if v != browser_id}
                self._persist()
                resp = {"ok": True, "ready": False, "state": "closed", "browser_id": browser_id}
            else:
                self.active_by_agent.pop(key, None)
                self._persist()
                resp = {"ok": True, "ready": False, "state": "released", "browser_id": browser_id}
        if cleanup is not None:
            cleanup_backend(cleanup)
        return resp

    def lock(self, req: dict) -> dict:
        with self._lock:
            run_id, agent_id = run_agent(req)
            client_id = req.get("client_id") or agent_id
            browser_id = req.get("browser_id")
            if not browser_id:
                return error("bad-request", "browser_id is required", ["browser_new", "browser_switch"])
            lease = self.leases.get(browser_id)
            if not lease:
                return error("not-found", "browser id not found", ["browser_list", "browser_new"])
            if lease.run_id != run_id or agent_id not in lease.allowed_agents:
                return error("forbidden", "browser belongs to another run or agent", ["browser_list", "browser_new"])
            active = lease.active_execution or {}
            if active and active.get("client_id") != client_id:
                return error("busy", "browser is currently active in another browser-harness process", ["wait", "browser_new"])
            if active and active.get("client_id") == client_id:
                return {"ok": True, "state": "ready", "browser_id": browser_id, "lock_id": active["lock_id"]}
            lock_id = f"lk_{int(time.time() * 1000):x}_{secrets.token_hex(4)}"
            lease.active_execution = {"agent_id": agent_id, "client_id": client_id, "lock_id": lock_id}
            self._persist()
            return {"ok": True, "state": "ready", "browser_id": browser_id, "lock_id": lock_id}

    def unlock(self, req: dict) -> dict:
        with self._lock:
            _run_id, agent_id = run_agent(req)
            client_id = req.get("client_id") or agent_id
            browser_id = req.get("browser_id")
            lease = self.leases.get(browser_id or "")
            if not lease:
                return {"ok": True, "state": "not-found"}
            active = lease.active_execution or {}
            if (
                active.get("agent_id") == agent_id
                and active.get("client_id") == client_id
                and active.get("lock_id") == req.get("lock_id")
            ):
                lease.active_execution = None
                self._persist()
            return {"ok": True, "state": "released", "browser_id": browser_id}

    def _allocate_lease(self, run_id: str, agent_id: str, backend: str, profile_kind: str) -> BrowserLease:
        self.next_seq += 1
        short = f"{int(time.time() * 1000):x}{self.next_seq:x}"
        browser_id = f"br_{short}"
        bu_name = f"bh_{short[-16:]}"
        base = self.root / "leases" / browser_id
        runtime_dir = base / "r"
        tmp_dir = base / "t"
        download_dir = base / "downloads"
        artifact_dir = base / "artifacts"
        profile_dir = base / "profile"
        for p in (runtime_dir, tmp_dir, download_dir, artifact_dir, profile_dir):
            p.mkdir(parents=True, exist_ok=True)
        return BrowserLease(
            browser_id=browser_id,
            run_id=run_id,
            owner_agent_id=agent_id,
            backend=backend,
            profile_kind=profile_kind,
            harness_daemon_name=bu_name,
            runtime_dir=str(runtime_dir),
            tmp_dir=str(tmp_dir),
            download_dir=str(download_dir),
            artifact_dir=str(artifact_dir),
            profile_dir=str(profile_dir),
            allowed_agents=[agent_id],
        )


def start_cloud_backend(lease: BrowserLease, proxy_country: str | None):
    auth.get_browser_use_api_key()
    body = {}
    if proxy_country:
        body["proxyCountryCode"] = proxy_country
    browser = _browser_use("/browsers", "POST", body)
    lease.cloud_browser_id = browser["id"]
    lease.cloud_live_url = browser.get("liveUrl")
    lease.cdp_url = browser["cdpUrl"]
    try:
        start_harness_daemon(lease)
    except BaseException:
        stop_cloud_browser(lease.cloud_browser_id)
        raise


def start_managed_backend(lease: BrowserLease):
    browser = find_browser_binary()
    if not browser:
        raise RuntimeError("no Chrome/Chromium binary found; set BH_CHROME_PATH or CHROME_PATH")
    port = allocate_port()
    lease.cdp_url = f"http://127.0.0.1:{port}"
    args = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={lease.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-dev-shm-usage",
        "about:blank",
    ]
    headless = os.environ.get("BH_MANAGED_HEADLESS") == "1" or (not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))
    if headless:
        args.insert(-1, "--headless=new")
        args.insert(-1, "--disable-gpu")
    if os.environ.get("BH_CHROME_NO_SANDBOX") == "1":
        args.insert(-1, "--no-sandbox")
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    lease.local_process_id = proc.pid
    lease.local_debug_port = port
    wait_devtools(port)
    start_harness_daemon(lease)


def start_harness_daemon(lease: BrowserLease):
    binding = context.BrowserBinding(
        browser_id=lease.browser_id,
        bu_name=lease.harness_daemon_name,
        runtime_dir=Path(lease.runtime_dir),
        tmp_dir=Path(lease.tmp_dir),
        download_dir=Path(lease.download_dir),
        artifact_dir=Path(lease.artifact_dir),
        cdp_url=lease.cdp_url,
        cdp_ws=lease.cdp_ws,
        manager_mode=True,
    )
    admin.ensure_daemon(wait=60.0, binding=binding)


def cleanup_backend(lease: BrowserLease):
    binding = context.BrowserBinding(
        browser_id=lease.browser_id,
        bu_name=lease.harness_daemon_name,
        runtime_dir=Path(lease.runtime_dir),
        tmp_dir=Path(lease.tmp_dir),
        manager_mode=True,
    )
    try:
        admin.restart_daemon(binding=binding)
    except Exception:
        pass
    if lease.backend == "cloud" and lease.cloud_browser_id:
        stop_cloud_browser(lease.cloud_browser_id)
    if lease.backend == "managed" and lease.local_process_id:
        try:
            os.killpg(lease.local_process_id, 15)
        except Exception:
            try:
                os.kill(lease.local_process_id, 15)
            except Exception:
                pass
        for _ in range(25):
            try:
                os.kill(lease.local_process_id, 0)
            except OSError:
                return
            time.sleep(0.2)
        try:
            os.killpg(lease.local_process_id, 9)
        except Exception:
            try:
                os.kill(lease.local_process_id, 9)
            except Exception:
                pass


def _browser_use(path: str, method: str, body=None):
    key = auth.get_browser_use_api_key()
    req = urllib.request.Request(
        f"{BU_API}{path}",
        method=method,
        data=(json.dumps(body).encode() if body is not None else None),
        headers={"X-Browser-Use-API-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"{}")


def stop_cloud_browser(browser_id: str | None):
    if not browser_id:
        return
    try:
        _browser_use(f"/browsers/{browser_id}", "PATCH", {"action": "stop"})
    except Exception:
        pass


def _browser_binary_usable(path: str) -> bool:
    try:
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            return False
        return subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def find_browser_binary() -> str | None:
    for key in ("BH_CHROME_PATH", "CHROME_PATH"):
        value = os.environ.get(key)
        if value:
            return value
    candidates = []
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            candidates.append(path)
    if sys.platform == "darwin":
        candidates.extend(MAC_BROWSER_PATHS)
    for path in candidates:
        if _browser_binary_usable(path):
            return path
    return None


def allocate_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def wait_devtools(port: int, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                data = json.loads(resp.read() or b"{}")
                if data.get("webSocketDebuggerUrl"):
                    return
        except Exception as e:
            last = e
        time.sleep(0.2)
    raise RuntimeError(f"Chrome DevTools did not become ready on port {port}: {last}")


def ready_public(lease: BrowserLease) -> dict:
    state = {
        "ok": True,
        "ready": True,
        "state": "ready",
        "browser_id": lease.browser_id,
        "backend": lease.backend,
        "shared": len(lease.allowed_agents) > 1,
    }
    if lease.cloud_live_url:
        state["live_url"] = lease.cloud_live_url
    return state


def ready_response(lease: BrowserLease) -> dict:
    return {**ready_public(lease), "binding": lease.binding()}


def error(state: str, reason: str, safe_actions: list[str]) -> dict:
    return {"ok": False, "ready": False, "state": state, "reason": reason, "safe_actions": safe_actions}


def run_agent(req: dict) -> tuple[str, str]:
    return sanitize(req.get("run_id") or "unknown-run"), sanitize(req.get("agent_id") or "unknown-agent")


def agent_key(req: dict) -> str:
    return agent_key_parts(*run_agent(req))


def agent_key_parts(run_id: str, agent_id: str) -> str:
    return f"{run_id}/{agent_id}"


def sanitize(value: str) -> str:
    out = "".join(c for c in str(value) if c.isalnum() or c in "_-")[:64]
    return out or "unknown"


def serve(socket_path: Path, root: Path):
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    manager = Manager(root)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(128)
    print(f"browser-harness manager listening on {socket_path}", file=sys.stderr, flush=True)
    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle_conn, args=(manager, conn), daemon=True).start()
    finally:
        server.close()


def handle_conn(manager: Manager, conn: socket.socket):
    with conn:
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
            if not data:
                return
            req = json.loads(data or b"{}")
            resp = manager.handle(req)
        except Exception as e:
            resp = error("bad-request", str(e), [])
        conn.sendall((json.dumps(resp, default=str) + "\n").encode())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=os.environ.get("BH_MANAGER_SOCKET") or str(Path(tempfile.gettempdir()) / "bhm" / "manager.sock"))
    parser.add_argument("--root", default=os.environ.get("BH_MANAGER_ROOT") or str(Path(tempfile.gettempdir()) / "bhm"))
    args = parser.parse_args(argv)
    serve(Path(args.socket), Path(args.root))


if __name__ == "__main__":
    main()
