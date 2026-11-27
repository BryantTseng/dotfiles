use crate::config::Config;
use crate::session;
use anyhow::{Context, Result};
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::mpsc::{self, Receiver, Sender},
    thread,
};

const SUBAGENT_LIST: &str = "Subagents & Tasks";
const SUBAGENT_PREFIX: &str = "[subagent] ";

#[derive(Clone, Debug)]
pub struct Subagent {
    pub name: String,
    pub done: bool,
}

#[derive(Clone, Debug)]
pub struct Card {
    pub name: String,
    pub labels: Vec<String>,
    pub session: Option<String>,
    pub url: String,
    pub subagents: Vec<Subagent>,
}

#[derive(Clone, Debug)]
pub struct Column {
    pub name: String,
    pub cards: Vec<Card>,
}

#[derive(Clone, Debug, Default)]
pub struct Board {
    pub name: String,
    pub columns: Vec<Column>,
}

pub fn spawn(cfg: Config) -> (Sender<()>, Receiver<Result<Board, String>>) {
    let (req_tx, req_rx) = mpsc::channel::<()>();
    let (upd_tx, upd_rx) = mpsc::channel::<Result<Board, String>>();
    thread::spawn(move || {
        let client = Client { cfg };
        for () in req_rx {
            if upd_tx.send(client.board().map_err(|e| format!("{e:#}"))).is_err() {
                break;
            }
        }
    });
    (req_tx, upd_rx)
}

struct Client {
    cfg: Config,
}

impl Client {
    fn get(&self, path: &str) -> Result<Value> {
        let url = format!("{}/api{}", self.cfg.base_url, path);
        ureq::get(&url)
            .set("X-Api-Key", &self.cfg.api_key)
            .timeout(std::time::Duration::from_secs(10))
            .call()
            .with_context(|| format!("GET {path}"))?
            .into_json()
            .with_context(|| format!("decoding {path}"))
    }

    fn board(&self) -> Result<Board> {
        let projects = self.get("/projects")?;
        let boards = array(&projects["included"]["boards"]);
        let wanted = [self.cfg.trigger_label.to_lowercase(), "agy".to_string()];
        let chosen = wanted
            .iter()
            .find_map(|w| {
                boards
                    .iter()
                    .find(|b| b["name"].as_str().map(|n| n.to_lowercase()) == Some(w.clone()))
            })
            .or_else(|| boards.first())
            .context("no Planka boards found")?;

        let board_id = id_str(&chosen["id"]);
        let bundle = self.get(&format!("/boards/{board_id}"))?;
        let inc = &bundle["included"];

        let labels: HashMap<String, String> = array(&inc["labels"])
            .iter()
            .map(|l| (id_str(&l["id"]), str_of(&l["name"])))
            .collect();
        let mut card_labels: HashMap<String, Vec<String>> = HashMap::new();
        for cl in array(&inc["cardLabels"]) {
            if let Some(name) = labels.get(&id_str(&cl["labelId"])).filter(|n| !n.is_empty()) {
                card_labels.entry(id_str(&cl["cardId"])).or_default().push(name.clone());
            }
        }

        let subagent_lists: HashMap<String, String> = array(&inc["taskLists"])
            .iter()
            .filter(|tl| tl["name"].as_str() == Some(SUBAGENT_LIST))
            .map(|tl| (id_str(&tl["id"]), id_str(&tl["cardId"])))
            .collect();
        let mut tasks: Vec<&Value> = array(&inc["tasks"]).iter().collect();
        tasks.sort_by_key(|t| position(t));
        let mut subagents: HashMap<String, Vec<Subagent>> = HashMap::new();
        for t in tasks {
            if let Some(card_id) = subagent_lists.get(&id_str(&t["taskListId"])) {
                let raw = str_of(&t["name"]);
                let name = raw.strip_prefix(SUBAGENT_PREFIX).unwrap_or(&raw).to_string();
                subagents
                    .entry(card_id.clone())
                    .or_default()
                    .push(Subagent {
                        name,
                        done: t["isCompleted"].as_bool().unwrap_or(false),
                    });
            }
        }

        let mut lists: Vec<&Value> = array(&inc["lists"])
            .iter()
            .filter(|l| matches!(l["type"].as_str(), Some("active" | "closed")))
            .filter(|l| !str_of(&l["name"]).is_empty())
            .collect();
        lists.sort_by_key(|l| position(l));

        let mut cards_by_list: HashMap<String, Vec<&Value>> = HashMap::new();
        for c in array(&inc["cards"]) {
            cards_by_list.entry(id_str(&c["listId"])).or_default().push(c);
        }

        let columns = lists
            .iter()
            .map(|l| {
                let lid = id_str(&l["id"]);
                let mut cards = cards_by_list.remove(&lid).unwrap_or_default();
                cards.sort_by_key(|c| position(c));
                Column {
                    name: str_of(&l["name"]),
                    cards: cards
                        .iter()
                        .map(|c| {
                            let id = id_str(&c["id"]);
                            Card {
                                url: format!("{}/cards/{}", self.cfg.base_url, id),
                                labels: card_labels.get(&id).cloned().unwrap_or_default(),
                                session: session::session_id(&str_of(&c["description"])),
                                subagents: subagents.get(&id).cloned().unwrap_or_default(),
                                name: str_of(&c["name"]),
                            }
                        })
                        .collect(),
                }
            })
            .collect();

        Ok(Board {
            name: str_of(&chosen["name"]),
            columns,
        })
    }
}

fn array(v: &Value) -> &[Value] {
    v.as_array().map(Vec::as_slice).unwrap_or(&[])
}

fn str_of(v: &Value) -> String {
    v.as_str().unwrap_or_default().to_string()
}

fn id_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

fn position(v: &Value) -> i64 {
    v["position"].as_f64().map(|p| p as i64).unwrap_or(0)
}
