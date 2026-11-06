//! End-to-end check of the supervisor with the real backend wheel and uv: first-run
//! install, restart on exit code 75, promoting a pending update, rolling back a broken one.
//!
//! Needs network (uv downloads Python and packages), so it's ignored by default:
//!   E2E_WHEEL=../../dist/press_engine-0.2.0-py3-none-any.whl E2E_UV=$(which uv) \
//!   cargo test --test update_protocol -- --ignored --nocapture

use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use press_engine_desktop_lib::backend::{self, Paths, Prefs, Supervisor, Ui};

#[derive(Default)]
struct RecordingUi {
    ready: Mutex<Vec<u16>>,
    errors: Mutex<Vec<String>>,
}

impl Ui for RecordingUi {
    fn status(&self, message: &str) {
        eprintln!("[status] {message}");
    }
    fn ready(&self, port: u16, _secret: &str) {
        eprintln!("[ready] port {port}");
        self.ready.lock().unwrap().push(port);
    }
    fn error(&self, message: &str, log_tail: &str) {
        eprintln!("[error] {message}\n{log_tail}");
        self.errors.lock().unwrap().push(message.to_string());
    }
    fn apply_prefs(&self, _prefs: &Prefs) {}
}

fn http(port: u16, method: &str, path: &str, body: &str, cookie: Option<&str>) -> String {
    let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    let cookie = cookie.map(|c| format!("Cookie: {c}\r\n")).unwrap_or_default();
    let req = format!(
        "{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n{cookie}Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    s.write_all(req.as_bytes()).unwrap();
    let mut out = String::new();
    s.read_to_string(&mut out).unwrap();
    out
}

fn wait_ready(ui: &RecordingUi, count: usize, secs: u64) -> u16 {
    let start = Instant::now();
    loop {
        if let Some(p) = ui.ready.lock().unwrap().get(count - 1) {
            return *p;
        }
        assert!(ui.errors.lock().unwrap().is_empty(), "supervisor reported an error");
        assert!(start.elapsed() < Duration::from_secs(secs), "backend didn't become ready in time");
        thread::sleep(Duration::from_millis(200));
    }
}

fn copy_venv_as(paths: &Paths, from: &str, to: &str) {
    // A real second version: same wheel, new venv dir (the supervisor only cares about the dir).
    let status = std::process::Command::new("cp").arg("-a").arg(paths.venv(from)).arg(paths.venv(to)).status().unwrap();
    assert!(status.success());
}

#[test]
#[ignore]
fn install_restart_promote_and_rollback() {
    let wheel = PathBuf::from(std::env::var("E2E_WHEEL").expect("set E2E_WHEEL"));
    let uv = PathBuf::from(std::env::var("E2E_UV").expect("set E2E_UV"));
    let dir = tempfile::tempdir().unwrap();
    let paths = Paths { support: dir.path().join("support"), resources: dir.path().join("resources") };
    fs::create_dir_all(paths.resources.join("bin")).unwrap();
    fs::create_dir_all(paths.resources.join("backend")).unwrap();
    fs::copy(&uv, paths.resources.join("bin/uv")).unwrap();
    fs::copy(&wheel, paths.resources.join("backend").join(wheel.file_name().unwrap())).unwrap();
    fs::create_dir_all(&paths.support).unwrap();
    // Use a high port so we don't collide with anything on the machine.
    fs::write(paths.support.join("desktop.json"), r#"{"host": "127.0.0.1", "port": 18765, "launch_at_login": false}"#).unwrap();

    let sup = Supervisor::new(paths.clone(), "test-secret".into(), "1.0.0".into());
    let ui = Arc::new(RecordingUi::default());
    let ui_dyn: Arc<dyn Ui> = ui.clone();
    let runner = sup.clone();
    let handle = thread::spawn(move || runner.run(ui_dyn));

    // 1. First run installs the bundled wheel and starts it.
    let port = wait_ready(&ui, 1, 600);
    assert!(backend::health_check(port));
    let current = backend::read_marker(&paths, "current").unwrap();
    assert_eq!(current, "0.2.0");

    // First-run setup through the real web UI, then save an API key -> backend asks to restart (exit 75).
    let setup = http(port, "POST", "/setup", "name=D&band=Night+Swim&timezone=UTC&seeds=Athletic+Progression", None);
    let cookie = setup.lines().find_map(|l| l.strip_prefix("set-cookie: ").or_else(|| l.strip_prefix("Set-Cookie: ")))
        .map(|c| c.split(';').next().unwrap().to_string()).expect("session cookie");

    // 2. Pretend the self-updater installed 0.2.1: it should be started and promoted.
    copy_venv_as(&paths, "0.2.0", "0.2.1");
    backend::write_marker(&paths, "pending", "0.2.1").unwrap();
    let r = http(port, "POST", "/connections/keys", "PRESS_LASTFM_API_KEY=abc123456", Some(&cookie));
    assert!(r.contains("restarting"), "save should trigger a restart: {r}");
    let port2 = wait_ready(&ui, 2, 120);
    assert_eq!(backend::read_marker(&paths, "current").unwrap(), "0.2.1");
    assert!(backend::read_marker(&paths, "pending").is_none());
    assert!(fs::read_to_string(paths.data().join(".env")).unwrap().contains("PRESS_LASTFM_API_KEY=abc123456"));

    // 3. A broken update (0.2.2) is rolled back and never retried.
    let bad_bin = paths.venv("0.2.2").join("bin");
    fs::create_dir_all(&bad_bin).unwrap();
    fs::write(bad_bin.join("press-engine"), "#!/bin/sh\necho broken >&2\nexit 1\n").unwrap();
    make_executable(&bad_bin.join("press-engine"));
    backend::write_marker(&paths, "pending", "0.2.2").unwrap();
    let r = http(port2, "POST", "/connections/keys", "PRESS_LASTFM_API_KEY=changed999", Some(&cookie));
    assert!(r.contains("restarting"));
    let port3 = wait_ready(&ui, 3, 120);
    assert!(backend::health_check(port3));
    assert_eq!(backend::read_marker(&paths, "current").unwrap(), "0.2.1");
    assert!(backend::bad_versions(&paths).contains("0.2.2"));

    sup.shutdown();
    handle.join().unwrap();
    assert!(!backend::health_check(port3), "backend stops when the app quits");
    eprintln!("log tail:\n{}", backend::log_tail(&paths.log_file(), 15));
}

#[cfg(unix)]
fn make_executable(p: &Path) {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(p, fs::Permissions::from_mode(0o755)).unwrap();
}
