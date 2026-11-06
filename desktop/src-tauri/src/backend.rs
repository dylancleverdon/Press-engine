//! Runs the Press Engine backend (a Python web app) for the Mac app.
//!
//! Layout under the app's support folder (~/Library/Application Support/<identifier>/):
//!
//! ```text
//! bin/uv               copied from the app bundle; installs Python and the backend
//! python/              Python runtimes managed by uv
//! venvs/<version>/     one virtualenv per installed backend version
//! current              version that starts normally
//! pending              version installed by the backend's self-updater, not yet proven
//! bad_versions         versions that failed to start and were rolled back
//! desktop.json         host / port / launch_at_login (also edited by the dashboard)
//! data/                database, Google token, API keys (.env)
//! logs/backend.log
//! ```
//!
//! Protocol with the backend (see backend/press_engine/runtime.py and updater.py):
//! exit code 75 = "restart me" (after an update, new API keys, ...). A pending
//! version is promoted to current only after it answers /api/health.

use std::collections::HashSet;
use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

pub const RESTART_EXIT_CODE: i32 = 75;
pub const PYTHON_VERSION: &str = "3.13";
const HEALTH_TIMEOUT: Duration = Duration::from_secs(90);

// --- paths ----------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Paths {
    pub support: PathBuf,
    pub resources: PathBuf,
}

impl Paths {
    pub fn uv(&self) -> PathBuf {
        self.support.join("bin").join("uv")
    }
    pub fn venvs(&self) -> PathBuf {
        self.support.join("venvs")
    }
    pub fn venv(&self, version: &str) -> PathBuf {
        self.venvs().join(version)
    }
    pub fn data(&self) -> PathBuf {
        self.support.join("data")
    }
    pub fn logs(&self) -> PathBuf {
        self.support.join("logs")
    }
    pub fn log_file(&self) -> PathBuf {
        self.logs().join("backend.log")
    }
    pub fn python_dir(&self) -> PathBuf {
        self.support.join("python")
    }
    fn marker(&self, name: &str) -> PathBuf {
        self.support.join(name)
    }
    pub fn ensure_dirs(&self) -> std::io::Result<()> {
        for dir in [self.support.join("bin"), self.venvs(), self.data(), self.logs(), self.python_dir()] {
            fs::create_dir_all(dir)?;
        }
        Ok(())
    }
}

// --- markers --------------------------------------------------------------------------

pub fn read_marker(paths: &Paths, name: &str) -> Option<String> {
    fs::read_to_string(paths.marker(name)).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

pub fn write_marker(paths: &Paths, name: &str, value: &str) -> std::io::Result<()> {
    let tmp = paths.marker(&format!("{name}.tmp"));
    fs::write(&tmp, value)?;
    fs::rename(tmp, paths.marker(name))
}

pub fn remove_marker(paths: &Paths, name: &str) {
    let _ = fs::remove_file(paths.marker(name));
}

pub fn bad_versions(paths: &Paths) -> HashSet<String> {
    fs::read_to_string(paths.marker("bad_versions"))
        .map(|s| s.lines().map(|l| l.trim().to_string()).filter(|l| !l.is_empty()).collect())
        .unwrap_or_default()
}

pub fn add_bad_version(paths: &Paths, version: &str) -> std::io::Result<()> {
    let mut all = bad_versions(paths);
    all.insert(version.to_string());
    let mut list: Vec<_> = all.into_iter().collect();
    list.sort();
    fs::write(paths.marker("bad_versions"), list.join("\n") + "\n")
}

// --- versions ---------------------------------------------------------------------------

pub fn parse_version(v: &str) -> (u64, u64, u64) {
    let nums: Vec<u64> = v
        .trim_start_matches('v')
        .split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .take(3)
        .filter_map(|s| s.parse().ok())
        .collect();
    (nums.first().copied().unwrap_or(0), nums.get(1).copied().unwrap_or(0), nums.get(2).copied().unwrap_or(0))
}

/// The backend wheel shipped inside the app bundle: `press_engine-<version>-py3-none-any.whl`.
pub fn bundled_wheel(resources: &Path) -> Option<(PathBuf, String)> {
    let dir = resources.join("backend");
    let mut found: Vec<(PathBuf, String)> = fs::read_dir(dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().to_string();
            let version = name.strip_prefix("press_engine-")?.strip_suffix(".whl")?.split('-').next()?.to_string();
            Some((e.path(), version))
        })
        .collect();
    found.sort_by_key(|(_, v)| parse_version(v));
    found.pop()
}

