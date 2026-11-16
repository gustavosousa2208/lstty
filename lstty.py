#!/usr/bin/env python3
import os
import tempfile
import json
import stat
import subprocess
import re
import shlex
import shutil
import sys
import time
import argparse
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchlib import probelock  # noqa: E402  (machine-wide, per-probe exclusive lock)

TTY_ROOT = Path("/sys/class/tty")
DEV_ROOT = Path("/dev")
BY_ID_ROOT = Path("/dev/serial/by-id")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lstty"
LINKS_CONFIG = CONFIG_DIR / "links.json"
BOARD_CONFIG = CONFIG_DIR / "boards.json"
BOARD_LOCK = CONFIG_DIR / ".boards.lock"
DEFAULT_LINK_DIR = Path(os.environ.get("LSTTY_LINK_DIR", Path.home() / ".serial"))
COMPLETION_SOURCE = Path(__file__).resolve().with_name("completions") / "_lstty"
COMPLETION_DEST = Path.home() / ".zfunc" / "_lstty"


class Color:
    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    reset = "\033[0m" if enabled else ""
    dim = "\033[2m" if enabled else ""
    cyan = "\033[36m" if enabled else ""
    green = "\033[32m" if enabled else ""
    yellow = "\033[33m" if enabled else ""
    magenta = "\033[35m" if enabled else ""
    blue = "\033[34m" if enabled else ""
    red = "\033[31m" if enabled else ""
    bold = "\033[1m" if enabled else ""


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def normalize_usb_id(value):
    value = str(value or "").strip().lower()
    value = value[2:] if value.startswith("0x") else value
    return value.zfill(4) if re.fullmatch(r"[0-9a-f]{1,4}", value) else value


def clean_missing(value):
    value = str(value or "").strip()
    return "" if value == "-" else value


def udev_props(devname):
    try:
        out = subprocess.check_output(
            ["udevadm", "info", "--query=property", f"--name={devname}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}

    props = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def usb_parent(sys_path):
    path = Path(sys_path).resolve()
    for parent in (path, *path.parents):
        if (parent / "idVendor").exists() and (parent / "idProduct").exists():
            return parent
    return None


def by_id_for(devname):
    if not BY_ID_ROOT.exists():
        return ""

    target = Path(devname).resolve()
    matches = []
    for link in BY_ID_ROOT.iterdir():
        try:
            if link.resolve() == target:
                matches.append(str(link))
        except OSError:
            continue
    return ", ".join(sorted(matches))


def mode_user_group(path):
    try:
        st = os.stat(path)
    except OSError:
        return "-"

    perms = stat.filemode(st.st_mode)
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
    except Exception:
        owner = str(st.st_uid)
        group = str(st.st_gid)
    return f"{perms} {owner}:{group}"


def speed_label(raw):
    if not raw:
        return "-"
    try:
        value = float(raw)
    except ValueError:
        return raw
    return f"{value:.1f} Mb/s"


def icon_for(product, driver):
    product_l = product.lower()
    if "mcu-link" in product_l or "mculink" in product_l:
        return ""
    if "stlink" in product_l or "st-link" in product_l:
        return ""
    if "j-link" in product_l or "jlink" in product_l:
        return ""
    if driver in {"cdc_acm", "ch341", "cp210x", "ftdi_sio", "pl2303"}:
        return ""
    return ""


def clean_ioreg_value(value):
    value = value.split("=", 1)[1].strip()
    if value.startswith('"'):
        return value.split('"', 2)[1]
    return value


def ioreg_depth(line):
    prefix = line.split("+-o ", 1)[0]
    return len(prefix)


def speed_from_bits(raw):
    if not raw:
        return "-"
    try:
        value = float(raw)
    except ValueError:
        return raw
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} Gb/s"
    return f"{value / 1_000_000:.1f} Mb/s"


def windows_device_details(device_id):
    """Extract USB identifiers that Windows embeds in a PnP instance ID."""
    device_id = device_id or ""
    upper = device_id.upper()
    vid_match = re.search(r"VID[_&+]([0-9A-F]{4})", upper)
    pid_match = re.search(r"PID[_&+]([0-9A-F]{4})", upper)

    serial = ""
    # FTDI places the serial in the hardware portion of its instance ID.
    ftdi_match = re.search(
        r"VID_[0-9A-F]{4}\+PID_[0-9A-F]{4}\+([^\\]+)", device_id, re.I
    )
    if ftdi_match:
        serial = ftdi_match.group(1)
    elif upper.startswith("USB\\"):
        candidate = device_id.rsplit("\\", 1)[-1]
        # IDs containing '&' are normally topology-generated, not serial numbers.
        if candidate and "&" not in candidate:
            serial = candidate

    return (
        vid_match.group(1).lower() if vid_match else "",
        pid_match.group(1).lower() if pid_match else "",
        serial,
    )


def windows_registry_ports():
    """Return COM names known to Windows, including ports with limited PnP data."""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM"
        )
    except (ImportError, OSError):
        return []

    ports = []
    try:
        index = 0
        while True:
            try:
                _, value, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            if isinstance(value, str) and re.fullmatch(r"COM\d+", value, re.I):
                ports.append(value.upper())
            index += 1
    finally:
        winreg.CloseKey(key)
    return sorted(set(ports), key=lambda value: int(value[3:]))


def _devpropkey(guid, pid):
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    class DEVPROPKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.ULONG)]

    a, b, c, d, e = guid.split("-")
    tail = bytes.fromhex(d + e)
    return DEVPROPKEY(GUID(int(a, 16), int(b, 16), int(c, 16), (ctypes.c_ubyte * 8)(*tail)), pid)


