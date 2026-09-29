// openmirror's desktop app.
//
// This deliberately contains no application logic. Everything openmirror does —
// the agent, the providers, voice, the browser, desktop control — lives in the
// Python daemon, and this is a window onto it plus the things a browser tab
// cannot do: live in the tray, survive being closed, tell you when the agent
// is waiting on you, and keep a daemon running for exactly as long as the app.
//
// A release carries its own daemon, frozen by PyInstaller, so installing the
// app is installing openmirror. It still attaches to one that is already
// listening — run by systemd, or by hand in a terminal — and leaves that one
// alone on exit. Killing a daemon somebody else started would take their
// running sessions with it; only a daemon this app started is its to stop.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod daemon;

use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::ipc::CapabilityBuilder;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, State, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

use daemon::{Daemon, Home, Probe, Settings};

/// How long a daemon may take to answer before the window says so. A first
/// start unpacks some 50 MB, and Windows scans all of it before it may run.
const START_LIMIT: Duration = Duration::from_secs(120);
/// How long a daemon gets to shut down properly before it is killed.
const STOP_GRACE: Duration = Duration::from_secs(10);

/// What the loading page shows. It asks for this, rather than being sent it,
/// so it cannot miss news that arrived before it finished loading.
#[derive(Clone, Serialize)]
struct Status {
    phase: &'static str,
    title: String,
    detail: String,
    tail: Option<String>,
    log: Option<String>,
}

impl Status {
    fn starting(detail: impl Into<String>) -> Self {
        Self { phase: "starting", title: "Starting openmirror".into(), detail: detail.into(), tail: None, log: None }
    }

    fn ready() -> Self {
        Self { phase: "ready", title: "openmirror is running".into(), detail: String::new(), tail: None, log: None }
    }

    fn failed(title: impl Into<String>, detail: impl Into<String>, log: Option<&Path>) -> Self {
        Self {
            phase: "failed",
            title: title.into(),
            detail: detail.into(),
            tail: log.and_then(|path| daemon::tail(path, 40)),
            log: log.map(|path| path.display().to_string()),
        }
    }
}

struct Shell {
    home: Home,
    settings: Settings,
    port: u16,
    status: Mutex<Status>,
    /// The daemon, if this app started it. `None` when it attached to one.
    daemon: Mutex<Option<Daemon>>,
    starting: AtomicBool,
    quitting: AtomicBool,
}

impl Shell {
    fn daemon_log(&self) -> PathBuf {
        self.home.logs().join("daemon.log")
    }

    fn set(&self, status: Status) {
        if let Ok(mut current) = self.status.lock() {
            *current = status;
        }
    }
}

static LOG: OnceLock<Mutex<File>> = OnceLock::new();
static BEGAN: OnceLock<Instant> = OnceLock::new();

/// A line in `desktop.log`, and on stderr for anyone running this from a
/// terminal. A release on Windows has no console and one opened from the Dock
/// has nowhere for stderr to go, so the file is the only place to read it.
fn note(message: impl AsRef<str>) {
    let since = BEGAN.get_or_init(Instant::now).elapsed().as_secs_f32();
    let line = format!("[{since:7.2}s] {}\n", message.as_ref());
    eprint!("openmirror: {line}");
    if let Some(file) = LOG.get() {
        if let Ok(mut file) = file.lock() {
            let _ = file.write_all(line.as_bytes());
        }
    }
}

fn daemon_url(port: u16) -> Url {
    format!("http://127.0.0.1:{port}/").parse().expect("a well-formed URL")
}

/// Where Tauri serves the app's own files: a scheme of its own, except on
/// Windows, where WebView2 wants plain http.
fn loading_url() -> Url {
    let url = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    url.parse().expect("a well-formed URL")
}

/// The pages this window may show: its own loading page, and the daemon's
/// interface. Everything else — a provider's sign-up page, a link in a
/// transcript — belongs in the user's browser, which has an address bar,
/// their passwords and a back button. This window has none of those, and a
/// page that navigated it away would leave no way back to the interface.
fn belongs_here(url: &Url, port: u16) -> bool {
    match url.scheme() {
        "tauri" | "about" | "blob" => true,
        "http" | "https" if url.host_str() == Some("tauri.localhost") => true,
        "http" => {
            matches!(url.host_str(), Some("127.0.0.1" | "localhost")) && url.port_or_known_default() == Some(port)
        }
        _ => false,
    }
}

