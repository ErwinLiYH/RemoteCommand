# RemoteCommand

RemoteCommand is a lightweight server for running commands on a remote machine
over HTTP.

Commands run locally on the RemoteCommand server. Their output is streamed back
to the client in real time, and continues to be recorded if the client
disconnects. You can reconnect later, recover the output produced while you
were away, and continue following the command until it exits.

## Highlights

- Run commands remotely through an authenticated HTTP connection
- Stream stdout and stderr in real time
- Keep commands running when the client disconnects
- Reconnect to a command and recover missed output
- Set a default server working directory or override it per command
- Optional PTY mode for terminal-aware programs, colors, and progress bars
- List running and completed commands from the CLI
- Persist command state and output on disk
- Clean completed command history when it is no longer needed

RemoteCommand currently supports Linux and macOS with Python 3.10 or newer.

## Installation

Clone the repository and install it with pip:

```bash
git clone https://github.com/ErwinLiYH/RemoteCommand.git
cd RemoteCommand

python -m pip install .
```

For development, install the package in editable mode:

```bash
python -m pip install -e ".[test]"
```

The installation provides two commands:

```text
remote-command-server   Start the HTTP server
remote-command          Run and manage remote commands
```

## Start the Server

RemoteCommand requires a Bearer Token because clients can execute arbitrary
commands on the server.

```bash
export REMOTE_COMMAND_TOKEN="replace-with-a-long-random-token"

remote-command-server \
  --host 0.0.0.0 \
  --port 8000 \
  --working-directory /path/to/default/workspace
```

`--working-directory` is the default directory used by commands. Individual
commands can override it.

By default, command metadata and output are stored in
`~/.remote-command`. Completed command history is retained for seven days
unless it is cleaned manually or the retention setting is changed.

For local-only testing, bind the server to `127.0.0.1` instead:

```bash
remote-command-server \
  --host 127.0.0.1 \
  --port 8000 \
  --working-directory "$(pwd)"
```

## Configure the CLI

Set the server URL and use the same Token configured on the server:

```bash
export REMOTE_COMMAND_URL="http://192.168.1.20:8000"
export REMOTE_COMMAND_TOKEN="replace-with-a-long-random-token"
```

You can also provide them for a single command:

```bash
remote-command \
  --url http://192.168.1.20:8000 \
  --token replace-with-a-long-random-token \
  --list
```

## Run a Command

```bash
remote-command --command "pwd && ls -la"
```

Output is displayed as ordinary terminal output as soon as it is produced.
The CLI also prints the generated command ID, process ID, status, and return
code.

Assign a memorable command ID and override the working directory:

```bash
remote-command \
  --command "python train.py" \
  --command-id training-1 \
  --working-directory /path/to/project
```

If no command ID is provided, the server generates one automatically.

Use PTY mode when a program buffers output or expects a terminal:

```bash
remote-command \
  --command "python train.py" \
  --command-id training-1 \
  --pty
```

## Reconnect and Recover Output

Pressing `Ctrl-C` disconnects the client but does not stop the remote command.
The CLI prints a reconnect command containing the last received event
sequence.

Reconnect from the beginning of the saved output:

```bash
remote-command --reconnect training-1
```

Reconnect after the last event you received:

```bash
remote-command \
  --reconnect training-1 \
  --after-seq 120
```

The server first sends all output produced after event 120, including output
generated while the client was disconnected, and then continues streaming new
output in real time.

To replay currently saved output without waiting for future output:

```bash
remote-command --reconnect training-1 --no-follow
```

## List Commands

List all retained commands:

```bash
remote-command --list
```

List only commands that are still running:

```bash
remote-command --list --status running
```

Inspect one command:

```bash
remote-command --get training-1
```

Stop a running command and its child processes:

```bash
remote-command --cancel training-1
```

Add `--json` to list or inspect commands in a machine-readable format:

```bash
remote-command --list --json
remote-command --get training-1 --json
```

## Clean Completed Commands

Delete the state and saved output of all completed, failed, cancelled, or lost
commands:

```bash
remote-command --cleanup
```

Running commands are never removed by cleanup. After a command is cleaned, its
saved output can no longer be replayed.

## Security

RemoteCommand can execute arbitrary shell commands. Use a strong Token, limit
network access with a firewall, and use HTTPS through a reverse proxy when the
server is exposed outside a trusted network. Do not send the Token over
unencrypted public HTTP.

## License

MIT
