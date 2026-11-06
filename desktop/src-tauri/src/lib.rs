//! Press Engine for Mac: a window onto the local dashboard, plus a menu bar item.
//! The app keeps running (and sending) when the window is closed.

pub mod backend;

use std::sync::{Arc, Mutex};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_opener::OpenerExt;

use backend::{Paths, Prefs, Supervisor, Ui};

const MAIN: &str = "main";

struct TauriUi {
    app: AppHandle,
    loading_url: Mutex<Option<Url>>,
    last_path: Mutex<Option<String>>,
    port: Mutex<Option<u16>>,
}

/// Like JavaScript's encodeURIComponent, so the loading page can always decode it.
fn encode_component(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn is_local(url: &Url) -> bool {
    matches!(url.scheme(), "tauri" | "asset") || matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "tauri.localhost"))
}

impl TauriUi {
    fn window(&self) -> Option<tauri::WebviewWindow> {
        self.app.get_webview_window(MAIN)
    }

    /// Show the bundled loading page with a message (the hash carries the message so it
    /// survives the navigation).
    fn show_loading(&self, mode: &str, payload: &serde_json::Value) {
        let Some(w) = self.window() else { return };
        let Some(mut url) = self.loading_url.lock().unwrap().clone() else { return };
        let on_loading = w.url().map(|u| u.scheme() == url.scheme() && u.host_str() == url.host_str()).unwrap_or(false);
        if on_loading {
            let js = format!("window.pressEngine && window.pressEngine.show({}, {})", serde_json::json!(mode), payload);
            let _ = w.eval(js);
            return;
        }
        if let Ok(current) = w.url() {
            if current.host_str() == Some("127.0.0.1") {
                let mut path = current.path().to_string();
                if let Some(q) = current.query() {
                    path = format!("{path}?{q}");
                }
                *self.last_path.lock().unwrap() = Some(path);
            }
        }
        url.set_fragment(Some(&encode_component(&serde_json::json!({ "mode": mode, "data": payload }).to_string())));
        let _ = w.navigate(url);
    }

    fn open_dashboard(&self, path: &str) {
        let port = *self.port.lock().unwrap();
        if let (Some(w), Some(port)) = (self.window(), port) {
            if let Ok(url) = Url::parse(&format!("http://127.0.0.1:{port}{path}")) {
                let _ = w.navigate(url);
            }
        }
        show_main(&self.app);
    }
}

impl Ui for TauriUi {
    fn status(&self, message: &str) {
        self.show_loading("status", &serde_json::json!(message));
    }

    fn ready(&self, port: u16, secret: &str) {
        *self.port.lock().unwrap() = Some(port);
        let next = self.last_path.lock().unwrap().take().unwrap_or_else(|| "/".to_string());
        let Ok(mut url) = Url::parse(&format!("http://127.0.0.1:{port}/desktop-login")) else { return };
        url.query_pairs_mut().append_pair("secret", secret).append_pair("next", &next);
        if let Some(w) = self.window() {
            let _ = w.navigate(url);
        }
    }

    fn error(&self, message: &str, log_tail: &str) {
        self.show_loading("error", &serde_json::json!({ "message": message, "log": log_tail }));
        show_main(&self.app);
    }

    fn apply_prefs(&self, prefs: &Prefs) {
        let launcher = self.app.autolaunch();
        if let Ok(enabled) = launcher.is_enabled() {
            if enabled != prefs.launch_at_login {
                let _ = if prefs.launch_at_login { launcher.enable() } else { launcher.disable() };
            }
        }
    }
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

#[tauri::command]
fn retry(supervisor: tauri::State<'_, Arc<Supervisor>>) {
    supervisor.request_retry();
}

#[tauri::command]
fn open_log(app: AppHandle, supervisor: tauri::State<'_, Arc<Supervisor>>) {
    let _ = app.opener().open_path(supervisor.paths.log_file().to_string_lossy().to_string(), None::<&str>);
}

fn random_secret() -> String {
    let mut buf = [0u8; 24];
    getrandom::fill(&mut buf).expect("no randomness available");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_opener::init())
        .plugin(
            // Launch agent (the default on macOS), started with --hidden so the window stays closed.
            tauri_plugin_autostart::Builder::new().args(["--hidden"]).build(),
        )
        .invoke_handler(tauri::generate_handler![retry, open_log])
        .setup(|app| {
            let handle = app.handle().clone();
            let hidden = std::env::args().any(|a| a == "--hidden"); // started at login
            let opener = handle.clone();
            let window = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
                .title("Press Engine")
                .inner_size(1240.0, 860.0)
                .min_inner_size(760.0, 520.0)
                .visible(!hidden)
                .on_navigation(move |url| {
                    if is_local(url) {
                        return true;
                    }
                    // Profiles, articles, Google, docs...: open in the normal browser.
                    let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    false
                })
                .build()?;

            let paths = Paths { support: app.path().app_data_dir()?, resources: app.path().resource_dir()? };
            let version = app.package_info().version.to_string();
            let supervisor = Supervisor::new(paths.clone(), random_secret(), version);
            app.manage(supervisor.clone());
            let ui = Arc::new(TauriUi {
                app: handle.clone(),
                loading_url: Mutex::new(window.url().ok()),
                last_path: Mutex::new(None),
                port: Mutex::new(None),
            });
            app.manage(ui.clone());

            let open = MenuItem::with_id(app, "open", "Open Press Engine", true, None::<&str>)?;
            let updates = MenuItem::with_id(app, "updates", "Check for Updates…", true, None::<&str>)?;
            let folder = MenuItem::with_id(app, "folder", "Show Data Folder", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Press Engine", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open, &updates, &folder, &separator, &quit])?;
            let mut tray = TrayIconBuilder::with_id("press-engine")
                .tooltip("Press Engine")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "open" => show_main(app),
                    "updates" => {
                        if let Some(ui) = app.try_state::<Arc<TauriUi>>() {
                            ui.open_dashboard("/connections");
                        }
                    }
                    "folder" => {
                        let _ = app.opener().open_path(paths.support.to_string_lossy().to_string(), None::<&str>);
                    }
                    "quit" => app.exit(0),
                    _ => {}
                });
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            }
            tray.build(app)?;

            let ui_dyn: Arc<dyn Ui> = ui;
            std::thread::Builder::new()
                .name("press-engine-backend".into())
                .spawn(move || supervisor.run(ui_dyn))?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Closing the window keeps Press Engine running in the menu bar.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("couldn't start Press Engine")
        .run(|app, event| match event {
            RunEvent::ExitRequested { api, code, .. } => {
                if code.is_none() {
                    api.prevent_exit(); // last window hidden: keep running
                }
            }
            RunEvent::Exit => {
                if let Some(supervisor) = app.try_state::<Arc<Supervisor>>() {
                    supervisor.shutdown();
                }
            }
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => show_main(app),
            _ => {}
        });
}
