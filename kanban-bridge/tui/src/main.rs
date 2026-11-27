mod app;
mod config;
mod planka;
mod ui;

use anyhow::Result;
use app::App;
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use planka::Request;
use ratatui::DefaultTerminal;
use std::{
    sync::mpsc::{Receiver, Sender},
    time::{Duration, Instant},
};

const POLL_INTERVAL: Duration = Duration::from_secs(3);

fn main() -> Result<()> {
    let cfg = config::Config::load()?;
    let (tx, rx) = planka::spawn(cfg.clone());

    let mut terminal = ratatui::init();
    let result = run(&mut terminal, tx, rx);
    ratatui::restore();
    result
}

fn run(
    terminal: &mut DefaultTerminal,
    tx: Sender<Request>,
    rx: Receiver<planka::Update>,
) -> Result<()> {
    let mut app = App::default();
    let mut last_poll = Instant::now() - POLL_INTERVAL;
    let mut last_selected: Option<String> = None;

    loop {
        if last_poll.elapsed() >= POLL_INTERVAL {
            tx.send(Request::Board)?;
            if let Some(id) = app.selected_id() {
                tx.send(Request::Card(id))?;
            }
            last_poll = Instant::now();
        }

        while let Ok(update) = rx.try_recv() {
            app.apply(update);
        }

        let selected = app.selected_id();
        if selected != last_selected {
            if let Some(id) = &selected {
                tx.send(Request::Card(id.clone()))?;
            }
            last_selected = selected;
        }

        terminal.draw(|f| ui::draw(f, &app))?;

        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(key) = event::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                match key.code {
                    KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                    KeyCode::Tab => app.move_col(1),
                    KeyCode::BackTab => app.move_col(-1),
                    KeyCode::Char('k') | KeyCode::Up => app.move_row(-1),
                    KeyCode::Char('j') | KeyCode::Down => app.move_row(1),
                    KeyCode::Char('r') => last_poll = Instant::now() - POLL_INTERVAL,
                    KeyCode::Char('o') => {
                        if let Some(card) = app.selected_card() {
                            let _ = std::process::Command::new("xdg-open").arg(&card.url).spawn();
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}
