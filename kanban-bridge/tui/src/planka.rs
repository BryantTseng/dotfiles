use crate::config::Config;
use anyhow::{Context, Result};
use serde_json::Value;
use std::{
    collections::HashMap,
    sync::mpsc::{self, Receiver, Sender},
    thread,
};

#[derive(Clone, Debug)]
pub struct Card {
    pub id: String,
    pub name: String,
    pub labels: Vec<String>,
    pub has_session: bool,
    pub url: String,
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

#[derive(Clone, Debug)]
pub struct Task {
    pub text: String,
    pub done: bool,
}

#[derive(Clone, Debug)]
pub struct Checklist {
    pub name: String,
    pub tasks: Vec<Task>,
}

pub enum Request {
    Board,
    Card(String),
}

pub enum Update {
    Board(Result<Board, String>),
    Checklists(String, Result<Vec<Checklist>, String>),
}

pub fn spawn(cfg: Config) -> (Sender<Request>, Receiver<Update>) {
    let (req_tx, req_rx) = mpsc::channel::<Request>();
    let (upd_tx, upd_rx) = mpsc::channel::<Update>();
    thread::spawn(move || {
        let client = Client { cfg };
        for req in req_rx {
            let update = match req {
                Request::Board => Update::Board(client.board().map_err(fmt_err)),
                Request::Card(id) => {
                    let res = client.checklists(&id).map_err(fmt_err);
                    Update::Checklists(id, res)
                }
            };
            if upd_tx.send(update).is_err() {
                break;
            }
        }
    });
    (req_tx, upd_rx)
}

fn fmt_err(e: anyhow::Error) -> String {
    format!("{e:#}")
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
        let boards = projects["included"]["boards"].as_array().cloned().unwrap_or_default();
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
                                has_session: has_session_tag(&str_of(&c["description"])),
                                name: str_of(&c["name"]),
                                id,
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

    fn checklists(&self, card_id: &str) -> Result<Vec<Checklist>> {
        let data = self.get(&format!("/cards/{card_id}"))?;
        let inc = &data["included"];

        let mut lists: Vec<&Value> = array(&inc["taskLists"]).iter().collect();
        lists.sort_by_key(|l| position(l));
        let mut tasks: Vec<&Value> = array(&inc["tasks"]).iter().collect();
        tasks.sort_by_key(|t| position(t));

        Ok(lists
            .iter()
            .map(|tl| {
                let tlid = id_str(&tl["id"]);
                Checklist {
                    name: str_of(&tl["name"]),
                    tasks: tasks
                        .iter()
                        .filter(|t| id_str(&t["taskListId"]) == tlid)
                        .map(|t| Task {
                            text: str_of(&t["name"]),
                            done: t["isCompleted"].as_bool().unwrap_or(false),
                        })
                        .collect(),
                }
            })
            .collect())
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

fn has_session_tag(description: &str) -> bool {
    description.contains("<!--") && description.contains("-session:")
}
