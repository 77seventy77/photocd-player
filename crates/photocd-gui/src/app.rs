//! PhotoCdApp: library browser + single-image viewer.
//!
//! UI modelled after the original Python/Tkinter Photo CD Player:
//!   - Dark theme (#1a1a1a bg, #212121 toolbar, #000000 canvas)
//!   - Library list centered in main area when no disc is loaded
//!   - Bottom toolbar with info row + button row
//!   - No side panels; no top menu bar
//!
//! Threading model:
//!   A single decode worker thread owns the `OpenedDisc` (non-Send through
//!   its Box<dyn SectorReader>), receives `DecodeRequest` messages, decodes
//!   to RGB, and sends `DecodeResult` back.

use std::path::PathBuf;
use std::sync::mpsc;
use std::thread::{self, JoinHandle};

use eframe::egui;
use egui::{
    Align, Align2, Color32, ColorImage, FontId, Layout, Margin, Pos2, Rect, RichText, Rounding,
    Stroke, TextureHandle, TextureOptions, Vec2,
};

use photocd_core::base::{decode_base_plane, BASE_H, BASE_RAW_LEN, BASE_W};
use photocd_core::cue::Track;
use photocd_core::disc::{
    open_disc, read_image_pack, read_raw_rgb_variant, DiscInfo, ImageEntry, OpenedDisc,
};
use photocd_core::hires::{
    decode_16base, decode_4base, read_ipa_byte, resolution_order, FOURBASE_H, FOURBASE_W,
    SIXTEENBASE_H, SIXTEENBASE_W,
};
use photocd_core::playlist::{self, PlaySequence};

use crate::audio::{AudioPlayer, AudioTrackInfo};

// ---------------------------------------------------------------------------
// Version
// ---------------------------------------------------------------------------

pub const APP_VERSION: &str = "v1.0.4";
pub const APP_NAME: &str = "Photo CD Player";

const LOGO_PNG: &[u8] = include_bytes!("../../../icons/photo_cd_player_-_logo_v3.png");

// ---------------------------------------------------------------------------
// Theme colours (matching Python build)
// ---------------------------------------------------------------------------

struct Theme;
impl Theme {
    const BG: Color32 = Color32::from_rgb(0x12, 0x0D, 0x09);
    const SIDEBAR: Color32 = Color32::from_rgb(0x14, 0x14, 0x14);
    const TOOLBAR: Color32 = Color32::from_rgb(0x21, 0x21, 0x21);
    const CANVAS: Color32 = Color32::from_rgb(0x00, 0x00, 0x00);
    const FG: Color32 = Color32::from_rgb(0xCC, 0xB0, 0x9C);
    const FG_DIM: Color32 = Color32::from_rgb(0xCC, 0xB0, 0x9C);
    const BTN: Color32 = Color32::from_rgb(0xC8, 0xB8, 0xAE);
    const BTN_HOVER: Color32 = Color32::from_rgb(0xD8, 0xCA, 0xC2);
    const BTN_FG: Color32 = Color32::from_rgb(0x12, 0x0D, 0x09);
    const BTN_STROKE: Color32 = Color32::from_rgb(0x8A, 0x75, 0x65);
    const BTN_SUBTLE: Color32 = Color32::from_rgb(0x3A, 0x2D, 0x25);
    const BTN_SUBTLE_HOVER: Color32 = Color32::from_rgb(0x4A, 0x39, 0x2F);
    const BTN_SUBTLE_FG: Color32 = Color32::from_rgb(0xE8, 0xD5, 0xC7);
    const BTN_SUBTLE_STROKE: Color32 = Color32::from_rgb(0x63, 0x4C, 0x3E);
    const SEP: Color32 = Color32::from_rgb(0x38, 0x38, 0x38);
    const SELECT_BG: Color32 = Color32::from_rgb(0x50, 0x38, 0x22);
    const SELECT_FG: Color32 = Color32::from_rgb(0xCC, 0xB0, 0x9C);

    fn btn_shadow() -> Color32 {
        Color32::from_rgba_unmultiplied(0x00, 0x00, 0x00, 0x48)
    }
}

// ---------------------------------------------------------------------------
// Config persistence (~/.config/photocd/config.json)
// ---------------------------------------------------------------------------

#[derive(Debug, Default, serde::Serialize, serde::Deserialize)]
struct Config {
    #[serde(default)]
    library_dir: Option<String>,
    #[serde(default)]
    save_dir: Option<String>,
}

fn config_path() -> PathBuf {
    if let Some(home) = dirs::home_dir() {
        home.join(".config").join("photocd").join("config.json")
    } else {
        PathBuf::from(".photocd_config.json")
    }
}

fn load_config() -> Config {
    let path = config_path();
    if let Ok(data) = std::fs::read_to_string(&path) {
        serde_json::from_str(&data).unwrap_or_default()
    } else {
        Config::default()
    }
}

fn save_config(cfg: &Config) {
    let path = config_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(json) = serde_json::to_string_pretty(cfg) {
        let _ = std::fs::write(&path, json);
    }
}

// ---------------------------------------------------------------------------
// Resolution tiers
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Base,
    FourBase,
    SixteenBase,
}

impl Tier {
    fn label(self) -> &'static str {
        match self {
            Tier::Base => "Base",
            Tier::FourBase => "4Base",
            Tier::SixteenBase => "16Base",
        }
    }

    fn dims(self) -> (u32, u32) {
        match self {
            Tier::Base => (BASE_W as u32, BASE_H as u32),
            Tier::FourBase => (FOURBASE_W as u32, FOURBASE_H as u32),
            Tier::SixteenBase => (SIXTEENBASE_W as u32, SIXTEENBASE_H as u32),
        }
    }

    fn label_with_dims(self) -> String {
        let (w, h) = self.dims();
        format!("{} ({}x{})", self.label(), w, h)
    }
}

const ALL_TIERS: [Tier; 3] = [Tier::Base, Tier::FourBase, Tier::SixteenBase];

// ---------------------------------------------------------------------------
// Library entry
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct LibraryEntry {
    pub cue_path: PathBuf,
    pub display_name: String,
}

// ---------------------------------------------------------------------------
// View mode
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum View {
    Library,
    Image,
}

// ---------------------------------------------------------------------------
// Worker messages
// ---------------------------------------------------------------------------

enum WorkerMsg {
    OpenDisc(PathBuf),
    Decode {
        image_idx: usize,
        tier: Tier,
        rotation: u8, // 0=0, 1=90CCW, 2=180, 3=270CCW
    },
}

enum WorkerResult {
    DiscOpened {
        cue_path: PathBuf,
        disc_name: Option<String>,
        images: Vec<ImageEntry>,
        audio_tracks: Vec<Track>,
        disc_info: DiscInfo,
        play_sequences: Vec<PlaySequence>,
        /// Ground-truth max tier for this disc (0=Base, 1=4Base, 2=16Base),
        /// derived from the first image pack's IPA byte or Kodak USA variants.
        max_tier: u8,
    },
    DiscError(String),
    ImageDecoded {
        image_idx: usize,
        tier: Tier,
        width: u32,
        height: u32,
        rgb: Vec<u8>,
    },
    DecodeError(String),
}

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