class _Cfgmgr:
    """Minimal cfgmgr32 binding: locate present devnodes and read string properties."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes, self._wintypes = ctypes, wintypes
        self._dll = ctypes.WinDLL("cfgmgr32")
        self.parent_key = _devpropkey("4340a6c5-93fa-4706-972c-7b648008a5a7", 8)
        self.busdesc_key = _devpropkey("540b947e-8b40-45bc-a8a2-6a0b894cbda2", 4)

    def locate(self, instance):
        """Devnode handle for a *present* device, or None (unplugged ghosts fail)."""
        devinst = self._wintypes.DWORD()
        if self._dll.CM_Locate_DevNodeW(self._ctypes.byref(devinst), instance, 0):
            return None
        return devinst

    def string(self, devinst, key):
        ctypes = self._ctypes
        ptype = self._wintypes.ULONG()
        size = self._wintypes.ULONG(1024)
        buf = ctypes.create_string_buffer(1024)
        if self._dll.CM_Get_DevNode_PropertyW(
            devinst, ctypes.byref(key), ctypes.byref(ptype), buf, ctypes.byref(size), 0
        ):
            return ""
        return buf.raw[: size.value].decode("utf-16-le", "replace").rstrip("\x00")

    def parent(self, devinst):
        return self.string(devinst, self.parent_key)

    def bus_description(self, devinst):
        return self.string(devinst, self.busdesc_key)


def windows_usb_devices():
    """[(vid, pid, serial, product)] for present USB devices, read in-process.

    Only top-level devices are listed; per-interface (MI_xx) children belong to
    the composite parent that carries the serial and the product string.
    """
    import winreg

    cfg = _Cfgmgr()
    devices = []
    root = r"SYSTEM\CurrentControlSet\Enum\USB"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as usb:
        for dev in _subkeys(usb):
            match = re.fullmatch(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", dev, re.I)
            if not match:  # skips &MI_xx interfaces and root hubs
                continue
            with winreg.OpenKey(usb, dev) as dev_key:
                for inst in _subkeys(dev_key):
                    devinst = cfg.locate("USB\\" + dev + "\\" + inst)
                    if devinst is None:
                        continue
                    # '&' marks an ID generated from the port topology, not a serial.
                    serial = "" if "&" in inst else inst
                    devices.append((
                        match.group(1).lower(),
                        match.group(2).lower(),
                        serial,
                        cfg.bus_description(devinst),
                    ))
    return devices


def windows_fast_ports():
    """Enumerate COM ports from the registry and cfgmgr32 in milliseconds.

    Spawning PowerShell/CIM costs ~2 s; this reads the same data in-process.
    Raises on any failure so the caller can fall back to the slow path.
    """
    import winreg

    cfg = _Cfgmgr()

    def value(key, name):
        try:
            data, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return ""
        return data if isinstance(data, str) else ""

    root = r"SYSTEM\CurrentControlSet\Enum"
    # SERIALCOMM drops a port while it is closed, so presence comes from cfgmgr32.
    candidates = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as enum:
        for bus in _subkeys(enum):
            with winreg.OpenKey(enum, bus) as bus_key:
                for dev in _subkeys(bus_key):
                    with winreg.OpenKey(bus_key, dev) as dev_key:
                        for inst in _subkeys(dev_key):
                            try:
                                with winreg.OpenKey(dev_key, inst) as inst_key:
                                    with winreg.OpenKey(inst_key, "Device Parameters") as params:
                                        port = value(params, "PortName").upper()
                                    if not port:
                                        continue
                                    candidates.append((
                                        port,
                                        f"{bus}\\{dev}\\{inst}",
                                        value(inst_key, "FriendlyName"),
                                        value(inst_key, "DeviceDesc").rsplit(";", 1)[-1],
                                        value(inst_key, "Mfg").rsplit(";", 1)[-1],
                                        value(inst_key, "Service"),
                                    ))
                            except OSError:
                                continue

    result = []
    seen = set()
    for port, instance, friendly, desc, mfg, service in candidates:
        if port in seen:
            continue
        # Locating fails for ghost (unplugged) devices, so stale entries drop out.
        devinst = cfg.locate(instance)
        if devinst is None:
            continue
        seen.add(port)
        upper = instance.upper()
        usb = upper.startswith(("USB\\", "FTDIBUS\\"))
        parent = cfg.parent(devinst) if usb else ""
        busdesc = cfg.bus_description(devinst) if usb else ""
        if usb and not busdesc and parent:
            # FTDIBUS ports carry no descriptor; their USB parent does.
            parent_inst = cfg.locate(parent)
            if parent_inst is not None:
                busdesc = cfg.bus_description(parent_inst)
        result.append(
            {
                "Port": port,
                "Name": friendly or f"{desc} ({port})",
                "Description": desc,
                "Manufacturer": mfg.rsplit(";", 1)[-1],
                "Service": service,
                "PNPDeviceID": instance,
                "Status": "OK",
                "Parent": parent,
                "BusDesc": busdesc,
            }
        )
    return sorted(result, key=lambda item: int(item["Port"][3:]))


def _subkeys(key):
    import winreg

    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def windows_pnp_ports():
    """Query serial-port metadata without requiring pyserial or third-party WMI."""
    try:
        return windows_fast_ports()
    except Exception:  # noqa: BLE001 - any API/registry surprise falls back to CIM
        pass
    script = r'''
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-CimInstance -ClassName Win32_PnPEntity -Filter "PNPClass = 'Ports'" |
    Where-Object { $_.Name -match '\(COM\d+\)\s*$' } |
    ForEach-Object {
        # Only USB ports carry a product string and a parent holding the serial.
        $props = @{}
        if ($_.PNPDeviceID -match '^(USB|FTDIBUS)\\') {
            Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID -ErrorAction SilentlyContinue `
                -KeyName 'DEVPKEY_Device_Parent', 'DEVPKEY_Device_BusReportedDeviceDesc' |
                ForEach-Object { $props[$_.KeyName] = $_.Data }
        }
        [PSCustomObject]@{
            Port = [regex]::Match($_.Name, '\((COM\d+)\)\s*$').Groups[1].Value
            Name = $_.Name
            Description = $_.Description
            Manufacturer = $_.Manufacturer
            Service = $_.Service
            PNPDeviceID = $_.PNPDeviceID
            Status = $_.Status
            Parent = $props['DEVPKEY_Device_Parent']
            BusDesc = $props['DEVPKEY_Device_BusReportedDeviceDesc']
        }
    } | ConvertTo-Json -Compress
'''
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        return []
    try:
        out = subprocess.check_output(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            text=True,
            encoding="utf-8-sig",
            errors="replace",
            stderr=subprocess.DEVNULL,
        ).strip()
        data = json.loads(out) if out else []
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else [data]


def windows_serial_rows():
    devices = {}
    for item in windows_pnp_ports():
        port = str(item.get("Port") or "").upper()
        if re.fullmatch(r"COM\d+", port):
            devices[port] = item

    # The registry fallback also catches virtual and legacy ports that CIM omits.
    for port in windows_registry_ports():
        devices.setdefault(port, {"Port": port})

    rows = []
    for port in sorted(devices, key=lambda value: int(value[3:])):
        item = devices[port]
        device_id = item.get("PNPDeviceID") or ""
        vid, pid, serial = windows_device_details(device_id)
        is_ftdi = device_id.upper().startswith("FTDIBUS\\")
        if not (is_ftdi or device_id.upper().startswith("USB\\")):
            # Bluetooth instance IDs contain VID&/PID& tokens that are not USB.
            vid = pid = serial = ""
        if vid and (is_ftdi or not serial):
            # FTDI appends the channel letter to the serial in its instance ID, and
            # composite devices keep the serial on the parent, not on the
            # per-interface (MI_xx) instance that owns the COM port.
            serial = windows_device_details(item.get("Parent") or "")[2] or serial
        name = item.get("Name") or item.get("Description") or "Serial port"
        # The localized "USB Serial Device (COMn)" name is identical for every
        # CDC port; the descriptor string from the device itself tells them apart.
        product = item.get("BusDesc") or re.sub(r"\s*\(COM\d+\)\s*$", "", name, flags=re.I)
        bus = "USB" if vid and pid else "-"
        driver = item.get("Service") or "-"
        rows.append(
            {
                "tty": port,
                "bus": bus,
                "dev": "-",
                # Keep this ASCII: legacy Windows consoles cannot encode the
                # private-use Nerd Font glyphs used by the Unix presentation.
                "icon": "U" if bus == "USB" else "S",
                "vid": f"0x{vid}" if vid else "-",
                "pid": f"0x{pid}" if pid else "-",
                "product": product or "Serial port",
                "serial": serial or "-",
                "driver": driver,
                "speed": "-",
                "perms": item.get("Status") or "-",
                "by_id": device_id,
                "manufacturer": item.get("Manufacturer") or "-",
                "match_vid": vid,
                "match_pid": pid,
                "match_serial": serial,
                "match_product": product or "Serial port",
            }
        )
    return rows


_IOREG_CACHE = {}


def macos_ioreg(*args):
    if args not in _IOREG_CACHE:
        try:
            _IOREG_CACHE[args] = subprocess.check_output(
                ["ioreg", *args, "-l", "-w", "0"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.CalledProcessError):
            _IOREG_CACHE[args] = ""
    return _IOREG_CACHE[args]


def macos_usb_ioreg():
    # Only the USB device subtrees: ~30x faster than dumping the whole registry.
    # Older macOS names the class IOUSBDevice, so fall back to the full dump.
    return macos_ioreg("-r", "-c", "IOUSBHostDevice") or macos_ioreg()


def macos_ioreg_rows():
    out = macos_usb_ioreg()
    if not out:
        return []

    inherited_keys = {
        "product",
        "manufacturer",
        "serial",
        "vid",
        "pid",
        "speed",
        "bus",
        "dev",
        "driver",
    }
    stack = {}
    rows = []
    depth = 0

    def new_node(line):
        nonlocal depth
        depth = ioreg_depth(line)
        for existing in list(stack):
            if existing >= depth:
                del stack[existing]

        parent_depths = [existing for existing in stack if existing < depth]
        parent = stack[max(parent_depths)] if parent_depths else {}
        node = {key: parent.get(key, "") for key in inherited_keys}
        node["callout"] = ""
        node["class"] = ""
        stack[depth] = node

    seen = set()

    def emit(node):
        callout = node.get("callout", "")
        if not callout or not node.get("vid") or not node.get("pid"):
            return
        # Hubs are IOUSBHostDevices too, so -r lists nested devices more than once.
        if not callout.startswith("/dev/cu.") or callout in seen:
            return
        seen.add(callout)

        product = node.get("product") or "USB serial device"
        driver = node.get("driver") or "-"
        rows.append(
            {
                "tty": callout,
                "bus": node.get("bus") or "-",
                "dev": node.get("dev") or "-",
                "icon": icon_for(product, driver),
                "vid": f"0x{int(node['vid']):04x}",
                "pid": f"0x{int(node['pid']):04x}",
                "product": product,
                "serial": node.get("serial") or "-",
                "driver": driver,
                "speed": speed_from_bits(node.get("speed", "")),
                "perms": mode_user_group(callout),
                "by_id": "",
                "manufacturer": node.get("manufacturer") or "-",
                "match_vid": f"{int(node['vid']):04x}",
                "match_pid": f"{int(node['pid']):04x}",
                "match_serial": clean_missing(node.get("serial", "")),
                "match_product": product,
            }
        )

    for line in out.splitlines():
        if "+-o " in line:
            if depth in stack:
                emit(stack[depth])
            new_node(line)
            continue

        if depth not in stack or '"' not in line or "=" not in line:
            continue

        key = line.split('"', 2)[1]
        value = clean_ioreg_value(line)
        node = stack[depth]

        if key == "IOCalloutDevice":
            node["callout"] = value
        elif key in {"USB Product Name", "kUSBProductString", "Product Name"}:
            node["product"] = value
        elif key in {"USB Vendor Name", "kUSBVendorString"}:
            node["manufacturer"] = value
        elif key in {"USB Serial Number", "kUSBSerialNumberString"}:
            node["serial"] = value
        elif key == "idVendor":
            node["vid"] = value
        elif key == "idProduct":
            node["pid"] = value
        elif key == "UsbLinkSpeed":
            node["speed"] = value
        elif key == "USB Address":
            node["dev"] = value
        elif key == "locationID":
            node["bus"] = f"{int(value):x}"[-4:]
        elif key in {"kUSBString", "IOClass"}:
            node["driver"] = value

    if depth in stack:
        emit(stack[depth])

    return rows


def short(value, width):
    value = value or "-"
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def middle_short(value, width):
    value = value or "-"
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    left = (width - 1) // 2
    right = width - 1 - left
    return f"{value[:left]}…{value[-right:]}"


def visible_len(value):
    return len(ANSI_RE.sub("", value))


def paint(value, color):
    return f"{color}{value}{Color.reset}" if Color.enabled else value


def fit_ansi(value, width):
    plain = ANSI_RE.sub("", value)
    if len(plain) <= width:
        return value
    return short(plain, width)


def output_width():
    """Terminal width; unlimited when piped (agents) unless COLUMNS says otherwise."""
    if os.environ.get("COLUMNS", "").isdigit():
        return int(os.environ["COLUMNS"])
    if not sys.stdout.isatty():
        return 10**6
    return shutil.get_terminal_size((100, 20)).columns


class Col:
    """A table column.

    min:  narrowest useful width.
    drop: optional column; when space is short it is shrunk first and then
          dropped, higher numbers first. None means always shown.
    flex: free text that only takes the space left over (never forces drops).
    mid:  truncate in the middle (paths, serials) instead of at the end.
    """

    def __init__(self, key, header, color="", min=4, drop=None, flex=False, mid=False, right=False):
        self.key, self.header, self.color = key, header, color
        self.min, self.drop, self.flex, self.mid, self.right = min, drop, flex, mid, right


def fit_widths(cols, rows, width):
    """Pick the visible columns and their widths so each line fits in width."""
    natural = {c.key: max([len(c.header)] + [len(r.get(c.key) or "-") for r in rows]) for c in cols}
    floor = {key: min(value, next(c.min for c in cols if c.key == key)) for key, value in natural.items()}
    cols = list(cols)

    def total(widths):
        return sum(widths[c.key] for c in cols) + len(cols) - 1

    def shrink(targets, widths, widest_first=False):
        excess = total(widths) - width
        while excess > 0:
            open_ = [c for c in targets if widths[c.key] > floor[c.key]]
            if not open_:
                return
            pick = max(open_, key=lambda c: widths[c.key]) if widest_first else open_[0]
            widths[pick.key] -= 1
            excess -= 1

    while True:
        widths = {c.key: floor[c.key] if c.flex else natural[c.key] for c in cols}
        optional = sorted((c for c in cols if c.drop is not None), key=lambda c: -c.drop)
        shrink(optional, widths)
        if total(widths) <= width:
            break
        if optional:
            cols.remove(optional[0])
            continue
        shrink([c for c in cols if not c.flex], widths, widest_first=True)
        break

    # Hand leftover space to free text first, then back to shrunk optional columns.
    spare = width - total(widths)
    regrow = [c for c in cols if c.flex] + sorted(
        (c for c in cols if c.drop is not None), key=lambda c: c.drop
    )
    for c in regrow:
        if spare <= 0:
            break
        grow = min(spare, natural[c.key] - widths[c.key])
        widths[c.key] += grow
        spare -= grow
    return cols, widths


def render_columns(cols, rows, width=None):
    """Print rows (dicts of plain strings) sized to the terminal.

    A row may set "_dim" to grey the whole line.
    """
    width = output_width() if width is None else width
    cols, widths = fit_widths(cols, rows, width)

    def cell(col, value):
        w = widths[col.key]
        value = (middle_short if col.mid else short)(value, w)
        return f"{value:>{w}}" if col.right else f"{value:<{w}}"

    def row_cell(col, row):
        return cell(col, row.get(col.key) or "-")

    header = " ".join(cell(c, c.header) if c.header else " " * widths[c.key] for c in cols).rstrip()
    print(paint(header, Color.dim))
    print(paint("-" * len(header), Color.dim))
    for row in rows:
        if row.get("_dim"):
            print(paint(" ".join(row_cell(c, row) for c in cols).rstrip(), Color.dim))
            continue
        fields = [paint(row_cell(c, row), c.color) if c.color else row_cell(c, row) for c in cols]
        print(" ".join(fields).rstrip())


def by_id_label(value):
    if not value:
        return "-"
    return ", ".join(Path(part).name for part in value.split(", "))


def maybe_permission_color(perms):
    if perms.startswith("crw-rw-rw-"):
        return Color.green
    if "uucp" in perms or "dialout" in perms:
        return Color.yellow
    return Color.red


def load_link_config():
    try:
        data = json.loads(LINKS_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"link_dir": str(DEFAULT_LINK_DIR), "links": []}

    if not isinstance(data, dict):
        return {"link_dir": str(DEFAULT_LINK_DIR), "links": []}
    data.setdefault("link_dir", str(DEFAULT_LINK_DIR))
    data.setdefault("links", [])
    data["links"] = [link for link in data["links"] if isinstance(link, dict)]
    return data


def save_link_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LINKS_CONFIG.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def link_dir_from_config(config):
    return Path(config.get("link_dir") or DEFAULT_LINK_DIR).expanduser()


def valid_link_name(name):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name or ""))


def row_usb_session(row):
    return row["bus"] if sys.platform == "win32" else f"{row['bus']}/{row['dev']}"


def row_link_criteria(row):
    criteria = {
        "vid": row.get("match_vid", ""),
        "pid": row.get("match_pid", ""),
        "serial": row.get("match_serial", ""),
        "product": row.get("match_product", ""),
    }
    return {key: value for key, value in criteria.items() if value}


def row_matches_link(row, link):
    if link.get("vid") and normalize_usb_id(row.get("match_vid")) != normalize_usb_id(link["vid"]):
        return False
    if link.get("pid") and normalize_usb_id(row.get("match_pid")) != normalize_usb_id(link["pid"]):
        return False
    if link.get("serial") and clean_missing(row.get("match_serial")) != clean_missing(link["serial"]):
        return False
    if link.get("product"):
        product = clean_missing(row.get("match_product")).lower()
        if link["product"].lower() not in product:
            return False
    return True


def bound_names_for_row(row, config):
    names = [
        link.get("name", "")
        for link in config.get("links", [])
        if link.get("name") and row_matches_link(row, link)
    ]
    return ", ".join(sorted(names)) if names else "-"


def find_link_target(rows, link):
    matches = [row for row in rows if row_matches_link(row, link)]
    if len(matches) == 1:
        return matches[0], ""
    if len(matches) > 1:
        return None, f"multiple matches for {link['name']}"
    return None, "not connected"


def replace_symlink(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not path.is_symlink():
            raise RuntimeError(f"{path} exists and is not a symlink")
        current = os.readlink(path)
        if current == target:
            return False
        path.unlink()
    path.symlink_to(target)
    return True


def remove_owned_symlink(path):
    if path.is_symlink():
        path.unlink()
        return True
    return False


def sync_links(verbose=True, rows=None):
    config = load_link_config()
    link_dir = link_dir_from_config(config)
    rows = serial_ttys() if rows is None else rows
    changed = 0
    problems = 0

    for link in config["links"]:
        name = link.get("name", "")
        if not valid_link_name(name):
            problems += 1
            if verbose:
                print(f"skip invalid link name: {name}", file=sys.stderr)
            continue

        path = link_dir / name
        row, reason = find_link_target(rows, link)
        if not row:
            changed += int(remove_owned_symlink(path))
            if verbose:
                print(f"{name}: {reason}")
            continue

        try:
            did_change = replace_symlink(path, row["tty"])
        except RuntimeError as exc:
            problems += 1
            if verbose:
                print(f"{name}: {exc}", file=sys.stderr)
            continue

        changed += int(did_change)
        if verbose:
            action = "linked" if did_change else "ok"
            print(f"{name}: {action} {path} -> {row['tty']}")

    return 1 if problems else 0


def bind_link(args):
    if not valid_link_name(args.name):
        print("Link name may only contain letters, numbers, dot, dash, and underscore.", file=sys.stderr)
        return 2

    rows = serial_ttys()
    criteria = {
        "vid": normalize_usb_id(args.vid) if args.vid else "",
        "pid": normalize_usb_id(args.pid) if args.pid else "",
        "serial": args.serial or "",
        "product": args.product or "",
    }

    if args.usb:
        matches = [row for row in rows if row_usb_session(row) == args.usb]
        if len(matches) != 1:
            print(f"Expected exactly one device with USB session {args.usb}, found {len(matches)}.", file=sys.stderr)
            return 2
        criteria = row_link_criteria(matches[0])

    if args.id:
        if ":" not in args.id:
            print("--id must look like 0483:374f or 0x0483:0x374f", file=sys.stderr)
            return 2
        vid, pid = args.id.split(":", 1)
        criteria["vid"] = normalize_usb_id(vid)
        criteria["pid"] = normalize_usb_id(pid)

    if not any(criteria.values()):
        if len(rows) != 1:
            print("Specify USB session, --id/--serial/--product, or leave exactly one serial device connected.", file=sys.stderr)
            return 2
        criteria = row_link_criteria(rows[0])

    link = {"name": args.name, **{key: value for key, value in criteria.items() if value}}
    config = load_link_config()
    if args.link_dir:
        config["link_dir"] = str(Path(args.link_dir).expanduser())
    config["links"] = [existing for existing in config["links"] if existing.get("name") != args.name]
    config["links"].append(link)
    save_link_config(config)

    print(f"Bound {args.name} in {LINKS_CONFIG}")
    return sync_links(verbose=True)


def list_links():
    config = load_link_config()
    if not config["links"]:
        print("No links configured.")
        return 1
    print(f"Link dir: {link_dir_from_config(config)}")
    for link in config["links"]:
        parts = [link["name"]]
        if link.get("vid") and link.get("pid"):
            parts.append(f"{link['vid']}:{link['pid']}")
        if link.get("serial"):
            parts.append(f"serial={link['serial']}")
        if link.get("product"):
            parts.append(f"product={link['product']}")
        print("  " + "  ".join(parts))
    return 0


def unbind_link(name):
    config = load_link_config()
    link_dir = link_dir_from_config(config)
    before = len(config["links"])
    config["links"] = [link for link in config["links"] if link.get("name") != name]
    if len(config["links"]) == before:
        print(f"No link named {name}.", file=sys.stderr)
        return 1
    save_link_config(config)
    remove_owned_symlink(link_dir / name)
    print(f"Unbound {name}")
    return 0


def plan_link_rename(config, old, new):
    """Validate a rename and return (link, error); nothing is modified."""
    link_dir = link_dir_from_config(config)
    if not valid_link_name(new):
        return None, "Link name may only contain letters, numbers, dot, dash, and underscore."
    link = next((item for item in config["links"] if item.get("name") == old), None)
    if link is None:
        return None, f"No link named {old}."
    if old == new:
        return None, f"{old} already has that name."
    if any(item.get("name") == new for item in config["links"]):
        return None, f"A link named {new} already exists."
    new_path = link_dir / new
    if new_path.exists() or new_path.is_symlink():
        return None, f"{new_path} already exists; refusing to overwrite it."
    return link, ""


def rename_link(old, new):
    """Rename a bound link in the config and its symlink, rolling back on failure."""
    config = load_link_config()
    link, error = plan_link_rename(config, old, new)
    if error:
        print(error, file=sys.stderr)
        return 2
    link_dir = link_dir_from_config(config)
    old_path, new_path = link_dir / old, link_dir / new

    # Create the new symlink first so the device is never left without an alias.
    created = False
    try:
        if old_path.is_symlink():
            new_path.symlink_to(os.readlink(old_path))
            created = True
        link["name"] = new
        save_link_config(config)
    except OSError as exc:
        if created:
            new_path.unlink()
        print(f"Could not rename {old} to {new}: {exc}; nothing changed.", file=sys.stderr)
        return 1
    remove_owned_symlink(old_path)
    print(f"Renamed {old} -> {new}")
    return 0


def links_for_serial(config, normalize, serial):
    """Names of bound links whose USB serial matches the given probe serial."""
    return [
        link["name"]
        for link in config["links"]
        if link.get("name") and link.get("serial") and normalize(link["serial"]) == serial
    ]


def run_service(interval):
    while True:
        # Device topology can change while this process stays alive.  Keep the
        # per-invocation ioreg/USB caches useful for one sync, but discard them
        # before the next poll so newly attached devices can be linked.
        global _USB_DEVICES
        _IOREG_CACHE.clear()
        _USB_DEVICES = None
        sync_links(verbose=False)
        time.sleep(interval)


def install_macos_service(interval):
    if sys.platform != "darwin":
        print("install-service is currently implemented for macOS launchd only.", file=sys.stderr)
        return 2

    label = "local.lstty.links"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist = launch_agents / f"{label}.plist"
    was_installed = plist.exists()
    script_path = Path(__file__).resolve()
    stdout = CONFIG_DIR / "service.log"
    stderr = CONFIG_DIR / "service.err.log"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{script_path}</string>
    <string>service</string>
    <string>--interval</string>
    <string>{interval}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{stdout}</string>
  <key>StandardErrorPath</key>
  <string>{stderr}</string>
</dict>
</plist>
""",
        encoding="utf-8",
    )

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "enable", service], stderr=subprocess.DEVNULL)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)])
    if result.returncode != 0:
        print(f"Wrote {plist}, but launchctl bootstrap failed.", file=sys.stderr)
        return result.returncode
    subprocess.run(["launchctl", "enable", service], stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "kickstart", "-k", service], stderr=subprocess.DEVNULL)
    print(f"{'Updated' if was_installed else 'Installed'} {label}")
    print(f"Loaded in launchd user domain: {service}")
    print(f"Starts at login: {plist}")
    print(f"Links: {link_dir_from_config(load_link_config())}")
    return 0


