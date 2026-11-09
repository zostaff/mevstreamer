//! Unix domain socket server: newline-delimited JSON in, newline-delimited JSON out.

use crate::types::{Response, Result, Signal};
use crate::Executor;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::mpsc;

/// Remove a stale socket file, bind, and restrict access to the current user.
pub fn bind(path: &str) -> Result<UnixListener> {
    let p = Path::new(path);
    if p.exists() {
        // A live socket means another executor is trading this wallet — two would keep
        // separate position books (double buys, limits ignored).
        if std::os::unix::net::UnixStream::connect(p).is_ok() {
            return Err(format!("another executor is already listening on {path} — refusing to start a second one").into());
        }
        std::fs::remove_file(p).map_err(|e| format!("remove stale socket {path}: {e}"))?;
        log_warn!("removed stale socket {path}");
    }
    let listener = UnixListener::bind(p).map_err(|e| format!("bind {path}: {e}"))?;
    // Anyone who can connect can trigger trades — owner only.
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

pub async fn serve(listener: UnixListener, exec: Arc<Executor>) {
    loop {
        match listener.accept().await {
            Ok((stream, _)) => {
                log_info!("IPC client connected");
                tokio::spawn(handle_conn(stream, Arc::clone(&exec)));
            }
            Err(e) => log_error!("IPC accept failed: {e}"),
        }
    }
}

async fn handle_conn(stream: UnixStream, exec: Arc<Executor>) {
    let (rd, mut wr) = stream.into_split();
    // Signals on one connection are processed concurrently; a single writer
    // task serialises replies so lines never interleave.
    let (tx, mut rx) = mpsc::unbounded_channel::<Response>();
    let writer = tokio::spawn(async move {
        while let Some(resp) = rx.recv().await {
            let mut line = serde_json::to_vec(&resp).unwrap_or_else(|_| br#"{"status":"error","message":"serialize"}"#.to_vec());
            line.push(b'\n');
            if wr.write_all(&line).await.is_err() {
                break;
            }
        }
    });

    let mut lines = BufReader::new(rd).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_str::<Signal>(line) {
                    Ok(sig) => {
                        let exec = Arc::clone(&exec);
                        let tx = tx.clone();
                        tokio::spawn(async move {
                            let resp = exec.handle_signal(sig).await;
                            let _ = tx.send(resp);
                        });
                    }
                    Err(e) => {
                        log_warn!("bad signal JSON: {e}");
                        let _ = tx.send(Response::error(format!("invalid JSON: {e}")));
                    }
                }
            }
            Ok(None) => break,
            Err(e) => {
                log_warn!("IPC read error: {e}");
                break;
            }
        }
    }
    drop(tx);
    let _ = writer.await; // flush replies for signals still in flight
    log_info!("IPC client disconnected");
}