pub struct PhotoCdApp {
    config: Config,
    view: View,

    // Library
    library: Vec<LibraryEntry>,
    // Disc state
    loaded_cue: Option<PathBuf>,
    disc_title: String,
    images: Vec<ImageEntry>,
    current_idx: usize,
    tier: Tier,
    /// Highest tier the current disc actually encodes (0=Base).
    max_tier: Tier,

    // Disc metadata
    disc_info: DiscInfo,
    /// Per-image display time in seconds, keyed by 1-based image number.
    image_timings: Vec<(u16, f32)>,

    // Decoded texture
    current_texture: Option<TextureHandle>,
    current_texture_size: (u32, u32),
    current_rgb: Option<Vec<u8>>,
    /// Tier that produced `current_texture` (may lag `tier` while decoding).
    current_display_tier: Tier,
    decoding: bool,

    // Slideshow
    slideshow_on: bool,
    slideshow_deadline: Option<f64>,

    // Audio
    audio: AudioPlayer,
    volume: f32,

    // Status bar text
    status: String,

    // Fullscreen
    fullscreen: bool,

    // Cached width of the bottom toolbar button row (for centering).
    button_row_width: f32,

    // Temp dir extracted from an archive, cleaned up on disc close.
    temp_disc_dir: Option<std::path::PathBuf>,

    // Cached library logo texture
    logo_texture: Option<TextureHandle>,

    // Worker
    worker_tx: mpsc::Sender<WorkerMsg>,
    worker_rx: mpsc::Receiver<WorkerResult>,
    _worker: Option<JoinHandle<()>>,
}

impl Default for PhotoCdApp {
    fn default() -> Self {
        let (worker_tx, worker_rx, worker) = spawn_worker();
        let config = load_config();

        let mut app = Self {
            config,
            view: View::Library,
            library: Vec::new(),
            loaded_cue: None,
            disc_title: String::new(),
            images: Vec::new(),
            current_idx: 0,
            tier: Tier::Base,
            max_tier: Tier::Base,
            current_texture: None,
            current_texture_size: (0, 0),
            current_display_tier: Tier::Base,
            disc_info: DiscInfo::default(),
            image_timings: Vec::new(),
            current_rgb: None,
            decoding: false,
            slideshow_on: false,
            slideshow_deadline: None,
            audio: AudioPlayer::new(),
            volume: 100.0,
            status: "Click a Library title or open a file to load a disc.".into(),
            fullscreen: false,
            button_row_width: 0.0,
            temp_disc_dir: None,
            logo_texture: None,
            worker_tx,
            worker_rx,
            _worker: Some(worker),
        };
        app.scan_library();
        app
    }
}

// ---------------------------------------------------------------------------
// Worker thread
// ---------------------------------------------------------------------------

fn spawn_worker() -> (
    mpsc::Sender<WorkerMsg>,
    mpsc::Receiver<WorkerResult>,
    JoinHandle<()>,
) {
    let (to_tx, to_rx) = mpsc::channel::<WorkerMsg>();
    let (from_tx, from_rx) = mpsc::channel::<WorkerResult>();
    let handle = thread::spawn(move || worker_main(to_rx, from_tx));
    (to_tx, from_rx, handle)
}

fn worker_main(rx: mpsc::Receiver<WorkerMsg>, tx: mpsc::Sender<WorkerResult>) {
    let mut disc: Option<OpenedDisc> = None;
    while let Ok(msg) = rx.recv() {
        match msg {
            WorkerMsg::OpenDisc(path) => match open_disc(&path) {
                Ok(mut d) => {
                    let images = d.images.clone();
                    let audio_tracks = d.audio_tracks.clone();
                    let disc_info = d.info.clone();
                    let play_sequences = d.play_sequences.clone();
                    // Use parent folder name as disc title
                    let disc_name = path
                        .parent()
                        .and_then(|p| p.file_name())
                        .map(|s| s.to_string_lossy().into_owned());

                    // Derive max resolution from the first image pack,
                    // combining two signals (Python only trusts the IPA
                    // byte, but that is unreliable on discs with only
                    // Base/4Base data that still claim res=2 or write the
                    // 0xff "unknown" sentinel — see photocd_decoder.py:828):
                    //
                    //   1) IPA byte bits 3-2. 0=Base, 1=4Base, 2=16Base.
                    //      Some mastering writes 0xff → raw=3 (garbage).
                    //   2) Image pack file size from ISO9660. Real 16Base
                    //      packs are multi-MB; the Base+4Base-only packs
                    //      on this library are exactly 786432 bytes and
                    //      cannot physically contain 16Base data.
                    //
                    // Rule: if IPA is sane (0 or 1), trust it. If IPA
                    // claims >= 2, require size >= 1.5 MB to confirm; else
                    // cap at 4Base.
                    let max_tier: u8 = if let Some(first) = images.first() {
                        if let Some(v) = first.rgb_variants.as_ref() {
                            v.max_tier() as u8
                        } else {
                            let ipa_tier: u8 =
                                match read_image_pack(&mut *d.reader, first, 2) {
                                    Ok(buf) => {
                                        let ipa = read_ipa_byte(&buf);
                                        if ipa != 0 {
                                            resolution_order(ipa)
                                        } else {
                                            // IPA read failed — fall back
                                            // to INFO.PCD image descriptors.
                                            let from_desc = disc_info
                                                .image_descriptors
                                                .iter()
                                                .map(|d| d.resolution)
                                                .max()
                                                .unwrap_or(0);
                                            from_desc.max(disc_info.res_highest)
                                        }
                                    }
                                    Err(_) => 0,
                                };
                            if ipa_tier < 2 {
                                ipa_tier
                            } else if first.size >= 1_500_000 {
                                2
                            } else {
                                1
                            }
                        }
                    } else {
                        0
                    };

                    disc = Some(d);
                    let _ = tx.send(WorkerResult::DiscOpened {
                        cue_path: path,
                        disc_name,
                        images,
                        audio_tracks,
                        disc_info,
                        play_sequences,
                        max_tier,
                    });
                }
                Err(e) => {
                    disc = None;
                    let _ = tx.send(WorkerResult::DiscError(format!("{e}")));
                }
            },
            WorkerMsg::Decode { image_idx, tier, rotation } => {
                let Some(d) = disc.as_mut() else {
                    let _ = tx.send(WorkerResult::DecodeError("no disc loaded".into()));
                    continue;
                };
                if image_idx >= d.images.len() {
                    let _ = tx.send(WorkerResult::DecodeError("image index out of range".into()));
                    continue;
                }
                let img = d.images[image_idx].clone();

                // Kodak Photo CD (USA): raw uncompressed RGB files.
                if let Some(variants) = img.rgb_variants.as_ref() {
                    let tier_idx = match tier {
                        Tier::Base => 0,
                        Tier::FourBase => 1,
                        Tier::SixteenBase => 2,
                    };
                    let Some(variant) = variants.best_for(tier_idx) else {
                        let _ = tx.send(WorkerResult::DecodeError("no RGB variant".into()));
                        continue;
                    };
                    match read_raw_rgb_variant(&mut *d.reader, variant) {
                        Ok(rgb) => {
                            let (w, h, rgb) =
                                apply_rotation(variant.width, variant.height, rgb, rotation);
                            let _ = tx.send(WorkerResult::ImageDecoded {
                                image_idx,
                                tier,
                                width: w,
                                height: h,
                                rgb,
                            });
                        }
                        Err(e) => {
                            let _ = tx.send(WorkerResult::DecodeError(format!("read rgb: {e}")));
                        }
                    }
                    continue;
                }

                let pack = match read_image_pack(&mut *d.reader, &img, 3000) {
                    Ok(p) => p,
                    Err(e) => {
                        let _ = tx.send(WorkerResult::DecodeError(format!("read pack: {e}")));
                        continue;
                    }
                };
                match decode_to_rgb(&pack, tier) {
                    Ok((w, h, rgb)) => {
                        let (w, h, rgb) = apply_rotation(w, h, rgb, rotation);
                        let _ = tx.send(WorkerResult::ImageDecoded {
                            image_idx,
                            tier,
                            width: w,
                            height: h,
                            rgb,
                        });
                    }
                    Err(e) => {
                        let _ = tx.send(WorkerResult::DecodeError(e));
                    }
                }
            }
        }
    }
}

