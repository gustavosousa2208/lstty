//! Windows serial-port and USB inventory, read in-process from the registry and
//! cfgmgr32 (port of `windows_fast_ports`, `windows_usb_devices` and
//! `windows_serial_rows` in tools/bench/lstty).

use std::collections::{BTreeMap, BTreeSet};

use windows_sys::{
    Win32::{
        Devices::{
            DeviceAndDriverInstallation::{CM_Get_DevNode_PropertyW, CM_Locate_DevNodeW},
            Properties::DEVPROPKEY,
        },
        System::Registry::{
            HKEY, HKEY_LOCAL_MACHINE, KEY_READ, RegCloseKey, RegEnumKeyExW, RegOpenKeyExW, RegQueryValueExW,
        },
    },
    core::GUID,
};

use crate::probes::UsbDevice;

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}

struct Key(HKEY);

impl Key {
    fn open(parent: HKEY, path: &str) -> Option<Key> {
        let mut h: HKEY = std::ptr::null_mut();
        let rc = unsafe { RegOpenKeyExW(parent, wide(path).as_ptr(), 0, KEY_READ, &mut h) };
        (rc == 0).then_some(Key(h))
    }

    fn subkeys(&self) -> Vec<String> {
        let mut names = Vec::new();
        let mut index = 0;
        loop {
            let mut buf = [0u16; 512];
            let mut n = buf.len() as u32;
            let rc = unsafe {
                RegEnumKeyExW(
                    self.0,
                    index,
                    buf.as_mut_ptr(),
                    &mut n,
                    std::ptr::null(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            if rc != 0 {
                return names;
            }
            names.push(String::from_utf16_lossy(&buf[..n as usize]));
            index += 1;
        }
    }

    /// REG_SZ / REG_EXPAND_SZ value, or "" (other types are not strings for lstty).
    fn string(&self, name: &str) -> String {
        let key = wide(name);
        let mut kind = 0u32;
        let mut size = 0u32;
        let rc = unsafe {
            RegQueryValueExW(self.0, key.as_ptr(), std::ptr::null(), &mut kind, std::ptr::null_mut(), &mut size)
        };
        if rc != 0 || !(kind == 1 || kind == 2) || size == 0 {
            return String::new();
        }
        let mut buf = vec![0u16; (size as usize).div_ceil(2) + 1];
        let mut size = (buf.len() * 2) as u32;
        let rc = unsafe {
            RegQueryValueExW(self.0, key.as_ptr(), std::ptr::null(), &mut kind, buf.as_mut_ptr().cast(), &mut size)
        };
        if rc != 0 {
            return String::new();
        }
        let n = (size as usize / 2).min(buf.len());
        String::from_utf16_lossy(&buf[..n]).trim_end_matches('\0').to_string()
    }
}

impl Drop for Key {
    fn drop(&mut self) {
        unsafe { RegCloseKey(self.0) };
    }
}

fn devpropkey(fmtid: u128, pid: u32) -> DEVPROPKEY {
    DEVPROPKEY { fmtid: GUID::from_u128(fmtid), pid }
}

struct Cfgmgr {
    parent_key: DEVPROPKEY,
    busdesc_key: DEVPROPKEY,
}

impl Cfgmgr {
    fn new() -> Self {
        Cfgmgr {
            parent_key: devpropkey(0x4340a6c5_93fa_4706_972c_7b648008a5a7, 8),
            busdesc_key: devpropkey(0x540b947e_8b40_45bc_a8a2_6a0b894cbda2, 4),
        }
    }

    /// Devnode for a *present* device; unplugged ghosts fail to locate.
    fn locate(&self, instance: &str) -> Option<u32> {
        let mut devinst = 0u32;
        (unsafe { CM_Locate_DevNodeW(&mut devinst, wide(instance).as_ptr(), 0) } == 0).then_some(devinst)
    }

    fn string(&self, devinst: u32, key: &DEVPROPKEY) -> String {
        let mut kind = 0u32;
        let mut buf = [0u8; 1024];
        let mut size = buf.len() as u32;
        let rc = unsafe { CM_Get_DevNode_PropertyW(devinst, key, &mut kind, buf.as_mut_ptr(), &mut size, 0) };
        if rc != 0 {
            return String::new();
        }
        let units: Vec<u16> =
            buf[..(size as usize).min(buf.len())].chunks_exact(2).map(|b| u16::from_le_bytes([b[0], b[1]])).collect();
        String::from_utf16_lossy(&units).trim_end_matches('\0').to_string()
    }
}

/// Last `;`-separated field: registry descriptors look like `@oem.inf,%name%;Real name`.
fn last_field(s: &str) -> String {
    s.rsplit(';').next().unwrap_or("").to_string()
}

pub struct PnpPort {
    port: String,
    name: String,
    description: String,
    manufacturer: String,
    service: String,
    instance: String,
    parent: String,
    busdesc: String,
}

fn com_number(port: &str) -> u32 {
    port.get(3..).and_then(|n| n.parse().ok()).unwrap_or(0)
}

fn is_com(port: &str) -> bool {
    port.len() > 3 && port[..3].eq_ignore_ascii_case("COM") && port[3..].bytes().all(|b| b.is_ascii_digit())
}

fn fast_ports() -> Vec<PnpPort> {
    let cfg = Cfgmgr::new();
    let mut candidates = Vec::new();
    if let Some(enum_key) = Key::open(HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum") {
        for bus in enum_key.subkeys() {
            let Some(bus_key) = Key::open(enum_key.0, &bus) else { continue };
            for dev in bus_key.subkeys() {
                let Some(dev_key) = Key::open(bus_key.0, &dev) else { continue };
                for inst in dev_key.subkeys() {
                    let Some(inst_key) = Key::open(dev_key.0, &inst) else { continue };
                    let Some(params) = Key::open(inst_key.0, "Device Parameters") else { continue };
                    let port = params.string("PortName").to_uppercase();
                    if port.is_empty() {
                        continue;
                    }
                    candidates.push((
                        port,
                        format!("{bus}\\{dev}\\{inst}"),
                        inst_key.string("FriendlyName"),
                        last_field(&inst_key.string("DeviceDesc")),
                        last_field(&inst_key.string("Mfg")),
                        inst_key.string("Service"),
                    ));
                }
            }
        }
    }

    let mut result = Vec::new();
    let mut seen = BTreeSet::new();
    for (port, instance, friendly, desc, mfg, service) in candidates {
        if seen.contains(&port) {
            continue;
        }
        let Some(devinst) = cfg.locate(&instance) else { continue };
        seen.insert(port.clone());
        let upper = instance.to_uppercase();
        let usb = upper.starts_with("USB\\") || upper.starts_with("FTDIBUS\\");
        let parent = if usb { cfg.string(devinst, &cfg.parent_key) } else { String::new() };
        let mut busdesc = if usb { cfg.string(devinst, &cfg.busdesc_key) } else { String::new() };
        if usb && busdesc.is_empty() && !parent.is_empty() {
            // FTDIBUS ports carry no descriptor; their USB parent does.
            if let Some(parent_inst) = cfg.locate(&parent) {
                busdesc = cfg.string(parent_inst, &cfg.busdesc_key);
            }
        }
        let name = if friendly.is_empty() { format!("{desc} ({port})") } else { friendly };
        result.push(PnpPort {
            port,
            name,
            description: desc,
            manufacturer: mfg,
            service,
            instance,
            parent,
            busdesc,
        });
    }
    result.sort_by_key(|p| com_number(&p.port));
    result
}

fn registry_ports() -> Vec<String> {
    let Some(key) = Key::open(HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") else { return vec![] };
    let mut ports = BTreeSet::new();
    let mut index = 0u32;
    loop {
        let mut name = [0u16; 256];
        let mut nlen = name.len() as u32;
        let mut kind = 0u32;
        let mut data = [0u16; 64];
        let mut dlen = (data.len() * 2) as u32;
        let rc = unsafe {
            windows_sys::Win32::System::Registry::RegEnumValueW(
                key.0,
                index,
                name.as_mut_ptr(),
                &mut nlen,
                std::ptr::null(),
                &mut kind,
                data.as_mut_ptr().cast(),
                &mut dlen,
            )
        };
        if rc != 0 {
            break;
        }
        index += 1;
        if kind == 1 {
            let value = String::from_utf16_lossy(&data[..(dlen as usize / 2).min(data.len())])
                .trim_end_matches('\0')
                .to_uppercase();
            if is_com(&value) {
                ports.insert(value);
            }
        }
    }
    let mut ports: Vec<String> = ports.into_iter().collect();
    ports.sort_by_key(|p| com_number(p));
    ports
}

/// (vid, pid, serial) parsed from a PnP instance ID, all lowercase / possibly empty.
pub fn device_details(device_id: &str) -> (String, String, String) {
    let upper = device_id.to_ascii_uppercase();
    let hex4 = |marker: &str| -> String {
        let mut from = 0;
        while let Some(pos) = upper[from..].find(marker) {
            let at = from + pos + marker.len();
            if let Some(sep) = upper.as_bytes().get(at) {
                if matches!(sep, b'_' | b'&' | b'+') {
                    let digits = upper.get(at + 1..at + 5).unwrap_or("");
                    if digits.len() == 4 && digits.bytes().all(|b| b.is_ascii_hexdigit()) {
                        return digits.to_lowercase();
                    }
                }
            }
            from = at;
        }
        String::new()
    };
    let vid = hex4("VID");
    let pid = hex4("PID");

    let mut serial = String::new();
    // FTDI places the serial in the hardware portion of its instance ID: VID_x+PID_y+SERIAL
    if let Some(pos) = upper.find("VID_") {
        let rest = &upper[pos + 4..];
        if rest.len() >= 15
            && rest[..4].bytes().all(|b| b.is_ascii_hexdigit())
            && rest[4..9] == *"+PID_"
            && rest[9..13].bytes().all(|b| b.is_ascii_hexdigit())
            && rest.as_bytes()[13] == b'+'
        {
            let tail = &device_id[pos + 4 + 14..];
            let end = tail.find('\\').unwrap_or(tail.len());
            if end > 0 {
                serial = tail[..end].to_string();
            }
        }
    }
    if serial.is_empty() && upper.starts_with("USB\\") {
        let candidate = device_id.rsplit('\\').next().unwrap_or("");
        // IDs containing '&' are normally topology-generated, not serial numbers.
        if !candidate.is_empty() && !candidate.contains('&') {
            serial = candidate.to_string();
        }
    }
    (vid, pid, serial)
}

fn strip_com_suffix(name: &str) -> String {
    let t = name.trim_end();
    if let Some(open) = t.rfind('(') {
        if t.ends_with(')') && is_com(&t[open + 1..t.len() - 1]) {
            return t[..open].trim_end().to_string();
        }
    }
    t.to_string()
}

#[derive(Clone)]
pub struct SerialRow {
    pub tty: String,
    pub bus: &'static str,
    pub vid: String,
    pub pid: String,
    pub product: String,
    pub serial: String,
}

pub fn serial_rows() -> Vec<SerialRow> {
    let mut devices: BTreeMap<u32, (String, Option<PnpPort>)> = BTreeMap::new();
    for item in fast_ports() {
        if is_com(&item.port) {
            devices.insert(com_number(&item.port), (item.port.to_uppercase(), Some(item)));
        }
    }
    // The registry also catches virtual and legacy ports that PnP omits.
    for port in registry_ports() {
        devices.entry(com_number(&port)).or_insert((port, None));
    }

    let mut rows = Vec::new();
    for (_, (port, item)) in devices {
        let device_id = item.as_ref().map(|i| i.instance.as_str()).unwrap_or("");
        let (mut vid, mut pid, mut serial) = device_details(device_id);
        let upper = device_id.to_ascii_uppercase();
        let is_ftdi = upper.starts_with("FTDIBUS\\");
        if !(is_ftdi || upper.starts_with("USB\\")) {
            // Bluetooth instance IDs contain VID&/PID& tokens that are not USB.
            vid.clear();
            pid.clear();
            serial.clear();
        }
        if !vid.is_empty() && (is_ftdi || serial.is_empty()) {
            // Composite devices keep the serial on the parent, not on the MI_xx child.
            let parent_serial = item.as_ref().map(|i| device_details(&i.parent).2).unwrap_or_default();
            if !parent_serial.is_empty() {
                serial = parent_serial;
            }
        }
        let name = item
            .as_ref()
            .map(|i| {
                if !i.name.is_empty() { i.name.clone() } else { i.description.clone() }
            })
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| "Serial port".into());
        let busdesc = item.as_ref().map(|i| i.busdesc.clone()).unwrap_or_default();
        let product = if busdesc.is_empty() { strip_com_suffix(&name) } else { busdesc };
        let product = if product.is_empty() { "Serial port".to_string() } else { product };
        let _ = item.as_ref().map(|i| (&i.manufacturer, &i.service));
        rows.push(SerialRow {
            tty: port,
            bus: if !vid.is_empty() && !pid.is_empty() { "USB" } else { "-" },
            vid,
            pid,
            product,
            serial,
        });
    }
    rows
}

/// Present USB devices (top level only; per-interface MI_xx children are skipped).
pub fn usb_devices() -> Vec<UsbDevice> {
    let cfg = Cfgmgr::new();
    let mut devices = Vec::new();
    let Some(usb) = Key::open(HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\USB") else { return devices };
    for dev in usb.subkeys() {
        let up = dev.to_ascii_uppercase();
        let ok = up.len() == 17
            && up.starts_with("VID_")
            && &up[8..13] == "&PID_"
            && up[4..8].bytes().all(|b| b.is_ascii_hexdigit())
            && up[13..17].bytes().all(|b| b.is_ascii_hexdigit());
        if !ok {
            continue;
        }
        let Some(dev_key) = Key::open(usb.0, &dev) else { continue };
        for inst in dev_key.subkeys() {
            let Some(devinst) = cfg.locate(&format!("USB\\{dev}\\{inst}")) else { continue };
            devices.push(UsbDevice {
                vid: up[4..8].to_lowercase(),
                pid: up[13..17].to_lowercase(),
                serial: if inst.contains('&') { String::new() } else { inst.clone() },
                product: cfg.string(devinst, &cfg.busdesc_key),
            });
        }
    }
    devices
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn details_from_usb_instance() {
        let (v, p, s) = device_details(r"USB\VID_0483&PID_374B\066BFF485753667187244749");
        assert_eq!((v.as_str(), p.as_str(), s.as_str()), ("0483", "374b", "066BFF485753667187244749"));
    }

    #[test]
    fn composite_child_has_no_serial() {
        let (v, p, s) = device_details(r"USB\VID_1366&PID_1055&MI_00\7&2A1B&0&0000");
        assert_eq!((v.as_str(), p.as_str(), s.as_str()), ("1366", "1055", ""));
    }

    #[test]
    fn ftdi_serial_from_hardware_id() {
        let (v, p, s) = device_details(r"FTDIBUS\VID_0403+PID_6001+A50285BIA\0000");
        assert_eq!((v.as_str(), p.as_str(), s.as_str()), ("0403", "6001", "A50285BIA"));
    }

    #[test]
    fn com_suffix_is_stripped() {
        assert_eq!(strip_com_suffix("USB Serial Device (COM12) "), "USB Serial Device");
        assert_eq!(strip_com_suffix("Porta (x)"), "Porta (x)");
    }
}