def uninstall_macos_service():
    if sys.platform != "darwin":
        print("uninstall-service is currently implemented for macOS launchd only.", file=sys.stderr)
        return 2
    label = "local.lstty.links"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], stderr=subprocess.DEVNULL)
    if plist.exists():
        plist.unlink()
    print(f"Uninstalled {label}")
    return 0


# --- Debug probe nicknames (J-Link, ST-Link, MCU-Link) ---------------------
#
# Each probe family keeps nicknames and "last used for" notes in
# ~/.config/lstty/<family>.json. Every change also rewrites the family's env
# file, which `lstty <family> install` sources from the shell startup files,
# so new shells (including agents') see, e.g. for J-Link:
#
#   JLINK_SN_<NAME>           probe serial number
#   JLINK_DEVICE_<NAME>       target device name, when saved
#   JLINK_LAST_USE_<NAME>     "what it was last used for (YYYY-MM-DD HH:MM)"
#   JLINK_LAST_USE_AT_<NAME>  epoch seconds of that use
#   JLINK_LAST_USE            the most recent use across all probes
#   JLINK_PROBES              "name=serial ..." for every nickname
#
# and the same with STLINK_ for ST-Links. Environment values are a snapshot
# from when the shell started; for a live "how long ago", ask `lstty <family> last`.

XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SEGGER_VID = "1366"
ST_VID = "0483"
NXP_VID = "1fc9"


def jlink_serial(value):
    # USB descriptors zero-pad J-Link serials (000802008564); J-Link tools do not.
    value = clean_missing(value)
    if not value.isdigit():
        return ""
    return value.lstrip("0") or value


def stlink_serial(value):
    value = clean_missing(value)
    if not value:
        return ""
    if not value.isascii() or not value.isalnum():
        # Old ST-Link/V2 firmware reports raw bytes; tools show them as hex.
        value = value.encode("latin-1", "replace").hex()
    return value.upper()


def mculink_serial(value):
    value = clean_missing(value)
    if not value or not value.isascii() or not value.isalnum():
        return ""
    return value.upper()


def cmsisdap_serial(value):
    return mculink_serial(value)


class ProbeFamily:
    def __init__(self, key, title, vid, pids, normalize, serial_re, serial_hint, device_example, tools, ids=None):
        self.key = key
        self.title = title
        self.prefix = key.upper()
        self.vid = vid
        self.pids = pids
        self.ids = ids  # {(vid, pid)} for families that span vendors
        self.normalize = normalize
        self.serial_re = serial_re
        self.serial_hint = serial_hint
        self.device_example = device_example
        self.tools = tools
        self.config = CONFIG_DIR / f"{key}.json"
        self.lock = CONFIG_DIR / f".{key}.lock"
        self.env_file = XDG_CONFIG / key / "env.sh"
        # systemd user sessions (Linux desktops) read this, so GUI apps see it too.
        self.systemd_env = XDG_CONFIG / "environment.d" / f"60-{key}-probes.conf"
        self.mark_begin = f"# >>> {key}-nick >>>"
        self.mark_end = f"# <<< {key}-nick <<<"

    def matches(self, vid, pid):
        if self.ids is not None:
            return (vid, pid) in self.ids
        return vid == self.vid and (self.pids is None or pid in self.pids)


PROBE_FAMILIES = {
    "jlink": ProbeFamily(
        "jlink", "J-Link", SEGGER_VID, None, jlink_serial, r"\d+", "numeric",
        "nRF5340_xxAA_APP",
        "west flash --dev-id $JLINK_SN_X | JLinkExe -USB $JLINK_SN_X | nrfutil --serial-number",
    ),
    "stlink": ProbeFamily(
        "stlink", "ST-Link", ST_VID,
        # V1, V2, V2-1 variants, V3 loader/E/SET/MINI variants, V3PWR.
        {"3744", "3748", "374a", "374b", "3752", "374d", "374e", "374f", "3753", "3754", "3757"},
        stlink_serial, r"[0-9A-Za-z]+", "letters and digits",
        "STM32H743ZI",
        "west flash --dev-id $STLINK_SN_X | STM32_Programmer_CLI -c port=SWD sn=$STLINK_SN_X"
        " | openocd -c 'adapter serial $STLINK_SN_X' | st-flash --serial $STLINK_SN_X",
    ),
    "mculink": ProbeFamily(
        "mculink", "MCU-Link", NXP_VID, {"0143"}, mculink_serial,
        r"[0-9A-Za-z]+", "letters and digits", "LPC55S69",
        "LinkServer probes | CMSIS-DAP tools",
    ),
    # Generic CMSIS-DAP probes from other vendors. MCU-Link is a CMSIS-DAP probe
    # too, but it keeps its own family: bench RTT, MCULINK_SN_* and saved
    # nicknames already depend on that name.
    "cmsisdap": ProbeFamily(
        "cmsisdap", "CMSIS-DAP", None, None, cmsisdap_serial,
        r"[0-9A-Za-z]+", "letters and digits", "nRF52840_xxAA",
        "pyocd -u $CMSISDAP_SN_X | openocd -c 'adapter serial $CMSISDAP_SN_X' | probe-rs --probe VID:PID:$CMSISDAP_SN_X",
        ids={
            ("d6e7", "3507"),  # Sipeed RV-Debugger / Slogic Combo
            ("42bf", "b210"),  # CKLink-Lite
            ("0d28", "0204"),  # Arm DAPLink
            ("2e8a", "000c"),  # Raspberry Pi Debugprobe
        },
    ),
}


def probe_var_suffix(name):
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