fn decode_to_rgb(pack: &[u8], tier: Tier) -> Result<(u32, u32, Vec<u8>), String> {
    const SECTOR: usize = 2048;
    const BASE_OFF: usize = 96 * SECTOR;
    if pack.len() < BASE_OFF + BASE_RAW_LEN {
        return Err("pack too short for Base".into());
    }
    let base_rgb =
        decode_base_plane(&pack[BASE_OFF..BASE_OFF + BASE_RAW_LEN]).map_err(|e| format!("{e}"))?;
    match tier {
        Tier::Base => Ok((BASE_W as u32, BASE_H as u32, base_rgb)),
        Tier::FourBase => {
            let rgb = decode_4base(pack, &base_rgb).map_err(|e| format!("4base: {e}"))?;
            Ok((FOURBASE_W as u32, FOURBASE_H as u32, rgb))
        }
        Tier::SixteenBase => {
            let fb = decode_4base(pack, &base_rgb).map_err(|e| format!("4base: {e}"))?;
            let sb = decode_16base(pack, &fb).map_err(|e| format!("16base: {e}"))?;
            Ok((SIXTEENBASE_W as u32, SIXTEENBASE_H as u32, sb))
        }
    }
}

/// Rotate an RGB buffer by the 2-bit rotation code from INFO.PCD.
/// 0=none, 1=90 CCW, 2=180, 3=270 CCW.
fn apply_rotation(w: u32, h: u32, rgb: Vec<u8>, rotation: u8) -> (u32, u32, Vec<u8>) {
    match rotation {
        0 => (w, h, rgb),
        2 => {
            // 180 degrees: reverse pixel order
            let mut out = vec![0u8; rgb.len()];
            let n_pixels = (w * h) as usize;
            for i in 0..n_pixels {
                let src = i * 3;
                let dst = (n_pixels - 1 - i) * 3;
                out[dst] = rgb[src];
                out[dst + 1] = rgb[src + 1];
                out[dst + 2] = rgb[src + 2];
            }
            (w, h, out)
        }
        1 => {
            // 90 CCW: (x, y) -> (y, w-1-x), new dims = (h, w)
            let (nw, nh) = (h, w);
            let mut out = vec![0u8; rgb.len()];
            for y in 0..h {
                for x in 0..w {
                    let src = (y * w + x) as usize * 3;
                    let nx = y;
                    let ny = w - 1 - x;
                    let dst = (ny * nw + nx) as usize * 3;
                    out[dst] = rgb[src];
                    out[dst + 1] = rgb[src + 1];
                    out[dst + 2] = rgb[src + 2];
                }
            }
            (nw, nh, out)
        }
        3 => {
            // 270 CCW (90 CW): (x, y) -> (h-1-y, x), new dims = (h, w)
            let (nw, nh) = (h, w);
            let mut out = vec![0u8; rgb.len()];
            for y in 0..h {
                for x in 0..w {
                    let src = (y * w + x) as usize * 3;
                    let nx = h - 1 - y;
                    let ny = x;
                    let dst = (ny * nw + nx) as usize * 3;
                    out[dst] = rgb[src];
                    out[dst + 1] = rgb[src + 1];
                    out[dst + 2] = rgb[src + 2];
                }
            }
            (nw, nh, out)
        }
        _ => (w, h, rgb),
    }
}

// ---------------------------------------------------------------------------
// App methods
// ---------------------------------------------------------------------------

impl PhotoCdApp {
    fn scan_library(&mut self) {
        self.library.clear();
        let dir = match &self.config.library_dir {
            Some(d) => PathBuf::from(d),
            None => return,
        };
        if !dir.is_dir() {
            return;
        }
        let Ok(rd) = std::fs::read_dir(&dir) else {
            return;
        };

        let mut all: Vec<PathBuf> = rd.filter_map(|e| e.ok().map(|e| e.path())).collect();
        all.sort_by(|a, b| {
            a.file_name()
                .map(|s| s.to_ascii_lowercase())
                .cmp(&b.file_name().map(|s| s.to_ascii_lowercase()))
        });

        for item in all {
            let ext = item
                .extension()
                .and_then(|x| x.to_str())
                .map(|s| s.to_ascii_lowercase())
                .unwrap_or_default();

            // Archive files (.zip / .7z) directly in the library folder
            // — only show if store mode (fast metadata-only check)
            if ext == "zip" || ext == "7z" {
                if crate::archive::is_eligible(&item) {
                    let display_name = item
                        .file_stem()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_else(|| item.display().to_string());
                    self.library.push(LibraryEntry {
                        cue_path: item,
                        display_name,
                    });
                }
                continue;
            }

            // Subdirectory: look for a .cue inside (original behaviour)
            if item.is_dir() {
                if let Ok(inner) = std::fs::read_dir(&item) {
                    for e in inner.flatten() {
                        let p = e.path();
                        if p.extension()
                            .and_then(|x| x.to_str())
                            .map(|s| s.eq_ignore_ascii_case("cue"))
                            == Some(true)
                        {
                            let display_name = item
                                .file_name()
                                .map(|s| s.to_string_lossy().into_owned())
                                .unwrap_or_else(|| p.display().to_string());
                            self.library.push(LibraryEntry {
                                cue_path: p,
                                display_name,
                            });
                            break;
                        }
                    }
                }
            }
        }
    }

    fn open_cue(&mut self, path: PathBuf) {
        self.status = format!("Opening {}...", path.display());
        self.decoding = false;
        self.images.clear();
        self.current_idx = 0;
        self.current_texture = None;
        self.current_rgb = None;
        let _ = self.worker_tx.send(WorkerMsg::OpenDisc(path));
    }

