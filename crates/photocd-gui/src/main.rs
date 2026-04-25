//! Photo CD Player (egui) — styled to match the original Python/Tkinter build.

// Suppress the Windows console window in release builds. Debug builds keep
// the console so `eprintln!` from diagnostics is visible during development.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod app;
mod archive;
mod audio;

const ICON_PNG: &[u8] = include_bytes!("../../../icons/PhotoCD_AppIcon_v2.png");
const FONT_REGULAR: &[u8]  = include_bytes!("../../../assets/Lato-Regular.ttf");
const FONT_SEMIBOLD: &[u8] = include_bytes!("../../../assets/Lato-Semibold.ttf");
const FONT_BOLD: &[u8]     = include_bytes!("../../../assets/Lato-Bold.ttf");

fn load_icon() -> Option<egui::IconData> {
    let img = image::load_from_memory(ICON_PNG).ok()?.to_rgba8();
    let (width, height) = img.dimensions();
    Some(egui::IconData {
        rgba: img.into_raw(),
        width,
        height,
    })
}

fn setup_fonts(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();

    fonts.font_data.insert("Lato-Regular".into(),  egui::FontData::from_static(FONT_REGULAR));
    fonts.font_data.insert("Lato-Semibold".into(), egui::FontData::from_static(FONT_SEMIBOLD));
    fonts.font_data.insert("Lato-Bold".into(),     egui::FontData::from_static(FONT_BOLD));

    // Replace default proportional with Lato Regular (keep built-in as fallback)
    fonts.families
        .entry(egui::FontFamily::Proportional)
        .or_default()
        .insert(0, "Lato-Regular".into());

    // Named families for semibold (buttons, labels) and bold (credit link)
    fonts.families.insert(
        egui::FontFamily::Name("SemiBold".into()),
        vec!["Lato-Semibold".into(), "Lato-Regular".into()],
    );
    fonts.families.insert(
        egui::FontFamily::Name("Bold".into()),
        vec!["Lato-Bold".into(), "Lato-Regular".into()],
    );

    ctx.set_fonts(fonts);
}

fn main() -> eframe::Result<()> {
    let title = format!("{} {}", app::APP_NAME, app::APP_VERSION);
    let mut viewport = egui::ViewportBuilder::default()
        .with_title(&title)
        .with_inner_size([1188.0, 860.0])
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
        Box::new(|cc| {
            setup_fonts(&cc.egui_ctx);
            Ok(Box::new(app::PhotoCdApp::default()))
        }),
    )
}