def format_ago(epoch, now=None):
    if not epoch:
        return "-"
    delta = max(0, int((now or time.time()) - epoch))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{delta // size}{unit} ago"
    return f"{delta}s ago"


def format_stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def load_probe_config(fam):
    try:
        data = json.loads(fam.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("probes", {})
    data.setdefault("usage", {})
    return data


def load_probe_config_strict(fam):
    if not fam.config.exists():
        return {"probes": {}, "usage": {}}
    try:
        data = json.loads(fam.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {fam.config}: {exc}") from exc
    if (not isinstance(data, dict)
            or not isinstance(data.get("probes", {}), dict)
            or not isinstance(data.get("usage", {}), dict)):
        raise RuntimeError(f"invalid probe config in {fam.config}")
    data.setdefault("probes", {})
    data.setdefault("usage", {})
    return data


def load_board_registry():
    if not BOARD_CONFIG.exists():
        return {"chips": {}}
    try:
        data = json.loads(BOARD_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {BOARD_CONFIG}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("chips"), dict):
        raise RuntimeError(f"invalid board registry in {BOARD_CONFIG}; expected a chips object")
    return data


def write_board_registry(registry):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=CONFIG_DIR, prefix=".boards-", suffix=".tmp", delete=False
    ) as handle:
        tmp = Path(handle.name)
        handle.write(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    try:
        os.chmod(tmp, 0o600)
        tmp.replace(BOARD_CONFIG)
    finally:
        tmp.unlink(missing_ok=True)


def update_probe_config(fam, mutate):
    """Apply mutate(config) under a lock so concurrent agents don't lose writes."""
    try:
        import fcntl
    except ImportError:  # Windows: no flock; single-writer is fine there.
        fcntl = None

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(fam.lock, "w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        config = load_probe_config(fam)
        result = mutate(config)
        tmp = fam.config.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(fam.config)
        write_probe_env(fam, config)
    return result


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def last_use_text(usage):
    return f"{usage['what']} ({format_stamp(usage['at'])})"


def write_probe_env(fam, config):
    env = {}
    names = []
    latest = None
    p = fam.prefix
    for name, probe in sorted(config["probes"].items()):
        suffix = probe_var_suffix(name)
        serial = probe["serial"]
        names.append(f"{name}={serial}")
        env[f"{p}_SN_{suffix}"] = serial
        if probe.get("device"):
            env[f"{p}_DEVICE_{suffix}"] = probe["device"]
        usage = config["usage"].get(serial)
        if usage:
            env[f"{p}_LAST_USE_{suffix}"] = last_use_text(usage)
            env[f"{p}_LAST_USE_AT_{suffix}"] = str(int(usage["at"]))
            if latest is None or usage["at"] > latest[1]["at"]:
                latest = (name, usage)
    env[f"{p}_PROBES"] = " ".join(names)
    if latest:
        env[f"{p}_LAST_USE"] = f"{latest[0]}: {last_use_text(latest[1])}"

    fam.env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Generated by lstty {fam.key} from {fam.config}. Do not edit."]
    lines += [f"export {key}={shell_quote(value)}" for key, value in env.items()]
    tmp = fam.env_file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(fam.env_file)

    if sys.platform.startswith("linux"):
        fam.systemd_env.parent.mkdir(parents=True, exist_ok=True)
        # environment.d takes bare KEY=VALUE; newlines are never stored.
        fam.systemd_env.write_text(
            "".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8"
        )


_USB_DEVICES = None


def usb_devices():
    """Return [(vid, pid, serial, product)] for every USB device, with or without a tty."""
    global _USB_DEVICES
    if _USB_DEVICES is not None:
        return _USB_DEVICES
    devices = []
    if sys.platform == "darwin":
        for block in macos_usb_ioreg().split("+-o ")[1:]:
            props = {}
            for line in block.splitlines()[1:]:
                if '"' in line and "=" in line:
                    props[line.split('"', 2)[1]] = clean_ioreg_value(line)
            if props.get("idVendor", "").isdigit() and props.get("idProduct", "").isdigit():
                devices.append((
                    f"{int(props['idVendor']):04x}",
                    f"{int(props['idProduct']):04x}",
                    props.get("USB Serial Number", ""),
                    props.get("USB Product Name", ""),
                ))
    elif sys.platform == "win32":
        try:
            devices = windows_usb_devices()
        except Exception:  # noqa: BLE001 - no USB inventory beats a crashed listing
            devices = []
    elif sys.platform.startswith("linux"):
        for dev in Path("/sys/bus/usb/devices").glob("*"):
            vid = read_text(dev / "idVendor").lower()
            if vid:
                devices.append((
                    vid,
                    read_text(dev / "idProduct").lower(),
                    read_text(dev / "serial"),
                    read_text(dev / "product"),
                ))
    _USB_DEVICES = devices
    return devices


def connected_probes(fam):
    """Return {serial: product} for this family's probes on USB."""
    probes = {}
    for vid, pid, serial, product in usb_devices():
        serial = fam.normalize(serial) if fam.matches(vid, pid) else ""
        if serial:
            probes[serial] = product or fam.title
    return probes


def probe_names_by_serial(config):
    names = {}
    for name, probe in config["probes"].items():
        names.setdefault(probe["serial"], []).append(name)
    return {serial: sorted(values) for serial, values in names.items()}


def probe_ttys(fam, rows=None):
    """Return {serial: [tty, ...]} for probes that expose a virtual COM port."""
    # Probe lookups are often the first operation after a cable replug. Refresh
    # the stable links from the live USB inventory before returning any ports.
    # The configured link criteria (VID/PID/serial/product) identify the USB
    # device; the current tty is only the transient target for this session.
    if rows is None:
        sync_links(verbose=False)
    ttys = {}
    for row in serial_ttys() if rows is None else rows:
        if fam.matches(row.get("match_vid"), row.get("match_pid")):
            serial = fam.normalize(row.get("match_serial", ""))
            if serial:
                ttys.setdefault(serial, []).append(row["tty"])
    return ttys


def port_label(ttys):
    return ",".join(tty.removeprefix("/dev/") for tty in ttys)


def resolve_probe(fam, config, ref):
    """Accept a nickname or a serial; return (serial, nickname-or-None)."""
    if ref in config["probes"]:
        return config["probes"][ref]["serial"], ref
    serial = fam.normalize(ref)
    names = probe_names_by_serial(config).get(serial)
    if names:
        return serial, names[0]
    if serial and (serial in config["usage"] or serial in connected_probes(fam)):
        return serial, None
    raise KeyError(ref)


def probe_rows(fam, config=None, connected=None, ttys=None, show_all=False):
    """Rows for one probe family: the data behind both the table and --json."""
    config = config or load_probe_config(fam)
    connected = connected if connected is not None else connected_probes(fam)
    ttys = ttys if ttys is not None else probe_ttys(fam)
    names = probe_names_by_serial(config)
    serials = sorted(connected)
    if show_all:
        known = set(names) | set(config["usage"])
        serials += sorted(s for s in known if s not in connected)
    now = time.time()
    rows = []
    for serial in serials:
        nicks = names.get(serial, [])
        device = next((config["probes"][n].get("device") for n in nicks if config["probes"][n].get("device")), "")
        usage = config["usage"].get(serial)
        online = serial in connected
        rows.append({
            "serial": serial,
            "nick": ", ".join(nicks),
            "status": "ONLINE" if online else "OFFLINE",
            "port": (port_label(ttys.get(serial, [])) or "-") if online else "-",
            "device": device,
            "ago": format_ago(usage["at"], now) if usage else "",
            "what": usage["what"] if usage else "",
            "_dim": not online,
        })
    return rows


def render_probe_table(fam, config=None, connected=None, ttys=None, heading=False, show_all=False):
    rows = probe_rows(fam, config, connected, ttys, show_all)
    if not rows:
        if not heading:
            hint = "" if show_all else " (-a includes offline probes)"
            print(f"No {fam.title} probes connected{hint}.")
        return 1
    cols = [
        Col("serial", "SERIAL", Color.green, min=8, mid=True),
        Col("nick", "BOARD/PROBE", Color.magenta, min=11),
        Col("status", "STATUS", Color.green, min=8),
        Col("port", "PORT", Color.cyan, min=4, drop=1, mid=True),
        Col("device", "DEVICE", min=6, drop=2),
        Col("ago", "AGO", Color.yellow, min=7),
        Col("what", "LAST USE", min=8, flex=True),
    ]
    if heading:
        print()
        print(paint(f"{fam.title} probes", Color.bold))
    render_columns(cols, rows)
    return 0


def probe_set(fam, args):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", args.name):
        print("Nickname must be letters, digits, '-' or '_', starting with a letter.", file=sys.stderr)
        return 2
    serial = args.serial
    if not serial:
        connected = connected_probes(fam)
        if len(connected) != 1:
            print(f"Give a serial: {len(connected)} {fam.title} probes are connected.", file=sys.stderr)
            return 2
        serial = next(iter(connected))
    if not re.fullmatch(fam.serial_re, serial):
        print(f"{fam.title} serial must be {fam.serial_hint}: {serial}", file=sys.stderr)
        return 2
    serial = fam.normalize(serial)
    suffix = probe_var_suffix(args.name)

    def mutate(config):
        for other in config["probes"]:
            if other != args.name and probe_var_suffix(other) == suffix:
                return f"'{args.name}' collides with '{other}' (both {fam.prefix}_SN_{suffix})"
        previous = config["probes"].get(args.name, {})
        probe = {"serial": serial}
        # Keep device/note on a re-point unless new values were given.
        for key in ("device", "note"):
            value = getattr(args, key)
            if value is None:
                value = previous.get(key, "")
            if value:
                probe[key] = value
        config["probes"][args.name] = probe
        return None

    error = update_probe_config(fam, mutate)
    if error:
        print(error, file=sys.stderr)
        return 2
    print(f"{fam.prefix}_SN_{suffix}={serial}")
    return 0


def probe_rm(fam, args):
    def mutate(config):
        return config["probes"].pop(args.name, None) is not None

    if not update_probe_config(fam, mutate):
        print(f"No {fam.title} nickname {args.name}.", file=sys.stderr)
        return 1
    print(f"Removed {args.name}")
    return 0


def probe_get(fam, args):
    config = load_probe_config(fam)
    name = args.name
    if getattr(args, "serial", None):
        serial = fam.normalize(args.serial)
        names = probe_names_by_serial(config).get(serial)
        if not names:
            print(f"No {fam.title} nickname for serial {serial}.", file=sys.stderr)
            return 1
        name = names[0]
    if not name:
        print("Give a nickname or --serial SERIAL.", file=sys.stderr)
        return 2
    probe = config["probes"].get(name)
    if not probe:
        print(f"No {fam.title} nickname {name}.", file=sys.stderr)
        return 1
    ttys = probe_ttys(fam).get(probe["serial"], [])
    if getattr(args, "json", False):
        print(json.dumps({"name": name, "serial": probe["serial"], "device": probe.get("device", ""),
                          "note": probe.get("note", ""), "tty": ttys,
                          "connected": probe["serial"] in connected_probes(fam)}))
        return 0
    if args.field == "tty":
        value = ",".join(ttys)
    else:
        value = probe.get(args.field, "")
    if not value:
        print(f"No {args.field} {'connected' if args.field == 'tty' else 'saved'} for {name}.", file=sys.stderr)
        return 1
    print(value)
    return 0


def probe_use(fam, args):
    what = " ".join(args.what).replace("\n", " ").strip()
    if not what:
        print("Say what the probe is being used for.", file=sys.stderr)
        return 2

    def mutate(config):
        serial, name = resolve_probe(fam, config, args.probe)
        config["usage"][serial] = {"what": what, "at": int(time.time())}
        return name or serial

    try:
        label = update_probe_config(fam, mutate)
    except KeyError:
        print(f"No {fam.title} nickname or connected serial {args.probe}.", file=sys.stderr)
        return 1
    print(f"{label}: {what}")
    return 0


def probe_last(fam, args):
    config = load_probe_config(fam)
    if not args.probe:
        return render_probe_table(fam, config, show_all=args.all_sub)
    try:
        serial, _ = resolve_probe(fam, config, args.probe)
    except KeyError:
        print(f"No {fam.title} nickname {args.probe}.", file=sys.stderr)
        return 1
    usage = config["usage"].get(serial)
    if not usage:
        print(f"{args.probe}: never recorded")
        return 1
    print(f"{usage['what']} · {format_ago(usage['at'])} ({format_stamp(usage['at'])})")
    return 0


def probe_run(fam, args):
    # argparse.REMAINDER swallows options after the probe, so read --for here.
    command, what = list(args.cmd), None
    while command and command[0] != "--" and command[0].startswith("--for"):
        if command[0] == "--for" and len(command) > 1:
            what, command = command[1], command[2:]
        elif command[0].startswith("--for="):
            what, command = command[0].split("=", 1)[1], command[1:]
        else:
            break
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("Nothing to run; put the command after --.", file=sys.stderr)
        return 2
    config = load_probe_config(fam)
    try:
        serial, name = resolve_probe(fam, config, args.probe)
    except KeyError:
        print(f"No {fam.title} nickname {args.probe}.", file=sys.stderr)
        return 1
    what = what or " ".join(command)
    try:
        # inherit=True: the lock (fd + BENCH_LOCKS_HELD) survives the exec below, so it is held
        # for as long as the command runs (west flash, a gdb server, a shell...).
        probelock.hold(serial, f"lstty run: {what}", inherit=True).acquire()
    except probelock.ProbeBusy as busy:
        print(f"{name or serial}: {busy}", file=sys.stderr)
        return 75
    probe_use(fam, argparse.Namespace(probe=args.probe, what=[what]))
    p = fam.prefix
    env = {**os.environ, f"{p}_SERIAL": serial, f"{p}_NICK": name or ""}
    device = config["probes"].get(name or "", {}).get("device")
    if device:
        env[f"{p}_DEVICE"] = device
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"{command[0]}: {exc}", file=sys.stderr)
        return 127


def jlinkexe_run(serial, device, lines, timeout=90, speed=4000):
    """Run J-Link Commander lines against one probe; return (ok, output).

    This is the one place that builds a JLinkExe command file, so scripts do not
    have to. It never uses nrfjprog (which opens a probe-selection window).
    """
    exe = shutil.which("JLinkExe")
    if not exe:
        return False, "JLinkExe not found in PATH"
    with tempfile.NamedTemporaryFile("w", suffix=".jlink", delete=False) as handle:
        handle.write("\n".join([*lines, "exit"]) + "\n")
        path = handle.name
    try:
        with probelock.hold(serial, f"lstty jlink {lines[0] if lines else 'cmd'}") as lock:
            proc = subprocess.run(
                [exe, "-USB", serial, "-device", device, "-if", "SWD", "-speed", str(speed),
                 "-autoconnect", "1", "-NoGui", "1", "-CommandFile", path],
                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                pass_fds=lock.pass_fds,
            )
    except probelock.ProbeBusy as busy:
        return False, f"probe busy: {busy}"
    except subprocess.TimeoutExpired:
        return False, f"JLinkExe timed out after {timeout} s"
    finally:
        os.unlink(path)
    out = proc.stdout + proc.stderr
    failed = re.search(r"Cannot connect|Could not connect|No emulator|ERROR", out)
    return proc.returncode == 0 and not failed, out


def pyocd_executable():
    candidates = [shutil.which("pyocd")]
    for path in (
        Path.home() / "zephyrproject/.venv/bin/pyocd",
        Path.home() / "source/zephyr-v4.3.0/.venv/bin/pyocd",
    ):
        candidates.append(str(path) if path.is_file() else None)
    return next((path for path in candidates if path), None)


def stm32_programmer_executable():
    names = ("STM32_Programmer_CLI.exe", "STM32_Programmer_CLI")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "darwin":
        for path in sorted(Path("/opt/ST").glob(
            "STM32CubeCLT_*/STM32CubeProgrammer/bin/STM32_Programmer_CLI"
        ), reverse=True):
            if path.is_file():
                return str(path)
    if sys.platform == "win32":
        roots = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin",
        )
        for root in roots:
            path = root / "STM32_Programmer_CLI.exe"
            if path.is_file():
                return str(path)
    if sys.platform.startswith("linux"):
        for root in (
            "STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin",
            "STMicroelectronics/STM32/STM32CubeProgrammer/bin",
        ):
            path = Path.home() / root / "STM32_Programmer_CLI"
            if path.is_file():
                return str(path)
    return None


def parse_debug_words(output):
    """Parse address/value rows emitted by J-Link, pyOCD, or CubeProgrammer."""
    words = {}
    for line in ANSI_RE.sub("", output).splitlines():
        match = re.search(
            r"^\s*@?\s*(?:0x)?([0-9a-f]{8})\s*(?:=|:)\s*(.*?)\s*$",
            line, re.I,
        )
        if match:
            base = int(match.group(1), 16)
            values = re.findall(r"(?:0x)?([0-9a-f]{8})", match.group(2), re.I)
            for index, value in enumerate(values):
                words[base + 4 * index] = int(value, 16)
    return words


def read_nordic_identity(fam, serial, saved_device=""):
    """Read nRF52 FICR DEVICEID and PART without resetting or unlocking it."""
    device = saved_device or "nRF52840_xxAA"
    if fam.key == "jlink":
        ok, output = jlinkexe_run(serial, device, [
            "mem32 0x10000060 2",
            "mem32 0x10000100 1",
        ])
        if not ok:
            return None, jlink_failure(output)
    elif fam.key == "mculink":
        pyocd = pyocd_executable()
        if not pyocd:
            return None, "pyOCD not found in PATH or the Zephyr virtual environments"
        target = "nrf52833" if "52833" in device.lower() else "nrf52840"
        try:
            with probelock.hold(serial, "lstty read nRF52 FICR"):
                proc = subprocess.run([
                    pyocd, "commander", "--connect", "attach", "-O", "auto_unlock=False",
                    "-u", serial, "-t", target,
                    "-c", "read32 0x10000060 8",
                    "-c", "read32 0x10000100 4",
                ], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except probelock.ProbeBusy as busy:
            return None, f"probe busy: {busy}"
        except subprocess.TimeoutExpired:
            return None, "pyOCD timed out reading nRF52 FICR"
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            return None, output.strip().splitlines()[-1] if output.strip() else "pyOCD read failed"
    else:
        return None, f"nRF52 reader is not available for {fam.title}"

    words = parse_debug_words(output)
    lo = words.get(0x10000060)
    hi = words.get(0x10000064)
    part_id = words.get(0x10000100)
    parts = {0x52833: "nRF52833", 0x52840: "nRF52840"}
    part = parts.get(part_id)
    if lo is None or hi is None or part is None:
        return None, "could not parse a supported nRF52833/nRF52840 FICR DEVICEID and PART"
    device = "NRF52833_XXAA" if part_id == 0x52833 else "NRF52840_XXAA"
    return {"family": "nrf52", "silicon_id": f"{hi:08X}{lo:08X}",
            "source": "swd",
            "part": part, "device": device}, None


def read_stm32n6_swd_identity(serial):
    """Read the STM32N6 non-secure UID through ST-LINK in HOTPLUG mode."""
    exe = stm32_programmer_executable()
    if not exe:
        return None, "STM32_Programmer_CLI not found"
    try:
        with probelock.hold(serial, "lstty read STM32N6 UID"):
            proc = subprocess.run([
                exe, "-c", f"port=SWD sn={serial} mode=HOTPLUG ap=1",
                "-r32", "0x46009014", "12",
            ], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except probelock.ProbeBusy as busy:
        return None, f"probe busy: {busy}"
    except subprocess.TimeoutExpired:
        return None, "STM32CubeProgrammer timed out reading STM32N6 UID"
    output = ANSI_RE.sub("", proc.stdout + proc.stderr)
    if proc.returncode != 0:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        errors = [line for line in lines if re.search(r"error|failed|cannot", line, re.I)]
        detail = errors[-1] if errors else (lines[-1] if lines else "connection/read failed")
        return None, detail
    words = parse_debug_words(output)
    uid_words = [words.get(0x46009014 + offset) for offset in (0, 4, 8)]
    if any(word is None for word in uid_words):
        return None, "could not parse the STM32N6 96-bit UID from CubeProgrammer output"
    part_match = re.search(r"Device name\s*:\s*(STM32N\w+|ST32N657)\b", output, re.I)
    if not part_match:
        return None, "CubeProgrammer did not identify an STM32N6 target"
    part = part_match.group(1).upper()
    # CubeProgrammer 2.22 sometimes labels the N657 as ST32N657. Require
    # its device ID before accepting that spelling; do not guess a family.
    if part == "ST32N657":
        if not re.search(r"Device ID\s*:\s*0x486\b", output, re.I):
            return None, "CubeProgrammer ST32N657 label has no matching device ID"
        part = "STM32N657"
    if not part.startswith("STM32N6"):
        return None, "CubeProgrammer did not identify an STM32N6 target"
    # Zephyr hwinfo_stm32 exposes UID word2, word1, word0 as big-endian
    # bytes. Match identity show, regardless of whether SWD or VCP is used.
    uid = "".join(f"{word:08X}" for word in reversed(uid_words))
    if uid in {"0" * 24, "F" * 24}:
        return None, "CubeProgrammer returned an empty STM32N6 UID"
    return {"family": "stm32n6", "silicon_id": uid,
            "part": part, "device": part, "source": "swd"}, None


def read_stm32n6_identity(serial):
    """Read STM32N6 UID via read-only SWD HOTPLUG."""
    return read_stm32n6_swd_identity(serial)


# JTAG targets with a readable unique ID. Key: IDCODE without the 4 version bits.
# Value: (part, IR length, FUSE_DNA opcode, DNA bits). UG570: FUSE_DNA = 0x32, 96 bits.
# Only read-only instructions are ever shifted (FUSE_DNA, then BYPASS).
XILINX_JTAG_PARTS = {
    0x4A63093: ("XCKU3P", 6, 0x32, 96),
}
JTAG_IDENTITY_KHZ = 25   # slow on purpose: some benches use long flying JTAG leads


def openocd_executable():
    found = shutil.which("openocd")
    if found:
        return found
    local = Path.home() / ".local/bin/openocd"
    return str(local) if local.is_file() else None


def openocd_jlink_run(serial, commands, lock, timeout=60):
    """One openocd session on J-Link `serial` (JTAG, no reset lines). Returns (rc, output)."""
    openocd = openocd_executable()
    if not openocd:
        return None, "openocd not found in PATH or ~/.local/bin"
    base = ["adapter driver jlink", f"adapter serial {serial}", "transport select jtag",
            f"adapter speed {JTAG_IDENTITY_KHZ}", "reset_config none"]
    argv = [openocd]
    for command in [*base, *commands, "shutdown"]:
        argv += ["-c", command]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, pass_fds=lock.pass_fds)
    except subprocess.TimeoutExpired:
        return None, f"openocd timed out after {timeout} s"
    return proc.returncode, proc.stdout + proc.stderr


def parse_jtag_dna(output, tag, words):
    match = re.search(rf"{tag} ((?:[0-9a-fA-F]+\s*){{{words}}})", output)
    if not match:
        return None
    # drscan prints the first-shifted 32 bits first; show the most significant word first.
    return "".join(f"{int(word, 16):08X}" for word in reversed(match.group(1).split()))


def read_jtag_identity(fam, serial):
    """Scan the JTAG chain read-only; for a known Xilinx part, read its FUSE_DNA twice."""
    if fam.key != "jlink":
        return None, f"JTAG reader is not available for {fam.title}"
    try:
        with probelock.hold(serial, "lstty read JTAG identity") as lock:
            rc, output = openocd_jlink_run(serial, ["init"], lock)
            if rc is None:
                return None, output
            idcodes = [int(code, 16) for code in re.findall(r"tap/device found: 0x([0-9a-fA-F]{8})", output)]
            if rc != 0 or not idcodes:
                failure = re.search(r"(JTAG scan chain interrogation failed[^\n]*|Error: [^\n]*)", output)
                return None, failure.group(1).strip() if failure else "no JTAG TAP found"
            if len(idcodes) != 1:
                return None, "JTAG chain has several TAPs (" + ", ".join(f"0x{c:08X}" for c in idcodes) + \
                    "); only single-TAP chains are supported"
            idcode = idcodes[0]
            known = XILINX_JTAG_PARTS.get(idcode & 0x0FFFFFFF)
            if not known:
                return None, f"JTAG IDCODE 0x{idcode:08X} has no unique-ID reader"
            part, irlen, fuse_dna, bits = known
            words = bits // 32
            zeros = " ".join(["32 0"] * words)
            ones = " ".join(["32 0xFFFFFFFF"] * words)
            # Two reads with opposite fill: a real 96-bit DR returns the same value both
            # times; a shorter or bypassed register leaks the fill pattern and differs.
            rc, output = openocd_jlink_run(serial, [
                f"jtag newtap lstty tap -irlen {irlen} -ignore-version -expected-id 0x{idcode & 0x0FFFFFFF:08x}",
                "init",
                f"irscan lstty.tap 0x{fuse_dna:x}",
                f"echo \"DNA0 [drscan lstty.tap {zeros}]\"",
                f"irscan lstty.tap 0x{fuse_dna:x}",
                f"echo \"DNA1 [drscan lstty.tap {ones}]\"",
                f"irscan lstty.tap 0x{(1 << irlen) - 1:x}",
            ], lock)
    except probelock.ProbeBusy as busy:
        return None, f"probe busy: {busy}"
    if rc is None:
        return None, output
    first, second = parse_jtag_dna(output, "DNA0", words), parse_jtag_dna(output, "DNA1", words)
    if rc != 0 or not first or not second:
        return None, f"could not read {part} FUSE_DNA over JTAG"
    if first != second or set(first) <= {"0"} or set(first) <= {"F"}:
        return None, f"{part} FUSE_DNA is unstable or empty ({first} / {second})"
    return {"family": "xilinx", "silicon_id": first, "source": "jtag",
            "part": part, "device": part}, None


def read_chip_identity(fam, serial, saved_device=""):
    if fam.key == "jlink" and not saved_device.lower().startswith("nrf"):
        # Unknown or non-Nordic target: a JTAG IDCODE scan is read-only and harmless on
        # an SWD-only board (no TAP answers), so try it before an SWD connect.
        identity, jtag_error = read_jtag_identity(fam, serial)
        if identity or saved_device:
            return identity, None if identity else f"JTAG: {jtag_error}"
        identity, swd_error = read_nordic_identity(fam, serial, saved_device)
        return (identity, None) if identity else (None, f"JTAG: {jtag_error}; SWD: {swd_error}")
    if fam.key in {"jlink", "mculink"}:
        return read_nordic_identity(fam, serial, saved_device)
    if fam.key == "stlink":
        return read_stm32n6_identity(serial)
    return None, f"no identity reader for {fam.title}"


def reassign_probe(args):
    """Explicitly read one attached target and rename its lstty probe alias."""
    candidates = []
    config_errors = []
    for fam in PROBE_FAMILIES.values():
        if args.kind and fam.key != args.kind:
            continue
        try:
            config = load_probe_config_strict(fam)
        except RuntimeError as exc:
            config_errors.append(str(exc))
            continue
        serial = None
        previous_name = None
        previous = None
        if args.probe in config["probes"]:
            previous_name = args.probe
            previous = config["probes"][previous_name]
            serial = fam.normalize(previous.get("serial", ""))
        elif re.fullmatch(fam.serial_re, args.probe):
            serial = fam.normalize(args.probe)
        if serial and serial in connected_probes(fam):
            candidates.append((fam, serial, config, previous_name, previous))

    if len(candidates) != 1:
        if not candidates:
            for error in config_errors:
                print(error, file=sys.stderr)
            print(f"No connected debug probe matches {args.probe!r}; use a saved nickname or USB serial.",
                  file=sys.stderr)
            print("Run a fresh `lstty` first: replug invalidates older snapshots, never reuse one.",
                  file=sys.stderr)
        else:
            kinds = ", ".join(f"{fam.key}:{serial}" for fam, serial, *_ in candidates)
            print(f"Ambiguous probe {args.probe!r}; specify --kind ({kinds}).", file=sys.stderr)
        return 2

    fam, serial, config, previous_name, previous = candidates[0]
    link_rename = None
    if getattr(args, "link_name", None):
        # Decide the link rename before touching the nickname, so a bad request
        # changes nothing. Only a link bound to this probe's own serial qualifies.
        link_config = load_link_config()
        matches = links_for_serial(link_config, fam.normalize, serial)
        if len(matches) != 1:
            found = ", ".join(matches) if matches else "none"
            print(f"--link-name needs exactly one link bound to serial {serial} (found: {found}); "
                  f"use `lstty rename-link OLD NEW` for a specific one. No changes made.", file=sys.stderr)
            return 2
        _, error = plan_link_rename(link_config, matches[0], args.link_name)
        if error:
            print(f"{error} No changes made.", file=sys.stderr)
            return 2
        link_rename = matches[0]
    if previous is None:
        previous_name = next((
            name for name, probe in config["probes"].items()
            if fam.normalize(probe.get("serial", "")) == serial
        ), None)
        previous = config["probes"].get(previous_name) if previous_name else None
    try:
        identity, error = read_chip_identity(fam, serial, (previous or {}).get("device", ""))
    except OSError as exc:
        print(f"{fam.title} {serial}: {exc}; no nickname changed.", file=sys.stderr)
        return 1
    if error:
        print(f"{fam.title} {serial}: {error}; no nickname changed.", file=sys.stderr)
        return 1

    chip_id = identity["silicon_id"].upper()
    mapping_key = f"{identity['family']}:{chip_id}"
    try:
        registry = load_board_registry()
    except RuntimeError as exc:
        print(f"{exc}; no nickname changed.", file=sys.stderr)
        return 1
    saved = registry["chips"].get(mapping_key)
    if saved is not None and not isinstance(saved, dict):
        print(f"Invalid chip entry for {chip_id} in {BOARD_CONFIG}; no nickname changed.",
              file=sys.stderr)
        return 1
    if saved:
        board_name = saved.get("name", "")
        if args.name and args.name != board_name:
            print(f"chip ID {chip_id} is registered as {board_name!r}; no changes made.",
                  file=sys.stderr)
            return 2
    else:
        board_name = args.name
        if not board_name:
            try:
                board_name = input(
                    f"New {identity['part']} ID {chip_id}; board nickname: "
                ).strip()
            except EOFError:
                board_name = ""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", board_name or ""):
            print("A new chip ID needs --name NAME (letters, digits, '-' or '_').", file=sys.stderr)
            return 2

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", board_name or ""):
        print(f"Saved board nickname is invalid: {board_name!r}; repair {BOARD_CONFIG}.", file=sys.stderr)
        return 2

    def mutate(current):
        existing = current["probes"].get(board_name)
        if existing and existing.get("serial") != serial:
            return f"nickname {board_name!r} is already assigned to another {fam.title} probe"
        suffix = probe_var_suffix(board_name)
        for other, probe in current["probes"].items():
            if other != board_name and probe_var_suffix(other) == suffix:
                if probe.get("serial") != serial:
                    return f"nickname {board_name!r} collides with {other!r} in {fam.prefix}_* variables"
        for name, probe in list(current["probes"].items()):
            if name != board_name and probe.get("serial") == serial:
                del current["probes"][name]
        probe = dict(previous or {})
        probe.update({"serial": serial, "device": identity["device"]})
        current["probes"][board_name] = probe
        return None

    try:
        import fcntl
    except ImportError:  # Windows: single writer for this local configuration.
        fcntl = None
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOARD_LOCK, "w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current_registry = load_board_registry()
        except RuntimeError as exc:
            print(f"{exc}; no nickname changed.", file=sys.stderr)
            return 1
        current_saved = current_registry["chips"].get(mapping_key)
        if current_saved and current_saved.get("name") != board_name:
            print(f"chip ID {chip_id} is now registered as {current_saved.get('name')!r}; retry reassign.",
                  file=sys.stderr)
            return 2
        old_registry = json.loads(json.dumps(current_registry))
        current_registry["chips"][mapping_key] = {"name": board_name, "part": identity["part"]}
        try:
            write_board_registry(current_registry)
            error = update_probe_config(fam, mutate)
        except OSError as exc:
            error = str(exc)
        if error:
            try:
                write_board_registry(old_registry)
            except OSError as rollback_error:
                print(f"{error}; probe nickname unchanged, but registry rollback failed: {rollback_error}",
                      file=sys.stderr)
                return 1
    if error:
        print(error + "; no nickname changed.", file=sys.stderr)
        return 2
    suffix = probe_var_suffix(board_name)
    print(f"{fam.title} {serial}: {identity['part']} {chip_id} ({identity['source']}) → {board_name}")
    print(f"Saved {fam.prefix}_SN_{suffix}={serial}; re-run `lstty` after any replug, never reuse this snapshot.")
    if link_rename:
        return rename_link(link_rename, args.link_name)
    return 0


def jlink_failure(out):
    """One line that says why J-Link Commander failed, instead of its whole log."""
    vtref = re.search(r"VTref=([\d.]+)V", out)
    if "Target voltage too low" in out:
        return f"target is not powered (VTref={vtref.group(1) if vtref else '?'} V)"
    for pattern in (r"Error occurred: ([^\n]+)", r"(Cannot connect[^\n]*)", r"(ERROR[^\n]*)", r"(JLinkExe [^\n]*)"):
        match = re.search(pattern, out)
        if match:
            return match.group(1).strip()
    return out.strip().splitlines()[-1] if out.strip() else "J-Link failed"


def probe_target(fam, args):
    """Resolve args.probe (nickname or serial) and its device -> (serial, name, device)."""
    config = load_probe_config(fam)
    try:
        serial, name = resolve_probe(fam, config, args.probe)
    except KeyError:
        print(f"No {fam.title} nickname or connected serial {args.probe}.", file=sys.stderr)
        return None
    device = getattr(args, "device", None) or config["probes"].get(name or "", {}).get("device")
    if not device:
        print(f"No target device saved for {args.probe}; pass --device "
              f"(for example {fam.device_example}).", file=sys.stderr)
        return None
    return serial, name, device


def probe_reset(fam, args):
    target = probe_target(fam, args)
    if not target:
        return 1
    serial, name, device = target
    ok, out = jlinkexe_run(serial, device, ["r"] if args.halt else ["r", "g"])
    if not ok:
        print(f"{name or serial}: {jlink_failure(out)}", file=sys.stderr)
        return 1
    print(f"{name or serial}: reset{' (halted)' if args.halt else ''}")
    return 0


def probe_mem(fam, args):
    target = probe_target(fam, args)
    if not target:
        return 1
    serial, name, device = target
    ok, out = jlinkexe_run(serial, device, ["h", f"mem32 {args.address} {args.count}", "g"] if args.halt else [f"mem32 {args.address} {args.count}"])
    if not ok:
        print(f"{name or serial}: {jlink_failure(out)}", file=sys.stderr)
        return 1
    words = {}
    for match in re.finditer(r"^([0-9A-Fa-f]{8}) = ((?:[0-9A-Fa-f]{8} ?)+)", out, re.M):
        base = int(match.group(1), 16)
        for i, word in enumerate(match.group(2).split()):
            words[base + 4 * i] = int(word, 16)
    if not words:
        print("no memory words in the J-Link output:\n" + out.strip()[-400:], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({f"0x{a:08X}": f"0x{v:08X}" for a, v in sorted(words.items())}))
    else:
        for addr, value in sorted(words.items()):
            print(f"0x{addr:08X} = 0x{value:08X}")
    return 0


def probe_dump(fam, args):
    target = probe_target(fam, args)
    if not target:
        return 1
    serial, name, device = target
    out_path = str(Path(args.output).expanduser().resolve())
    ok, out = jlinkexe_run(serial, device, ["h", f"savebin {out_path} {args.address} {args.length}", "g"])
    if not ok or not Path(out_path).exists():
        print(f"{name or serial}: {jlink_failure(out)}", file=sys.stderr)
        return 1
    print(f"{out_path}: {Path(out_path).stat().st_size} bytes from {name or serial}")
    return 0


def probe_flash(fam, args):
    target = probe_target(fam, args)
    if not target:
        return 1
    serial, name, device = target
    image = str(Path(args.image).expanduser().resolve())
    image_path = Path(image)
    if not image_path.is_file():
        print(f"{image}: no such file", file=sys.stderr)
        return 1
    if image_path.stat().st_size == 0:
        print(f"{image}: refusing to flash an empty image", file=sys.stderr)
        return 1
    if args.confirm_serial != serial:
        print(f"confirmation mismatch; no changes made (target serial is {serial})", file=sys.stderr)
        return 2
    digest = hashlib.sha256()
    with image_path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    print(f"FLASH target={name or serial} serial={serial} device={device}", file=sys.stderr)
    print(f"FLASH image={image} size={image_path.stat().st_size} sha256={digest.hexdigest()}", file=sys.stderr)
    load = f"loadfile {image}" + (f" {args.address}" if args.address else "")
    ok, out = jlinkexe_run(serial, device, ["r", "h", load, *([] if args.no_reset else ["r", "g"])], timeout=300)
    if not ok or "O.K." not in out:
        print(f"{name or serial}: {jlink_failure(out)}", file=sys.stderr)
        return 1
    probe_use(fam, argparse.Namespace(probe=name or serial, what=[f"flashed {Path(image).name}"]))
    print(f"{name or serial}: flashed {Path(image).name}")
    return 0


def probe_cmd(fam, args):
    target = probe_target(fam, args)
    if not target:
        return 1
    serial, name, device = target
    ok, out = jlinkexe_run(serial, device, list(args.lines))
    if not out.startswith("probe busy"):
        sys.stdout.write(out)
    if not ok:
        print(f"{name or serial}: {jlink_failure(out)}", file=sys.stderr)
    return 0 if ok else 1


def probe_names(fam, as_json=False):
    """One "nickname<TAB>description" line per nickname, for shell completion."""
    config = load_probe_config(fam)
    if as_json:
        connected = connected_probes(fam)
        ttys = probe_ttys(fam)
        print(json.dumps([
            {"name": name, "serial": probe["serial"], "device": probe.get("device", ""),
             "note": probe.get("note", ""), "tty": ttys.get(probe["serial"], []),
             "connected": probe["serial"] in connected,
             "last_use": (config["usage"].get(probe["serial"]) or {}).get("what", "")}
            for name, probe in sorted(config["probes"].items())]))
        return 0
    for name, probe in sorted(config["probes"].items()):
        usage = config["usage"].get(probe["serial"])
        detail = " · ".join(
            part for part in (
                probe["serial"],
                probe.get("device", ""),
                probe.get("note", ""),
                f"used {format_ago(usage['at'])}" if usage else "",
            ) if part
        )
        print(f"{name}\t{detail}")
    return 0


def probe_connected(fam, as_json=False):
    """One "serial<TAB>description" line per connected probe, for completion."""
    config = load_probe_config(fam)
    names = probe_names_by_serial(config)
    if as_json:
        print(json.dumps([{"serial": serial, "product": product, "names": names.get(serial, [])}
                          for serial, product in sorted(connected_probes(fam).items())]))
        return 0
    for serial, product in sorted(connected_probes(fam).items()):
        detail = " · ".join(part for part in (product, ", ".join(names.get(serial, []))) if part)
        print(f"{serial}\t{detail}")
    return 0


def probe_env(fam):
    if not fam.env_file.exists():
        write_probe_env(fam, load_probe_config(fam))
    sys.stdout.write(fam.env_file.read_text(encoding="utf-8"))
    return 0


def probe_install(fam):
    write_probe_env(fam, load_probe_config(fam))
    rcs = [Path.home() / ".zshenv", Path.home() / ".bashrc"]
    if sys.platform.startswith("linux"):
        rcs.append(Path.home() / ".profile")
    block = (
        f"\n{fam.mark_begin}\n"
        f'[ -r "{fam.env_file}" ] && . "{fam.env_file}"\n'
        f"{fam.mark_end}\n"
    )
    for rc in rcs:
        if fam.mark_begin in read_text(rc):
            print(f"already hooked: {rc}")
            continue
        with open(rc, "a", encoding="utf-8") as handle:
            handle.write(block)
        print(f"hooked: {rc}")
    if sys.platform.startswith("linux"):
        print(f"wrote: {fam.systemd_env} (takes effect at next login)")
    print(f'open a new shell, or run: . "{fam.env_file}"')
    return 0


def install_completion():
    """Install the bundled zsh completion without replacing someone else's file."""
    if not COMPLETION_SOURCE.is_file():
        print(f"completion source not found: {COMPLETION_SOURCE}", file=sys.stderr)
        return 1
    if COMPLETION_DEST.is_symlink() and COMPLETION_DEST.resolve() == COMPLETION_SOURCE:
        print(f"already installed: {COMPLETION_DEST}")
        return 0
    if COMPLETION_DEST.exists() or COMPLETION_DEST.is_symlink():
        print(f"refusing to replace existing completion: {COMPLETION_DEST}", file=sys.stderr)
        return 1
    COMPLETION_DEST.parent.mkdir(parents=True, exist_ok=True)
    COMPLETION_DEST.symlink_to(COMPLETION_SOURCE)
    print(f"installed: {COMPLETION_DEST}")
    print("open a new zsh, or run: autoload -Uz compinit && compinit")
    return 0


def probe_main(fam, args):
    show_all = getattr(args, "all_top", False) or getattr(args, "all_sub", False)
    handlers = {
        None: lambda: render_probe_table(fam, show_all=show_all),
        "list": lambda: render_probe_table(fam, show_all=show_all),
        "set": lambda: probe_set(fam, args),
        "rm": lambda: probe_rm(fam, args),
        "get": lambda: probe_get(fam, args),
        "use": lambda: probe_use(fam, args),
        "last": lambda: probe_last(fam, args),
        "run": lambda: probe_run(fam, args),
        "names": lambda: probe_names(fam, getattr(args, "json", False)),
        "connected": lambda: probe_connected(fam, getattr(args, "json", False)),
        "reset": lambda: probe_reset(fam, args),
        "mem": lambda: probe_mem(fam, args),
        "dump": lambda: probe_dump(fam, args),
        "flash": lambda: probe_flash(fam, args),
        "cmd": lambda: probe_cmd(fam, args),
        "env": lambda: probe_env(fam),
        "install": lambda: probe_install(fam),
    }
    return handlers[args.probe_command]()


def add_probe_parser(subcommands, fam):
    parser = subcommands.add_parser(
        fam.key,
        help=f"nickname {fam.title} probes and record what they were last used for",
        description=f"Nickname {fam.title} probes; nicknames and last use are exported to "
        f"new shells as {fam.prefix}_* via {fam.env_file} (see `lstty {fam.key} install`). "
        f"Use with: {fam.tools}",
    )
    # Separate dests: a subparser default would overwrite the parent's value.
    parser.add_argument("-a", "--all", dest="all_top", action="store_true", help="also list offline probes")
    sub = parser.add_subparsers(dest="probe_command")
    list_ = sub.add_parser("list", help="connected probes with port and last use")
    list_.add_argument("-a", "--all", dest="all_sub", action="store_true", help="also list offline probes from history")

    set_ = sub.add_parser("set", help="nickname a probe (serial optional if only one is connected)")
    set_.add_argument("name", help="nickname, for example board-a")
    set_.add_argument("serial", nargs="?", help=f"{fam.title} serial number")
    set_.add_argument("--device", help=f"target device name, for example {fam.device_example}")
    set_.add_argument("--note", help="free-form note")

    rm = sub.add_parser("rm", help="remove a nickname")
    rm.add_argument("name")

    get = sub.add_parser("get", help="print a probe's serial (or --field device/note/tty)")
    get.add_argument("name", nargs="?", help="nickname (or use --serial)")
    get.add_argument("--serial", help="look the nickname up by probe serial instead")
    get.add_argument("--field", choices=["serial", "device", "note", "tty"], default="serial")
    get.add_argument("--json", action="store_true", help="all fields as one JSON object")

    use = sub.add_parser("use", help="record what a probe is being used for")
    use.add_argument("probe", help="nickname or serial")
    use.add_argument("what", nargs="+", help="for example: flashing zephyr blinky")

    last = sub.add_parser("last", help="what a probe was last used for, and how long ago")
    last.add_argument("probe", nargs="?", help="nickname or serial; omit for all connected")
    last.add_argument("-a", "--all", dest="all_sub", action="store_true", help="also list offline probes")

    run = sub.add_parser(
        "run",
        help=f"record use, then run a command with {fam.prefix}_SERIAL/_DEVICE/_NICK set",
    )
    run.add_argument("probe", help="nickname or serial")
    run.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="[--for TEXT] -- command [args...]; TEXT defaults to the command line",
    )

    names = sub.add_parser("names", help="list nicknames with details, one per line (for completions)")
    names.add_argument("--json", action="store_true", help="JSON array for scripts")
    connected = sub.add_parser("connected", help="list connected probe serials with details (for completions)")
    connected.add_argument("--json", action="store_true", help="JSON array for scripts")
    if fam.key == "jlink":
        # J-Link Commander wrappers: one place builds the command file and picks the device.
        dev = argparse.ArgumentParser(add_help=False)
        dev.add_argument("--device", help="target device (default: the nickname's saved device)")
        reset = sub.add_parser("reset", parents=[dev], help="reset the target and run it")
        reset.add_argument("probe", help="nickname or serial")
        reset.add_argument("--halt", action="store_true", help="leave the core halted")
        mem = sub.add_parser("mem", parents=[dev], help="read 32-bit words: mem <probe> <addr> [count]")
        mem.add_argument("probe")
        mem.add_argument("address", help="for example 0x50000804")
        mem.add_argument("count", nargs="?", type=int, default=1)
        mem.add_argument("--halt", action="store_true", help="halt around the read")
        mem.add_argument("--json", action="store_true")
        dump = sub.add_parser("dump", parents=[dev], help="save memory to a file: dump <probe> <addr> <length> -o FILE")
        dump.add_argument("probe")
        dump.add_argument("address")
        dump.add_argument("length")
        dump.add_argument("-o", "--output", required=True)
        flash = sub.add_parser("flash", parents=[dev], help="program a .hex/.bin (JLinkExe, never nrfjprog)")
        flash.add_argument("probe")
        flash.add_argument("image")
        flash.add_argument("--address", help="load address, needed for a .bin")
        flash.add_argument("--confirm-serial", required=True,
                           help="required: repeat the exact probe serial shown by lstty")
        flash.add_argument("--no-reset", action="store_true", help="do not reset and run afterwards")
        cmd = sub.add_parser("cmd", parents=[dev], help="run raw J-Link Commander lines: cmd <probe> LINE...")
        cmd.add_argument("probe")
        cmd.add_argument("lines", nargs="+")
    sub.add_parser("env", help="print the generated export lines")
    sub.add_parser("install", help="load the variables in every new shell (zsh/bash)")


def serial_ttys():
    if sys.platform == "darwin":
        return macos_ioreg_rows()
    if sys.platform == "win32":
        return windows_serial_rows()

    rows = []
    for tty in sorted(TTY_ROOT.iterdir(), key=lambda p: p.name):
        devname = str(DEV_ROOT / tty.name)
        if not Path(devname).exists():
            continue

        sys_dev = tty / "device"
        if not sys_dev.exists():
            continue

        props = udev_props(devname)
        usb = usb_parent(sys_dev)

        # Keep USB serial ports and common explicit serial device names.
        if not usb and not tty.name.startswith(("ttyUSB", "ttyACM", "ttyAMA", "rfcomm")):
            continue

        vid = read_text(usb / "idVendor") if usb else props.get("ID_VENDOR_ID", "")
        pid = read_text(usb / "idProduct") if usb else props.get("ID_MODEL_ID", "")
        product = read_text(usb / "product") if usb else ""
        serial = read_text(usb / "serial") if usb else ""
        manufacturer = read_text(usb / "manufacturer") if usb else ""
        busnum = read_text(usb / "busnum") if usb else "-"
        devnum = read_text(usb / "devnum") if usb else "-"
        speed = speed_label(read_text(usb / "speed") if usb else "")

        product = product or props.get("ID_MODEL", "").replace("_", " ") or "-"
        serial = serial or props.get("ID_SERIAL_SHORT", "") or "-"
        manufacturer = manufacturer or props.get("ID_VENDOR", "").replace("_", " ") or "-"
        driver = props.get("ID_USB_DRIVER") or Path(sys_dev).resolve().name or "-"

        rows.append(
            {
                "tty": devname,
                "bus": busnum,
                "dev": devnum,
                "icon": icon_for(product, driver),
                "vid": f"0x{vid}" if vid else "-",
                "pid": f"0x{pid}" if pid else "-",
                "product": product,
                "serial": serial,
                "driver": driver,
                "speed": speed,
                "perms": mode_user_group(devname),
                "by_id": by_id_for(devname),
                "manufacturer": manufacturer,
                "match_vid": normalize_usb_id(vid),
                "match_pid": normalize_usb_id(pid),
                "match_serial": clean_missing(serial),
                "match_product": product,
            }
        )
    return rows


def live_footer():
    stamp = time.strftime("%H:%M:%S")
    if sys.stdout.isatty():
        print(paint(f"Live at {stamp}", Color.dim))
        return
    # Not a terminal: an agent or a script is reading, so keep the explanations.
    print("STATUS = probe link; PORT '-' = no serial port. Probe, UART and USB CDC are independent: "
          "missing CDC is not proof of an unplugged or unpowered board.")
    print(f"Live at {stamp}: replug invalidates this snapshot; re-run `lstty` before reassign/flash/console.")
    print("Agents: `lstty --json` returns this listing as stable fields, with notes and how-to paths.")


# Static network probes: debuggers reached over Ethernet that do not announce themselves (no mDNS),
# such as the Abatron BDI3000. They are registered by hand and checked with one ping plus the MAC in
# the neighbour cache. lstty never opens their service ports: a BDI3000 serves a single Telnet
# session, and a stray connection could disturb a debug session in use.
NETPROBE_CONFIG = CONFIG_DIR / "netprobes.json"
NETPROBE_STATE = CONFIG_DIR / "netprobes-state.json"
NETPROBE_PING_SECONDS = 0.4  # LAN replies take milliseconds; macOS/Linux ping rounds this up to 1 s
NETPROBE_DEADLINE_SECONDS = 2.0
NETPROBE_KINDS = {
    "bdi3000": {"label": "Abatron BDI3000", "oui": "00:0c:01", "ports": "telnet 23, gdb 2001+"},
    "generic": {"label": "network probe", "oui": "", "ports": "-"},
}
MAC_RE = re.compile(r"(?<![0-9a-f:-])([0-9a-f]{1,2}(?:[:-][0-9a-f]{1,2}){5})(?![0-9a-f:-])", re.I)


def normalize_mac(text):
    """Canonical lower-case colon form, or None. macOS prints octets without leading zeros."""
    parts = re.split(r"[:-]", str(text or "").strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", part) for part in parts):
        return None
    return ":".join(part.zfill(2) for part in parts)


def quiet_run(cmd, timeout):
    """Run a short system command; its stdout, or "" when it cannot run or overruns."""
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kwargs = {"creationflags": no_window} if os.name == "nt" else {}
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=timeout, **kwargs).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def netprobe_ping(ip, timeout=NETPROBE_PING_SECONDS):
    """One echo through the system ping (no raw socket, no admin). Only a reply with a TTL counts:
    Windows ping exits 0 on "destination host unreachable" sent by the local host."""
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, round(timeout))), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, round(timeout))), ip]
    return "ttl=" in quiet_run(cmd, timeout + 1).lower()


