//! Column layout and rendering, a line-for-line port of `fit_widths` and
//! `render_columns` from tools/bench/lstty so both front ends print the same table.

use std::collections::HashMap;

pub const RESET: &str = "\x1b[0m";
pub const DIM: &str = "\x1b[2m";
pub const CYAN: &str = "\x1b[36m";
pub const GREEN: &str = "\x1b[32m";
pub const YELLOW: &str = "\x1b[33m";
pub const MAGENTA: &str = "\x1b[35m";
#[cfg(unix)]
pub const BLUE: &str = "\x1b[34m";
pub const BOLD: &str = "\x1b[1m";

pub struct Col {
    pub key: &'static str,
    pub header: &'static str,
    pub color: &'static str,
    pub min: usize,
    /// Optional column: shrunk first, then dropped, higher numbers first.
    pub drop: Option<u32>,
    /// Free text that only takes the space left over.
    pub flex: bool,
    /// Truncate in the middle (paths, serials).
    pub mid: bool,
    pub right: bool,
}

impl Col {
    pub fn new(key: &'static str, header: &'static str, color: &'static str, min: usize) -> Self {
        Col { key, header, color, min, drop: None, flex: false, mid: false, right: false }
    }
    pub fn drop(mut self, n: u32) -> Self {
        self.drop = Some(n);
        self
    }
    pub fn flex(mut self) -> Self {
        self.flex = true;
        self
    }
    pub fn mid(mut self) -> Self {
        self.mid = true;
        self
    }
}

#[derive(Default)]
pub struct Row {
    cells: HashMap<&'static str, String>,
    pub dim: bool,
}

impl Row {
    pub fn set(&mut self, key: &'static str, value: impl Into<String>) {
        self.cells.insert(key, value.into());
    }
    /// Empty or missing cells print as "-", like `row.get(key) or "-"`.
    pub fn get(&self, key: &str) -> &str {
        match self.cells.get(key) {
            Some(v) if !v.is_empty() => v,
            _ => "-",
        }
    }
}

fn len(s: &str) -> usize {
    s.chars().count()
}

pub fn short(value: &str, width: usize) -> String {
    let value = if value.is_empty() { "-" } else { value };
    if width == 0 {
        return String::new();
    }
    if len(value) <= width {
        return value.to_string();
    }
    if width <= 1 {
        return value.chars().take(width).collect();
    }
    let head: String = value.chars().take(width - 1).collect();
    format!("{head}…")
}

pub fn middle_short(value: &str, width: usize) -> String {
    let value = if value.is_empty() { "-" } else { value };
    if width == 0 {
        return String::new();
    }
    let n = len(value);
    if n <= width {
        return value.to_string();
    }
    if width <= 3 {
        return value.chars().take(width).collect();
    }
    let left = (width - 1) / 2;
    let right = width - 1 - left;
    let head: String = value.chars().take(left).collect();
    let tail: String = value.chars().skip(n - right).collect();
    format!("{head}…{tail}")
}

/// Pick the visible columns (indices into `cols`) and a width per column index.
pub fn fit_widths(cols: &[Col], rows: &[Row], width: usize) -> (Vec<usize>, Vec<usize>) {
    let natural: Vec<usize> = cols
        .iter()
        .map(|c| rows.iter().map(|r| len(r.get(c.key))).chain([len(c.header)]).max().unwrap())
        .collect();
    let floor: Vec<usize> = natural.iter().zip(cols).map(|(n, c)| (*n).min(c.min)).collect();
    let mut active: Vec<usize> = (0..cols.len()).collect();

    let total = |active: &[usize], w: &[usize]| -> isize {
        (active.iter().map(|&i| w[i]).sum::<usize>() + active.len()) as isize - 1
    };
    let shrink = |targets: &[usize], widths: &mut [usize], active: &[usize], widest_first: bool| {
        let mut excess = total(active, widths) - width as isize;
        while excess > 0 {
            let open: Vec<usize> = targets.iter().copied().filter(|&i| widths[i] > floor[i]).collect();
            if open.is_empty() {
                return;
            }
            let mut pick = open[0];
            if widest_first {
                for &i in &open {
                    if widths[i] > widths[pick] {
                        pick = i; // first maximum wins, like Python's max()
                    }
                }
            }
            widths[pick] -= 1;
            excess -= 1;
        }
    };

    let mut widths;
    loop {
        widths = (0..cols.len()).map(|i| if cols[i].flex { floor[i] } else { natural[i] }).collect::<Vec<_>>();
        let mut optional: Vec<usize> = active.iter().copied().filter(|&i| cols[i].drop.is_some()).collect();
        optional.sort_by_key(|&i| std::cmp::Reverse(cols[i].drop));
        shrink(&optional, &mut widths, &active, false);
        if total(&active, &widths) <= width as isize {
            break;
        }
        if let Some(&first) = optional.first() {
            active.retain(|&i| i != first);
            continue;
        }
        let fixed: Vec<usize> = active.iter().copied().filter(|&i| !cols[i].flex).collect();
        shrink(&fixed, &mut widths, &active, true);
        break;
    }

    // Hand leftover space to free text first, then back to shrunk optional columns.
    let mut spare = width as isize - total(&active, &widths);
    let mut regrow: Vec<usize> = active.iter().copied().filter(|&i| cols[i].flex).collect();
    let mut optional: Vec<usize> = active.iter().copied().filter(|&i| cols[i].drop.is_some()).collect();
    optional.sort_by_key(|&i| cols[i].drop);
    regrow.extend(optional);
    for i in regrow {
        if spare <= 0 {
            break;
        }
        let grow = spare.min(natural[i] as isize - widths[i] as isize);
        widths[i] = (widths[i] as isize + grow) as usize;
        spare -= grow;
    }
    (active, widths)
}

