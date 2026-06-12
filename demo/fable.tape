# fable hero demo — fictional corpus (demo/seed_demo.py), zero real data.
# Render: python3 demo/seed_demo.py && vhs demo/fable.tape
Output demo/fable.gif

Set FontSize 15
Set Width 1180
Set Height 640
Set Theme "Dracula"
Set TypingSpeed 35ms
Set Padding 18

Hide
Type "cd /Users/anoopgrover/Desktop/Trading/01_Algos/00/fable && alias fable='./bin/fable --db demo/demo.db' && clear"
Enter
Show

# ── beat 1: the threat → recall by meaning ────────────
Type "# Claude Code compacts your context (lossy) and deletes session files after 30 days."
Enter
Sleep 2.2s
Type "# fable indexes every session you ever ran — before they're gone. Weeks later:"
Enter
Sleep 1.8s
Type "fable search 'websocket reconnect fix' -n 3"
Enter
Sleep 4.5s

# ── beat 2: byte-exact, budgeted, paste-ready ─────────
Type "clear"
Enter
Type "# pull the real conversation back — under a token budget, paste-ready:"
Enter
Sleep 1.8s
Type "fable thread d23f0824-128b-4f33-8c5c-7fd0a6a3a450 --budget 900 | head -16"
Enter
Sleep 5s

# ── beat 3: decisions are first-class ─────────────────
Type "clear"
Enter
Type "# 'what did we DECIDE about the session cache?'"
Enter
Sleep 1.8s
Type "fable search 'session cache' --operative decide -n 2"
Enter
Sleep 4s

# ── beat 4: the kicker — weave a new session ──────────
Type "clear"
Enter
Type "# weave threads from DIFFERENT sessions into a NEW session Claude resumes:"
Enter
Sleep 2s
Type "fable compose 'jwt + cache workspace' -t 8e81973e-0bec-47b0-b898-d190f9ebdacc -t ae658f33-fe3b-490b-93f4-48b3a5aa3c81 --cwd /Users/demo/code/acme-shop --projects-dir demo/projects"
Enter
Sleep 4.5s
Type "# Claude wakes up with that as its lived history. fable — your agent never forgets."
Enter
Sleep 3.5s
