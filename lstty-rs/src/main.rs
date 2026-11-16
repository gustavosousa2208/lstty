//! lstty, native front end:
//! the listing hot path runs here (Windows, macOS, Linux); every other command delegates to
//! lstty.py. Set LSTTY_FORCE_PYTHON=1 to bypass this binary's listing.

#![cfg_attr(not(windows), allow(dead_code, unused_imports))]

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(unix)]
mod ports;
mod probes;
mod table;
#[cfg(windows)]
mod winports;

use std::{
    env,
    io::{IsTerminal, Write},
    path::PathBuf,
    process::{Command, ExitCode},
};

/// Closing lines of a listing. A terminal gets one dim line; a pipe (an agent or a script) keeps the
/// explanations and the pointer to `--json`. Mirrors `live_footer` in tools/bench/lstty.
fn print_footer(p: &table::Painter, tty: bool, stamp: &str) {
    if tty {
        println!("{}", p.paint(&format!("Live at {stamp}"), table::DIM));
        return;
    }
    println!(
        "STATUS = probe link; PORT '-' = no serial port. Probe, UART and USB CDC are independent: \
         missing CDC is not proof of an unplugged or unpowered board."
    );
    println!("Live at {stamp}: replug invalidates this snapshot; re-run `lstty` before reassign/flash/console.");
    println!("Agents: `lstty --json` returns this listing as stable fields, with notes and how-to paths.");
}

/// Locate lstty.py: LSTTY_PY, standalone bundle next to exe, then repo checkout.
fn python_script() -> PathBuf {
    if let Some(p) = env::var_os("LSTTY_PY") {
        return PathBuf::from(p);
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            let bundled = dir.join("lstty.py");
            if bundled.exists() {
                return bundled;
            }
        }
    }
    let from_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../lstty.py");
    if from_manifest.exists() {
        return from_manifest;
    }
    // <repo>/lstty-rs/target/release/lstty(.exe)
    env::current_exe()
        .ok()
        .and_then(|exe| exe.ancestors().nth(4).map(|root| root.join("lstty.py")))
        .unwrap_or_else(|| PathBuf::from("lstty.py"))
}

fn cleanup_old_exe() {
    #[cfg(windows)]
    if let Ok(exe) = env::current_exe() {
        let mut old = exe.into_os_string();
        old.push(".old");
        let _ = std::fs::remove_file(PathBuf::from(old));
    }
}

fn update_cache_path() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        let base = env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| env::var_os("USERPROFILE").map(|h| PathBuf::from(h).join("AppData/Local")))?;
        return Some(base.join("lstty/update-check.json"));
    }
    #[cfg(not(windows))]
    {
        let base = env::var_os("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .or_else(|| env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))?;
        Some(base.join("lstty/update-check.json"))
    }
}

fn passive_update_check(now: u64) {
    let Ok(exe) = env::current_exe() else { return };
    let Some(dir) = exe.parent() else { return };
    let version_file = dir.join("VERSION");
    let Ok(current_raw) = std::fs::read_to_string(&version_file) else { return };
    let current = current_raw.trim();
    if current.is_empty() {
        return;
    }

    let mut needs_refresh = true;
    if let Some(cache_path) = update_cache_path() {
        if let Ok(text) = std::fs::read_to_string(&cache_path) {
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(&text) {
                let checked_at = val.get("checked_at").and_then(|v| v.as_u64()).unwrap_or(0);
                if now.saturating_sub(checked_at) < 86_400 {
                    needs_refresh = false;
                }
                if let Some(latest) = val.get("latest").and_then(|v| v.as_str()) {
                    if !latest.is_empty() && latest != current {
                        eprintln!(
                            "lstty: update available ({latest}; current {current}). Run 'lstty update'."
                        );
                    }
                }
            }
        }
    }

    if needs_refresh && env::var_os("LSTTY_NO_UPDATE_CHECK").is_none() {
        let script = python_script();
        if script.exists() {
            use std::process::Stdio;
            let mut cmd = python_command(&script);
            cmd.arg("--check-update-bg")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                cmd.creation_flags(0x0800_0000 | 0x0000_0008);
            }
            if cmd.spawn().is_err() && cfg!(windows) {
                let mut fb = Command::new("python");
                fb.arg(&script)
                    .arg("--check-update-bg")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null());
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    fb.creation_flags(0x0800_0000 | 0x0000_0008);
                }
                let _ = fb.spawn();
            }
        }
    }
}