pub fn venv_ok(paths: &Paths, version: &str) -> bool {
    paths.venv(version).join("bin").join("press-engine").exists()
}

#[derive(Debug, PartialEq)]
pub enum Plan {
    /// Install the backend that ships inside the app (first run, broken venv, or the app
    /// was updated and carries a newer backend than the one installed).
    InstallBundled { wheel: PathBuf, version: String },
    /// Start a version the self-updater just installed; promote it if it's healthy.
    TryPending { version: String },
    Run { version: String },
}

pub fn choose_plan(paths: &Paths) -> Result<Plan, String> {
    let bundled = bundled_wheel(&paths.resources);
    let bad = bad_versions(paths);
    let current = read_marker(paths, "current").filter(|v| venv_ok(paths, v));
    if let Some(pending) = read_marker(paths, "pending") {
        if venv_ok(paths, &pending) && !bad.contains(&pending) && Some(&pending) != current.as_ref() {
            return Ok(Plan::TryPending { version: pending });
        }
        remove_marker(paths, "pending");
    }
    match (current, bundled) {
        (None, Some((wheel, version))) => Ok(Plan::InstallBundled { wheel, version }),
        (None, None) => Err("This copy of the app has no backend inside. Download it again.".into()),
        (Some(cur), Some((wheel, version))) if parse_version(&version) > parse_version(&cur) && !bad.contains(&version) => {
            Ok(Plan::InstallBundled { wheel, version })
        }
        (Some(cur), _) => Ok(Plan::Run { version: cur }),
    }
}

/// Keep the current version and the one before it (for rolling back by hand); delete the rest.
pub fn prune_venvs(paths: &Paths, keep: &[&str]) {
    let Ok(entries) = fs::read_dir(paths.venvs()) else { return };
    for e in entries.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        if !keep.contains(&name.as_str()) {
            let _ = fs::remove_dir_all(e.path());
        }
    }
}

// --- desktop.json ---------------------------------------------------------------------------

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Prefs {
    pub host: String,
    pub port: u16,
    pub launch_at_login: bool,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs { host: "127.0.0.1".into(), port: 8000, launch_at_login: true }
    }
}

