use crate::planka::{Board, Card, Checklist, Update};
use std::{collections::HashMap, time::Instant};

#[derive(Default)]
pub struct App {
    pub board: Board,
    pub col: usize,
    pub rows: Vec<usize>,
    pub checklists: HashMap<String, Vec<Checklist>>,
    pub error: Option<String>,
    pub loaded: bool,
    pub updated_at: Option<Instant>,
}

impl App {
    pub fn apply(&mut self, update: Update) {
        match update {
            Update::Board(Ok(board)) => {
                self.board = board;
                self.loaded = true;
                self.error = None;
                self.updated_at = Some(Instant::now());
                self.rows.resize(self.board.columns.len(), 0);
                self.clamp();
            }
            Update::Board(Err(e)) => self.error = Some(e),
            Update::Checklists(id, Ok(lists)) => {
                self.checklists.insert(id, lists);
            }
            Update::Checklists(_, Err(e)) => self.error = Some(e),
        }
    }

    pub fn card_count(&self) -> usize {
        self.board.columns.iter().map(|c| c.cards.len()).sum()
    }

    pub fn selected_card(&self) -> Option<&Card> {
        let column = self.board.columns.get(self.col)?;
        column.cards.get(*self.rows.get(self.col)?)
    }

    pub fn selected_id(&self) -> Option<String> {
        self.selected_card().map(|c| c.id.clone())
    }

    pub fn move_col(&mut self, delta: isize) {
        self.col = step(self.col, delta, self.board.columns.len());
    }

    pub fn move_row(&mut self, delta: isize) {
        if let Some(column) = self.board.columns.get(self.col) {
            if let Some(row) = self.rows.get_mut(self.col) {
                *row = step(*row, delta, column.cards.len());
            }
        }
    }

    fn clamp(&mut self) {
        self.col = self.col.min(self.board.columns.len().saturating_sub(1));
        for (i, column) in self.board.columns.iter().enumerate() {
            let max = column.cards.len().saturating_sub(1);
            self.rows[i] = self.rows[i].min(max);
        }
    }
}

fn step(current: usize, delta: isize, len: usize) -> usize {
    if len == 0 {
        return 0;
    }
    (current as isize + delta).clamp(0, len as isize - 1) as usize
}
