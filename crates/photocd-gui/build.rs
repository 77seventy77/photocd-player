fn main() {
    #[cfg(target_os = "windows")]
    {
        // Embed the Windows .exe icon. The .ico lives at the repo root
        // under "App Icon/PhotoCDLogo.ico" (two levels up from this crate).
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../../icons/PhotoCD_AppIcon_v2.ico");
        res.compile().unwrap();
    }
}
