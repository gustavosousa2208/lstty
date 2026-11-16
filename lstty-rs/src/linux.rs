//! Linux: serial ports from sysfs and the udev database, and the USB inventory from sysfs.
//! Mirrors the Linux branch of `serial_ttys` and `usb_devices` in tools/bench/lstty, the behaviour
//! oracle. The udev properties come straight from /run/udev/data (no `udevadm` process per port).

use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

use crate::{
    ports::{SerialRow, clean_missing, icon_for, normalize_usb_id},
    probes::UsbDevice,
};

fn read_text(path: impl AsRef<Path>) -> String {
    fs::read(path).map(|b| String::from_utf8_lossy(&b).trim().to_string()).unwrap_or_default()
}

/// `udevadm info --query=property` without the process: the `E:` lines of the device's database file.
fn udev_props(tty: &Path, devname: &str) -> BTreeMap<String, String> {
    let mut props = BTreeMap::new();
    let majmin = read_text(tty.join("dev"));
    if let Ok(db) = fs::read_to_string(format!("/run/udev/data/c{majmin}")) {
        for line in db.lines() {
            if let Some((key, value)) = line.strip_prefix("E:").and_then(|l| l.split_once('=')) {
                props.insert(key.to_string(), value.to_string());
            }
        }
        return props;
    }
    // No database entry (container, early boot): ask udevadm like the Python listing does.
    let out = Command::new("udevadm")
        .args(["info", "--query=property", &format!("--name={devname}")])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default();
    for line in out.lines() {
        if let Some((key, value)) = line.split_once('=') {
            props.insert(key.to_string(), value.to_string());
        }
    }
    props
}

fn usb_parent(sys_dev: &Path) -> Option<PathBuf> {
    let resolved = fs::canonicalize(sys_dev).ok()?;
    resolved
        .ancestors()
        .find(|p| p.join("idVendor").exists() && p.join("idProduct").exists())
        .map(Path::to_path_buf)
}

fn speed_label(raw: &str) -> String {
    if raw.is_empty() {
        return "-".into();
    }
    match raw.trim().parse::<f64>() {
        Ok(v) => format!("{v:.1} Mb/s"),
        Err(_) => raw.to_string(),
    }
}

pub fn serial_rows() -> Vec<SerialRow> {
    let mut names: Vec<String> = fs::read_dir("/sys/class/tty")
        .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect())
        .unwrap_or_default();
    names.sort();

    let mut rows = Vec::new();
    for name in names {
        let tty = Path::new("/sys/class/tty").join(&name);
        let devname = format!("/dev/{name}");
        let sys_dev = tty.join("device");
        if !Path::new(&devname).exists() || !sys_dev.exists() {
            continue;
        }
        let props = udev_props(&tty, &devname);
        let prop = |k: &str| props.get(k).cloned().unwrap_or_default();
        let usb = usb_parent(&sys_dev);

        // Keep USB serial ports and common explicit serial device names.
        if usb.is_none() && !["ttyUSB", "ttyACM", "ttyAMA", "rfcomm"].iter().any(|p| name.starts_with(p)) {
            continue;
        }
        let usb_text = |file: &str| usb.as_ref().map(|u| read_text(u.join(file))).unwrap_or_default();
        let vid = if usb.is_some() { usb_text("idVendor") } else { prop("ID_VENDOR_ID") };
        let pid = if usb.is_some() { usb_text("idProduct") } else { prop("ID_MODEL_ID") };

        let or_dash = |s: String| if s.is_empty() { "-".to_string() } else { s };
        let first = |a: String, b: String| if a.is_empty() { b } else { a };
        let product = or_dash(first(usb_text("product"), prop("ID_MODEL").replace('_', " ")));
        let serial = or_dash(first(usb_text("serial"), prop("ID_SERIAL_SHORT")));
        let driver = or_dash(first(
            prop("ID_USB_DRIVER"),
            fs::canonicalize(&sys_dev)
                .ok()
                .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
                .unwrap_or_default(),
        ));
        let (bus, dev) = if usb.is_some() {
            (or_dash(usb_text("busnum")), or_dash(usb_text("devnum")))
        } else {
            ("-".into(), "-".into())
        };

        rows.push(SerialRow {
            tty: devname,
            bus,
            dev,
            icon: icon_for(&product, &driver),
            vid: if vid.is_empty() { "-".into() } else { format!("0x{vid}") },
            pid: if pid.is_empty() { "-".into() } else { format!("0x{pid}") },
            speed: speed_label(&usb_text("speed")),
            match_vid: normalize_usb_id(&vid),
            match_pid: normalize_usb_id(&pid),
            match_serial: clean_missing(&serial).to_string(),
            match_product: product.clone(),
            product,
            serial,
            driver,
        });
    }
    rows
}

/// Every USB device, with or without a tty.
pub fn usb_devices() -> Vec<UsbDevice> {
    let mut devices = Vec::new();
    let Ok(dir) = fs::read_dir("/sys/bus/usb/devices") else { return devices };
    for entry in dir.flatten() {
        let dev = entry.path();
        let vid = read_text(dev.join("idVendor")).to_lowercase();
        if !vid.is_empty() {
            devices.push(UsbDevice {
                vid,
                pid: read_text(dev.join("idProduct")).to_lowercase(),
                serial: read_text(dev.join("serial")),
                product: read_text(dev.join("product")),
            });
        }
    }
    devices
}