    fn request_decode(&mut self) {
        if self.images.is_empty() || self.current_idx >= self.images.len() {
            return;
        }
        self.decoding = true;
        self.status = format!(
            "Decoding {} ({})...",
            self.images[self.current_idx].name,
            self.tier.label_with_dims()
        );
        // Look up per-image rotation from INFO.PCD
        let rotation = self
            .disc_info
            .image_descriptors
            .get(self.current_idx)
            .map(|d| d.rotation)
            .unwrap_or(0);
        let _ = self.worker_tx.send(WorkerMsg::Decode {
            image_idx: self.current_idx,
            tier: self.tier,
            rotation,
        });
    }

    fn drain_worker(&mut self, ctx: &egui::Context) {
        while let Ok(msg) = self.worker_rx.try_recv() {
            match msg {
                WorkerResult::DiscOpened {
                    cue_path,
                    disc_name,
                    images,
                    audio_tracks,
                    disc_info,
                    play_sequences,
                    max_tier,
                } => {
                    self.loaded_cue = Some(cue_path);
                    self.disc_title = disc_name.unwrap_or_default();
                    let n_images = images.len() as u16;
                    self.images = images;
                    self.current_idx = 0;
                    self.disc_info = disc_info;

                    // Default to (and cap at) the highest tier that actually
                    // has data on disc. Worker already corroborates IPA
                    // with image-pack size, so 2 means real 16Base.
                    self.max_tier = match max_tier {
                        0 => Tier::Base,
                        1 => Tier::FourBase,
                        _ => Tier::SixteenBase,
                    };
                    self.tier = self.max_tier;

                    self.image_timings =
                        playlist::image_timings(&play_sequences, n_images);
                    self.slideshow_on = false;
                    self.slideshow_deadline = None;
                    self.view = View::Image;
                    self.status =
                        format!("Loaded disc with {} image(s).", self.images.len());
                    if !self.images.is_empty() {
                        self.request_decode();
                    }
                    // Start audio playback
                    self.start_audio(&audio_tracks);
                    // Auto-start slideshow if timings exist
                    if !self.image_timings.is_empty() {
                        self.start_slideshow();
                    }
                    // Update window title
                    ctx.send_viewport_cmd(egui::ViewportCommand::Title(self.window_title()));
                }
                WorkerResult::DiscError(e) => {
                    self.status = format!("Error opening disc: {e}");
                }
                WorkerResult::ImageDecoded {
                    image_idx,
                    tier,
                    width,
                    height,
                    rgb,
                } => {
                    self.decoding = false;
                    if image_idx != self.current_idx || tier != self.tier {
                        continue;
                    }
                    let ci = ColorImage::from_rgb([width as usize, height as usize], &rgb);
                    let tex = ctx.load_texture(
                        format!("pcd_img_{image_idx}"),
                        ci,
                        TextureOptions::LINEAR,
                    );
                    self.current_texture_size = (width, height);
                    self.current_display_tier = tier;
                    self.current_texture = Some(tex);
                    self.current_rgb = Some(rgb);
                    self.status = format!(
                        "{}   {}  \u{00B7}  {}x{} ({})",
                        self.disc_title,
                        self.images[self.current_idx].name,
                        width,
                        height,
                        tier.label()
                    );
                    // Update window title with current image
                    ctx.send_viewport_cmd(egui::ViewportCommand::Title(self.window_title()));
                }
                WorkerResult::DecodeError(e) => {
                    self.decoding = false;
                    self.status = format!("Decode error: {e}");
                }
            }
        }
    }

    fn window_title(&self) -> String {
        match self.view {
            View::Library => format!("{} {}", APP_NAME, APP_VERSION),
            View::Image => {
                let img_name = if !self.images.is_empty() && self.current_idx < self.images.len() {
                    &self.images[self.current_idx].name
                } else {
                    ""
                };
                if self.disc_title.is_empty() {
                    format!("{} {}", APP_NAME, APP_VERSION)
                } else if img_name.is_empty() {
                    format!(
                        "{} {} \u{2014} {}",
                        APP_NAME, APP_VERSION, self.disc_title
                    )
                } else {
                    format!(
                        "{} \u{2014} {} \u{2014} {}",
                        APP_NAME, self.disc_title, img_name
                    )
                }
            }
        }
    }

    fn next_image(&mut self) {
        if self.images.is_empty() {
            return;
        }
        self.current_idx = (self.current_idx + 1) % self.images.len();
        self.request_decode();
    }

    fn prev_image(&mut self) {
        if self.images.is_empty() {
            return;
        }
        self.current_idx = if self.current_idx == 0 {
            self.images.len() - 1
        } else {
            self.current_idx - 1
        };
        self.request_decode();
    }

    fn start_audio(&mut self, audio_tracks: &[Track]) {
        self.audio.stop();

        // Detect multi-bin layout: each audio track has its own .bin file.
        // In multi-bin, the .bin contains only playable PCM (no pregap).
        // In single-bin, all tracks share one .bin and we must seek to index_01.
        let is_multi_bin = audio_tracks.iter().filter(|t| t.ttype.is_audio()).fold(
            (None::<&std::path::Path>, true),
            |(prev, all_unique), t| {
                let unique = prev.map_or(true, |p| p != t.bin_file.as_path());
                (Some(t.bin_file.as_path()), all_unique && unique)
            },
        ).1;

        let tracks: Vec<AudioTrackInfo> = audio_tracks
            .iter()
            .filter(|t| t.ttype.is_audio())
            .filter_map(|t| {
                let file_sectors = t.duration.unwrap_or(0);
                let index_01 = t.index_01.unwrap_or(0);
                let (start_sector, duration_sectors) = if is_multi_bin {
                    // .bin starts directly with playable PCM audio; no pregap.
                    (0u32, file_sectors)
                } else {
                    // Single-bin: seek past the pregap to actual audio start.
                    (index_01, file_sectors.saturating_sub(index_01))
                };
                if duration_sectors == 0 {
                    return None;
                }
                Some(AudioTrackInfo {
                    bin_path: &t.bin_file,
                    start_sector,
                    duration_sectors,
                })
            })
            .collect();

        if tracks.is_empty() {
            return;
        }

        // Python hardcodes audio_start_s = 0.0 — the parsed playlist value is
        // intentionally unused. Mirror that here.
        self.audio.play_chained(&tracks, 0.0);
        self.audio.set_volume(self.volume / 100.0);
    }

    fn start_slideshow(&mut self) {
        self.slideshow_on = true;
        self.schedule_slideshow_advance();
    }

    fn stop_slideshow(&mut self) {
        self.slideshow_on = false;
        self.slideshow_deadline = None;
    }

    fn toggle_slideshow(&mut self) {
        if self.slideshow_on {
            self.stop_slideshow();
        } else {
            self.start_slideshow();
        }
    }

