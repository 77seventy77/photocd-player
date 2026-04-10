//! Photo CD Player (egui) — styled to match the original Python/Tkinter build.

// Suppress the Windows console window in release builds. Debug builds keep
// the console so `eprintln!` from diagnostics is visible during development.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod app;
mod audio;

/// PNG icon embedded at compile time. Used for the running window/taskbar
/// icon on all platforms (the winresource build.rs separately stamps the
/// .ico into the Windows .exe metadata for Explorer thumbnails).
const ICON_PNG: &[u8] = include_bytes!("../../../App Icon/PhotoCDLogo.png");

fn load_icon() -> Option<egui::IconData> {
    let img = image::load_from_memory(ICON_PNG).ok()?.to_rgba8();
    let (width, height) = img.dimensions();
    Some(egui::IconData {
        rgba: img.into_raw(),
        width,
        height,
    })
}

fn main() -> eframe::Result<()> {
    let title = format!("{} {}", app::APP_NAME, app::APP_VERSION);
    let mut viewport = egui::ViewportBuilder::default()
        .with_title(&title)
        .with_inner_size([1280.0, 860.0])
        .with_min_inner_size([640.0, 400.0]);
    if let Some(icon) = load_icon() {
        viewport = viewport.with_icon(icon);
    }
    let options = eframe::NativeOptions {
        viewport,
        ..Default::default()
    };
    eframe::run_native(
        &title,
        options,
        Box::new(|_cc| Ok(Box::new(app::PhotoCdApp::default()))),
    )
}
