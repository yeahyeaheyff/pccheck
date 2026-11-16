"""
pc_check_basic.py - Basic Windows PC check: download history, Recycle Bin, Prefetch.
Read-only. Does NOT read any process memory.
Run as Administrator (needed for Prefetch and other users' Recycle Bins).
Only run it on a PC you own or whose owner has agreed to the check.

Usage:
    python pc_check_basic.py              # shows newest 150 entries per section
    python pc_check_basic.py --limit 0    # show everything
"""
import os, sys, struct, ctypes, sqlite3, shutil, tempfile, subprocess
import argparse, datetime, glob, string, urllib.parse
from contextlib import closing

# Anything containing these (case-insensitive) gets flagged with <<< MATCH.
# Add "real" / "drift" here if you want, but they will flag a lot of normal stuff.
KEYWORDS = ["xeno", "matcha", "severe", "cosmic", "crime.cc", "crimecc",
            "serotonin", "photon", "solara", "dx9", "cleaner", "universal"]

LINES = []
TMPDIRS = []
UTC = datetime.timezone.utc

def out(s=""):
    print(s)
    LINES.append(s)

def flag(text):
    t = text.lower()
    return "  <<< MATCH" if any(k in t for k in KEYWORDS) else ""

def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "unknown"

def from_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return None

