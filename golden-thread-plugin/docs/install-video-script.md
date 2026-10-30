# Install video: shot list and script

Generated from `dev/install_walkthrough.json` by `dev/build_install_video.py script`. Do not edit by hand.

Two videos can be made from this. The REPLAY video (`install-replay.mp4`) is drawn from recorded text in a
simulated terminal and says so on its first frame. A **screen recording** of the real Claude Code screens is
the owner's to make: follow the scenes below, say the spoken lines, and use the captions file as the subtitles.

Total running time: 134 seconds. Scenes marked *recorded* use text taken from a real install; *illustrative* ones show typical output and should be checked against the real screen.

## Scene 1: This is a replay  (00:00:00 to 00:00:08, illustrative)

On screen:
    Golden Thread: install from the Claude prompt
    
    A replay drawn from recorded text,
    not a screen recording.

Say: This is a replay of the install, drawn from recorded text. It is not a screen recording.

## Scene 2: Before you start  (00:00:08 to 00:00:19, illustrative)

On screen:
    Mac:      open Terminal
    Windows:  open PowerShell (or Windows Terminal)
    You need: Claude Code 2.1.300 or newer
    Time:     about five minutes
    Then:     type  claude  to open Claude Code

Say: Open Terminal on a Mac, or PowerShell on Windows. You will type claude to open Claude Code.

## Scene 3: Update Claude Code first  (00:00:19 to 00:00:28, recorded)

Type:
    claude update
    claude --version

On screen:
    2.1.306 (Claude Code)

Say: First, update Claude Code. You need version 2.1.300 or newer.

## Scene 4: Open Claude Code  (00:00:28 to 00:00:36, illustrative)

Type:
    claude

On screen:
    Claude Code
    > _

Say: Now type claude. That opens Claude Code. The rest happens inside it.

## Scene 5: Add the Golden Thread marketplace  (00:00:36 to 00:00:45, illustrative)

Type:
    /plugin marketplace add <github repo>/golden-thread.git

On screen:
    Added marketplace: golden-thread-plugin
    
    <github repo> is the GitHub address your team
    uses for the gt repository.

Say: In Claude Code, add the Golden Thread marketplace, using your team's GitHub address.

## Scene 6: Install the gt-all bundle  (00:00:45 to 00:00:55, illustrative)

Type:
    /plugin install gt-all@golden-thread-plugin

On screen:
    Installed gt-all, with gt and every module:
    gt-demo, gt-farm, gt-flow, gt-lotr, gt-pane, gt-recall,
    gt-report-card, gt-usage, gt-visualize, gt-watch,
    gt-wiki, gt-workflow

Say: Install gt-all. It brings gt and every module with it.

## Scene 7: Restart, then run gt-setup  (00:00:55 to 00:01:04, recorded)

Type:
    /gt:gt-setup

On screen:
    gt: Golden Thread is installed but not set up on this machine

Say: Restart Claude Code by typing exit and then claude again. Then run gt-setup.

## Scene 8: Choose Install everything  (00:01:04 to 00:01:12, recorded)

On screen:
    Which Golden Thread modules do you want?
    ❯ Install everything (recommended)
      Choose modules
      Core only

Say: Pick Install everything. It is the recommended answer.

## Scene 9: Take the recommended answers  (00:01:12 to 00:01:20, recorded)

On screen:
    Security level:  strict · trust local (recommended) · trust network
    Where should your vault live?
    ❯ ~/GoldenThread (recommended)
      A vault I already have

Say: For the other questions, take the recommended answer.

## Scene 10: Leave a module out, now or later  (00:01:20 to 00:01:30, illustrative)

Type:
    /gt:gt-setup

On screen:
    Choose modules  -> untick the ones you do not want
    Or later:  /gt:gt-setup again, or
      bash install.sh --without NAME

Say: To leave a module out, choose Choose modules, or run gt-setup again later.

## Scene 11: Restart once more  (00:01:30 to 00:01:39, illustrative)

Type:
    /exit
    claude

On screen:
    Installing a module turns none of its
    behavior on.

Say: Restart Claude Code once more. Installing a module turns none of its behavior on.

## Scene 12: Ask Claude for the gt help page  (00:01:39 to 00:01:51, recorded)

Type:
    /gt:gt-help

On screen:
    Opening the gt help page in your browser.

Show: the real gt help page (open it with /gt:gt-help and let it sit on its landing view).

Say: Ask Claude for the gt help page. It shows every command, and plays each one in a simulated terminal.

## Scene 13: The gt button pane  (00:01:51 to 00:02:03, illustrative)

Type:
    /gt-pane-buttons

On screen:
    Click a button, or press its key
    when the pane has the keyboard.
    Claude then does it as if you typed it.

Show: the gt button pane beside the chat (run /gt-pane-buttons if it is closed). Windows and Mac look the same.

Say: gt also puts a column of buttons beside the chat. Press one, and Claude does the task for you.

## Scene 14: More help  (00:02:03 to 00:02:14, illustrative)

On screen:
    Install guide:     <github repo>/golden-thread/golden-thread-plugin/INSTALL.md
    User manual:       <github repo>/golden-thread/golden-thread-plugin/MANUAL.md
    Getting started:   <github repo>/golden-thread/golden-thread-plugin/ONBOARDING.md
    Inside Claude:     /gt:gt-help

Say: Links to the install guide, the manual and the getting started guide are on screen. Inside Claude, gt-help is always there.