fn python_command(script: &PathBuf) -> Command {
    let (program, first): (&str, &[&str]) = if cfg!(windows) { ("py", &["-3"]) } else { ("python3", &[]) };
    let mut cmd = Command::new(program);
    cmd.args(first).arg(script);
    cmd
}

fn delegate(args: &[String]) -> ExitCode {
    let script = python_script();
    let mut cmd = python_command(&script);
    cmd.args(args);
    let mut status = cmd.status();
    if cfg!(windows) && status.is_err() {
        // No py launcher: fall back to python on PATH.
        status = Command::new("python").arg(&script).args(args).status();
    }
    match status {
        Ok(s) => ExitCode::from(s.code().unwrap_or(1).clamp(0, 255) as u8),
        Err(e) => {
            eprintln!("lstty: cannot run Python front end {}: {e}", script.display());
            ExitCode::from(127)
        }
    }
}

#[cfg(windows)]
mod console {
    use windows_sys::Win32::System::{
        Console::{
            CONSOLE_SCREEN_BUFFER_INFO, GetConsoleMode, GetConsoleScreenBufferInfo, GetStdHandle,
            STD_OUTPUT_HANDLE, SetConsoleMode,
        },
        SystemInformation::GetLocalTime,
    };

    const ENABLE_VIRTUAL_TERMINAL_PROCESSING: u32 = 0x0004;

    pub fn enable_vt() {
        unsafe {
            let h = GetStdHandle(STD_OUTPUT_HANDLE);
            let mut mode = 0u32;
            if GetConsoleMode(h, &mut mode) != 0 {
                SetConsoleMode(h, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
            }
        }
    }

    pub fn columns() -> Option<usize> {
        unsafe {
            let mut info: CONSOLE_SCREEN_BUFFER_INFO = std::mem::zeroed();
            if GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &mut info) == 0 {
                return None;
            }
            let w = info.srWindow.Right as i32 - info.srWindow.Left as i32 + 1;
            (w > 0).then_some(w as usize)
        }
    }

    pub fn clock() -> String {
        let t = unsafe {
            let mut t = std::mem::zeroed();
            GetLocalTime(&mut t);
            t
        };
        format!("{:02}:{:02}:{:02}", t.wHour, t.wMinute, t.wSecond)
    }
}

/// Terminal width; unlimited when piped (agents) unless COLUMNS says otherwise.
#[cfg(windows)]
fn output_width() -> usize {
    if let Some(n) = env::var("COLUMNS").ok().and_then(|v| v.parse::<usize>().ok()) {
        return n;
    }
    if !std::io::stdout().is_terminal() {
        return 1_000_000;
    }
    console::columns().unwrap_or(100)
}

