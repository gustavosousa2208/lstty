//! macOS: serial ports and the USB inventory from `ioreg`.
//! Mirrors `macos_ioreg_rows` and `usb_devices` in tools/bench/lstty, the behaviour oracle.

use std::{
    collections::{BTreeMap, HashSet},
    process::{Command, Stdio},
};

use crate::{
    ports::{SerialRow, clean_missing, icon_for},
    probes::UsbDevice,
};

fn ioreg(args: &[&str]) -> String {
    Command::new("ioreg")
        .args(args)
        .args(["-l", "-w", "0"])
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
        .unwrap_or_default()
}

/// Only the USB device subtrees: ~30x faster than dumping the whole registry. Older macOS names
/// the class IOUSBDevice, so fall back to the full dump.
pub fn usb_ioreg() -> String {
    let out = ioreg(&["-r", "-c", "IOUSBHostDevice"]);
    if out.is_empty() { ioreg(&[]) } else { out }
}

fn clean_value(line: &str) -> String {
    let value = line.split_once('=').map(|(_, v)| v.trim()).unwrap_or("");
    if value.starts_with('"') {
        return value.split('"').nth(1).unwrap_or("").to_string();
    }
    value.to_string()
}

fn key_of(line: &str) -> &str {
    line.split('"').nth(1).unwrap_or("")
}

fn ioreg_depth(line: &str) -> usize {
    line.split("+-o ").next().map(|p| p.chars().count()).unwrap_or(0)
}

fn speed_from_bits(raw: &str) -> String {
    if raw.is_empty() {
        return "-".into();
    }
    match raw.trim().parse::<f64>() {
        Ok(v) if v >= 1_000_000_000.0 => format!("{:.1} Gb/s", v / 1_000_000_000.0),
        Ok(v) => format!("{:.1} Mb/s", v / 1_000_000.0),
        Err(_) => raw.to_string(),
    }
}

#[derive(Default, Clone)]
struct Node {
    product: String,
    manufacturer: String,
    serial: String,
    vid: String,
    pid: String,
    speed: String,
    bus: String,
    dev: String,
    driver: String,
    callout: String,
}

fn emit(node: &Node, seen: &mut HashSet<String>, rows: &mut Vec<SerialRow>) {
    if node.callout.is_empty() || node.vid.is_empty() || node.pid.is_empty() {
        return;
    }
    // Hubs are IOUSBHostDevices too, so -r lists nested devices more than once.
    if !node.callout.starts_with("/dev/cu.") || !seen.insert(node.callout.clone()) {
        return;
    }
    let (Ok(vid), Ok(pid)) = (node.vid.parse::<u32>(), node.pid.parse::<u32>()) else { return };
    let product = if node.product.is_empty() { "USB serial device".to_string() } else { node.product.clone() };
    let driver = if node.driver.is_empty() { "-".to_string() } else { node.driver.clone() };
    rows.push(SerialRow {
        tty: node.callout.clone(),
        bus: if node.bus.is_empty() { "-".into() } else { node.bus.clone() },
        dev: if node.dev.is_empty() { "-".into() } else { node.dev.clone() },
        icon: icon_for(&product, &driver),
        vid: format!("0x{vid:04x}"),
        pid: format!("0x{pid:04x}"),
        serial: if node.serial.is_empty() { "-".into() } else { node.serial.clone() },
        speed: speed_from_bits(&node.speed),
        match_vid: format!("{vid:04x}"),
        match_pid: format!("{pid:04x}"),
        match_serial: clean_missing(&node.serial).to_string(),
        match_product: product.clone(),
        product,
        driver,
    });
}

/// Serial ports (`/dev/cu.*` call-out devices) with their USB parent's properties.
pub fn serial_rows(out: &str) -> Vec<SerialRow> {
    let mut stack: BTreeMap<usize, Node> = BTreeMap::new();
    let mut rows = Vec::new();
    let mut seen = HashSet::new();
    let mut depth = 0usize;

    for line in out.lines() {
        if line.contains("+-o ") {
            if let Some(node) = stack.get(&depth) {
                emit(node, &mut seen, &mut rows);
            }
            depth = ioreg_depth(line);
            stack.retain(|d, _| *d < depth);
            let parent = stack.range(..depth).next_back().map(|(_, n)| n.clone()).unwrap_or_default();
            stack.insert(depth, Node { callout: String::new(), ..parent });
            continue;
        }
        if !line.contains('"') || !line.contains('=') {
            continue;
        }
        let Some(node) = stack.get_mut(&depth) else { continue };
        let value = clean_value(line);
        match key_of(line) {
            "IOCalloutDevice" => node.callout = value,
            "USB Product Name" | "kUSBProductString" | "Product Name" => node.product = value,
            "USB Vendor Name" | "kUSBVendorString" => node.manufacturer = value,
            "USB Serial Number" | "kUSBSerialNumberString" => node.serial = value,
            "idVendor" => node.vid = value,
            "idProduct" => node.pid = value,
            "UsbLinkSpeed" => node.speed = value,
            "USB Address" => node.dev = value,
            "locationID" => {
                if let Ok(n) = value.parse::<u64>() {
                    let hex = format!("{n:x}");
                    node.bus = hex[hex.len().saturating_sub(4)..].to_string();
                }
            }
            "kUSBString" | "IOClass" => node.driver = value,
            _ => {}
        }
    }
    if let Some(node) = stack.get(&depth) {
        emit(node, &mut seen, &mut rows);
    }
    rows
}

/// Every USB device, with or without a call-out device.
pub fn usb_devices(out: &str) -> Vec<UsbDevice> {
    let mut devices = Vec::new();
    for block in out.split("+-o ").skip(1) {
        let mut props: BTreeMap<&str, String> = BTreeMap::new();
        for line in block.lines().skip(1) {
            if line.contains('"') && line.contains('=') {
                props.insert(key_of(line), clean_value(line));
            }
        }
        let num = |k: &str| props.get(k).filter(|v| !v.is_empty() && v.chars().all(|c| c.is_ascii_digit()));
        if let (Some(v), Some(p)) = (num("idVendor"), num("idProduct")) {
            let (Ok(v), Ok(p)) = (v.parse::<u64>(), p.parse::<u64>()) else { continue };
            devices.push(UsbDevice {
                vid: format!("{v:04x}"),
                pid: format!("{p:04x}"),
                serial: props.get("USB Serial Number").cloned().unwrap_or_default(),
                product: props.get("USB Product Name").cloned().unwrap_or_default(),
            });
        }
    }
    devices
}