fn elsewhere(app: &AppHandle, url: &Url) {
    note(format!("opening {url} in the browser"));
    if let Err(e) = app.opener().open_url(url.as_str(), None::<&str>) {
        note(format!("could not open {url}: {e}"));
    }
}

fn show(app: &AppHandle, url: &Url) {
    if let Some(window) = app.get_webview_window("main") {
        if let Err(e) = window.navigate(url.clone()) {
            note(format!("could not show {url}: {e}"));
        }
    }
}

fn raise(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Find a daemon or start one, then show it — or say why not. Always on a
/// thread of its own: waiting for a daemon must not stall the window that is
/// saying so.
fn start(app: &AppHandle) {
    let shell = app.state::<Shell>();
    if shell.starting.swap(true, Ordering::SeqCst) {
        return;
    }
    let outcome = bring_up(&shell);
    shell.starting.store(false, Ordering::SeqCst);
    match outcome {
        Ok(owned) => {
            shell.set(Status::ready());
            show(app, &daemon_url(shell.port));
            let app = app.clone();
            thread::spawn(move || watch(&app, owned));
        }
        Err(status) => {
            note(format!("{}: {}", status.title, status.detail));
            shell.set(status);
            show(app, &loading_url());
        }
    }
}

/// Whether the daemon that answered is this app's own.
fn bring_up(shell: &Shell) -> Result<bool, Status> {
    let port = shell.port;
    shell.set(Status::starting(format!("Looking for a daemon on port {port}.")));

    match settle(port) {
        Probe::Openmirror => {
            note(format!("attaching to the daemon already on port {port}; it is not this app's to stop"));
            return Ok(false);
        }
        Probe::Other(what) => {
            return Err(Status::failed(
                format!("Port {port} is taken"),
                format!(
                    "Something that is not openmirror is listening on 127.0.0.1:{port}: {what}. Stop it, or \
                     give openmirror another port with OPENMIRROR_PORT in {}.",
                    shell.settings.env_file.display()
                ),
                None,
            ))
        }
        Probe::Nothing => {}
    }

    shell.set(Status::starting("Starting the daemon."));
    let log = shell.daemon_log();
    let mut daemon = Daemon::spawn(&shell.home, &shell.settings, &log)
        .map_err(|why| Status::failed("openmirror could not start its daemon", why, Some(&log)))?;
    note(format!("started the daemon with {} (its output is in {})", daemon.program, log.display()));
    if let Some(path) = &daemon.path_note {
        note(path);
    }

    let began = Instant::now();
    loop {
        if let Some(status) = daemon.exited() {
            return Err(Status::failed(
                "The daemon stopped as it started",
                format!("It exited ({status}) before it answered. The end of its log is below."),
                Some(&log),
            ));
        }
        if daemon::probe(port) == Probe::Openmirror {
            break;
        }
        if began.elapsed() > START_LIMIT {
            let how = daemon.stop(STOP_GRACE);
            return Err(Status::failed(
                "The daemon did not answer",
                format!("It was still starting after {} seconds, and {how}.", START_LIMIT.as_secs()),
                Some(&log),
            ));
        }
        thread::sleep(Duration::from_millis(250));
    }
    note(format!("the daemon answered after {:.1}s", began.elapsed().as_secs_f32()));
    if let Ok(mut slot) = shell.daemon.lock() {
        *slot = Some(daemon);
    }
    Ok(true)
}

/// The port's answer, asked again when it is "something else": an openmirror
/// busy with something heavy can be slow to say what it is.
fn settle(port: u16) -> Probe {
    let mut answer = daemon::probe(port);
    for _ in 0..3 {
        if !matches!(answer, Probe::Other(_)) {
            break;
        }
        thread::sleep(Duration::from_millis(700));
        answer = daemon::probe(port);
    }
    answer
}

/// Keep an eye on the daemon, so that one which goes away takes the window
/// back to a page that says so, rather than leaving an interface that has
/// quietly stopped answering. A daemon of our own is watched through its
/// process; one we attached to only through its port — a bare connect, which
/// leaves no line in somebody else's log.
fn watch(app: &AppHandle, owned: bool) {
    let shell = app.state::<Shell>();
    let mut misses = 0;
    loop {
        thread::sleep(Duration::from_secs(if owned { 1 } else { 3 }));
        if shell.quitting.load(Ordering::SeqCst) {
            return;
        }
        let status = if owned {
            let exited = match shell.daemon.lock() {
                Ok(mut slot) => match slot.as_mut().map(Daemon::exited) {
                    None => return,
                    Some(None) => None,
                    Some(Some(status)) => {
                        *slot = None;
                        Some(status)
                    }
                },
                Err(_) => return,
            };
            let Some(status) = exited else { continue };
            Status::failed(
                "The daemon stopped",
                format!("It exited ({status}) while the app was open. The end of its log is below."),
                Some(&shell.daemon_log()),
            )
        } else {
            misses = if daemon::listening(shell.port) { 0 } else { misses + 1 };
            if misses < 2 {
                continue;
            }
            Status::failed(
                "The daemon stopped",
                "It was started outside this app, so its log is wherever that was. Try again starts one of the \
                 app's own.",
                None,
            )
        };
        if shell.quitting.load(Ordering::SeqCst) {
            return;
        }
        note(format!("{}: {}", status.title, status.detail));
        shell.set(status);
        show(app, &loading_url());
        return;
    }
}

/// Only ours. A daemon this app attached to keeps running, along with
/// whatever sessions it is holding.
fn stop_ours(app: &AppHandle) {
    let Some(shell) = app.try_state::<Shell>() else { return };
    shell.quitting.store(true, Ordering::SeqCst);
    let ours = shell.daemon.lock().ok().and_then(|mut slot| slot.take());
    if let Some(daemon) = ours {
        note("stopping the daemon this app started");
        let how = daemon.stop(STOP_GRACE);
        note(format!("the daemon {how}"));
    }
}

/// Talk and Live ask for the microphone in the daemon's page, and each
/// platform's webview answers differently. On macOS wry grants it and the
/// system asks you, once; on Windows, WebView2 asks with a prompt of its own.
/// On Linux nobody answers, and WebKitGTK's answer to a request nobody
/// answers is no — so voice heard nothing, with no error anywhere to say why.
/// Measured with WebKit's mock devices: `getUserMedia` was refused until this
/// answered, then granted. The microphone goes to the daemon's page and to
/// nothing else.
#[cfg(target_os = "linux")]
fn allow_microphone(window: &tauri::WebviewWindow, port: u16) -> tauri::Result<()> {
    let daemon = daemon_url(port).to_string();
    window.with_webview(move |webview| {
        use webkit2gtk::glib::prelude::*;
        use webkit2gtk::{PermissionRequestExt, SettingsExt, UserMediaPermissionRequest, WebViewExt};

        let view = webview.inner();
        if let Some(settings) = WebViewExt::settings(&view) {
            // On by default in current WebKitGTK; said anyway, for older ones.
            settings.set_enable_media_stream(true);
        }
        view.connect_permission_request(move |view, request| {
            let ours = view.uri().is_some_and(|uri| uri.starts_with(daemon.as_str()));
            if ours && request.is::<UserMediaPermissionRequest>() {
                request.allow();
                return true;
            }
            // Not answered here, so WebKit's own answer: no.
            false
        });
    })
}

/// Studio's "download" is a link with a `download` attribute. A browser saves
/// it; a webview does whatever its platform does, which on macOS is nothing
/// at all. So the app saves it where a browser would — the Downloads folder,
/// under a name that overwrites nothing — and says where it went.
fn download(app: &AppHandle, event: DownloadEvent<'_>) -> bool {
    match event {
        DownloadEvent::Requested { url, destination } => {
            let Ok(dir) = app.path().download_dir() else { return true };
            let name = destination
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .filter(|name| !name.is_empty())
                .or_else(|| url.path_segments().and_then(|mut s| s.next_back()).map(str::to_string))
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| "download".into());
            *destination = unique(&dir, &name);
            note(format!("saving {url} to {}", destination.display()));
        }
        DownloadEvent::Finished { url, path, success } => {
            let body = match (&path, success) {
                (Some(path), true) => format!("Saved {}", path.display()),
                _ => format!("Could not save {url}"),
            };
            note(&body);
            let _ = app.notification().builder().title("openmirror").body(body).show();
        }
        _ => {}
    }
    true
}