#[cfg(windows)]
fn native_list(show_all: bool) -> ExitCode {
    use std::collections::BTreeMap;
    use std::time::{SystemTime, UNIX_EPOCH};

    use table::{CYAN, Col, DIM, GREEN, MAGENTA, Painter, Row};

    console::enable_vt();
    let color = std::io::stdout().is_terminal() && env::var_os("NO_COLOR").is_none();
    let p = Painter { enabled: color };
    let width = output_width();
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);

    let mut rows = winports::serial_rows();
    if !show_all {
        // Bluetooth/ACPI ports are noise when looking for a board; -a lists them.
        rows.retain(|r| r.bus == "USB");
    }
    let usb = winports::usb_devices();
    let configs: Vec<probes::Config> = probes::FAMILIES.iter().map(probes::load_config).collect();

    let mut out = String::new();
    let mut code = 0;
    if rows.is_empty() {
        out.push_str("No serial ports found.\n");
        code = 1;
    } else {
        let table: Vec<Row> = rows
            .iter()
            .map(|row| {
                let mut labels = Vec::new();
                for (fam, cfg) in probes::FAMILIES.iter().zip(&configs) {
                    if fam.matches(&row.vid, &row.pid) {
                        if let Some(nicks) = cfg.names.get(&fam.normalize(&row.serial)) {
                            labels.push(format!("{}:{}", fam.key, nicks.join(",")));
                        }
                    }
                }
                let mut r = Row::default();
                r.set("tty", row.tty.clone());
                r.set("id", if row.vid.is_empty() { String::new() } else { format!("{}:{}", row.vid, row.pid) });
                r.set("product", row.product.clone());
                r.set("serial", row.serial.clone());
                r.set("probe", labels.join(" "));
                r.set("bind", "-");
                r
            })
            .collect();
        let columns = [
            Col::new("tty", "PORT", CYAN, 5),
            Col::new("id", "ID", MAGENTA, 9),
            Col::new("product", "DEVICE", "", 10),
            Col::new("serial", "SERIAL", GREEN, 8).mid(),
            Col::new("probe", "BOARD/PROBE", MAGENTA, 11),
            Col::new("bind", "SERIAL ALIAS", DIM, 12).flex(),
        ];
        table::render_columns(&mut out, &p, &columns, &table, width);
    }

    for (fam, cfg) in probes::FAMILIES.iter().zip(&configs) {
        let mut ttys: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for row in &rows {
            if fam.matches(&row.vid, &row.pid) {
                let serial = fam.normalize(&row.serial);
                if !serial.is_empty() {
                    ttys.entry(serial).or_default().push(row.tty.clone());
                }
            }
        }
        probes::render_probe_table(&mut out, &p, fam, cfg, &usb, &ttys, show_all, now, width);
    }

    // Ports and probes appear at once; the slow network sections follow from Python.
    let mut stdout = std::io::stdout();
    let _ = stdout.write_all(out.as_bytes());
    let _ = stdout.flush();
    if env::var_os("LSTTY_NO_NETWORK").is_none() {
        let mut args = vec!["--only-network".to_string()];
        if show_all {
            args.push("-a".into());
        }
        let _ = delegate(&args);
    }

    print_footer(&p, std::io::stdout().is_terminal(), &console::clock());
    ExitCode::from(code)
}

#[cfg(unix)]
mod unix_console {
    pub fn columns() -> Option<usize> {
        let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
        let ok = unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) } == 0;
        (ok && ws.ws_col > 0).then_some(ws.ws_col as usize)
    }

    pub fn clock() -> String {
        unsafe {
            let now = libc::time(std::ptr::null_mut());
            let mut tm: libc::tm = std::mem::zeroed();
            libc::localtime_r(&now, &mut tm);
            format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec)
        }
    }
}

/// Terminal width; unlimited when piped (agents) unless COLUMNS says otherwise.
#[cfg(unix)]
fn output_width() -> usize {
    if let Some(n) = env::var("COLUMNS").ok().and_then(|v| v.parse::<usize>().ok()) {
        return n;
    }
    if !std::io::stdout().is_terminal() {
        return 1_000_000;
    }
    unix_console::columns().unwrap_or(100)
}

