// Archive eligibility checker for ZIP and 7z disc images.
//
// Only "store" (uncompressed) archives are eligible — compressed archives
// cannot be sector-read without fully decompressing first, which defeats the
// purpose of keeping disc data accessible on demand.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

pub struct ExtractedDisc {
    pub cue_path: PathBuf,
    /// Caller must delete this when the disc is unloaded.
    pub temp_dir: PathBuf,
}

pub enum ArchiveResult {
    Eligible(ExtractedDisc),
    /// Not eligible — the reason string is shown to the user.
    NotEligible(String),
}

// ---------------------------------------------------------------------------
// Fast eligibility probe (metadata only, no extraction)
// ---------------------------------------------------------------------------

/// Returns true only if the archive contains a .cue file and every entry
/// uses store/copy compression. Intended for library scanning — fast,
/// reads central directory only.
pub fn is_eligible(path: &Path) -> bool {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_default();
    match ext.as_str() {
        "zip" => is_eligible_zip(path),
        "7z"  => is_eligible_7z(path),
        _     => false,
    }
}

fn is_eligible_zip(path: &Path) -> bool {
    let f = match fs::File::open(path) { Ok(f) => f, Err(_) => return false };
    let mut zip = match zip::ZipArchive::new(f) { Ok(z) => z, Err(_) => return false };
    let mut has_cue = false;
    for i in 0..zip.len() {
        // by_index_raw reads metadata without decompressing
        let entry = match zip.by_index_raw(i) { Ok(e) => e, Err(_) => return false };
        if entry.is_dir() { continue; }
        let lower = entry.name().to_lowercase();
        if lower.ends_with(".cue") { has_cue = true; }
        if entry.compression() != zip::CompressionMethod::Stored { return false; }
    }
    has_cue
}

fn is_eligible_7z(path: &Path) -> bool {
    let reader = match sevenz_rust::SevenZReader::open(path, sevenz_rust::Password::empty()) {
        Ok(r) => r,
        Err(_) => return false,
    };
    let archive = reader.archive();
    let has_cue = archive.files.iter().any(|e| {
        !e.is_directory() && e.name().to_lowercase().ends_with(".cue")
    });
    if !has_cue { return false; }
    archive.folders.iter().all(|folder| {
        folder.coders.iter().all(|c| c.decompression_method_id() == SEVENZ_COPY_ID)
    })
}

// ---------------------------------------------------------------------------
// ZIP
// ---------------------------------------------------------------------------

pub fn check_zip(archive_path: &Path) -> ArchiveResult {
    let f = match fs::File::open(archive_path) {
        Ok(f) => f,
        Err(e) => return ArchiveResult::NotEligible(format!("Cannot open ZIP: {e}")),
    };
    let mut zip = match zip::ZipArchive::new(f) {
        Ok(z) => z,
        Err(e) => return ArchiveResult::NotEligible(format!("Not a valid ZIP: {e}")),
    };

    let mut cue_names: Vec<String> = Vec::new();
    let mut compressed_names: Vec<String> = Vec::new();

    for i in 0..zip.len() {
        let entry = match zip.by_index(i) {
            Ok(e) => e,
            Err(_) => continue,
        };
        if entry.is_dir() {
            continue;
        }
        let lower = entry.name().to_lowercase();
        if lower.ends_with(".cue") {
            cue_names.push(entry.name().to_owned());
        }
        if entry.compression() != zip::CompressionMethod::Stored {
            compressed_names.push(entry.name().to_owned());
        }
    }

    if cue_names.is_empty() {
        return ArchiveResult::NotEligible(
            "No .cue file found — not a disc image archive.".into(),
        );
    }
    if !compressed_names.is_empty() {
        return ArchiveResult::NotEligible(format!(
            "Archive contains compressed files (not Store mode). \
             Re-pack with Store/no-compression to use with this app.\n\
             Compressed: {}",
            compressed_names.join(", ")
        ));
    }

    extract_zip(archive_path, &mut zip, &cue_names[0])
}