def netprobe_arp(ip):
    """MAC of ip from the neighbour cache, or None (no entry, or the host is behind a router)."""
    if sys.platform == "win32":
        commands = [["arp", "-a", ip]]
    elif sys.platform == "darwin":
        commands = [["arp", "-n", ip]]
    else:
        commands = [["ip", "neigh", "show", ip], ["arp", "-n", ip]]
    ip_re = re.compile(rf"(?<![\d.]){re.escape(ip)}(?![\d.])")
    for cmd in commands:
        for line in quiet_run(cmd, 2).splitlines():
            if not ip_re.search(line):
                continue
            match = MAC_RE.search(line)
            mac = normalize_mac(match.group(1)) if match else None
            if mac and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                return mac
    return None


def netprobe_check(probe):
    """(status, mac) for one registered probe.

    status is "online" (answers, and the MAC matches when one is pinned), "offline", "unresolved",
    "wrong-mac" (something else answers at that address) or "unverified" (answers, MAC unreadable).
    """
    import socket
    try:
        ip = socket.gethostbyname(str(probe.get("host") or ""))
    except (OSError, UnicodeError):
        return "unresolved", None
    if not netprobe_ping(ip):
        return "offline", None
    mac = netprobe_arp(ip)
    expected = normalize_mac(probe.get("mac"))
    if not expected:
        return "online", mac
    if mac is None:
        return "unverified", None
    return ("online" if mac == expected else "wrong-mac"), mac


