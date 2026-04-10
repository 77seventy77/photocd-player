//! Photo CD Player (egui) — styled to match the original Python/Tkinter build.

mod app;
mod audio;

fn main() -> eframe::Result<()> {
    let title = format!("{} {}", app::APP_NAME, app::APP_VERSION);
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(&title)
            .with_inner_size([1280.0, 860.0])
            .with_min_inner_size([640.0, 400.0]),
        ..Default::default()
    };
    eframe::run_native(
        &title,
        options,
        Box::new(|_cc| Ok(Box::new(app::PhotoCdApp::default()))),
    )
}