#[cfg(unix)]
fn native_list(show_all: bool) -> ExitCode {
    use std::collections::{BTreeMap, BTreeSet};
    use std::time::{SystemTime, UNIX_EPOCH};

    use table::{BLUE, CYAN, Col, DIM, GREEN, MAGENTA, Painter, Row, YELLOW};

    let tty = std::io::stdout().is_terminal();
    let p = Painter { enabled: tty && env::var_os("NO_COLOR").is_none() };
    let width = output_width();
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);

    #[cfg(target_os = "macos")]
    let (rows, usb) = {
        let ioreg = macos::usb_ioreg();
        (macos::serial_rows(&ioreg), macos::usb_devices(&ioreg))
    };
    #[cfg(target_os = "linux")]
    let (rows, usb) = (linux::serial_rows(), linux::usb_devices());
    // Keep the ~/.serial names pointing at the current port, like the Python listing does.
    let links = ports::load_links();
    ports::sync_links(&links, &rows);
    let configs: Vec<probes::Config> = probes::FAMILIES.iter().map(probes::load_config).collect();

    let mut out = String::new();
    let mut code = 0;
    if rows.is_empty() {
        out.push_str("No serial ports found.\n");
        code = 1;
    } else {
        let mut notices = Vec::new();
        let table: Vec<Row> = rows
            .iter()
            .map(|row| {
                let bind = ports::bound_names(row, &links);
                let mut labels = Vec::new();
                for (fam, cfg) in probes::FAMILIES.iter().zip(&configs) {
                    if !fam.matches(&row.match_vid, &row.match_pid) {
                        continue;
                    }
                    let serial = fam.normalize(&row.match_serial);
                    if let Some(nicks) = cfg.names.get(&serial) {
                        let label = format!("{}:{}", fam.key, nicks.join(","));
                        let bound: BTreeSet<&str> = bind.split(", ").collect();
                        let named: BTreeSet<&str> = nicks.iter().map(String::as_str).collect();
                        if bind != "-" && bound != named {
                            notices.push(format!(
                                "{}: serial alias '{bind}' differs from probe '{label}'. \
                                 Different aliases do not establish different boards; the serial alias may be stale. \
                                 If identity is uncertain, use `lstty reassign {serial} --kind {}` \
                                 within the target safety gates.",
                                row.tty, fam.key
                            ));
                        }
                        labels.push(label);
                    }
                }
                let mut r = Row::default();
                r.set("tty", row.tty.clone());
                r.set("usb", format!("{}/{}", row.bus, row.dev));
                r.set("icon", row.icon);
                r.set(
                    "id",
                    if row.vid.starts_with("0x") {
                        format!("{}:{}", &row.vid[2..], row.pid.get(2..).unwrap_or(""))
                    } else {
                        "-".into()
                    },
                );
                r.set("product", row.product.clone());
                r.set("serial", row.serial.clone());
                r.set("driver", row.driver.clone());
                r.set("speed", row.speed.clone());
                r.set("bind", bind);
                r.set("probe", if labels.is_empty() { "-".to_string() } else { labels.join(" ") });
                r
            })
            .collect();
        let mut speed = Col::new("speed", "SPEED", "", 10).drop(4);
        speed.right = true;
        let mut columns = vec![
            Col::new("tty", "TTY", CYAN, 12).mid(),
            Col::new("usb", "USB", BLUE, 6).drop(2),
            Col::new("icon", "", "", 1).drop(5),
            Col::new("id", "ID", MAGENTA, 9),
            Col::new("product", "DEVICE", "", 10),
            Col::new("serial", "SERIAL", GREEN, 8).mid(),
        ];
        // DRV is for agents and scripts (also in --json); people on a terminal get the width back.
        if !tty {
            columns.push(Col::new("driver", "DRV", YELLOW, 4).drop(3));
        }
        columns.push(speed);
        columns.push(Col::new("probe", "BOARD/PROBE", MAGENTA, 11));
        columns.push(Col::new("bind", "SERIAL ALIAS", DIM, 12).flex());
        table::render_columns(&mut out, &p, &columns, &table, width);
        for notice in notices {
            out.push_str(&format!("Alias note: {notice}\n"));
        }
    }

    for (fam, cfg) in probes::FAMILIES.iter().zip(&configs) {
        let mut ttys: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for row in &rows {
            if fam.matches(&row.match_vid, &row.match_pid) {
                let serial = fam.normalize(&row.match_serial);
                if !serial.is_empty() {
                    ttys.entry(serial).or_default().push(row.tty.clone());
                }
            }
        }
        probes::render_probe_table(&mut out, &p, fam, cfg, &usb, &ttys, show_all, now, width);
    }

    let mut stdout = std::io::stdout();
    let _ = stdout.write_all(out.as_bytes());
    let _ = stdout.flush();

    print_footer(&p, tty, &unix_console::clock());
    passive_update_check(now as u64);
    ExitCode::from(code)
}



fn main() -> ExitCode {
    cleanup_old_exe();
    let args: Vec<String> = env::args().skip(1).collect();
    let listing = args.iter().all(|a| a == "-a" || a == "--all");
    let native = listing
        && env::var_os("LSTTY_FORCE_PYTHON").is_none()
        && if cfg!(windows) {
            // The Python listing also refreshes serial-alias symlinks; Windows leaves that case to it.
            !probes::has_links()
        } else {
            // macOS and Linux keep the links in sync natively (ports::sync_links).
            cfg!(any(target_os = "macos", target_os = "linux"))
        };
    if native {
        let show_all = !args.is_empty();
        return native_list(show_all);
    }
    delegate(&args)
}
