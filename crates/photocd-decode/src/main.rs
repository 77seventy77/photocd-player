//! CLI: decode a Photo CD image to PNG.
//!
//! Input modes:
//!   * `--raw <file>`  treat `<file>` as 589,824 bytes of raw Base data.
//!   * `--pack <file> --res {base,4base,16base}`  treat `<file>` as a full
//!     image pack (from sector 0 onward) and decode at the requested tier.
//!   * `<file>.pcd`  treat `<file>` as a .pcd image pack (Base only).
//!
//! Usage:
//!   photocd-decode <file.pcd> <out.png>
//!   photocd-decode --raw <raw.bin> <out.png>
//!   photocd-decode --pack <pack.bin> --res base <out.png>
//!   photocd-decode --pack <pack.bin> --res 4base <out.png>
//!   photocd-decode --pack <pack.bin> --res 16base <out.png>

use std::env;
use std::fs;
use std::io::BufWriter;
use std::path::Path;
use std::process::ExitCode;

use photocd_core::base::{decode_base_plane, BASE_H, BASE_RAW_LEN, BASE_W};
use photocd_core::disc::{open_disc, read_image_pack};
use photocd_core::hires::{
    decode_16base, decode_4base, FOURBASE_H, FOURBASE_W, SIXTEENBASE_H, SIXTEENBASE_W,
};

const SECTOR: usize = 2048;
const BASE_SECTOR_OFFSET: usize = 96;

fn usage() {
    eprintln!(
        "usage:\n  \
         photocd-decode <file.pcd> <out.png>\n  \
         photocd-decode --raw <raw.bin> <out.png>\n  \
         photocd-decode --pack <pack.bin> --res {{base,4base,16base}} <out.png>\n  \
         photocd-decode --cue <disc.cue> --list\n  \
         photocd-decode --cue <disc.cue> --image <N> --res {{base,4base,16base}} <out.png>"
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();

    // Pack mode.
    if args.len() == 6 && args[1] == "--pack" && args[3] == "--res" {
        let pack_path = &args[2];
        let res = args[4].as_str();
        let output = &args[5];
        return run_pack(pack_path, res, output);
    }

    // Cue list mode.
    if args.len() == 4 && args[1] == "--cue" && args[3] == "--list" {
        return run_cue_list(&args[2]);
    }

    // Cue decode mode: --cue X --image N --res R out.png
    if args.len() == 8
        && args[1] == "--cue"
        && args[3] == "--image"
        && args[5] == "--res"
    {
        let cue_path = &args[2];
        let image_num: usize = match args[4].parse() {
            Ok(n) => n,
            Err(_) => {
                eprintln!("invalid --image number: {}", args[4]);
                return ExitCode::from(2);
            }
        };
        let res = args[6].as_str();
        let output = &args[7];
        return run_cue_decode(cue_path, image_num, res, output);
    }

    let (raw_mode, input, output) = match args.as_slice() {
        [_, flag, inp, out] if flag == "--raw" => (true, inp.clone(), out.clone()),
        [_, inp, out] => (false, inp.clone(), out.clone()),
        _ => {
            usage();
            return ExitCode::from(2);
        }
    };

    let bytes = match fs::read(&input) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("read {input}: {e}");
            return ExitCode::FAILURE;
        }
    };

    let raw_slice: &[u8] = if raw_mode {
        &bytes
    } else {
        let offset = BASE_SECTOR_OFFSET * SECTOR;
        let end = offset + BASE_RAW_LEN;
        if bytes.len() < end {
            eprintln!(
                "{input}: file too short ({} bytes, need >= {})",
                bytes.len(),
                end
            );
            return ExitCode::FAILURE;
        }
        &bytes[offset..end]
    };

    let rgb = match decode_base_plane(raw_slice) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("decode: {e}");
            return ExitCode::FAILURE;
        }
    };

    if let Err(e) = write_png(Path::new(&output), &rgb, BASE_W as u32, BASE_H as u32) {
        eprintln!("write {output}: {e}");
        return ExitCode::FAILURE;
    }

    println!("wrote {output} ({}x{})", BASE_W, BASE_H);
    ExitCode::SUCCESS
}