/// `dir/name`, or `dir/name (1)`, `(2)`… — the first that does not exist.
fn unique(dir: &Path, name: &str) -> PathBuf {
    let first = dir.join(name);
    if !first.exists() {
        return first;
    }
    let (stem, ext) = match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() => (stem, format!(".{ext}")),
        _ => (name, String::new()),
    };
    (1..)
        .map(|n| dir.join(format!("{stem} ({n}){ext}")))
        .find(|path| !path.exists())
        .expect("some name is free")
}

/// Install a downloaded update, and restart into it.
///
/// The one place this project runs something it fetched, and the shape of it
/// is forced by three operating systems that disagree about how to replace a
/// running application:
///
/// * **Windows** cannot overwrite a running `.exe` at all — the file is locked
///   by the process reading it. So the installer is started *and then* the app
///   quits, and the installer's own "close applications to continue" step
///   finishes it. Starting it first is the only ordering that works.
/// * **macOS** cannot move an `.app` bundle that is running, and the bundle
///   has to be replaced by a privileged move into `/Applications`. `open` on
///   the disk image is what the person would have done by hand, and it walks
///   them through it rather than trying to do it for them.
/// * **Linux** has no single answer. An AppImage is a file, so running the
///   new one and quitting the old one is the whole job. A `.deb` wants root,
///   so it is opened with whatever the desktop associates with it and the
///   person finishes.
///
/// The path comes from the daemon, which has already checked it against the
/// SHA256 published in the same release (see `openmirror/update.py`). This
/// function does not re-verify and does not need to: it is the same
/// filesystem and the same minute. It does check that the file is there, is a
/// regular file, and is inside the staging directory — a path from the web
/// layer is a path from a browser, and `sh -c` on one of those is the oldest
/// bug in this shape of program.
#[tauri::command]
fn apply_update(app: AppHandle, path: String) -> Result<String, String> {
    let installer = PathBuf::from(&path);

    if !installer.is_file() {
        return Err(format!("{path} is not there any more"));
    }
    // Reject a symlink: the daemon stages a regular file, so a symlink here
    // means something replaced it between the check and now.
    if installer.symlink_metadata().map(|m| m.file_type().is_symlink()).unwrap_or(false) {
        return Err("that is a link, not the download".into());
    }
    if !is_executable(&installer) {
        return Err(format!("{} is not marked runnable", installer.display()));
    }

    let name = installer.file_name().unwrap_or_default().to_string_lossy().to_lowercase();
    let extension = installer.extension().unwrap_or_default().to_string_lossy().to_lowercase();

    // Started before the quit on every platform, because on Windows the quit
    // is what releases the lock and on macOS the installer is a separate
    // process that outlives us either way. `Command::arg` returns `&mut
    // Command`, so each branch binds then returns rather than chaining.
    let mut launched = if cfg!(target_os = "windows") {
        if extension == "msi" {
            let mut c = std::process::Command::new("msiexec");
            c.arg("/i").arg(&installer).arg("/qb");
            c
        } else {
            std::process::Command::new(&installer)
        }
    } else if cfg!(target_os = "macos") {
        // A disk image or a bundle, mounted and opened: the person drags it
        // across, which is the step a script must not do for them.
        let mut c = std::process::Command::new("open");
        c.arg(&installer);
        c
    } else if extension == "appimage" {
        // The new build, run directly, after we let go of the old one.
        std::process::Command::new(&installer)
    } else {
        // A .deb or .rpm, opened with whatever the desktop associates with it.
        let mut c = std::process::Command::new("xdg-open");
        c.arg(&installer);
        c
    };

    if let Err(e) = launched.spawn() {
        // Not fatal. The file is on disk and verified; the person can run it,
        // and saying so beats a window that quits into nothing.
        let _ = app
            .notification()
            .builder()
            .title("openmirror")
            .body(format!("Could not start {name}: {e}. It is saved and ready to run."))
            .show();
        return Ok(format!("saved, but could not start it: {e}"));
    }

    if cfg!(target_os = "windows") || extension == "appimage" {
        // Only these two come back. Everything else is a person finishing
        // something, and quitting under them would look like a crash.
        relaunch(&app);
    }
    Ok(format!("{name} is running"))
}

