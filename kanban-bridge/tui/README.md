# kanban-tui

Read-only, live terminal view of the Planka board used by the kanban bridge.

- Columns, cards, labels, and the `●` marker for cards with an agent session
- Selected card's `Subagents & Tasks` checklists with progress
- Polls Planka every 3 seconds (`r` to refresh now)

It reads `PLANKA_URL`, `PLANKA_API_KEY`, and `BRIDGE_TRIGGER_LABEL` from `~/.config/kanban-bridge/env`
and picks the board named after the trigger label (falling back to `Agy`).

## Usage

```bash
cd ~/workspace/kanban-bridge/tui   # or ~/workspace/dotfiles/kanban-bridge/tui
cargo run --release
```

| Key | Action |
| --- | --- |
| `h` / `←`, `l` / `→` | Switch column |
| `k` / `↑`, `j` / `↓` | Move between cards |
| `o` | Open selected card in the browser (`xdg-open`) |
| `r` | Refresh now |
| `q` / `Esc` | Quit |