pub fn read_prefs(paths: &Paths) -> Prefs {
    fs::read_to_string(paths.marker("desktop.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<Prefs>(&s).ok())
        .unwrap_or_default()
}

pub fn ensure_prefs_file(paths: &Paths) {
    let path = paths.marker("desktop.json");
    if !path.exists() {
        if let Ok(json) = serde_json::to_string_pretty(&Prefs::default()) {
            let _ = fs::write(path, json);
        }
    }
}

// --- networking -------------------------------------------------------------------------------

/// The preferred port if it's free, otherwise the next free one nearby.
pub fn find_free_port(host: &str, preferred: u16) -> Option<u16> {
    (preferred..preferred.saturating_add(20)).find(|p| TcpListener::bind((host, *p)).is_ok())
}

pub fn health_check(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else { return false };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    if stream.write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_err() {
        return false;
    }
    let mut buf = [0u8; 32];
    match stream.read(&mut buf) {
        Ok(n) => {
            let head = String::from_utf8_lossy(&buf[..n]);
            head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200")
        }
        Err(_) => false,
    }
}

/// Running straight from the DMG or from macOS's temporary "translocated" copy, where
/// launch at login would point at a path that disappears.
pub fn is_temporary_location(exe: &Path) -> bool {
    let p = exe.to_string_lossy();
    p.starts_with("/Volumes/") || p.contains("/AppTranslocation/")
}

pub fn resolves(host: &str) -> bool {
    (host, 0).to_socket_addrs().map(|mut a| a.next().is_some()).unwrap_or(false)
}

// --- installing -------------------------------------------------------------------------------

/// Copy the bundled uv out of the app. Writing the bytes (rather than copying the file)
/// leaves the download quarantine flag behind, so macOS doesn't stop to ask about it.
pub fn install_uv(paths: &Paths) -> Result<(), String> {
    let src = paths.resources.join("bin").join("uv");
    let dst = paths.uv();
    let bytes = fs::read(&src).map_err(|e| format!("uv is missing from the app bundle ({}): {e}", src.display()))?;
    let same = fs::read(&dst).map(|old| old == bytes).unwrap_or(false);
    if !same {
        let tmp = dst.with_extension("tmp");
        fs::write(&tmp, &bytes).map_err(|e| format!("couldn't copy uv: {e}"))?;
        set_executable(&tmp)?;
        fs::rename(&tmp, &dst).map_err(|e| format!("couldn't copy uv: {e}"))?;
    }
    Ok(())
}

#[cfg(unix)]
fn set_executable(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).map_err(|e| format!("chmod failed: {e}"))
}

#[cfg(not(unix))]
fn set_executable(_path: &Path) -> Result<(), String> {
    Ok(())
}

fn uv_command(paths: &Paths) -> Command {
    let mut cmd = Command::new(paths.uv());
    cmd.env("UV_PYTHON_INSTALL_DIR", paths.python_dir()).env_remove("VIRTUAL_ENV");
    cmd
}

fn run_logged(mut cmd: Command, log: &Path, what: &str) -> Result<(), String> {
    let out = cmd.output().map_err(|e| format!("{what}: couldn't start ({e})"))?;
    append_log(log, &format!("$ {what}\n{}{}\n", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)));
    if out.status.success() {
        Ok(())
    } else {
        let err = String::from_utf8_lossy(&out.stderr);
        let tail: String = err.lines().rev().take(6).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n");
        Err(format!("{what} failed:\n{tail}"))
    }
}

/// Same steps as the Python self-updater (backend/press_engine/updater.py: install).
pub fn install_version(paths: &Paths, wheel: &str, version: &str) -> Result<(), String> {
    let venv = paths.venv(version);
    if venv.exists() {
        let _ = fs::remove_dir_all(&venv);
    }
    let log = paths.log_file();
    let mut c = uv_command(paths);
    c.args(["venv", "--python", PYTHON_VERSION, "--python-preference", "only-managed"]).arg(&venv);
    run_logged(c, &log, "uv venv")?;
    let mut c = uv_command(paths);
    c.args(["pip", "install", "--python"]).arg(venv.join("bin").join("python")).arg(wheel);
    if let Err(e) = run_logged(c, &log, "uv pip install") {
        let _ = fs::remove_dir_all(&venv);
        return Err(e);
    }
    let mut c = Command::new(venv.join("bin").join("python"));
    c.args(["-c", "import press_engine.api.app, press_engine.cli"]);
    if let Err(e) = run_logged(c, &log, "smoke test") {
        let _ = fs::remove_dir_all(&venv);
        return Err(e);
    }
    Ok(())
}

// --- logging -----------------------------------------------------------------------------------

pub fn append_log(path: &Path, text: &str) {
    if let Ok(meta) = fs::metadata(path) {
        if meta.len() > 5_000_000 {
            let _ = fs::rename(path, path.with_extension("log.1"));
        }
    }
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(text.as_bytes());
    }
}

pub fn log_tail(path: &Path, lines: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

// --- the running process ---------------------------------------------------------------------

pub struct Launch<'a> {
    pub version: &'a str,
    pub host: &'a str,
    pub port: u16,
    pub secret: &'a str,
    pub desktop_version: &'a str,
    /// The app is running from the disk image / a temporary location: ask D to move it.
    pub move_hint: bool,
}

