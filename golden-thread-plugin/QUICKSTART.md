# Golden Thread quickstart

One page. You have installed gt and restarted Claude Code. Five commands cover a normal day.

1. **Set up once.** Type `/gt:gt-init`. It creates your notes folder (the vault) and checks
   that everything is wired. It ends by telling you what to type next.

2. **Start work.** Type `/gt:gt-open` and a project name. Claude reads that project's notes
   so you do not have to explain it again.

3. **Save what you learned.** Type `/gt:gt-work`. Claude writes the findings and decisions
   from this session into the project's notes.

4. **Finish.** Type `/gt:gt-close` when you are done for now. It saves what is left to
   do and lets go of the project.

5. **Change how gt behaves.** Type `/gt:gt-settings`. It shows the few settings that matter,
   in plain words. Each one says how to change it. The full list is `show --all`.

## Good to know

- Everything gt writes goes into your vault as plain text files. You can read them in
  Obsidian or any editor, and nothing is deleted without being marked first.
- If something seems wrong, ask Claude to run the gt doctor. It names the problem and the fix.

## Want more

The small install gives you the four rules a new user needs. When you are ready for the
rest, `install.sh --all-modules` adds every module, and the README lists them all.
