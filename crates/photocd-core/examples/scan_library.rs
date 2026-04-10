//! Walk a Photo CD library directory and try to open every disc.
//!
//! Usage: cargo run -p photocd-core --example scan_library -- <library_dir>

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use photocd_core::disc::{open_disc, read_image_pack};
use photocd_core::hires::{read_ipa_byte, resolution_order};

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let lib = match args.next() {
        Some(s) => PathBuf::from(s),
        None => {
            eprintln!("usage: scan_library <library_dir>");
            return ExitCode::FAILURE;
        }
    };

    if !lib.is_dir() {
        eprintln!("Not a directory: {}", lib.display());
        return ExitCode::FAILURE;
    }

    let mut discs: Vec<(String, PathBuf)> = Vec::new();
    for sub in read_dir_sorted(&lib) {
        if !sub.is_dir() {
            continue;
        }
        let Some(cue) = find_first_cue(&sub) else {
            continue;
        };
        let name = sub
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        discs.push((name, cue));
    }

    println!("Scanning {} discs...\n", discs.len());

    let mut failures: Vec<(String, String)> = Vec::new();
    let mut empty: Vec<String> = Vec::new();
    let mut ok = 0usize;
    // (name, ipa, res_order, size_bytes)
    let mut rows: Vec<(String, u8, u8, u32)> = Vec::new();

    for (name, cue) in &discs {
        match open_disc(cue) {
            Ok(mut d) => {
                if d.images.is_empty() {
                    empty.push(name.clone());
                    continue;
                }
                ok += 1;
                if let Some(first) = d.images.first().cloned() {
                    if first.rgb_variants.is_some() {
                        rows.push((format!("{} [USA]", name), 0, 2, first.size));
                        continue;
                    }
                    if let Ok(buf) = read_image_pack(&mut *d.reader, &first, 2) {
                        let ipa = read_ipa_byte(&buf);
                        if ipa == 0 {
                            println!("  [no PCD_IPI magic] {}", name);
                        } else {
                            let res = resolution_order(ipa);
                            rows.push((name.clone(), ipa, res, first.size));
                        }
                    }
                }
            }
            Err(e) => {
                failures.push((name.clone(), format!("{e}")));
            }
        }
    }

    // Sort by first-image-pack size descending — biggest packs are the
    // most likely to actually carry 16Base data.
    rows.sort_by(|a, b| b.3.cmp(&a.3));
    println!("\n-- First image pack size (descending) --");
    println!("  {:>10}  {:>4}  {:>3}  {}", "size", "ipa", "res", "disc");
    for (name, ipa, res, size) in &rows {
        println!("  {:>10}  0x{:02x}  {:>3}  {}", size, ipa, res, name);
    }

    println!("\nOK: {}", ok);
    println!("Empty (opened, 0 images): {}", empty.len());
    println!("Failed: {}", failures.len());

    if !empty.is_empty() {
        println!("\n-- Empty discs --");
        for n in &empty {
            println!("  {}", n);
        }
    }

    if !failures.is_empty() {
        println!("\n-- Failed discs --");
        for (n, e) in &failures {
            println!("  {}\n    {}", n, e);
        }
    }

    if failures.is_empty() && empty.is_empty() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

fn read_dir_sorted(dir: &Path) -> Vec<PathBuf> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut out: Vec<PathBuf> = rd.filter_map(|e| e.ok().map(|e| e.path())).collect();
    out.sort_by(|a, b| {
        a.file_name()
            .map(|s| s.to_ascii_lowercase())
            .cmp(&b.file_name().map(|s| s.to_ascii_lowercase()))
    });
    out
}

fn find_first_cue(dir: &Path) -> Option<PathBuf> {
    read_dir_sorted(dir)
        .into_iter()
        .find(|p| p.extension().and_then(|e| e.to_str()).map(|s| s.eq_ignore_ascii_case("cue")) == Some(true))
}
