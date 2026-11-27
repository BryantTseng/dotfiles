use crate::{
    app::App,
    planka::{Card, Column, Subagent},
    session::now_ms,
};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Style, Stylize},
    text::{Line, Span},
    widgets::{Block, List, ListItem, ListState, Paragraph, Wrap},
    Frame,
};
use std::collections::BTreeMap;

const IDLE_AFTER_MS: u64 = 120_000;

pub fn draw(f: &mut Frame, app: &App) {
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(1),
    ])
    .areas(f.area());

    draw_header(f, app, header);

    let panes = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(20),
            Constraint::Percentage(35),
            Constraint::Percentage(45),
        ])
        .split(body);
    let right = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(45), Constraint::Min(0)])
        .split(panes[2]);

    draw_columns(f, app, panes[0]);
    draw_cards(f, app, panes[1]);
    draw_stats(f, app, right[0]);
    draw_live(f, app, right[1]);

    let keys = "Tab/Shift+Tab column  ↑/↓ or j/k card  o open  r refresh  q quit";
    f.render_widget(Paragraph::new(keys).dark_gray(), footer);
}

fn draw_header(f: &mut Frame, app: &App, area: Rect) {
    let mut spans = vec![Span::styled(
        format!(" {} ", app.board.name),
        Style::default().bold().reversed(),
    )];
    spans.push(format!(" {} cards", app.card_count()).into());
    if let Some(at) = app.updated_at {
        spans.push(format!("  updated {}s ago", at.elapsed().as_secs()).dark_gray());
    }
    if !app.loaded {
        spans.push("  connecting…".yellow());
    }
    if let Some(err) = &app.error {
        spans.push(format!("  ✗ {err}").red());
    }
    f.render_widget(Paragraph::new(Line::from(spans)), area);
}

fn draw_columns(f: &mut Frame, app: &App, area: Rect) {
    let items: Vec<ListItem> = app
        .board
        .columns
        .iter()
        .map(|c| {
            ListItem::new(Line::from(vec![
                Span::styled(c.name.clone(), column_color(&c.name)),
                format!(" ({})", c.cards.len()).dark_gray(),
            ]))
        })
        .collect();

    let list = List::new(items)
        .block(Block::bordered().title(" Columns "))
        .highlight_style(Style::default().bold().reversed())
        .highlight_symbol("▶ ");

    let mut state = ListState::default().with_selected(Some(app.col));
    f.render_stateful_widget(list, area, &mut state);
}

fn draw_cards(f: &mut Frame, app: &App, area: Rect) {
    let Some(column) = app.board.columns.get(app.col) else {
        f.render_widget(Block::bordered().title(" Cards "), area);
        return;
    };

    let items: Vec<ListItem> = column
        .cards
        .iter()
        .map(|c| {
            let mut title = vec![Span::raw(c.name.clone())];
            if c.session.is_some() {
                title.push(" ●".green());
            }
            if !c.subagents.is_empty() {
                let running = running_count(&c.subagents);
                title.push(format!("  {} sub", c.subagents.len()).cyan());
                if running > 0 {
                    title.push(format!(" ({running} running)").yellow());
                }
            }
            let labels = if c.labels.is_empty() {
                Line::from("")
            } else {
                Line::from(format!("  {}", c.labels.join(", "))).dark_gray()
            };
            ListItem::new(vec![Line::from(title), labels])
        })
        .collect();

    let list = List::new(items)
        .block(Block::bordered().title(format!(" {} ", column.name)))
        .highlight_style(Style::default().bold().reversed())
        .highlight_symbol("▶ ");

    let row = app.rows.get(app.col).copied().unwrap_or(0);
    let mut state = ListState::default().with_selected(Some(row));
    f.render_stateful_widget(list, area, &mut state);
}