pub struct Painter {
    pub enabled: bool,
}

impl Painter {
    pub fn paint(&self, value: &str, color: &str) -> String {
        if self.enabled { format!("{color}{value}{RESET}") } else { value.to_string() }
    }
}

fn pad(value: &str, w: usize, right: bool) -> String {
    let fill = " ".repeat(w.saturating_sub(len(value)));
    if right { format!("{fill}{value}") } else { format!("{value}{fill}") }
}

pub fn render_columns(out: &mut String, p: &Painter, cols: &[Col], rows: &[Row], width: usize) {
    let (active, widths) = fit_widths(cols, rows, width);
    let cell = |i: usize, value: &str| {
        let (c, w) = (&cols[i], widths[i]);
        let text = if c.mid { middle_short(value, w) } else { short(value, w) };
        pad(&text, w, c.right)
    };

    let header = active
        .iter()
        .map(|&i| {
            if cols[i].header.is_empty() { " ".repeat(widths[i]) } else { cell(i, cols[i].header) }
        })
        .collect::<Vec<_>>()
        .join(" ");
    let header = header.trim_end();
    out.push_str(&p.paint(header, DIM));
    out.push('\n');
    out.push_str(&p.paint(&"-".repeat(len(header)), DIM));
    out.push('\n');
    for row in rows {
        let cells = active.iter().map(|&i| (i, cell(i, row.get(cols[i].key))));
        let line = if row.dim {
            p.paint(cells.map(|(_, v)| v).collect::<Vec<_>>().join(" ").trim_end(), DIM)
        } else {
            cells
                .map(|(i, v)| if cols[i].color.is_empty() { v } else { p.paint(&v, cols[i].color) })
                .collect::<Vec<_>>()
                .join(" ")
                .trim_end()
                .to_string()
        };
        out.push_str(&line);
        out.push('\n');
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(pairs: &[(&'static str, &str)]) -> Row {
        let mut r = Row::default();
        for (k, v) in pairs {
            r.set(k, *v);
        }
        r
    }

    #[test]
    fn truncation_matches_python() {
        assert_eq!(short("abcdef", 4), "abc…");
        assert_eq!(short("", 4), "-");
        assert_eq!(short("abc", 1), "a");
        assert_eq!(middle_short("0123456789", 6), "01…789");
        assert_eq!(middle_short("0123456789", 3), "012");
    }

    #[test]
    fn drops_optional_columns_when_narrow() {
        let cols = vec![
            Col::new("a", "AAAA", "", 4),
            Col::new("b", "BBBB", "", 4).drop(1),
        ];
        let rows = vec![row(&[("a", "x"), ("b", "y")])];
        let (active, _) = fit_widths(&cols, &rows, 5);
        assert_eq!(active, vec![0]);
        let (active, _) = fit_widths(&cols, &rows, 100);
        assert_eq!(active, vec![0, 1]);
    }

    #[test]
    fn flex_column_takes_leftover() {
        let cols = vec![Col::new("a", "A", "", 1), Col::new("b", "NOTE", "", 4).flex()];
        let rows = vec![row(&[("a", "x"), ("b", "a long note here")])];
        let (_, widths) = fit_widths(&cols, &rows, 12);
        assert_eq!(widths, vec![1, 10]);
    }

    #[test]
    fn rows_render_trimmed_with_dash_separator() {
        let cols = vec![Col::new("a", "PORT", "", 4), Col::new("b", "NOTE", "", 4)];
        let rows = vec![row(&[("a", "COM1")])];
        let mut out = String::new();
        render_columns(&mut out, &Painter { enabled: false }, &cols, &rows, 100);
        assert_eq!(out, "PORT NOTE\n---------\nCOM1 -\n");
    }
}