def load_netprobes():
    try:
        probes = json.loads(NETPROBE_CONFIG.read_text(encoding="utf-8")).get("probes")
        return probes if isinstance(probes, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def write_json_atomic(path, document, prefix):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=prefix,
                                     suffix=".tmp", delete=False) as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
    os.replace(handle.name, path)


def netprobe_check_all(probes):
    """Check every probe at once under one shared deadline; late ones count as offline."""
    from concurrent.futures import ThreadPoolExecutor, wait
    if not probes:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(32, len(probes)))
    try:
        futures = {name: pool.submit(netprobe_check, probe) for name, probe in probes.items()}
        wait(list(futures.values()), timeout=NETPROBE_DEADLINE_SECONDS)
        return {name: (future.result() if future.done() else ("offline", None))
                for name, future in futures.items()}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def start_netprobe_check():
    """Check registered network probes in the background; None when none are registered."""
    from concurrent.futures import ThreadPoolExecutor
    probes = load_netprobes()
    if not probes:
        return None
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(netprobe_check_all, probes)
    pool.shutdown(wait=False)
    return probes, future


def netprobe_rows(pending, show_all=False):
    """Registered network probe rows, notes and the registered count. Never raises; rows are None when none are registered."""
    if pending is None:
        return None, [], 0
    probes, future = pending
    try:
        results = future.result(timeout=NETPROBE_DEADLINE_SECONDS + 1)
    except Exception:  # a broken check must not hide the rest of the listing
        results = {}
    try:
        state = json.loads(NETPROBE_STATE.read_text(encoding="utf-8"))
        seen = state.get("last_seen") if isinstance(state, dict) else None
        seen = seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        seen = {}
    now = time.time()
    table, notes = [], []
    for name, probe in sorted(probes.items(), key=lambda item: item[0].casefold()):
        status, mac = results.get(name, ("offline", None))
        if status == "online":
            seen[name] = now
        elif status == "wrong-mac":
            notes.append(f"{name}: {probe.get('host')} answers with MAC {mac}, expected {probe.get('mac')}; "
                         "another device has this address, so it is not this probe.")
        elif status == "unverified":
            notes.append(f"{name}: answers, but its MAC is not in the neighbour cache (routed?); "
                         "identity not verified.")
        if status in ("offline", "unresolved") and not show_all:
            continue
        kind = NETPROBE_KINDS.get(probe.get("kind"), NETPROBE_KINDS["generic"])
        table.append({
            "name": name,
            "kind": kind["label"],
            "addr": str(probe.get("host") or "-"),
            "mac": mac or normalize_mac(probe.get("mac")) or "-",
            "status": status,
            "seen": "now" if status == "online" else format_ago(seen.get(name)),
            "note": str(probe.get("note") or kind["ports"]),
        })
    try:
        write_json_atomic(NETPROBE_STATE, {"last_seen": seen}, ".netprobes-")
    except OSError:
        pass
    return table, notes, len(probes)