def from_filetime(ft):
    try:
        if not ft:
            return None
        return (datetime.datetime(1601, 1, 1, tzinfo=UTC) +
                datetime.timedelta(microseconds=ft // 10)).astimezone()
    except (OverflowError, ValueError, OSError):
        return None

def from_chrome(us):
    try:
        if not us:
            return None
        return (datetime.datetime(1601, 1, 1, tzinfo=UTC) +
                datetime.timedelta(microseconds=us)).astimezone()
    except (OverflowError, ValueError, OSError):
        return None

def from_unix_us(us):
    try:
        if not us:
            return None
        return (datetime.datetime(1970, 1, 1, tzinfo=UTC) +
                datetime.timedelta(microseconds=us)).astimezone()
    except (OverflowError, ValueError, OSError):
        return None

def human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def users_dirs():
    base = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users")
    try:
        return [os.path.join(base, d) for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return [os.path.expanduser("~")]

def snapshot(src):
    """Copy a (possibly locked) browser DB to a temp folder so it can be read."""
    tmp = tempfile.mkdtemp()
    TMPDIRS.append(tmp)
    dst = os.path.join(tmp, os.path.basename(src))
    try:
        shutil.copy2(src, dst)
    except OSError:
        # Locked by a running browser: try Windows' esentutl (shadow copy needs admin)
        for extra in ([], ["/vss"]):
            try:
                subprocess.run(["esentutl.exe", "/y", src] + extra + ["/d", dst, "/o"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
            if os.path.exists(dst):
                break
        if not os.path.exists(dst):
            return None
    try:  # Firefox keeps recent data in a -wal file
        if os.path.exists(src + "-wal"):
            shutil.copy2(src + "-wal", dst + "-wal")
    except OSError:
        pass
    return dst

# ------------------------------------------------------------ downloads
def show_downloads(label, rows, limit):
    out(f"\n  [{label}]  {len(rows)} download(s) found")
    for i, (when, path, size, url) in enumerate(rows):
        f = flag(f"{path} {url}")
        if limit and i >= limit and not f:
            continue
        exists = "" if os.path.exists(path) else "  (file no longer at that path)"
        out(f"    {fmt(when)} | {human(size):>9} | {path}{exists}{f}")
        if url:
            out(f"        from: {url[:150]}")

def scan_downloads(limit):
    out("\n=== Browser download history ===")
    any_found = False
    for user in users_dirs():
        local = os.path.join(user, "AppData", "Local")
        roaming = os.path.join(user, "AppData", "Roaming")
        chromium = {
            "Chrome": os.path.join(local, "Google", "Chrome", "User Data"),
            "Edge": os.path.join(local, "Microsoft", "Edge", "User Data"),
            "Brave": os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            "Vivaldi": os.path.join(local, "Vivaldi", "User Data"),
            "Opera": os.path.join(roaming, "Opera Software", "Opera Stable"),
            "Opera GX": os.path.join(roaming, "Opera Software", "Opera GX Stable"),
        }
        for browser, root in chromium.items():
            if not os.path.isdir(root):
                continue
            profiles = [root]
            try:
                profiles += [os.path.join(root, d) for d in os.listdir(root)
                             if d == "Default" or d.startswith("Profile ")]
            except OSError:
                pass
            for prof in profiles:
                hist = os.path.join(prof, "History")
                if not os.path.isfile(hist):
                    continue
                any_found = True
                label = f"{browser} - {os.path.basename(user)} - {os.path.basename(prof)}"
                db = snapshot(hist)
                if not db:
                    out(f"\n  [{label}] could not read history (close the browser and rerun)")
                    continue
                rows = []
                try:
                    with closing(sqlite3.connect(db)) as con:
                        try:
                            cur = con.execute(
                                "SELECT d.target_path, d.start_time, d.total_bytes, "
                                "(SELECT c.url FROM downloads_url_chains c WHERE c.id=d.id "
                                " ORDER BY c.chain_index DESC LIMIT 1), d.tab_url "
                                "FROM downloads d ORDER BY d.start_time DESC")
                        except sqlite3.Error:
                            cur = con.execute(
                                "SELECT target_path, start_time, total_bytes, '', tab_url "
                                "FROM downloads ORDER BY start_time DESC")
                        for tp, st, tb, url, tab in cur.fetchall():
                            rows.append((from_chrome(st), tp or "?", tb, url or tab or ""))
                except sqlite3.Error as e:
                    out(f"\n  [{label}] database error: {e}")
                    continue
                show_downloads(label, rows, limit)

        ff_root = os.path.join(roaming, "Mozilla", "Firefox", "Profiles")
        for prof in glob.glob(os.path.join(ff_root, "*")):
            places = os.path.join(prof, "places.sqlite")
            if not os.path.isfile(places):
                continue
            any_found = True
            label = f"Firefox - {os.path.basename(user)} - {os.path.basename(prof)}"
            db = snapshot(places)
            if not db:
                out(f"\n  [{label}] could not read history (close the browser and rerun)")
                continue
            rows = []
            try:
                with closing(sqlite3.connect(db)) as con:
                    cur = con.execute(
                        "SELECT p.url, a.content, a.dateAdded FROM moz_annos a "
                        "JOIN moz_anno_attributes n ON n.id=a.anno_attribute_id "
                        "JOIN moz_places p ON p.id=a.place_id "
                        "WHERE n.name='downloads/destinationFileURI' "
                        "ORDER BY a.dateAdded DESC")
                    for url, dest, added in cur.fetchall():
                        path = urllib.parse.unquote((dest or "").replace("file:///", "")).replace("/", "\\")
                        rows.append((from_unix_us(added), path or "?", None, url or ""))
            except sqlite3.Error as e:
                out(f"\n  [{label}] database error: {e}")
                continue
            show_downloads(label, rows, limit)
    if not any_found:
        out("  No supported browser profiles found.")
    out("\n  Note: incognito/private downloads are not recorded, and history can be cleared.")

# ----------------------------------------------------------- recycle bin
def parse_dollar_i(path):
    with open(path, "rb") as f:
        d = f.read()
    ver = struct.unpack_from("<q", d, 0)[0]
    size = struct.unpack_from("<q", d, 8)[0]
    ft = struct.unpack_from("<Q", d, 16)[0]
    if ver == 2:
        n = struct.unpack_from("<I", d, 24)[0]
        name = d[28:28 + n * 2].decode("utf-16le", "ignore")
    else:
        name = d[24:].decode("utf-16le", "ignore")
    return from_filetime(ft), size, name.split("\x00")[0]

def scan_recycle(limit):
    out("\n=== Recycle Bin ===")
    for letter in string.ascii_uppercase:
        rb = f"{letter}:\\$Recycle.Bin"
        if not os.path.isdir(rb):
            continue
        try:
            sids = os.listdir(rb)
        except OSError:
            out(f"  {rb}: access denied (run as Administrator)")
            continue
        for sid in sids:
            sp = os.path.join(rb, sid)
            if not os.path.isdir(sp):
                continue
            try:
                st = os.stat(sp)
                entries = [e for e in os.listdir(sp) if e.upper().startswith("$I")]
            except OSError:
                out(f"\n  {sp}: access denied")
                continue
            out(f"\n  {sp}")
            out(f"    folder last changed: {fmt(from_ts(st.st_mtime))}  |  {len(entries)} item(s)")
            out("    (a recent change time with 0 items usually means it was just emptied)")
            rows = []
            for e in entries:
                try:
                    when, size, name = parse_dollar_i(os.path.join(sp, e))
                    rows.append((when, size, name))
                except Exception:
                    rows.append((None, 0, f"(unreadable {e})"))
            rows.sort(key=lambda r: r[0].timestamp() if r[0] else 0, reverse=True)
            for i, (when, size, name) in enumerate(rows):
                f = flag(name)
                if limit and i >= limit and not f:
                    continue
                out(f"    deleted {fmt(when)} | {human(size):>9} | {name}{f}")

# -------------------------------------------------------------- prefetch
def decompress_pf(data):
    """Windows 10/11 .pf files are XPRESS-Huffman compressed ('MAM' header)."""
    try:
        size = struct.unpack_from("<I", data, 4)[0]
        ntdll = ctypes.windll.ntdll
        ntdll.RtlGetCompressionWorkSpaceSize.argtypes = [
            ctypes.c_ushort, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong)]
        ntdll.RtlDecompressBufferEx.argtypes = [
            ctypes.c_ushort, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        ntdll.RtlDecompressBufferEx.restype = ctypes.c_long
        ws, frag = ctypes.c_ulong(), ctypes.c_ulong()
        ntdll.RtlGetCompressionWorkSpaceSize(4, ctypes.byref(ws), ctypes.byref(frag))
        work = ctypes.create_string_buffer(ws.value)
        buf = ctypes.create_string_buffer(size)
        final = ctypes.c_ulong()
        comp = data[8:]
        status = ntdll.RtlDecompressBufferEx(4, buf, size, comp, len(comp),
                                             ctypes.byref(final), work)
        return buf.raw[:final.value] if status == 0 else None
    except Exception:
        return None

def parse_pf(path):
    """Returns (run_count, [last run datetimes]) or (None, [])."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data[:3] == b"MAM":
            data = decompress_pf(data)
        if not data or data[4:8] != b"SCCA":
            return None, []
        ver = struct.unpack_from("<I", data, 0)[0]
        if ver == 17:
            n, off, cnt = 1, 120, struct.unpack_from("<I", data, 144)[0]
        elif ver == 23:
            n, off, cnt = 1, 128, struct.unpack_from("<I", data, 152)[0]
        elif ver in (26, 30, 31):
            n, off = 8, 128
            c208 = struct.unpack_from("<I", data, 208)[0]
            c200 = struct.unpack_from("<I", data, 200)[0]
            cnt = c208 or c200  # position varies slightly between Windows builds
        else:
            return None, []
        times = []
        for i in range(n):
            t = from_filetime(struct.unpack_from("<Q", data, off + 8 * i)[0])
            if t:
                times.append(t)
        return cnt, times
    except Exception:
        return None, []

def scan_prefetch(limit):
    out("\n=== Prefetch (programs that have been run) ===")
    pf_dir = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "Prefetch")
    try:
        files = [f for f in os.listdir(pf_dir) if f.lower().endswith(".pf")]
    except OSError:
        out("  Cannot read Prefetch (run as Administrator).")
        return
    try:
        out(f"  Prefetch folder last changed: {fmt(from_ts(os.stat(pf_dir).st_mtime))}")
    except OSError:
        pass
    out(f"  {len(files)} .pf file(s)")
    if len(files) < 20:
        out("  WARNING: very few Prefetch files. It may have been cleared or disabled.")
    rows = []
    for f in files:
        full = os.path.join(pf_dir, f)
        try:
            st = os.stat(full)
        except OSError:
            continue
        cnt, times = parse_pf(full)
        last = times[0] if times else from_ts(st.st_mtime)
        rows.append((last, cnt, f, times, from_ts(st.st_ctime)))
    rows.sort(key=lambda r: r[0].timestamp() if r[0] else 0, reverse=True)
    for i, (last, cnt, name, times, first) in enumerate(rows):
        f = flag(name)
        if limit and i >= limit and not f:
            continue
        runs = f"runs {cnt}" if cnt is not None else "runs ?"
        out(f"    last run {fmt(last)} | {runs:<8} | {name}{f}")
        if len(times) > 1:
            out("        earlier: " + ", ".join(fmt(t) for t in times[1:4]))

# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150,
                    help="max entries per section (0 = all). Flagged entries always show.")
    args = ap.parse_args()

    if not ctypes.windll.shell32.IsUserAnAdmin():
        out("WARNING: not running as Administrator. Prefetch and other users' data will be incomplete.\n")

    out(f"PC check - {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    scan_downloads(args.limit)
    scan_recycle(args.limit)
    scan_prefetch(args.limit)

    for t in TMPDIRS:
        shutil.rmtree(t, ignore_errors=True)

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    folder = desktop if os.path.isdir(desktop) else os.getcwd()
    path = os.path.join(folder, f"pc_check_basic_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(LINES))
    print(f"\nDone. Report saved to: {path}")

if __name__ == "__main__":
    main()