fn extract_zip(
    archive_path: &Path,
    zip: &mut zip::ZipArchive<fs::File>,
    cue_name: &str,
) -> ArchiveResult {
    let temp_dir = make_temp_dir(archive_path);
    if let Err(e) = fs::create_dir_all(&temp_dir) {
        return ArchiveResult::NotEligible(format!("Cannot create temp dir: {e}"));
    }
    for i in 0..zip.len() {
        let mut entry = match zip.by_index(i) {
            Ok(e) => e,
            Err(_) => continue,
        };
        if entry.is_dir() {
            continue;
        }
        let file_name = flatten_name(entry.name());
        let dest = temp_dir.join(&file_name);
        let mut data = Vec::with_capacity(entry.size() as usize);
        if let Err(e) = entry.read_to_end(&mut data) {
            let _ = fs::remove_dir_all(&temp_dir);
            return ArchiveResult::NotEligible(format!("Read error on {file_name}: {e}"));
        }
        if let Err(e) = fs::write(&dest, &data) {
            let _ = fs::remove_dir_all(&temp_dir);
            return ArchiveResult::NotEligible(format!("Write error on {file_name}: {e}"));
        }
    }
    let cue_path = temp_dir.join(flatten_name(cue_name));
    ArchiveResult::Eligible(ExtractedDisc { cue_path, temp_dir })
}

// ---------------------------------------------------------------------------
// 7z
// ---------------------------------------------------------------------------

// The COPY (store) method id in 7z is a single byte 0x00.
const SEVENZ_COPY_ID: &[u8] = &[0x00];

pub fn check_7z(archive_path: &Path) -> ArchiveResult {
    let reader = match sevenz_rust::SevenZReader::open(archive_path, sevenz_rust::Password::empty()) {
        Ok(r) => r,
        Err(e) => return ArchiveResult::NotEligible(format!("Cannot open 7z: {e}")),
    };

    let archive = reader.archive();

    // Check that every compression folder uses only the COPY coder.
    let mut compressed_folders: Vec<usize> = Vec::new();
    for (i, folder) in archive.folders.iter().enumerate() {
        let all_copy = folder
            .coders
            .iter()
            .all(|c| c.decompression_method_id() == SEVENZ_COPY_ID);
        if !all_copy {
            compressed_folders.push(i);
        }
    }

    let mut cue_names: Vec<String> = Vec::new();
    for entry in &archive.files {
        if !entry.is_directory() {
            let lower = entry.name().to_lowercase();
            if lower.ends_with(".cue") {
                cue_names.push(entry.name().to_owned());
            }
        }
    }

    if cue_names.is_empty() {
        return ArchiveResult::NotEligible(
            "No .cue file found — not a disc image archive.".into(),
        );
    }
    if !compressed_folders.is_empty() {
        return ArchiveResult::NotEligible(
            "Archive contains compressed data (not Copy/Store mode). \
             Re-pack with Store method to use with this app."
                .into(),
        );
    }

    extract_7z(archive_path)
}

fn extract_7z(archive_path: &Path) -> ArchiveResult {
    let temp_dir = make_temp_dir(archive_path);
    if let Err(e) = fs::create_dir_all(&temp_dir) {
        return ArchiveResult::NotEligible(format!("Cannot create temp dir: {e}"));
    }

    let dest = temp_dir.clone();
    let result = sevenz_rust::decompress_file_with_extract_fn(
        archive_path,
        &dest,
        |entry, reader, _dest_path| {
            if entry.is_directory() {
                return Ok(true);
            }
            let file_name = flatten_name(entry.name());
            let out_path = dest.join(&file_name);
            let mut data = Vec::new();
            reader.read_to_end(&mut data)?;
            fs::write(&out_path, &data)?;
            Ok(true)
        },
    );

    if let Err(e) = result {
        let _ = fs::remove_dir_all(&temp_dir);
        return ArchiveResult::NotEligible(format!("Extraction failed: {e}"));
    }

    // Find the .cue in the extracted files.
    let cue_path = match fs::read_dir(&temp_dir)
        .ok()
        .and_then(|rd| {
            rd.flatten()
                .find(|e| {
                    e.path()
                        .extension()
                        .and_then(|x| x.to_str())
                        .map(|s| s.eq_ignore_ascii_case("cue"))
                        == Some(true)
                })
                .map(|e| e.path())
        }) {
        Some(p) => p,
        None => {
            let _ = fs::remove_dir_all(&temp_dir);
            return ArchiveResult::NotEligible("Extracted archive has no .cue file.".into());
        }
    };

    ArchiveResult::Eligible(ExtractedDisc { cue_path, temp_dir })
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn make_temp_dir(archive_path: &Path) -> PathBuf {
    let stem = archive_path
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "disc".into());
    std::env::temp_dir().join(format!("photocd_{stem}"))
}

fn flatten_name(name: &str) -> String {
    Path::new(name)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| name.replace(['/', '\\'], "_"))
}