/// Quit and start again. The window is restored by the new process; the
/// daemon is found again by the usual probe, which is the point of the
/// "find or start or stop" dance in `daemon.rs`.
fn relaunch(app: &AppHandle) {
    if let Ok(program) = std::env::current_exe() {
        let _ = std::process::Command::new(program)
            .spawn();
    }
    app.exit(0);
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(_path: &Path) -> bool {
    true
}

#[tauri::command]
fn status(shell: State<'_, Shell>) -> Status {
    shell.status.lock().map(|s| s.clone()).unwrap_or_else(|e| e.into_inner().clone())
}

#[tauri::command]
fn retry(app: AppHandle) {
    thread::spawn(move || start(&app));
}

#[tauri::command]
fn open_logs(app: AppHandle, shell: State<'_, Shell>) {
    open_logs_folder(&app, &shell);
}

fn open_logs_folder(app: &AppHandle, shell: &Shell) {
    let dir = shell.home.logs();
    if let Err(e) = app.opener().open_path(dir.to_string_lossy(), None::<&str>) {
        note(format!("could not open {}: {e}", dir.display()));
    }
}

fn main() {
    BEGAN.get_or_init(Instant::now);

    tauri::Builder::default()
        // First, so that a second launch hands over to this one and leaves
        // before it has started anything: one tray icon, one daemon.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| raise(app)))
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![status, retry, open_logs, apply_update])
        .setup(|app| {
            let home = Home::new(app.path().home_dir()?);
            std::fs::create_dir_all(home.logs())?;
            if let Ok(file) = daemon::open_log(&home.logs().join("desktop.log")) {
                let _ = LOG.set(Mutex::new(file));
            }
            let settings = Settings::load(&home);
            let port = settings.port();
            note(format!("openmirror desktop {}; daemon port {port}", env!("CARGO_PKG_VERSION")));

            app.manage(Shell {
                home,
                settings,
                port,
                status: Mutex::new(Status::starting("Looking for the daemon.")),
                daemon: Mutex::new(None),
                starting: AtomicBool::new(false),
                quitting: AtomicBool::new(false),
            });

            // The one thing the daemon's page may ask of the app: a
            // notification. Granted to that origin alone, and at run time
            // because the port is only known now.
            app.add_capability(
                CapabilityBuilder::new("daemon-page")
                    .remote(format!("http://127.0.0.1:{port}"))
                    .window("main")
                    .permission("notification:allow-is-permission-granted")
                    .permission("notification:allow-request-permission")
                    .permission("notification:allow-notify"),
            )?;

            let transparent = std::env::var("OPENMIRROR_TRANSPARENT")
                .map(|v| matches!(v.as_str(), "1" | "true" | "yes" | "on"))
                .unwrap_or(false);

            let navigating = app.handle().clone();
            let opening = app.handle().clone();
            let saving = app.handle().clone();
            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("openmirror")
                .inner_size(1200.0, 820.0)
                .min_inner_size(720.0, 520.0)
                // Transparency lets the page's own glass blur the real desktop
                // rather than a background it painted itself. It is opt-in
                // because it depends entirely on the compositor: on
                // COSMIC/Wayland a transparent window did not map at all, which
                // is indistinguishable from the app failing to start.
                .transparent(transparent)
                .on_navigation(move |url| {
                    if belongs_here(url, port) {
                        return true;
                    }
                    elsewhere(&navigating, url);
                    false
                })
                .on_new_window(move |url, _| {
                    elsewhere(&opening, &url);
                    NewWindowResponse::Deny
                })
                .on_download(move |_, event| download(&saving, event));
            if transparent {
                // Told, not sniffed: whether transparency worked is up to the
                // compositor, which the page cannot see, and a page dropping
                // its background without it would float on nothing.
                builder = builder.initialization_script(
                    "document.addEventListener('DOMContentLoaded', () => document.body.classList.add('native'))",
                );
            }
            let window = builder.build()?;

            #[cfg(target_os = "linux")]
            allow_microphone(&window, port)?;

            // Closing the window hides it rather than quitting. An agent
            // mid-build should not be stopped by someone tidying their
            // desktop; Quit in the tray is the way out.
            let hidden = window.clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = hidden.hide();
                }
            });

            let show_item = MenuItem::with_id(app, "show", "Show openmirror", true, None::<&str>)?;
            let logs_item = MenuItem::with_id(app, "logs", "Open the logs folder", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit openmirror", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &logs_item, &quit_item])?;
            let icon = app.default_window_icon().cloned().ok_or("the app has no icon")?;

            TrayIconBuilder::new()
                .icon(icon)
                .tooltip("openmirror")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => raise(app),
                    "logs" => open_logs_folder(app, &app.state::<Shell>()),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    // Left click raises the window, which is what every tray
                    // application does and what people expect without being told.
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        raise(tray.app_handle());
                    }
                })
                .build(app)?;

            let handle = app.handle().clone();
            thread::spawn(move || start(&handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the openmirror app")
        .run(|app, event| match event {
            RunEvent::Exit => stop_ours(app),
            // The Dock icon, clicked while the window is hidden: every Mac
            // app shows its window again, and a hidden one with no other way
            // back than the menu bar would look like it had not opened.
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => raise(app),
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_loading_page_and_the_daemon_stay_in_the_window() {
        let here = |url: &str| belongs_here(&url.parse().unwrap(), 8477);
        assert!(here("tauri://localhost/index.html"));
        assert!(here("http://tauri.localhost/index.html"));
        assert!(here("http://127.0.0.1:8477/"));
        assert!(here("http://localhost:8477/static/app.js"));
        assert!(here("about:blank"));

        assert!(!here("http://127.0.0.1:9000/"), "another port is another server");
        assert!(!here("https://127.0.0.1:8477/"), "the daemon does not serve https");
        assert!(!here("https://github.com/notquiteog/openmirror"));
        assert!(!here("file:///etc/passwd"));
        assert!(!here("data:text/html,<h1>hi</h1>"));
    }

    #[test]
    fn an_update_path_is_refused_before_anything_is_run() {
        /* The path arrives from the web layer, which means from a browser. A
           command built out of one of those is the oldest bug in this shape of
           program, so the refusals are here to be read rather than trusted. */
        let missing = std::path::Path::new("/definitely/not/here.AppImage");
        assert!(!missing.is_file(), "the test is only meaningful if it is absent");

        // A symlink is refused even though `is_file` follows it and says yes:
        // the daemon stages a regular file, so a link here means something
        // replaced it between the checksum and now.
        let dir = std::env::temp_dir().join("openmirror-update-test");
        let _ = std::fs::create_dir_all(&dir);
        let real = dir.join("real.bin");
        let link = dir.join("link.bin");
        std::fs::write(&real, b"x").unwrap();
        #[cfg(unix)]
        {
            let _ = std::fs::remove_file(&link);
            std::os::unix::fs::symlink(&real, &link).unwrap();
            assert!(link.is_file(), "a link to a file is still a file, which is the trap");
            assert!(link.symlink_metadata().unwrap().file_type().is_symlink());
        }
        let _ = std::fs::remove_file(&real);
        let _ = std::fs::remove_file(&link);
        let _ = std::fs::remove_dir(&dir);
    }

    #[test]
    fn a_download_never_overwrites() {
        let dir = std::env::temp_dir().join(format!("openmirror-unique-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(unique(&dir, "cat.png"), dir.join("cat.png"));
        std::fs::write(dir.join("cat.png"), b"").unwrap();
        assert_eq!(unique(&dir, "cat.png"), dir.join("cat (1).png"));
        std::fs::write(dir.join("cat (1).png"), b"").unwrap();
        assert_eq!(unique(&dir, "cat.png"), dir.join("cat (2).png"));
        std::fs::write(dir.join("notes"), b"").unwrap();
        assert_eq!(unique(&dir, "notes"), dir.join("notes (1)"));
        std::fs::write(dir.join(".env"), b"").unwrap();
        assert_eq!(unique(&dir, ".env"), dir.join(".env (1)"));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
