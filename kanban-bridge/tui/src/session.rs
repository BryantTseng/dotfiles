use serde_json::Value;
use std::{
    fs::File,
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};

const TAIL_BYTES: u64 = 2 * 1024 * 1024;

pub fn session_id(description: &str) -> Option<String> {
    let idx = description.rfind("-session:")?;
    let rest = &description[idx + "-session:".len()..];
    let id: String = rest
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
        .collect();
    (!id.is_empty()).then_some(id)
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[derive(Clone, Debug)]
pub struct Tool {
    pub name: String,
    pub detail: String,
    pub status: String,
}

#[derive(Clone, Debug, Default)]
pub struct Live {
    pub started_ms: Option<u64>,
    pub last_ms: Option<u64>,
    pub steps: usize,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cost: f64,
    pub turn_ended: bool,
    pub tool: Option<Tool>,
    pub last_text: Option<String>,
    pub error: Option<String>,
}

pub fn read(session_id: &str) -> Option<Live> {
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    let path = home
        .join(".local/share/kanban-bridge/logs")
        .join(format!("{session_id}.jsonl"));
    let mut file = File::open(path).ok()?;
    let len = file.metadata().ok()?.len();

    let mut live = Live::default();
    let mut first = String::new();
    BufReader::new(&mut file).read_line(&mut first).ok()?;
    live.started_ms = serde_json::from_str::<Value>(&first)
        .ok()
        .and_then(|v| v["timestamp"].as_u64());

    let partial = len > TAIL_BYTES;
    file.seek(SeekFrom::Start(len.saturating_sub(TAIL_BYTES))).ok()?;
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).ok()?;
    let text = String::from_utf8_lossy(&buf);
    let mut lines = text.lines();
    if partial {
        lines.next();
    }
    for line in lines {
        if let Ok(ev) = serde_json::from_str::<Value>(line) {
            live.apply(&ev);
        }
    }
    Some(live)
}

impl Live {
    fn apply(&mut self, ev: &Value) {
        if let Some(ts) = ev["timestamp"].as_u64() {
            self.last_ms = Some(ts);
        }
        let part = &ev["part"];
        match ev["type"].as_str() {
            Some("step_start") => {
                self.steps += 1;
                self.turn_ended = false;
                self.error = None;
            }
            Some("step_finish") => {
                self.turn_ended = part["reason"].as_str() == Some("stop");
                self.input_tokens += part["tokens"]["input"].as_u64().unwrap_or(0);
                self.output_tokens += part["tokens"]["output"].as_u64().unwrap_or(0);
                self.cost += part["cost"].as_f64().unwrap_or(0.0);
            }
            Some("tool_use") => {
                let state = &part["state"];
                let name = part["tool"].as_str().unwrap_or("?").to_string();
                self.tool = Some(Tool {
                    detail: describe(&name, &state["input"]),
                    status: state["status"].as_str().unwrap_or_default().to_string(),
                    name,
                });
            }
            Some("text") => {
                if let Some(line) = part["text"]
                    .as_str()
                    .and_then(|t| t.lines().find(|l| !l.trim().is_empty()))
                {
                    self.last_text = Some(line.trim().to_string());
                }
            }
            Some("error") => {
                self.error = ev["error"]["message"].as_str().map(str::to_string);
            }
            _ => {}
        }
    }
}

fn describe(name: &str, input: &Value) -> String {
    let pick = |key: &str| input[key].as_str().unwrap_or_default();
    let raw = match name {
        "shell" | "bash" => pick("command").to_string(),
        "subagent" | "task" => format!("{}: {}", pick("agent"), pick("description")),
        _ if !pick("path").is_empty() => pick("path").to_string(),
        _ => pick("pattern").to_string(),
    };
    raw.lines().next().unwrap_or_default().chars().take(100).collect()
}
