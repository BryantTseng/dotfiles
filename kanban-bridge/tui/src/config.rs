use anyhow::{bail, Context, Result};
use std::{collections::HashMap, fs, path::PathBuf};

#[derive(Clone, Debug)]
pub struct Config {
    pub base_url: String,
    pub api_key: String,
    pub trigger_label: String,
}

impl Config {
    pub fn load() -> Result<Self> {
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
        let path = home.join(".config/kanban-bridge/env");
        let text = fs::read_to_string(&path)
            .with_context(|| format!("reading {}", path.display()))?;
        let vars = parse_env(&text);

        let api_key = vars.get("PLANKA_API_KEY").cloned().unwrap_or_default();
        if api_key.is_empty() {
            bail!("PLANKA_API_KEY is missing in {}", path.display());
        }

        Ok(Self {
            base_url: vars
                .get("PLANKA_URL")
                .cloned()
                .unwrap_or_else(|| "http://localhost:3000".into())
                .trim_end_matches('/')
                .to_string(),
            api_key,
            trigger_label: vars
                .get("BRIDGE_TRIGGER_LABEL")
                .cloned()
                .unwrap_or_else(|| "agy".into()),
        })
    }
}

fn parse_env(text: &str) -> HashMap<String, String> {
    text.lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (key, value) = line.split_once('=')?;
            let value = value.trim().trim_matches(|c| c == '"' || c == '\'');
            Some((key.trim().to_string(), value.to_string()))
        })
        .collect()
}