    /// Schedule next advance based on current image's display time.
    fn schedule_slideshow_advance(&mut self) {
        let img_num = (self.current_idx + 1) as u16; // 1-based
        let duration = self
            .image_timings
            .iter()
            .find(|(n, _)| *n == img_num)
            .map(|(_, t)| *t as f64)
            .unwrap_or(5.0); // default 5s if no timing
        // Use std::time for absolute deadline
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        self.slideshow_deadline = Some(now + duration);
    }

    fn check_slideshow_advance(&mut self) {
        if !self.slideshow_on || self.decoding {
            return;
        }
        let Some(deadline) = self.slideshow_deadline else {
            return;
        };
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        if now >= deadline {
            // Check if there's a next image with timing
            let next_idx = (self.current_idx + 1) % self.images.len();
            let next_num = (next_idx + 1) as u16;
            let has_timing = self.image_timings.iter().any(|(n, _)| *n == next_num);
            if has_timing || next_idx != 0 {
                self.current_idx = next_idx;
                self.request_decode();
                self.schedule_slideshow_advance();
            } else {
                // Reached end of timed sequence, stop
                self.stop_slideshow();
            }
        }
    }

    fn show_library(&mut self, ctx: &egui::Context) {
        self.view = View::Library;
        self.loaded_cue = None;
        self.disc_title.clear();
        self.images.clear();
        self.current_texture = None;
        self.current_rgb = None;
        self.audio.stop();
        self.stop_slideshow();
        self.image_timings.clear();
        self.disc_info = DiscInfo::default();
        self.status = "Click a Library title or open a file to load a disc.".into();
        // Clean up any archive temp dir from the previous disc.
        if let Some(dir) = self.temp_disc_dir.take() {
            let _ = std::fs::remove_dir_all(dir);
        }
        ctx.send_viewport_cmd(egui::ViewportCommand::Title(self.window_title()));
    }

    fn open_archive(&mut self, path: PathBuf) {
        use crate::archive::{check_zip, check_7z, ArchiveResult};
        let ext = path.extension()
            .and_then(|e| e.to_str())
            .map(|s| s.to_ascii_lowercase())
            .unwrap_or_default();

        self.status = format!("Checking archive {}…", path.file_name().unwrap_or_default().to_string_lossy());

        let result = if ext == "zip" {
            check_zip(&path)
        } else {
            check_7z(&path)
        };

        match result {
            ArchiveResult::Eligible(disc) => {
                // Clean up any previous temp dir.
                if let Some(old) = self.temp_disc_dir.take() {
                    let _ = std::fs::remove_dir_all(old);
                }
                self.temp_disc_dir = Some(disc.temp_dir);
                self.open_cue(disc.cue_path);
            }
            ArchiveResult::NotEligible(reason) => {
                self.status = format!("Archive not eligible: {reason}");
            }
        }
    }

    fn save_png(&mut self) {
        let Some(rgb) = &self.current_rgb else {
            self.status = "Nothing to save \u{2014} image not loaded yet.".into();
            return;
        };

        let name = if !self.images.is_empty() && self.current_idx < self.images.len() {
            self.images[self.current_idx].name.clone()
        } else {
            "image".to_string()
        };
        let base = name.strip_suffix(".PCD").or(name.strip_suffix(".pcd")).unwrap_or(&name);
        let suggested = format!("{}.png", base);

        let out_path = if let Some(dir) = &self.config.save_dir {
            PathBuf::from(dir).join(&suggested)
        } else {
            // No save folder set yet — ask the user to pick one
            let initial = dirs::home_dir().unwrap_or_default();
            let Some(dir) = rfd::FileDialog::new().set_directory(&initial).pick_folder() else {
                return;
            };
            self.config.save_dir = Some(dir.display().to_string());
            save_config(&self.config);
            dir.join(&suggested)
        };

        let (w, h) = self.current_texture_size;
        match image::save_buffer(&out_path, rgb, w, h, image::ColorType::Rgb8) {
            Ok(()) => {
                let fname = out_path.file_name().unwrap_or_default().to_string_lossy();
                self.status = format!("Saved  {}  \u{2713}", fname);
            }
            Err(e) => {
                self.status = format!("Save failed: {e}");
            }
        }
    }

    fn open_file_dialog(&mut self) {
        let initial = self
            .config
            .library_dir
            .as_deref()
            .unwrap_or("~");
        let dialog = rfd::FileDialog::new()
            .add_filter("Photo CD / Archive", &["cue", "pcd", "rgb", "zip", "7z"])
            .set_directory(initial);
        if let Some(path) = dialog.pick_file() {
            let ext = path.extension()
                .and_then(|e| e.to_str())
                .map(|s| s.to_ascii_lowercase())
                .unwrap_or_default();
            if ext == "zip" || ext == "7z" {
                self.open_archive(path);
            } else {
                self.open_cue(path);
            }
        }
    }

    fn toggle_fullscreen(&mut self, ctx: &egui::Context) {
        self.fullscreen = !self.fullscreen;
        ctx.send_viewport_cmd(egui::ViewportCommand::Fullscreen(self.fullscreen));
    }

    fn set_library_dir(&mut self) {
        let initial = self
            .config
            .library_dir
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| dirs::home_dir().unwrap_or_default());
        if let Some(dir) = rfd::FileDialog::new().set_directory(&initial).pick_folder() {
            self.config.library_dir = Some(dir.display().to_string());
            save_config(&self.config);
            self.scan_library();
            self.status = format!("{} discs found in library.", self.library.len());
        }
    }

    fn set_save_dir(&mut self) {
        let initial = self
            .config
            .save_dir
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| dirs::home_dir().unwrap_or_default());
        if let Some(dir) = rfd::FileDialog::new().set_directory(&initial).pick_folder() {
            self.config.save_dir = Some(dir.display().to_string());
            save_config(&self.config);
            self.status = format!("Save location set to: {}", dir.display());
        }
    }
}

// ---------------------------------------------------------------------------
// Custom button helper
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
enum ButtonStyle {
    Secondary,
    Subtle,
}