def render_netprobe_section(pending, show_all=False):
    """Print registered network probes. Silent when none are registered."""
    table, notes, registered = netprobe_rows(pending, show_all)
    if table is None:
        return
    print()
    print(paint("Network probes", Color.dim))
    if table:
        render_columns([
            Col("name", "NAME", Color.cyan, min=8),
            Col("kind", "KIND", Color.magenta, min=6, drop=3),
            Col("addr", "ADDRESS", Color.blue, min=10),
            Col("mac", "MAC", Color.green, min=8, drop=2),
            Col("status", "STATUS", min=6),
            Col("seen", "SEEN", Color.dim, min=4, drop=1, right=True),
            Col("note", "NOTE", Color.dim, min=8, flex=True),
        ], table)
    else:
        print(f"No network probes online ({registered} registered; -a lists offline probes).")
    for note in notes:
        print(paint(f"Network probe note: {note}", Color.yellow))


def netprobe_main(args):
    probes = load_netprobes()
    command = args.netprobe_command or "list"
    if command == "add":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", args.name):
            print("name must be 1-32 letters, digits, dot, dash or underscore", file=sys.stderr)
            return 2
        kind = NETPROBE_KINDS[args.kind]
        mac = None
        if args.mac:
            mac = normalize_mac(args.mac)
            if not mac:
                print(f"not a MAC address: {args.mac!r}", file=sys.stderr)
                return 2
        else:
            status, mac = netprobe_check({"host": args.host})
            if status != "online" or not mac:
                print(f"{args.host}: {status}, MAC unknown; saved without a MAC, so lstty cannot tell "
                      "this probe from another device at the same address. Re-run add when it is online, "
                      "or pass --mac.", file=sys.stderr)
        if mac and kind["oui"] and not mac.startswith(kind["oui"]):
            if not args.mac:
                print(f"{args.host} answers with {mac}, not a {kind['label']} address "
                      f"({kind['oui']}:..); not saved. Pass --mac to override.", file=sys.stderr)
                return 1
            print(f"warning: {mac} is not a {kind['label']} address ({kind['oui']}:..)", file=sys.stderr)
        entry = {"host": args.host, "kind": args.kind}
        if mac:
            entry["mac"] = mac
        if args.note:
            entry["note"] = args.note
        probes[args.name] = entry
    elif command == "rm":
        if args.name not in probes:
            print(f"no network probe named {args.name!r}", file=sys.stderr)
            return 1
        del probes[args.name]
    else:
        if not probes:
            print("No network probes registered. Add one with: lstty netprobe add NAME HOST --kind bdi3000")
            return 0
        render_columns([
            Col("name", "NAME", Color.cyan, min=8),
            Col("kind", "KIND", Color.magenta, min=6),
            Col("host", "ADDRESS", Color.blue, min=10),
            Col("mac", "MAC", Color.green, min=8),
            Col("note", "NOTE", Color.dim, min=8, flex=True),
        ], [{"name": name, "kind": probe.get("kind", "generic"), "host": str(probe.get("host") or "-"),
             "mac": normalize_mac(probe.get("mac")) or "-", "note": str(probe.get("note") or "-")}
            for name, probe in sorted(probes.items(), key=lambda item: item[0].casefold())])
        return 0
    write_json_atomic(NETPROBE_CONFIG, {"probes": probes}, ".netprobes-")
    print(f"{'saved' if command == 'add' else 'removed'} {args.name} in {NETPROBE_CONFIG}")
    return 0


def serial_port_rows(rows, families, probe_names, link_config):
    """Serial port rows and alias notices: the data behind both the table and --json."""
    table = []
    alias_notices = []
    for row in rows:
        probe_labels = []
        bind_name = bound_names_for_row(row, link_config)
        for fam in families:
            if fam.matches(row.get("match_vid"), row.get("match_pid")):
                nicks = probe_names[fam.key].get(fam.normalize(row.get("match_serial", "")))
                if nicks:
                    label = f"{fam.key}:" + ",".join(nicks)
                    probe_labels.append(label)
                    if bind_name != "-" and set(bind_name.split(", ")) != set(nicks):
                        alias_notices.append(
                            f"{row['tty']}: serial alias '{bind_name}' differs from probe '{label}'. "
                            "Different aliases do not establish different boards; the serial alias may be stale. "
                            f"If identity is uncertain, use `lstty reassign {fam.normalize(row.get('match_serial', ''))} --kind {fam.key}` "
                            "within the target safety gates."
                        )
        table.append({
            "tty": row["tty"],
            "usb": row_usb_session(row),
            "icon": row["icon"],
            "id": f"{row['vid'][2:]}:{row['pid'][2:]}" if row["vid"].startswith("0x") else "-",
            "product": row["product"],
            "serial": row["serial"],
            "driver": row["driver"],
            "speed": row["speed"],
            "bind": bind_name,
            "probe": " ".join(probe_labels) or "-",
        })
    return table, alias_notices


def render_table(show_all=False):
    netprobes = start_netprobe_check()
    rows = serial_ttys()
    if sys.platform == "win32" and not show_all:
        # Bluetooth/ACPI ports are noise when looking for a board; -a lists them.
        rows = [row for row in rows if row["bus"] == "USB"]
    # Keep ~/.serial names pointing at the current port for the matching USB
    # identity whenever lstty is used, including the first command after replug.
    sync_links(verbose=False, rows=rows)
    families = list(PROBE_FAMILIES.values())
    probe_configs = {fam.key: load_probe_config(fam) for fam in families}
    probe_names = {key: probe_names_by_serial(config) for key, config in probe_configs.items()}

    def render_probe_sections():
        for fam in families:
            render_probe_table(
                fam, probe_configs[fam.key], None, probe_ttys(fam, rows), heading=True, show_all=show_all
            )

    if not rows:
        print("No serial ports found.")
        render_probe_sections()
        render_netprobe_section(netprobes, show_all)
        live_footer()
        maybe_print_update_notice()
        return 1
    link_config = load_link_config()
    table, alias_notices = serial_port_rows(rows, families, probe_names, link_config)
    if sys.platform == "win32":
        # Every CDC port reports the same driver (usbser) and no speed, so those
        # columns would only repeat; the descriptor and serial identify a probe.
        columns = [
            Col("tty", "PORT", Color.cyan, min=5),
            Col("id", "ID", Color.magenta, min=9),
            Col("product", "DEVICE", min=10),
            Col("serial", "SERIAL", Color.green, min=8, mid=True),
            Col("probe", "BOARD/PROBE", Color.magenta, min=11),
            Col("bind", "SERIAL ALIAS", Color.dim, min=12, flex=True),
        ]
    else:
        columns = [
            Col("tty", "TTY", Color.cyan, min=12, mid=True),
            Col("usb", "USB", Color.blue, min=6, drop=2),
            Col("icon", "", min=1, drop=5),
            Col("id", "ID", Color.magenta, min=9),
            Col("product", "DEVICE", min=10),
            Col("serial", "SERIAL", Color.green, min=8, mid=True),
            # DRV is for agents and scripts (also in --json); people on a terminal get the width back.
            *([] if sys.stdout.isatty() else [Col("driver", "DRV", Color.yellow, min=4, drop=3)]),
            Col("speed", "SPEED", min=10, drop=4, right=True),
            Col("probe", "BOARD/PROBE", Color.magenta, min=11),
            Col("bind", "SERIAL ALIAS", Color.dim, min=12, flex=True),
        ]
    render_columns(columns, table)
    for notice in alias_notices:
        print(f"Alias note: {notice}")
    render_probe_sections()
    render_netprobe_section(netprobes, show_all)
    live_footer()
    maybe_print_update_notice()
    return 0


