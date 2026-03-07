use std::sync::Mutex;
use tauri::Manager;

struct BackendState {
    python_process: Option<u32>,
}

#[tauri::command]
fn get_backend_port() -> u16 {
    8765
}

#[tauri::command]
fn start_backend(state: tauri::State<'_, Mutex<BackendState>>) -> Result<String, String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if s.python_process.is_some() {
        return Ok("Backend already running".into());
    }

    // Resolve project root: in dev the CWD is src-tauri/, in prod use the exe's parent
    let project_root = std::env::current_dir()
        .unwrap()
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| std::env::current_dir().unwrap());

    let child = std::process::Command::new("python")
        .args(["-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", "8765"])
        .current_dir(&project_root)
        .spawn()
        .map_err(|e| format!("Failed to start backend: {}", e))?;

    s.python_process = Some(child.id());
    Ok(format!("Backend started with PID {}", child.id()))
}

#[tauri::command]
fn stop_backend(state: tauri::State<'_, Mutex<BackendState>>) -> Result<String, String> {
    let mut s = state.lock().map_err(|e| e.to_string())?;
    if let Some(pid) = s.python_process.take() {
        #[cfg(target_os = "windows")]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/F", "/T", "/PID", &pid.to_string()])
                .output();
        }
        #[cfg(not(target_os = "windows"))]
        {
            let _ = std::process::Command::new("kill")
                .arg(pid.to_string())
                .output();
        }
        Ok("Backend stopped".into())
    } else {
        Ok("Backend was not running".into())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(Mutex::new(BackendState {
            python_process: None,
        }))
        .invoke_handler(tauri::generate_handler![
            get_backend_port,
            start_backend,
            stop_backend
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<Mutex<BackendState>>();
                let pid = {
                    state.lock().ok().and_then(|mut s| s.python_process.take())
                };
                if let Some(pid) = pid {
                    #[cfg(target_os = "windows")]
                    {
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/T", "/PID", &pid.to_string()])
                            .output();
                    }
                    #[cfg(not(target_os = "windows"))]
                    {
                        let _ = std::process::Command::new("kill")
                            .arg(pid.to_string())
                            .output();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
