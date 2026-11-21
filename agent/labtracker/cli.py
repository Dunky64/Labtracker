"""Command line: `labtracker [run|setup|status|link|revoke-link|backup-keys|restore-keys|forget]`."""
import getpass
import os
import secrets
import sys
import urllib.parse

from . import VERSION, crypto
from .backend import Backend, BackendError, valid_webapp_url
from .keystore import Keyring, KeyringError, Keys
from .storage import Store

HELP = f"""Labtracker {VERSION}

  labtracker               start the tray app (normally starts automatically at login)
  labtracker setup         first-time setup (or change settings)
  labtracker status        show whether you're clocked in and if anything is waiting to sync
  labtracker link          print the private dashboard link to give your employer
  labtracker revoke-link   stop the old dashboard link working and make a new one
  labtracker backup-keys   show your recovery code (store it in a password manager)
  labtracker restore-keys  restore keys from a recovery code (e.g. on a new laptop)
  labtracker forget        delete your keys from this computer
"""


def dashboard_link(cfg: dict, keys: Keys) -> str:
    frag = urllib.parse.urlencode({
        "u": cfg["webapp_url"], "k": crypto.b64e(keys.share), "t": keys.read_token,
        "id": cfg["user_id"], "n": cfg["name"],
    }, quote_via=urllib.parse.quote)
    base = cfg["dashboard_url"].split("#")[0]
    return f"{base}#{frag}"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


def _yes(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    v = _ask(f"{prompt} ({d})").lower()
    return default if not v else v.startswith("y")


def _load():
    store = Store()
    cfg = store.load_config()
    try:
        keys = Keyring().load()
    except KeyringError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    return store, cfg, keys


def _require_setup(cfg, keys):
    if not keys or not cfg.get("webapp_url") or not cfg.get("user_id"):
        print("Labtracker isn't set up yet. Run:  labtracker setup", file=sys.stderr)
        sys.exit(1)


def cmd_setup() -> int:
    print("\n=== Labtracker setup ===\n")
    store, cfg, keys = _load()
    kr = Keyring()

    if keys:
        if not _yes("Encryption keys already exist on this computer. Keep them?", True):
            print("\nWARNING: new keys make ALL your existing records unreadable,"
                  " and your employer's link stops working.")
            if _ask("Type DELETE to create new keys anyway") != "DELETE":
                print("Keeping your existing keys.")
            else:
                keys = None
    if not keys:
        if _yes("Do you have a recovery code from a previous install?", False):
            code = getpass.getpass("Paste your recovery code (hidden): ")
            try:
                keys, cfg["user_id"] = Keys.from_recovery_code(code)
            except Exception:
                print("That recovery code isn't valid.")
                return 1
        else:
            keys = Keys.generate()
            cfg["user_id"] = "u" + secrets.token_hex(6)
        kr.save(keys)
        print("Keys created and stored in your login keyring.\n")
    if not cfg.get("user_id"):
        cfg["user_id"] = "u" + secrets.token_hex(6)

    cfg["name"] = _ask("Your name, as your employer will see it", cfg.get("name") or getpass.getuser())
    store.save_config(cfg)

    print("\n--- Step A: give the Google script your signing key ---")
    print("In the Apps Script editor: Project Settings (gear icon) -> Script Properties ->")
    print("'Add script property'.  Property:  LT_AUTH_KEY   Value:\n")
    print("    " + crypto.b64e(keys.auth) + "\n")
    print("Then click 'Save script properties'.")
    _ask("Press Enter when done")

    print("\n--- Step B: connect to your web app ---")
    while True:
        url = _ask("Paste your Web App URL (ends in /exec)", cfg.get("webapp_url", ""))
        if valid_webapp_url(url):
            break
        print("That isn't a web app URL. It should look like "
              "https://script.google.com/macros/s/AKfy.../exec")
    cfg["webapp_url"] = url
    store.save_config(cfg)

    while True:
        try:
            be = Backend(url, keys.auth)
            be.call("ping")
            be.call("set_read_token", token=keys.read_token)
            print("Connected. The backend accepted a signed test message.")
            break
        except BackendError as e:
            print(f"Problem: {e}")
            if not _yes("Try again?", True):
                print("You can run `labtracker setup` again later.")
                return 1

    cfg["dashboard_url"] = _ask("Dashboard address", cfg["dashboard_url"])
    store.save_config(cfg)

    print("\n--- Step C: back up your recovery code ---")
    print("If this laptop is lost, this code is the ONLY way to read your records again.")
    print("Store it in a password manager. Anyone with it can read and add to your records.\n")
    print("    " + keys.recovery_code(cfg["user_id"]) + "\n")
    _ask("Press Enter once it's saved")
    os.system("clear")  # don't leave the secrets on screen

    print("Setup complete.\n")
    print("Employer dashboard link (send this privately, e.g. in an encrypted message):\n")
    print(dashboard_link(cfg, keys) + "\n")
    print("Start the tray app from the Menu (search 'Labtracker'). It also starts at login.")
    return 0


def cmd_status() -> int:
    store, cfg, keys = _load()
    _require_setup(cfg, keys)
    st = store.load_state()
    from .timeutil import fmt_dt
    if st.get("clocked_in"):
        print(f"Clocked in since {fmt_dt(st['session_start'])}")
    else:
        print("Clocked out")
    print(f"Events waiting to sync: {len(store.load_queue())}")
    return 0


def cmd_link() -> int:
    store, cfg, keys = _load()
    _require_setup(cfg, keys)
    print(dashboard_link(cfg, keys))
    return 0


def cmd_revoke() -> int:
    store, cfg, keys = _load()
    _require_setup(cfg, keys)
    new = Keys(keys.share, keys.private, keys.auth, crypto.new_token())
    try:
        Backend(cfg["webapp_url"], keys.auth).call("set_read_token", token=new.read_token)
    except BackendError as e:
        print(f"Couldn't update the backend: {e}")
        return 1
    Keyring().save(new)
    print("The old link no longer works. New link:\n")
    print(dashboard_link(cfg, new))
    return 0


def cmd_backup() -> int:
    store, cfg, keys = _load()
    _require_setup(cfg, keys)
    print("Your recovery code. Keep it secret, keep it safe (a password manager is ideal):\n")
    print(keys.recovery_code(cfg["user_id"]))
    return 0


def cmd_restore() -> int:
    store, cfg, _ = _load()
    code = getpass.getpass("Paste your recovery code (hidden): ")
    try:
        keys, user_id = Keys.from_recovery_code(code)
    except Exception:
        print("That recovery code isn't valid.")
        return 1
    Keyring().save(keys)
    cfg["user_id"] = user_id
    store.save_config(cfg)
    print("Keys restored. Run `labtracker setup` to finish connecting.")
    return 0


def cmd_forget() -> int:
    if _ask("This deletes your keys from this computer. Type FORGET to continue") != "FORGET":
        print("Cancelled.")
        return 1
    Keyring().delete()
    print("Keys deleted from the keyring.")
    return 0


def main(argv) -> int:
    os.umask(0o077)
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if cmd == "--version":
        print(VERSION)
        return 0
    if cmd == "run":
        from .gui import run
        return run()
    table = {"setup": cmd_setup, "status": cmd_status, "link": cmd_link, "revoke-link": cmd_revoke,
             "backup-keys": cmd_backup, "restore-keys": cmd_restore, "forget": cmd_forget}
    if cmd not in table:
        print(HELP)
        return 2
    try:
        return table[cmd]()
    except KeyringError as e:
        print(f"Keyring error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