JSON_SCHEMA_VERSION = 1


def _nil(value):
    """Table placeholders ('-', '') become null in JSON."""
    return None if value in ("-", "", None) else value


def json_snapshot(show_all=False):
    """The listing as stable fields for agents. Same data sources as render_table."""
    import socket

    netprobes = start_netprobe_check()
    rows = serial_ttys()
    if sys.platform == "win32" and not show_all:
        rows = [row for row in rows if row["bus"] == "USB"]
    sync_links(verbose=False, rows=rows)
    families = list(PROBE_FAMILIES.values())
    probe_configs = {fam.key: load_probe_config(fam) for fam in families}
    probe_names = {key: probe_names_by_serial(config) for key, config in probe_configs.items()}
    link_config = load_link_config()
    ports, alias_notices = serial_port_rows(rows, families, probe_names, link_config)

    notes = [{"code": "serial_alias_differs", "text": text} for text in alias_notices]
    notes.append({"code": "cdc_absent_not_proof_of_unplugged",
                  "text": "Probe link, UART and USB CDC are independent; a missing CDC port is not proof "
                          "of an unplugged or unpowered board."})
    notes.append({"code": "snapshot_goes_stale_on_replug",
                  "text": "Replugging invalidates this snapshot; re-run before reassign/flash/console."})

    np_rows, np_notes, np_registered = netprobe_rows(netprobes, show_all)
    notes += [{"code": "network_probe_note", "text": note} for note in np_notes]

    return {
        "schema": JSON_SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": socket.gethostname(),
        "platform": sys.platform,
        "include_offline": bool(show_all),
        "serial_ports": [{
            "tty": port["tty"],
            "usb": _nil(port["usb"]),
            "vid_pid": _nil(port["id"]),
            "device": _nil(port["product"]),
            "serial": _nil(port["serial"]),
            "driver": _nil(port["driver"]),
            "speed": _nil(port["speed"]),
            "board_probe": _nil(port["probe"]),
            "serial_alias": _nil(port["bind"]),
        } for port in ports],
        "debug_probes": {
            fam.key: [{
                "serial": row["serial"],
                "nicknames": [n for n in row["nick"].split(", ") if n],
                "status": row["status"].lower(),
                "port": _nil(row["port"]),
                "device": _nil(row["device"]),
                "last_use_ago": _nil(row["ago"]),
                "last_use": _nil(row["what"]),
            } for row in probe_rows(fam, probe_configs[fam.key], None, probe_ttys(fam, rows), show_all)]
            for fam in families
        },
        "network_probes": [{
            "name": row["name"],
            "kind": row["kind"],
            "address": _nil(row["addr"]),
            "mac": _nil(row["mac"]),
            "status": row["status"],
            "seen": row["seen"],
            "note": _nil(row["note"]),
        } for row in (np_rows or [])],
        "network_probes_registered": np_registered,
        "notes": notes,
    }


INSTALL_ROOT = Path(__file__).resolve().parent
VERSION_FILE = INSTALL_ROOT / "VERSION"
UPDATE_REPO = os.environ.get("LSTTY_UPDATE_REPO", "gustavosousa2208/lstty")
UPDATE_CHECK_TTL = 86400


def read_local_version():
    if VERSION_FILE.exists():
        text = read_text(VERSION_FILE)
        if text:
            return text
    return "dev"


def is_standalone_install():
    return VERSION_FILE.exists() and not (INSTALL_ROOT / ".git").exists()


def _update_cache_dir():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / "lstty"


def _update_cache_file():
    return _update_cache_dir() / "update-check.json"


def _github_token():
    return os.environ.get("LSTTY_GITHUB_TOKEN", "").strip()


def _write_update_cache(latest, current):
    cache_file = _update_cache_file()
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"checked_at": int(time.time()), "latest": latest, "current": current}
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _fetch_latest_release(repo, token="", timeout=10):
    import urllib.request

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "lstty",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_release_asset(asset, token, dest_path, timeout=30):
    import urllib.request

    url = asset.get("browser_download_url") or asset.get("url")
    headers = {"User-Agent": "lstty"}
    if token and url == asset.get("url"):
        headers["Accept"] = "application/octet-stream"
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out:
        shutil.copyfileobj(resp, out)


def check_update_background():
    local_ver = read_local_version()
    if local_ver == "dev":
        return 0
    try:
        release = _fetch_latest_release(UPDATE_REPO, _github_token(), timeout=5)
        latest = str(release.get("tag_name") or "").strip()
        if latest:
            _write_update_cache(latest, local_ver)
    except Exception:
        pass
    return 0


def maybe_print_update_notice():
    if not is_standalone_install() or os.environ.get("LSTTY_NO_UPDATE_CHECK"):
        return
    local_ver = read_local_version()
    if local_ver == "dev":
        return
    needs_refresh = True
    cache_file = _update_cache_file()
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            checked_at = int(data.get("checked_at") or 0)
            if time.time() - checked_at < UPDATE_CHECK_TTL:
                needs_refresh = False
            latest = str(data.get("latest") or "").strip()
            if latest and latest != local_ver:
                print(
                    f"lstty: update available ({latest}; current {local_ver}). Run 'lstty update'.",
                    file=sys.stderr,
                )
        except Exception:
            pass
    if needs_refresh:
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000008
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--check-update-bg"], **kwargs)
        except OSError:
            pass


def _expected_asset_name():
    import platform

    machine = platform.machine().lower()
    if sys.platform == "win32":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"lstty-windows-{arch}.zip"
    if sys.platform == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return f"lstty-macos-{arch}.tar.gz"
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    return f"lstty-linux-{arch}.tar.gz"


def _verify_sha256(file_path, checksums_text, asset_name):
    expected = ""
    for line in checksums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1].lstrip("*") == asset_name:
            expected = parts[0].lower()
            break
    if not expected:
        raise RuntimeError(f"checksum for {asset_name} not found in checksums.txt")
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise RuntimeError(f"SHA-256 mismatch for {asset_name}: expected {expected}, got {actual}")


def _extract_archive(archive_path, dest_dir):
    if str(archive_path).endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
    else:
        import tarfile

        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest_dir)


def _install_extracted_tree(src_root, latest_tag):
    src_script = src_root / "lstty.py"
    if not src_script.exists():
        raise RuntimeError("downloaded archive is missing lstty.py")
    (INSTALL_ROOT / "benchlib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_script, INSTALL_ROOT / "lstty.py")
    src_benchlib = src_root / "benchlib"
    if src_benchlib.is_dir():
        for item in src_benchlib.iterdir():
            if item.is_file():
                shutil.copy2(item, INSTALL_ROOT / "benchlib" / item.name)
    src_exe = src_root / ("lstty.exe" if sys.platform == "win32" else "lstty")
    if src_exe.exists():
        dst_exe = INSTALL_ROOT / src_exe.name
        if sys.platform == "win32" and dst_exe.exists():
            old_exe = INSTALL_ROOT / f"{src_exe.name}.old"
            if old_exe.exists():
                try:
                    old_exe.unlink()
                except OSError:
                    pass
            dst_exe.rename(old_exe)
        shutil.copy2(src_exe, dst_exe)
        if sys.platform != "win32":
            dst_exe.chmod(0o755)
    src_version = src_root / "VERSION"
    VERSION_FILE.write_text(
        read_text(src_version) if src_version.exists() else latest_tag,
        encoding="utf-8",
    )


def update_self(args):
    if (INSTALL_ROOT / ".git").exists() and not args.force:
        print(
            f"lstty: running from a git repository ({INSTALL_ROOT}); update with 'git pull' instead.",
            file=sys.stderr,
        )
        return 1

    token = _github_token()
    try:
        release = _fetch_latest_release(UPDATE_REPO, token)
    except Exception as exc:
        print(f"lstty: failed to check updates: {exc}", file=sys.stderr)
        return 1

    latest = str(release.get("tag_name") or "").strip()
    local_ver = read_local_version()
    if not latest:
        print("lstty: latest release has no tag_name.", file=sys.stderr)
        return 1

    if latest == local_ver and not args.force:
        _write_update_cache(latest, local_ver)
        if not args.quiet:
            print(f"lstty: already up to date ({local_ver}).")
        return 0

    if args.check:
        _write_update_cache(latest, local_ver)
        print(f"lstty: update available ({latest}; current {local_ver}).")
        return 0

    asset_name = _expected_asset_name()
    assets = {a.get("name"): a for a in release.get("assets") or [] if a.get("name")}
    target_asset = assets.get(asset_name)
    if not target_asset:
        print(f"lstty: release {latest} has no asset named {asset_name}.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="lstty-update-") as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / asset_name
        _download_release_asset(target_asset, token, archive_path)
        checksums_asset = assets.get("checksums.txt")
        if checksums_asset:
            checksums_path = tmp_dir / "checksums.txt"
            _download_release_asset(checksums_asset, token, checksums_path)
            _verify_sha256(archive_path, checksums_path.read_text(encoding="utf-8"), asset_name)
        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        _extract_archive(archive_path, extract_dir)
        _install_extracted_tree(extract_dir, latest)

    _write_update_cache(latest, latest)
    if not args.quiet:
        print(f"lstty: updated {local_ver} -> {latest}.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="List serial ports and maintain stable user symlinks."
    )
    parser.add_argument("-a", "--all", action="store_true", help="also list offline debug probes")
    parser.add_argument("--json", action="store_true",
                        help="print the listing as JSON (stable fields for agents and scripts)")
    parser.add_argument("-V", "--version", action="store_true", help="print version and exit")
    parser.add_argument("--check-update-bg", action="store_true", help=argparse.SUPPRESS)
    subcommands = parser.add_subparsers(dest="command")

    bind = subcommands.add_parser("bind", help="bind a stable symlink name to a serial device")
    bind.add_argument("name", help="symlink name to create, for example stlink")
    bind.add_argument("usb", nargs="?", help="USB session from the table, for example 0000/3")
    bind.add_argument("--id", help="USB VID:PID, for example 0483:374f")
    bind.add_argument("--vid", help="USB vendor ID")
    bind.add_argument("--pid", help="USB product ID")
    bind.add_argument("--serial", help="USB serial number")
    bind.add_argument("--product", help="case-insensitive product-name substring")
    bind.add_argument("--link-dir", help=f"directory for symlinks, default: {DEFAULT_LINK_DIR}")

    subcommands.add_parser("links", help="show configured stable links")
    subcommands.add_parser("sync", help="create or update configured symlinks once")

    unbind = subcommands.add_parser("unbind", help="remove a configured stable link")
    unbind.add_argument("name")

    service = subcommands.add_parser("service", help="continuously keep configured symlinks updated")
    service.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")

    install = subcommands.add_parser("install-service", help="install the macOS launchd service")
    install.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")

    subcommands.add_parser("uninstall-service", help="uninstall the macOS launchd service")
    subcommands.add_parser("install-completion", help="install bundled zsh completions into ~/.zfunc")

    upd = subcommands.add_parser("update", help="check for or install standalone lstty updates from GitHub Releases")
    upd.add_argument("--check", action="store_true", help="only check if a newer release is available")
    upd.add_argument("--quiet", action="store_true", help="suppress output when already up to date")
    upd.add_argument("--force", action="store_true", help="reinstall even if version matches or inside a git checkout")

    reassign = subcommands.add_parser(
        "reassign", help="read one target ID and save its board nickname to a probe (run fresh lstty first)"
    )
    reassign.add_argument("probe", help="connected probe nickname or USB serial from a fresh lstty")
    reassign.add_argument("--name", help="nickname to save for a new/unregistered chip ID")
    reassign.add_argument("--link-name", metavar="NEW",
                          help="also rename the one serial link bound to this probe's USB serial")
    reassign.add_argument("--kind", choices=sorted(PROBE_FAMILIES),
                          help="probe family, needed only when the reference is ambiguous")

    rename = subcommands.add_parser("rename-link", help="rename a bound serial link (config and symlink)")
    rename.add_argument("old")
    rename.add_argument("new")

    static = subcommands.add_parser(
        "netprobe", help="register Ethernet debuggers without discovery, such as a BDI3000 (Network probes)",
        description="Register network debuggers that do not announce themselves. The listing checks each "
                    "with one ping and its MAC in the neighbour cache; it never opens Telnet or GDB ports. "
                    "Example: lstty netprobe add bdi3000 192.0.2.10 --kind bdi3000 --note 'bench JTAG'",
    )
    static_sub = static.add_subparsers(dest="netprobe_command")
    static_add = static_sub.add_parser("add", help="register or update a probe; learns its MAC when online")
    static_add.add_argument("name")
    static_add.add_argument("host", help="IPv4 address or host name")
    static_add.add_argument("--kind", choices=sorted(NETPROBE_KINDS), default="generic",
                            help="probe type; bdi3000 checks the Abatron MAC prefix")
    static_add.add_argument("--mac", help="pin this MAC instead of learning it")
    static_add.add_argument("--note", help="free text shown in the listing")
    static_rm = static_sub.add_parser("rm", help="remove a registered probe")
    static_rm.add_argument("name")
    static_sub.add_parser("list", help="show registered probes without checking them (default)")

    for fam in PROBE_FAMILIES.values():
        add_probe_parser(subcommands, fam)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"lstty {read_local_version()}")
        return 0
    if args.check_update_bg:
        return check_update_background()
    if args.command is None and args.json:
        print(json.dumps(json_snapshot(show_all=args.all), indent=2, ensure_ascii=False))
        return 0
    if args.command is None:
        return render_table(show_all=args.all)
    if args.command == "bind":
        return bind_link(args)
    if args.command == "links":
        return list_links()
    if args.command == "sync":
        return sync_links(verbose=True)
    if args.command == "unbind":
        return unbind_link(args.name)
    if args.command == "service":
        return run_service(args.interval)
    if args.command == "install-service":
        return install_macos_service(args.interval)
    if args.command == "uninstall-service":
        return uninstall_macos_service()
    if args.command == "install-completion":
        return install_completion()
    if args.command == "update":
        return update_self(args)
    if args.command == "reassign":
        return reassign_probe(args)
    if args.command == "rename-link":
        return rename_link(args.old, args.new)
    if args.command == "netprobe":
        return netprobe_main(args)
    if args.command in PROBE_FAMILIES:
        return probe_main(PROBE_FAMILIES[args.command], args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