pub fn spawn(paths: &Paths, l: &Launch) -> Result<Child, String> {
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(paths.log_file())
        .map_err(|e| format!("can't open log: {e}"))?;
    let log2 = log.try_clone().map_err(|e| e.to_string())?;
    let exe = paths.venv(l.version).join("bin").join("press-engine");
    Command::new(&exe)
        .args(["serve", "--host", l.host, "--port", &l.port.to_string()])
        .current_dir(paths.data())
        .env("PRESS_DATA_DIR", paths.data())
        .env("PRESS_DESKTOP", "1")
        .env("PRESS_DESKTOP_VERSION", l.desktop_version)
        .env("PRESS_DESKTOP_LOGIN_SECRET", l.secret)
        .env("PRESS_APP_SUPPORT_DIR", &paths.support)
        .env("PRESS_UV_PATH", paths.uv())
        .env("PRESS_PORT", l.port.to_string())
        .env("PRESS_DESKTOP_MOVE_HINT", if l.move_hint { "1" } else { "0" })
        .env("UV_PYTHON_INSTALL_DIR", paths.python_dir())
        .env_remove("VIRTUAL_ENV")
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(log2)
        .spawn()
        .map_err(|e| format!("couldn't start {}: {e}", exe.display()))
}

#[cfg(unix)]
pub fn terminate(child: &mut Child) {
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(not(unix))]
pub fn terminate(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

/// If the app crashed last time, its backend may still be holding the port.
#[cfg(unix)]
pub fn kill_stale(paths: &Paths) {
    let Some(pid) = read_marker(paths, "backend.pid").and_then(|p| p.parse::<i32>().ok()) else { return };
    let alive = unsafe { libc::kill(pid, 0) } == 0;
    if alive {
        let cmdline = Command::new("ps").args(["-p", &pid.to_string(), "-o", "command="]).output();
        if let Ok(out) = cmdline {
            if String::from_utf8_lossy(&out.stdout).contains("press-engine") {
                unsafe {
                    libc::kill(pid, libc::SIGTERM);
                }
                thread::sleep(Duration::from_secs(2));
            }
        }
    }
    remove_marker(paths, "backend.pid");
}

#[cfg(not(unix))]
pub fn kill_stale(_paths: &Paths) {}

// --- supervisor ----------------------------------------------------------------------------------

/// What the window shows. Implemented by the Tauri side (lib.rs) and by tests.
pub trait Ui: Send + Sync {
    fn status(&self, message: &str);
    fn ready(&self, port: u16, secret: &str);
    fn error(&self, message: &str, log_tail: &str);
    fn apply_prefs(&self, prefs: &Prefs);
}

pub struct Supervisor {
    pub paths: Paths,
    pub secret: String,
    pub desktop_version: String,
    child: Mutex<Option<Child>>,
    shutting_down: AtomicBool,
    move_hint: AtomicBool,
    retry: (Mutex<bool>, std::sync::Condvar),
}

enum Outcome {
    Restart,
    Crashed(String),
    Shutdown,
}

impl Supervisor {
    pub fn new(paths: Paths, secret: String, desktop_version: String) -> Arc<Self> {
        Arc::new(Supervisor {
            paths,
            secret,
            desktop_version,
            child: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            move_hint: AtomicBool::new(false),
            retry: (Mutex::new(false), std::sync::Condvar::new()),
        })
    }

    pub fn set_move_hint(&self, on: bool) {
        self.move_hint.store(on, Ordering::SeqCst);
    }

    pub fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::SeqCst);
        let child = self.child.lock().unwrap().take(); // don't hold the lock while waiting
        if let Some(mut child) = child {
            terminate(&mut child);
        }
        remove_marker(&self.paths, "backend.pid");
        self.request_retry();
    }

    pub fn request_retry(&self) {
        let (lock, cv) = &self.retry;
        *lock.lock().unwrap() = true;
        cv.notify_all();
    }

    fn wait_for_retry(&self) {
        let (lock, cv) = &self.retry;
        let mut go = lock.lock().unwrap();
        while !*go && !self.shutting_down.load(Ordering::SeqCst) {
            go = cv.wait(go).unwrap();
        }
        *go = false;
    }

    fn stopping(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }

    /// Runs until the app quits: set up, start, watch, restart.
    pub fn run(self: Arc<Self>, ui: Arc<dyn Ui>) {
        let mut crashes: Vec<Instant> = Vec::new();
        while !self.stopping() {
            match self.cycle(ui.as_ref()) {
                Ok(Outcome::Restart) => {
                    ui.status("Restarting Press Engine…");
                    continue;
                }
                Ok(Outcome::Shutdown) => break,
                Ok(Outcome::Crashed(msg)) | Err(msg) => {
                    if self.stopping() {
                        break;
                    }
                    crashes.retain(|t| t.elapsed() < Duration::from_secs(180));
                    crashes.push(Instant::now());
                    append_log(&self.paths.log_file(), &format!("[app] {msg}\n"));
                    if crashes.len() >= 3 {
                        ui.error(&msg, &log_tail(&self.paths.log_file(), 40));
                        self.wait_for_retry();
                        crashes.clear();
                    } else {
                        ui.status("Press Engine stopped unexpectedly. Starting it again…");
                        thread::sleep(Duration::from_secs(2 * crashes.len() as u64));
                    }
                }
            }
        }
    }

    fn cycle(&self, ui: &dyn Ui) -> Result<Outcome, String> {
        self.paths.ensure_dirs().map_err(|e| format!("couldn't create {}: {e}", self.paths.support.display()))?;
        ensure_prefs_file(&self.paths);
        install_uv(&self.paths)?;
        kill_stale(&self.paths);

        let (version, trial) = match choose_plan(&self.paths)? {
            Plan::InstallBundled { wheel, version } => {
                ui.status("Setting up Press Engine. The first time takes a couple of minutes (downloading Python and its libraries)…");
                install_version(&self.paths, &wheel.to_string_lossy(), &version)?;
                let previous = read_marker(&self.paths, "current");
                write_marker(&self.paths, "current", &version).map_err(|e| e.to_string())?;
                let keep: Vec<&str> = [Some(version.as_str()), previous.as_deref()].into_iter().flatten().collect();
                prune_venvs(&self.paths, &keep);
                (version, false)
            }
            Plan::TryPending { version } => {
                ui.status(&format!("Starting the new version of Press Engine ({version})…"));
                (version, true)
            }
            Plan::Run { version } => {
                ui.status("Starting Press Engine…");
                (version, false)
            }
        };

        let prefs = read_prefs(&self.paths);
        ui.apply_prefs(&prefs);
        let host = if prefs.host == "0.0.0.0" || resolves(&prefs.host) { prefs.host.clone() } else { "127.0.0.1".into() };
        let port = find_free_port(&host, prefs.port).ok_or("no free port to listen on")?;
        let mut child = spawn(
            &self.paths,
            &Launch {
                version: &version,
                host: &host,
                port,
                secret: &self.secret,
                desktop_version: &self.desktop_version,
                move_hint: self.move_hint.load(Ordering::SeqCst),
            },
        )?;
        let _ = write_marker(&self.paths, "backend.pid", &child.id().to_string());

        // Wait until it answers, or gives up.
        let started = Instant::now();
        let healthy = loop {
            if self.stopping() {
                terminate(&mut child);
                return Ok(Outcome::Shutdown);
            }
            if let Ok(Some(status)) = child.try_wait() {
                break Err(format!("Press Engine exited while starting ({status})"));
            }
            if health_check(port) {
                break Ok(());
            }
            if started.elapsed() > HEALTH_TIMEOUT {
                terminate(&mut child);
                break Err("Press Engine didn't start within 90 seconds".to_string());
            }
            thread::sleep(Duration::from_millis(400));
        };
        if let Err(msg) = healthy {
            if trial {
                // Roll back: never try this version again; the next cycle starts `current`.
                let _ = add_bad_version(&self.paths, &version);
                remove_marker(&self.paths, "pending");
                append_log(&self.paths.log_file(), &format!("[app] version {version} failed to start, rolled back: {msg}\n"));
                return Ok(Outcome::Restart);
            }
            return Err(msg);
        }
        if trial {
            let previous = read_marker(&self.paths, "current");
            write_marker(&self.paths, "current", &version).map_err(|e| e.to_string())?;
            remove_marker(&self.paths, "pending");
            let keep: Vec<&str> = [Some(version.as_str()), previous.as_deref()].into_iter().flatten().collect();
            prune_venvs(&self.paths, &keep);
        }
        ui.ready(port, &self.secret);
        *self.child.lock().unwrap() = Some(child);

        // Watch it.
        let mut ticks = 0u32;
        loop {
            thread::sleep(Duration::from_millis(500));
            if self.stopping() {
                return Ok(Outcome::Shutdown);
            }
            ticks += 1;
            if ticks.is_multiple_of(60) {
                ui.apply_prefs(&read_prefs(&self.paths)); // launch-at-login can change from the dashboard
            }
            let mut guard = self.child.lock().unwrap();
            let Some(child) = guard.as_mut() else { return Ok(Outcome::Shutdown) };
            if let Ok(Some(status)) = child.try_wait() {
                guard.take();
                remove_marker(&self.paths, "backend.pid");
                return Ok(match status.code() {
                    Some(RESTART_EXIT_CODE) => Outcome::Restart,
                    _ if self.stopping() => Outcome::Shutdown,
                    _ => Outcome::Crashed(format!("Press Engine stopped ({status})")),
                });
            }
        }
    }
}