fn draw_stats(f: &mut Frame, app: &App, area: Rect) {
    let block = Block::bordered().title(" Stats ");
    let all: Vec<&Subagent> = app
        .board
        .columns
        .iter()
        .flat_map(|c| &c.cards)
        .flat_map(|c| &c.subagents)
        .collect();
    let total = all.len();
    let running = running_count(all.iter().copied());

    let mut lines = vec![
        heading("Board"),
        kv("Cards", app.card_count().to_string()),
        kv("Subagents spawned", total.to_string()),
        kv("  running", running.to_string()),
        kv("  done", (total - running).to_string()),
        Line::raw(""),
        heading("Columns"),
        Line::from(format!("{:<14}{:>7}{:>6}{:>9}", "", "cards", "sub", "running")).dark_gray(),
    ];

    for column in &app.board.columns {
        lines.push(column_row(column));
    }

    let models = model_usage(app);
    if !models.is_empty() {
        lines.push(Line::raw(""));
        lines.push(heading("Models"));
        for (name, count) in models {
            lines.push(kv(&name, count.to_string()));
        }
    }

    f.render_widget(
        Paragraph::new(lines).block(block).wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_live(f: &mut Frame, app: &App, area: Rect) {
    let block = Block::bordered().title(" Live ");
    let Some(card) = app.selected_card() else {
        f.render_widget(Paragraph::new("No card selected.").block(block), area);
        return;
    };
    let Some(session) = card.session.as_deref() else {
        let text = format!("{}\n\nNo agent session yet.", card.name);
        f.render_widget(Paragraph::new(text).block(block).wrap(Wrap { trim: false }), area);
        return;
    };

    let models: Vec<&str> = card.labels.iter().filter_map(|l| l.strip_prefix("model:")).collect();
    let mut lines = vec![
        heading(&card.name),
        kv("Session", session.to_string()),
        kv(
            "Model",
            if models.is_empty() { "unknown".to_string() } else { models.join(", ") },
        ),
    ];

    match &app.live {
        None => lines.push("No log yet.".dark_gray().into()),
        Some(live) => {
            let now = now_ms();
            let idle_ms = live.last_ms.map(|last| now.saturating_sub(last)).unwrap_or(0);
            let status = if live.turn_ended {
                "turn ended".to_string()
            } else if idle_ms > IDLE_AFTER_MS {
                format!("idle, no events for {}", fmt_dur(idle_ms))
            } else {
                "working".to_string()
            };
            lines.push(kv("Status", status));
            if let Some(start) = live.started_ms {
                lines.push(kv("Running for", fmt_dur(now.saturating_sub(start))));
            }
            if live.last_ms.is_some() {
                lines.push(kv("Last activity", format!("{} ago", fmt_dur(idle_ms))));
            }
            lines.push(kv("Steps", live.steps.to_string()));
            lines.push(kv(
                "Tokens in/out",
                format!("{} / {}", live.input_tokens, live.output_tokens),
            ));
            lines.push(kv("Cost", format!("${:.4}", live.cost)));

            lines.push(Line::raw(""));
            lines.push(heading("Now"));
            match &live.tool {
                Some(tool) => lines.push(Line::from(vec![
                    Span::styled(format!("{} ", tool.name), Style::default().cyan().bold()),
                    format!("[{}] ", tool.status).dark_gray(),
                    Span::raw(tool.detail.clone()),
                ])),
                None => lines.push("no tool calls yet".dark_gray().into()),
            }
            if let Some(text) = &live.last_text {
                lines.push(Line::raw(""));
                lines.push(heading("Last message"));
                lines.push(Line::from(text.chars().take(240).collect::<String>()).dark_gray());
            }
            if let Some(err) = &live.error {
                lines.push(Line::raw(""));
                lines.push(Line::from(format!("error: {err}")).red());
            }
        }
    }

    lines.push(Line::raw(""));
    lines.push(heading(&format!("Subagents ({})", card.subagents.len())));
    lines.extend(card_subagents(card));

    f.render_widget(
        Paragraph::new(lines).block(block).wrap(Wrap { trim: false }),
        area,
    );
}

fn column_row(column: &Column) -> Line<'static> {
    let subs: Vec<&Subagent> = column.cards.iter().flat_map(|c| &c.subagents).collect();
    let running = running_count(subs.iter().copied());
    Line::from(vec![
        Span::styled(format!("{:<14}", column.name), column_color(&column.name)),
        format!("{:>7}", column.cards.len()).into(),
        format!("{:>6}", subs.len()).into(),
        format!("{:>9}", running).yellow(),
    ])
}

fn card_subagents(card: &Card) -> Vec<Line<'static>> {
    if card.subagents.is_empty() {
        return vec!["No subagents spawned.".dark_gray().into()];
    }
    let running = running_count(&card.subagents);
    let mut lines = vec![Line::from(format!(
        "{} spawned · {} running · {} done",
        card.subagents.len(),
        running,
        card.subagents.len() - running
    ))];
    for sub in &card.subagents {
        let (mark, style) = if sub.done {
            ("✓ ", Style::default().fg(Color::Green))
        } else {
            ("● ", Style::default().fg(Color::Yellow))
        };
        lines.push(Line::from(vec![
            Span::styled(mark, style),
            Span::styled(sub.name.clone(), style),
        ]));
    }
    lines
}

fn model_usage(app: &App) -> Vec<(String, usize)> {
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for card in app.board.columns.iter().flat_map(|c| &c.cards) {
        for label in card.labels.iter().filter_map(|l| l.strip_prefix("model:")) {
            *counts.entry(label.to_string()).or_default() += 1;
        }
    }
    let mut out: Vec<(String, usize)> = counts.into_iter().collect();
    out.sort_by_key(|(_, count)| std::cmp::Reverse(*count));
    out
}

fn running_count<'a>(items: impl IntoIterator<Item = &'a Subagent>) -> usize {
    items.into_iter().filter(|s| !s.done).count()
}

fn fmt_dur(ms: u64) -> String {
    let s = ms / 1000;
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{}m {:02}s", s / 60, s % 60),
        _ => format!("{}h {:02}m", s / 3600, (s % 3600) / 60),
    }
}

fn heading(text: &str) -> Line<'static> {
    Line::from(text.to_string()).bold()
}

fn kv(label: &str, value: String) -> Line<'static> {
    Line::from(vec![format!("{label:<20}").into(), value.bold()])
}

fn column_color(name: &str) -> Style {
    let color = match name {
        "Need Action" => Color::Yellow,
        "Doing" => Color::Cyan,
        "Pending" => Color::Green,
        "Auditing" => Color::Magenta,
        "Flagged" => Color::Red,
        _ => Color::Reset,
    };
    Style::default().fg(color)
}
