import ast, os, sys, urllib.request

# Windows default stdout encoding is cp1252, which can't encode the 🐴 marker
# helpers prepend to tab titles (or anything else outside Latin-1). Force UTF-8
# so `print(page_info())` doesn't UnicodeEncodeError on Windows. Issue #124(4).
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

from .admin import (
    _version,
    NAME,
    daemon_alive,
    ensure_daemon,
    list_cloud_profiles,
    list_local_profiles,
    open_local_profile,
    print_update_banner,
    restart_daemon,
    run_doctor,
    run_doctor_fix_snap,
    run_update,
    start_remote_daemon,
    stop_remote_daemon,
    sync_local_profile,
    use_local_profile,
)
from . import auth, context, manager_client
from .helpers import *
from .manager_helpers import *

HELP = """Browser Harness

Read SKILL.md for the default workflow and examples.

Typical usage:
  browser-harness <<'PY'
  ensure_real_tab()
  print(page_info())
  PY

Helpers are pre-imported. The daemon auto-starts and connects to the running browser.
For local Chrome, first choose a stable profile id with list_local_profiles() and use_local_profile(id).

Commands:
  browser-harness --version        print the installed version
  browser-harness --doctor         diagnose install, daemon, and browser state
  browser-harness doctor           same as --doctor
  browser-harness doctor --fix-snap   print how to fix Snap Chromium blocking CDP (Linux)
  browser-harness auth login          sign in to Browser Use Cloud for cloud browsers
  browser-harness auth login --device-code   sign in from SSH/headless environments
  browser-harness auth status         show Browser Use Cloud auth state
  browser-harness auth logout         remove stored Browser Use Cloud auth
  browser-harness --update [-y]    pull the latest version (agents: pass -y)
  browser-harness --reload         stop the daemon so next call picks up code changes
"""

USAGE = """Usage:
  browser-harness <<'PY'
  print(page_info())
  PY
"""

_MANAGER_HELPER_NAMES = (
    "browser_status",
    "browser_new",
    "browser_switch",
    "browser_list",
    "browser_close",
)

_NO_DAEMON_HELPER_NAMES = {
    "list_local_profiles",
    "use_local_profile",
    "open_local_profile",
    "list_cloud_profiles",
    "sync_local_profile",
    "start_remote_daemon",
    "stop_remote_daemon",
    "restart_daemon",
}

_NO_DAEMON_WRAPPER_NAMES = {
    "print",
    "repr",
    "str",
    "bool",
    "len",
    "sorted",
    "list",
    "dict",
    "tuple",
    "set",
}


def _uses_manager_helpers(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _MANAGER_HELPER_NAMES:
            return True
    return False


def _can_run_without_daemon(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    saw_no_daemon_helper = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _NO_DAEMON_HELPER_NAMES:
                saw_no_daemon_helper = True
                continue
            if func.id in _NO_DAEMON_WRAPPER_NAMES:
                continue
            return False
        if isinstance(func, ast.Attribute):
            # Allow simple formatting around passive helper output, e.g.
            # json.dumps(list_local_profiles()).
            if func.attr in {"dumps", "loads"}:
                continue
            return False
        return False
    return saw_no_daemon_helper


# Probe /json/version (not a bare TCP connect) so a non-Chrome process bound to
# 9222/9223 doesn't masquerade as Chrome and skip the cloud bootstrap. Mirrors
# daemon.py's fallback probe.
def _local_chrome_listening():
    for port in (9222, 9223):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.3).close()
            return True
        except OSError: pass
    return False


# BU_CDP_URL / BU_CDP_WS are documented to override local Chrome discovery
# (install.md:58-59), so they must also block cloud auto-bootstrap. Without this
# guard, start_remote_daemon() in admin.py overwrites BU_CDP_WS in the daemon
# env with a cloud WebSocket URL, silently replacing the user's explicit endpoint
# *and* billing them for a cloud browser they never asked for.
def _explicit_cdp_configured():
    return bool(os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS"))


def main():
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(HELP)
        return
    if args and args[0] == "--version":
        print(_version() or "unknown")
        return
    if args and args[0] == "--doctor":
        sys.exit(run_doctor())
    if args and args[0] == "doctor":
        rest = args[1:]
        if rest == ["--fix-snap"]:
            sys.exit(run_doctor_fix_snap())
        if rest:
            print("usage: browser-harness doctor [--fix-snap]", file=sys.stderr)
            sys.exit(2)
        sys.exit(run_doctor())
    if args and args[0] == "auth":
        sys.exit(auth.run_auth_cli(args[1:]))
    if args and args[0] == "--update":
        yes = any(a in {"-y", "--yes"} for a in args[1:])
        sys.exit(run_update(yes=yes))
    if args and args[0] == "--reload":
        restart_daemon()
        print("daemon stopped — will restart fresh on next call")
        return
    if args and args[0] == "--debug-clicks":
        os.environ["BH_DEBUG_CLICKS"] = "1"
        args = args[1:]
    if not args and not sys.stdin.isatty():
        code = sys.stdin.read()
        if not code.strip():
            sys.exit(USAGE)
    else:
        sys.exit(USAGE)
    print_update_banner()
    if context.manager_enabled() or _uses_manager_helpers(code):
        os.environ.setdefault("BH_MANAGER_MODE", "1")
        if os.environ.get("BH_BROWSER_ID"):
            browser_switch(os.environ["BH_BROWSER_ID"])
        else:
            context.clear_active_binding()
        try:
            exec(code, globals())
        finally:
            manager_client.release_active_execution_lock()
        return

    # Auto-bootstrap a cloud browser is opt-in via BU_AUTOSPAWN — BROWSER_USE_API_KEY alone
    # is not enough, since the key is commonly set for unrelated reasons (profile sync,
    # cloud API calls, parent agents managing their own session). An explicit BU_CDP_URL
    # or BU_CDP_WS also blocks the spawn so we honour the precedence install.md promises.
    if (
        not daemon_alive()
        and not _local_chrome_listening()
        and not _explicit_cdp_configured()
        and os.environ.get("BROWSER_USE_API_KEY")
        and os.environ.get("BU_AUTOSPAWN")
    ):
        start_remote_daemon(NAME)
    if not _can_run_without_daemon(code):
        ensure_daemon()
    exec(code, globals())


if __name__ == "__main__":
    main()