// --- tests ----------------------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (tempfile::TempDir, Paths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths { support: dir.path().join("support"), resources: dir.path().join("resources") };
        paths.ensure_dirs().unwrap();
        fs::create_dir_all(paths.resources.join("backend")).unwrap();
        (dir, paths)
    }

    fn fake_venv(paths: &Paths, version: &str) {
        let bin = paths.venv(version).join("bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("press-engine"), "#!/bin/sh\n").unwrap();
    }

    fn bundle(paths: &Paths, version: &str) {
        fs::write(paths.resources.join("backend").join(format!("press_engine-{version}-py3-none-any.whl")), b"wheel").unwrap();
    }

    #[test]
    fn temporary_locations_are_detected() {
        assert!(is_temporary_location(Path::new("/Volumes/Press Engine/Press Engine.app/Contents/MacOS/press-engine-desktop")));
        assert!(is_temporary_location(Path::new("/private/var/folders/x/AppTranslocation/ABC/d/Press Engine.app/Contents/MacOS/p")));
        assert!(!is_temporary_location(Path::new("/Applications/Press Engine.app/Contents/MacOS/press-engine-desktop")));
    }

    #[test]
    fn versions_compare_numerically() {
        assert!(parse_version("0.10.0") > parse_version("0.9.9"));
        assert_eq!(parse_version("v1.2.3"), (1, 2, 3));
        assert_eq!(parse_version("garbage"), (0, 0, 0));
    }

    #[test]
    fn first_run_installs_the_bundled_backend() {
        let (_d, p) = setup();
        bundle(&p, "0.2.0");
        bundle(&p, "0.1.0");
        assert_eq!(
            choose_plan(&p).unwrap(),
            Plan::InstallBundled { wheel: p.resources.join("backend/press_engine-0.2.0-py3-none-any.whl"), version: "0.2.0".into() }
        );
    }

    #[test]
    fn normal_start_runs_current() {
        let (_d, p) = setup();
        bundle(&p, "0.2.0");
        fake_venv(&p, "0.3.0");
        write_marker(&p, "current", "0.3.0").unwrap();
        assert_eq!(choose_plan(&p).unwrap(), Plan::Run { version: "0.3.0".into() });
    }

    #[test]
    fn broken_current_venv_reinstalls() {
        let (_d, p) = setup();
        bundle(&p, "0.2.0");
        write_marker(&p, "current", "0.3.0").unwrap(); // venv deleted by hand
        assert!(matches!(choose_plan(&p).unwrap(), Plan::InstallBundled { .. }));
    }

    #[test]
    fn newer_bundled_backend_after_app_update_is_installed() {
        let (_d, p) = setup();
        bundle(&p, "0.4.0");
        fake_venv(&p, "0.3.0");
        write_marker(&p, "current", "0.3.0").unwrap();
        assert!(matches!(choose_plan(&p).unwrap(), Plan::InstallBundled { version, .. } if version == "0.4.0"));
        add_bad_version(&p, "0.4.0").unwrap();
        assert_eq!(choose_plan(&p).unwrap(), Plan::Run { version: "0.3.0".into() });
    }

    #[test]
    fn pending_update_is_tried_unless_bad_or_missing() {
        let (_d, p) = setup();
        bundle(&p, "0.2.0");
        fake_venv(&p, "0.2.0");
        write_marker(&p, "current", "0.2.0").unwrap();
        fake_venv(&p, "0.3.0");
        write_marker(&p, "pending", "0.3.0").unwrap();
        assert_eq!(choose_plan(&p).unwrap(), Plan::TryPending { version: "0.3.0".into() });

        add_bad_version(&p, "0.3.0").unwrap();
        assert_eq!(choose_plan(&p).unwrap(), Plan::Run { version: "0.2.0".into() });
        assert!(read_marker(&p, "pending").is_none(), "stale pending marker is cleared");

        write_marker(&p, "pending", "0.9.0").unwrap(); // venv never finished installing
        assert_eq!(choose_plan(&p).unwrap(), Plan::Run { version: "0.2.0".into() });
    }

    #[test]
    fn prune_keeps_current_and_previous() {
        let (_d, p) = setup();
        for v in ["0.1.0", "0.2.0", "0.3.0"] {
            fake_venv(&p, v);
        }
        prune_venvs(&p, &["0.3.0", "0.2.0"]);
        assert!(!p.venv("0.1.0").exists() && p.venv("0.2.0").exists() && p.venv("0.3.0").exists());
    }

    #[test]
    fn prefs_defaults_and_partial_files() {
        let (_d, p) = setup();
        assert_eq!(read_prefs(&p), Prefs::default());
        fs::write(p.support.join("desktop.json"), r#"{"host": "0.0.0.0"}"#).unwrap();
        let prefs = read_prefs(&p);
        assert_eq!((prefs.host.as_str(), prefs.port, prefs.launch_at_login), ("0.0.0.0", 8000, true));
        fs::write(p.support.join("desktop.json"), "not json").unwrap();
        assert_eq!(read_prefs(&p), Prefs::default());
    }

    #[test]
    fn uv_copy_is_executable_and_idempotent() {
        let (_d, p) = setup();
        fs::create_dir_all(p.resources.join("bin")).unwrap();
        fs::write(p.resources.join("bin").join("uv"), b"#!/bin/sh\necho uv\n").unwrap();
        install_uv(&p).unwrap();
        install_uv(&p).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(p.uv()).unwrap().permissions().mode() & 0o777, 0o755);
        }
    }

    #[test]
    fn free_port_skips_busy_ones() {
        let busy = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = busy.local_addr().unwrap().port();
        let found = find_free_port("127.0.0.1", port).unwrap();
        assert_ne!(found, port);
    }

    #[test]
    fn health_check_reads_status_line() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            for (i, stream) in listener.incoming().take(2).enumerate() {
                let mut s = stream.unwrap();
                let mut buf = [0u8; 256];
                let _ = s.read(&mut buf);
                let reply: &[u8] = if i == 0 { b"HTTP/1.1 200 OK\r\n\r\n{\"ok\":true}" } else { b"HTTP/1.1 503 Busy\r\n\r\n" };
                s.write_all(reply).unwrap();
            }
        });
        assert!(health_check(port));
        assert!(!health_check(port));
        server.join().unwrap();
        assert!(!health_check(port)); // nothing listening any more
    }
}
