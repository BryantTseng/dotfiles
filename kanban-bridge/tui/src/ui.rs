use crate::app::App;
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Style, Stylize},
    text::{Line, Span},
    widgets::{Block, List, ListItem, ListState, Paragraph, Wrap},
    Frame,
};

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

    draw_columns(f, app, panes[0]);
    draw_cards(f, app, panes[1]);
    draw_detail(f, app, panes[2]);

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
            if c.has_session {
                title.push(" ●".green());
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

fn draw_detail(f: &mut Frame, app: &App, area: Rect) {
    let block = Block::bordered().title(" Detail ");
    let Some(card) = app.selected_card() else {
        f.render_widget(Paragraph::new("No card selected.").block(block), area);
        return;
    };

    let mut lines = vec![
        Line::from(card.name.clone()).bold(),
        Line::from(format!("column: {}", app.board.columns[app.col].name)).dark_gray(),
        Line::from(format!("labels: {}", card.labels.join(", "))).dark_gray(),
        Line::from(card.url.clone()).dark_gray(),
        Line::raw(""),
    ];

    match app.checklists.get(&card.id) {
        None => lines.push("loading subtasks…".dark_gray().into()),
        Some(lists) if lists.is_empty() => lines.push("No subtasks.".dark_gray().into()),
        Some(lists) => {
            for list in lists {
                let done = list.tasks.iter().filter(|t| t.done).count();
                lines.push(
                    Line::from(format!("{} ({}/{})", list.name, done, list.tasks.len())).bold(),
                );
                for task in &list.tasks {
                    let (mark, style) = if task.done {
                        ("[x] ", Style::default().fg(Color::Green))
                    } else {
                        ("[ ] ", Style::default())
                    };
                    lines.push(Line::from(vec![
                        Span::styled(mark, style),
                        Span::styled(task.text.clone(), style),
                    ]));
                }
                lines.push(Line::raw(""));
            }
        }
    }

    f.render_widget(
        Paragraph::new(lines).block(block).wrap(Wrap { trim: false }),
        area,
    );
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