fn run_pack(pack_path: &str, res: &str, output: &str) -> ExitCode {
    let pack = match fs::read(pack_path) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("read {pack_path}: {e}");
            return ExitCode::FAILURE;
        }
    };

    // All tiers need the Base decode first.
    let base_off = BASE_SECTOR_OFFSET * SECTOR;
    let base_end = base_off + BASE_RAW_LEN;
    if pack.len() < base_end {
        eprintln!("{pack_path}: too short for Base ({} < {})", pack.len(), base_end);
        return ExitCode::FAILURE;
    }
    let base_rgb = match decode_base_plane(&pack[base_off..base_end]) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("base decode: {e}");
            return ExitCode::FAILURE;
        }
    };

    let (rgb, w, h) = match res {
        "base" => (base_rgb, BASE_W as u32, BASE_H as u32),
        "4base" => {
            let rgb = match decode_4base(&pack, &base_rgb) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("4base decode: {e}");
                    return ExitCode::FAILURE;
                }
            };
            (rgb, FOURBASE_W as u32, FOURBASE_H as u32)
        }
        "16base" => {
            let fb = match decode_4base(&pack, &base_rgb) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("4base decode: {e}");
                    return ExitCode::FAILURE;
                }
            };
            let sb = match decode_16base(&pack, &fb) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("16base decode: {e}");
                    return ExitCode::FAILURE;
                }
            };
            (sb, SIXTEENBASE_W as u32, SIXTEENBASE_H as u32)
        }
        other => {
            eprintln!("unknown --res {other}");
            usage();
            return ExitCode::from(2);
        }
    };

    if let Err(e) = write_png(Path::new(output), &rgb, w, h) {
        eprintln!("write {output}: {e}");
        return ExitCode::FAILURE;
    }
    println!("wrote {output} ({}x{})", w, h);
    ExitCode::SUCCESS
}

fn run_cue_list(cue_path: &str) -> ExitCode {
    let disc = match open_disc(Path::new(cue_path)) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("open {cue_path}: {e}");
            return ExitCode::FAILURE;
        }
    };
    println!("volume:     {}", disc.pvd.volume_id);
    println!("disc_id:    {}", disc.info.disc_id);
    println!("spec:       {}", disc.info.spec_version);
    println!("serial:     {}", disc.info.serial);
    println!("sessions:   {}", disc.info.n_sessions);
    println!(
        "images:     {} (INFO.PCD) / {} (directory)",
        disc.info.n_images,
        disc.images.len()
    );
    println!(
        "res:        highest={} lowest={}",
        disc.info.res_highest, disc.info.res_lowest
    );
    println!(
        "writer:     {} / {}",
        disc.info.writer_vendor, disc.info.writer_product
    );
    println!("audio:      {} track(s)", disc.audio_tracks.len());
    println!("playlist:   {} play sequence(s)", disc.play_sequences.len());
    for (i, img) in disc.images.iter().enumerate() {
        println!(
            "  {:>4}. {:<14}  {:>8.1} KB  (LBA {})",
            i + 1,
            img.name,
            img.size as f64 / 1024.0,
            img.lba
        );
    }
    ExitCode::SUCCESS
}

fn run_cue_decode(cue_path: &str, image_num: usize, res: &str, output: &str) -> ExitCode {
    let mut disc = match open_disc(Path::new(cue_path)) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("open {cue_path}: {e}");
            return ExitCode::FAILURE;
        }
    };
    if image_num < 1 || image_num > disc.images.len() {
        eprintln!(
            "image {image_num} out of range (have {} images)",
            disc.images.len()
        );
        return ExitCode::FAILURE;
    }
    let img = disc.images[image_num - 1].clone();

    // 3000 sectors = 6 MB, plenty for a full pack including 16Base.
    let pack = match read_image_pack(&mut *disc.reader, &img, 3000) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("read pack: {e}");
            return ExitCode::FAILURE;
        }
    };

    let base_off = BASE_SECTOR_OFFSET * SECTOR;
    if pack.len() < base_off + BASE_RAW_LEN {
        eprintln!("pack too short for Base");
        return ExitCode::FAILURE;
    }
    let base_rgb = match decode_base_plane(&pack[base_off..base_off + BASE_RAW_LEN]) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("base decode: {e}");
            return ExitCode::FAILURE;
        }
    };

    let (rgb, w, h) = match res {
        "base" => (base_rgb, BASE_W as u32, BASE_H as u32),
        "4base" => match decode_4base(&pack, &base_rgb) {
            Ok(v) => (v, FOURBASE_W as u32, FOURBASE_H as u32),
            Err(e) => {
                eprintln!("4base decode: {e}");
                return ExitCode::FAILURE;
            }
        },
        "16base" => {
            let fb = match decode_4base(&pack, &base_rgb) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("4base decode: {e}");
                    return ExitCode::FAILURE;
                }
            };
            match decode_16base(&pack, &fb) {
                Ok(v) => (v, SIXTEENBASE_W as u32, SIXTEENBASE_H as u32),
                Err(e) => {
                    eprintln!("16base decode: {e}");
                    return ExitCode::FAILURE;
                }
            }
        }
        other => {
            eprintln!("unknown --res {other}");
            usage();
            return ExitCode::from(2);
        }
    };

    if let Err(e) = write_png(Path::new(output), &rgb, w, h) {
        eprintln!("write {output}: {e}");
        return ExitCode::FAILURE;
    }
    println!("wrote {output} ({w}x{h}) from {}", img.name);
    ExitCode::SUCCESS
}

fn write_png(path: &Path, rgb: &[u8], w: u32, h: u32) -> std::io::Result<()> {
    let file = fs::File::create(path)?;
    let bw = BufWriter::new(file);
    let mut enc = png::Encoder::new(bw, w, h);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    let mut writer = enc
        .write_header()
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
    writer
        .write_image_data(rgb)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
    Ok(())
}