fn paint_toolbar_button(
    ui: &egui::Ui,
    rect: Rect,
    response: &egui::Response,
    text: &str,
    font_id: FontId,
    style: ButtonStyle,
) {
    let (bg, fg, stroke_color) = match style {
        ButtonStyle::Subtle => {
            if response.is_pointer_button_down_on() {
                (Theme::BTN_SUBTLE_HOVER, Theme::BTN_SUBTLE_FG, Theme::BTN_SUBTLE_STROKE)
            } else if response.hovered() {
                (Theme::BTN_SUBTLE_HOVER, Theme::BTN_SUBTLE_FG, Theme::BTN_SUBTLE_STROKE)
            } else {
                (Theme::BTN_SUBTLE, Theme::BTN_SUBTLE_FG, Theme::BTN_SUBTLE_STROKE)
            }
        }
        ButtonStyle::Secondary => {
            if response.is_pointer_button_down_on() {
                (Theme::BTN_HOVER, Theme::BTN_FG, Theme::BTN_STROKE)
            } else if response.hovered() {
                (Theme::BTN_HOVER, Theme::BTN_FG, Theme::BTN_STROKE)
            } else {
                (Theme::BTN, Theme::BTN_FG, Theme::BTN_STROKE)
            }
        }
    };
    let rounding = Rounding::same(9.0);
    let shadow_offset = if response.is_pointer_button_down_on() {
        Vec2::new(0.0, 1.0)
    } else {
        Vec2::new(0.0, 2.0)
    };
    let face_rect = if response.is_pointer_button_down_on() {
        rect.translate(Vec2::new(0.0, 1.0))
    } else {
        rect
    };

    ui.painter()
        .rect_filled(rect.translate(shadow_offset), rounding, Theme::btn_shadow());
    ui.painter().rect_filled(face_rect, rounding, bg);
    ui.painter()
        .rect_stroke(face_rect, rounding, Stroke::new(1.0, stroke_color));
    if response.has_focus() {
        ui.painter().rect_stroke(
            face_rect.expand(2.0),
            Rounding::same(11.0),
            Stroke::new(1.0, Theme::SELECT_FG),
        );
    }
    ui.painter().text(
        face_rect.center(),
        Align2::CENTER_CENTER,
        text,
        font_id,
        fg,
    );
}

fn themed_button(ui: &mut egui::Ui, text: &str, style: ButtonStyle) -> bool {
    let font_id = FontId::new(17.0, egui::FontFamily::Name("SemiBold".into()));
    let text_width = ui.fonts(|f| {
        f.layout_no_wrap(text.to_owned(), font_id.clone(), Theme::BTN_FG)
            .size()
            .x
    });
    let desired_size = Vec2::new(ui.spacing().interact_size.x.max(text_width + 26.0), 36.0);
    let (rect, response) = ui.allocate_exact_size(desired_size, egui::Sense::click());

    if ui.is_rect_visible(rect) {
        paint_toolbar_button(ui, rect, &response, text, font_id, style);
    }
    response.clicked()
}

fn themed_nav_button(ui: &mut egui::Ui, text: &str) -> bool {
    let desired_size = Vec2::new(36.0, 36.0);
    let (rect, response) = ui.allocate_exact_size(desired_size, egui::Sense::click());
    if ui.is_rect_visible(rect) {
        paint_toolbar_button(
            ui,
            rect,
            &response,
            text,
            FontId::proportional(19.0),
            ButtonStyle::Subtle,
        );
    }
    response.clicked()
}

// ---------------------------------------------------------------------------
// egui App impl
// ---------------------------------------------------------------------------

impl eframe::App for PhotoCdApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.drain_worker(ctx);
        self.check_slideshow_advance();

        // Request repaint while slideshow is active
        if self.slideshow_on {
            ctx.request_repaint_after(std::time::Duration::from_millis(100));
        }

        // Apply dark theme
        apply_theme(ctx);

        // Keyboard shortcuts
        let (prev_hit, next_hit, space_hit, esc_hit, f_hit) = ctx.input(|i| {
            (
                i.key_pressed(egui::Key::ArrowLeft),
                i.key_pressed(egui::Key::ArrowRight),
                i.key_pressed(egui::Key::Space),
                i.key_pressed(egui::Key::Escape),
                i.key_pressed(egui::Key::F) || i.key_pressed(egui::Key::F11),
            )
        });
        if self.view == View::Image {
            if prev_hit {
                self.prev_image();
            }
            if next_hit {
                self.next_image();
            }
            if space_hit {
                if !self.image_timings.is_empty() {
                    self.toggle_slideshow();
                } else {
                    self.next_image();
                }
            }
        }
        if f_hit {
            self.toggle_fullscreen(ctx);
        }
        if esc_hit && self.fullscreen {
            self.fullscreen = false;
            ctx.send_viewport_cmd(egui::ViewportCommand::Fullscreen(false));
        }

        // ── Bottom toolbar ──────────────────────────────────────────────
        egui::TopBottomPanel::bottom("toolbar")
            .frame(
                egui::Frame::none()
                    .fill(Theme::TOOLBAR)
                    .inner_margin(Margin::symmetric(8.0, 0.0)),
            )
            .show(ctx, |ui| {
                ui.set_min_height(66.0);

                // Info row: disc title (bold) + status
                ui.add_space(4.0);
                ui.horizontal(|ui| {
                    ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                        ui.add_space(ui.available_width() * 0.02);
                        // Centered status spanning full width (credit painted on right after)
                        let status_text = if self.view == View::Image
                            && !self.disc_title.is_empty()
                            && !self.images.is_empty()
                        {
                            let img_name = if self.current_idx < self.images.len() {
                                &self.images[self.current_idx].name
                            } else {
                                ""
                            };
                            let (w, h) = self.current_texture_size;
                            if w > 0 {
                                format!(
                                    "{}   {}  \u{00B7}  {}x{} ({})",
                                    self.disc_title,
                                    img_name,
                                    w,
                                    h,
                                    self.current_display_tier.label()
                                )
                            } else {
                                self.status.clone()
                            }
                        } else {
                            self.status.clone()
                        };

                        let available = ui.available_width();
                        ui.allocate_ui_with_layout(
                            Vec2::new(available, 16.0),
                            Layout::centered_and_justified(egui::Direction::LeftToRight),
                            |ui| {
                                ui.label(
                                    RichText::new(&status_text)
                                        .color(Theme::FG_DIM)
                                        .size(14.0)
                                        .family(egui::FontFamily::Name("SemiBold".into())),
                                );
                            },
                        );

                        // "whatev.indus" credit — right edge of this row, vertically centred
                        {
                            let credit = "whatev.indus";
                            let font = FontId::new(14.0, egui::FontFamily::Name("SemiBold".into()));
                            let dim = Theme::FG_DIM;
                            let lit = Color32::from_rgb(0xF0, 0xCC, 0xAA);
                            let row = ui.max_rect();
                            let sz = ui.fonts(|f| {
                                f.layout_no_wrap(credit.to_owned(), font.clone(), dim).size()
                            });
                            let tl = Pos2::new(
                                row.right() - sz.x - 8.0,
                                row.center().y - sz.y / 2.0,
                            );
                            let rect = Rect::from_min_size(tl, sz);
                            let resp = ui.allocate_rect(rect, egui::Sense::click());
                            if resp.hovered() {
                                ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
                            }
                            let color = if resp.hovered() { lit } else { dim };
                            ui.painter().text(tl, Align2::LEFT_TOP, credit, font, color);
                            if resp.clicked() {
                                ui.output_mut(|o| {
                                    o.open_url = Some(egui::OpenUrl {
                                        url: "https://sites.google.com/view/whateverindustries/home"
                                            .to_owned(),
                                        new_tab: true,
                                    });
                                });
                            }
                        }
                    });
                });

                // Separator
                ui.add_space(2.0);
                let sep_rect = ui.available_rect_before_wrap();
                let y = sep_rect.top();
                ui.painter().line_segment(
                    [Pos2::new(sep_rect.left(), y), Pos2::new(sep_rect.right(), y)],
                    Stroke::new(1.0, Theme::SEP),
                );
                ui.add_space(6.0);

                // Button row — horizontally centered by padding based on
                // last frame's measured content width.
                let avail_w = ui.available_width();
                let left_pad = ((avail_w - self.button_row_width) * 0.5).max(4.0);
                let row_resp = ui.horizontal(|ui| {
                    ui.add_space(left_pad);
                    let x_start = ui.cursor().min.x;

                    // Volume slider — icons painted manually so they vertically centre
                    // against the 32px button row height.
                    {
                        let icon = "\u{1F508}";
                        let font = FontId::proportional(20.0);
                        let iw = ui.fonts(|f| f.layout_no_wrap(icon.to_owned(), font.clone(), Theme::BTN).size().x);
                        let (rect, _) = ui.allocate_exact_size(Vec2::new(iw + 2.0, 36.0), egui::Sense::hover());
                        ui.painter().text(rect.center(), Align2::CENTER_CENTER, icon, font, Theme::BTN);
                    }
                    let vol_rect = ui.allocate_space(Vec2::new(120.0, 36.0)).1;
                    self.paint_volume_slider(ui, vol_rect);
                    ui.add_space(6.0);
                    {
                        let icon = "\u{1F50A}";
                        let font = FontId::proportional(20.0);
                        let iw = ui.fonts(|f| f.layout_no_wrap(icon.to_owned(), font.clone(), Theme::BTN).size().x);
                        let (rect, _) = ui.allocate_exact_size(Vec2::new(iw + 2.0, 36.0), egui::Sense::hover());
                        ui.painter().text(rect.center(), Align2::CENTER_CENTER, icon, font, Theme::BTN);
                    }

                    // Separator
                    paint_vsep(ui);

                    // Disc-specific controls
                    if self.view == View::Image && !self.images.is_empty() {
                        // Play/pause slideshow button (only if timings exist)
                        if !self.image_timings.is_empty() {
                            let icon = if self.slideshow_on {
                                "\u{25A0}" // stop square
                            } else {
                                "\u{25B6}" // play triangle
                            };
                            if themed_nav_button(ui, icon) {
                                self.toggle_slideshow();
                            }
                            ui.add_space(2.0);
                        }

                        // Prev / Next
                        if themed_nav_button(ui, "\u{25C0}") {
                            self.prev_image();
                        }
                        if themed_nav_button(ui, "\u{25B6}") {
                            self.next_image();
                        }

                        // Image counter
                        ui.label(
                            RichText::new(format!(
                                "{} / {}",
                                self.current_idx + 1,
                                self.images.len()
                            ))
                            .color(Theme::FG_DIM)
                            .size(16.0)
                            .family(egui::FontFamily::Name("SemiBold".into())),
                        );

                        paint_vsep(ui);

                        // Resolution selector
                        ui.label(
                            RichText::new("Resolution:")
                                .color(Theme::FG_DIM)
                                .size(16.0)
                                .family(egui::FontFamily::Name("SemiBold".into())),
                        );
                        let prev_tier = self.tier;
                        let max_idx = match self.max_tier {
                            Tier::Base => 0usize,
                            Tier::FourBase => 1,
                            Tier::SixteenBase => 2,
                        };
                        let tier_font = egui::FontFamily::Name("SemiBold".into());
                        egui::ComboBox::from_id_salt("tier_sel")
                            .selected_text(RichText::new(self.tier.label()).size(16.0).family(tier_font.clone()))
                            .width(86.0)
                            .show_ui(ui, |ui| {
                                for (i, t) in ALL_TIERS.iter().enumerate() {
                                    if i > max_idx {
                                        break;
                                    }
                                    ui.selectable_value(&mut self.tier, *t, RichText::new(t.label()).size(16.0).family(tier_font.clone()));
                                }
                            });
                        if self.tier != prev_tier {
                            self.request_decode();
                        }

                        paint_vsep(ui);
                    }

                    // Always-visible buttons (right side)
                    // Library button (only when disc is loaded)
                    if self.view == View::Image {
                        if themed_button(ui, "Library", ButtonStyle::Secondary) {
                            self.show_library(ctx);
                        }
                    }

                    // Open File
                    let mut want_open = false;
                    if themed_button(ui, "Open File", ButtonStyle::Secondary) {
                        want_open = true;
                    }

                    // Fullscreen
                    let fs_text = if self.fullscreen {
                        "Exit Full"
                    } else {
                        "Fullscreen"
                    };
                    if themed_button(ui, fs_text, ButtonStyle::Secondary) {
                        self.toggle_fullscreen(ctx);
                    }

                    // Save PNG (only when image loaded)
                    if self.view == View::Image && self.current_rgb.is_some() {
                        if themed_button(ui, "Save PNG", ButtonStyle::Secondary) {
                            self.save_png();
                        }
                    }

                    // Set-folder buttons only in Library view
                    if self.view == View::Library {
                        paint_vsep(ui);
                        if themed_button(ui, "Set Library Folder", ButtonStyle::Secondary) {
                            self.set_library_dir();
                        }
                        if themed_button(ui, "Set PNG Save Folder", ButtonStyle::Secondary) {
                            self.set_save_dir();
                        }
                    }

                    // Deferred open (to avoid borrow issues)
                    if want_open {
                        self.open_file_dialog();
                    }

                    // Return content width by comparing cursor positions.
                    ui.cursor().min.x - x_start
                });

                // Cache measured button-row width for next frame's centering.
                self.button_row_width = row_resp.inner.max(0.0);

                ui.add_space(4.0);
            });

        // ── Central panel ───────────────────────────────────────────────
        egui::CentralPanel::default()
            .frame(
                egui::Frame::none()
                    .fill(if self.view == View::Library {
                        Theme::BG
                    } else {
                        Theme::CANVAS
                    })
                    .inner_margin(Margin::ZERO),
            )
            .show(ctx, |ui| match self.view {
                View::Library => self.paint_library(ui),
                View::Image => self.paint_image(ui),
            });
    }
}

// ---------------------------------------------------------------------------
// Paint helpers
// ---------------------------------------------------------------------------

impl PhotoCdApp {
    fn paint_library(&mut self, ui: &mut egui::Ui) {
        let avail = ui.available_size();

        // Logo
        ui.add_space(20.0);
        let tex = self.logo_texture.get_or_insert_with(|| {
            let img = image::load_from_memory(LOGO_PNG)
                .expect("logo PNG")
                .to_rgba8();
            let (w, h) = img.dimensions();
            let ci = egui::ColorImage::from_rgba_unmultiplied(
                [w as usize, h as usize],
                img.as_raw(),
            );
            ui.ctx().load_texture("library_logo", ci, TextureOptions::LINEAR)
        });
        let logo_h = 90.0;
        let logo_w = tex.size()[0] as f32 / tex.size()[1] as f32 * logo_h;
        ui.vertical_centered(|ui| {
            ui.add(egui::Image::new((tex.id(), Vec2::new(logo_w, logo_h))));
        });
        ui.add_space(9.0);

        if self.library.is_empty() {
            // Centre only in the space below the logo, not the full panel.
            let remaining = ui.available_height();
            let text_h = 42.0; // approximate height of 2 lines at 16px
            let pad = (remaining * 0.4 - text_h / 2.0).max(0.0);
            ui.add_space(pad);
            ui.vertical_centered(|ui| {
                ui.label(
                    RichText::new(
                        "Click \"Set Library Folder\" and navigate\nwhere your Photo CD files are kept.",
                    )
                    .color(Theme::FG_DIM)
                    .size(16.0),
                );
            });
            return;
        }

        // Library list
        let list_width = (avail.x - 80.0).min(900.0).max(300.0);
        ui.vertical_centered(|ui| {
            ui.set_max_width(list_width);

            let row_height = 28.0;
            egui::ScrollArea::vertical()
                .auto_shrink([false, false])
                .show(ui, |ui| {
                    ui.style_mut().spacing.item_spacing.y = 0.0;
                    let mut open_request: Option<PathBuf> = None;
                    for (_i, entry) in self.library.iter().enumerate() {
                        let response = ui.allocate_response(
                            Vec2::new(ui.available_width(), row_height),
                            egui::Sense::click(),
                        );
                        let rect = response.rect;

                        // Subtle hover highlight
                        if response.hovered() {
                            ui.painter().rect_filled(
                                rect,
                                Rounding::ZERO,
                                Color32::from_rgb(0x22, 0x1A, 0x12),
                            );
                        }

                        let font = FontId::new(19.0, egui::FontFamily::Name("SemiBold".into()));
                        ui.painter().text(
                            Pos2::new(rect.left() + 8.0, rect.center().y),
                            Align2::LEFT_CENTER,
                            &entry.display_name,
                            font,
                            Theme::FG_DIM,
                        );

                        if response.clicked() {
                            open_request = Some(entry.cue_path.clone());
                        }
                    }
                    if let Some(p) = open_request {
                        let ext = p.extension()
                            .and_then(|e| e.to_str())
                            .map(|s| s.to_ascii_lowercase())
                            .unwrap_or_default();
                        if ext == "zip" || ext == "7z" {
                            self.open_archive(p);
                        } else {
                            self.open_cue(p);
                        }
                    }
                });
        });
    }

    fn paint_image(&mut self, ui: &mut egui::Ui) {
        if let Some(tex) = &self.current_texture {
            let avail = ui.available_size();
            let (w, h) = self.current_texture_size;
            let (w, h) = (w as f32, h as f32);
            let scale = (avail.x / w).min(avail.y / h).max(0.01);
            let size = Vec2::new(w * scale, h * scale);

            // Center image
            let offset = (avail - size) * 0.5;
            let img_rect = Rect::from_min_size(
                ui.min_rect().left_top() + Vec2::new(offset.x.max(0.0), offset.y.max(0.0)),
                size,
            );
            ui.put(img_rect, egui::Image::new((tex.id(), size)));
        } else if self.decoding {
            ui.centered_and_justified(|ui| {
                ui.spinner();
            });
        } else {
            ui.centered_and_justified(|ui| {
                ui.label(
                    RichText::new("No image.")
                        .color(Theme::FG_DIM)
                        .size(14.0),
                );
            });
        }
    }

    fn paint_volume_slider(&mut self, ui: &mut egui::Ui, rect: Rect) {
        let painter = ui.painter();
        let cy = rect.center().y;
        let track_h = 5.0;
        let handle_r = 8.0;

        // Trough
        painter.rect_filled(
            Rect::from_min_max(
                Pos2::new(rect.left(), cy - track_h / 2.0),
                Pos2::new(rect.right(), cy + track_h / 2.0),
            ),
            Rounding::same(2.0),
            Theme::SEP,
        );

        // Filled portion
        let cx = rect.left() + (self.volume / 100.0) * rect.width();
        if cx > rect.left() {
            painter.rect_filled(
                Rect::from_min_max(
                    Pos2::new(rect.left(), cy - track_h / 2.0),
                    Pos2::new(cx, cy + track_h / 2.0),
                ),
                Rounding::same(2.0),
                Theme::BTN,
            );
        }

        // Handle
        painter.circle(
            Pos2::new(cx, cy),
            handle_r,
            Theme::BTN,
            Stroke::new(1.0, Theme::BTN),
        );

        // Interaction
        let response = ui.interact(rect, ui.id().with("vol_slider"), egui::Sense::drag());
        if response.dragged() || response.clicked() {
            if let Some(pos) = response.interact_pointer_pos() {
                let frac = ((pos.x - rect.left()) / rect.width()).clamp(0.0, 1.0);
                self.volume = frac * 100.0;
                self.audio.set_volume(frac);
            }
        }
    }
}

fn paint_vsep(ui: &mut egui::Ui) {
    ui.add_space(6.0);
    // Allocate full button height so egui centers it; draw only the visible portion.
    let (rect, _) = ui.allocate_exact_size(Vec2::new(1.0, 36.0), egui::Sense::hover());
    let cy = rect.center().y;
    let half = 14.0;
    ui.painter().rect_filled(
        Rect::from_min_max(Pos2::new(rect.left(), cy - half), Pos2::new(rect.right(), cy + half)),
        Rounding::ZERO,
        Theme::SEP,
    );
    ui.add_space(6.0);
}

// ---------------------------------------------------------------------------
// Theme setup
// ---------------------------------------------------------------------------

fn apply_theme(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    let visuals = &mut style.visuals;

    visuals.dark_mode = true;
    visuals.override_text_color = Some(Theme::FG);

    visuals.widgets.noninteractive.bg_fill = Theme::BG;
    visuals.widgets.noninteractive.fg_stroke = Stroke::new(1.0, Theme::FG);
    visuals.widgets.inactive.bg_fill = Theme::BTN;
    visuals.widgets.inactive.fg_stroke = Stroke::new(1.0, Theme::BTN_FG);
    visuals.widgets.hovered.bg_fill = Theme::BTN_HOVER;
    visuals.widgets.hovered.fg_stroke = Stroke::new(1.0, Theme::BTN_FG);
    visuals.widgets.active.bg_fill = Theme::BTN_HOVER;
    visuals.widgets.active.fg_stroke = Stroke::new(1.0, Theme::BTN_FG);
    visuals.widgets.open.bg_fill = Theme::BTN_HOVER;
    visuals.widgets.open.fg_stroke = Stroke::new(1.0, Theme::BTN_FG);

    visuals.selection.bg_fill = Theme::SELECT_BG;
    visuals.selection.stroke = Stroke::new(1.0, Theme::SELECT_FG);

    visuals.window_fill = Theme::TOOLBAR;
    visuals.panel_fill = Theme::BG;
    visuals.extreme_bg_color = Theme::SIDEBAR;

    visuals.window_stroke = Stroke::new(1.0, Theme::SEP);

    ctx.set_style(style);
}
