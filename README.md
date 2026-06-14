# RemoteCommand Server

RemoteCommand Server 是一个独立的 HTTP 命令执行服务。调用方通过 HTTP
提交命令，命令在服务器本机运行，stdout、stderr 和退出状态通过 NDJSON
实时返回。

## 特性

- 启动时设置默认工作目录，单条命令可以覆盖
- stdout 和 stderr 实时分流，不要求输出以换行结尾
- 可选 PTY，支持依赖终端的颜色、进度条和 `\r` 刷新
- HTTP 连接断开后命令继续运行
- 输出写入磁盘，可使用事件序号断点重连
- 查询、取消命令，并终止完整的 POSIX 进程组
- Bearer Token 认证
- 同步 Python 客户端

目前支持 Linux 和 macOS，不支持 Windows。

## 安装

使用 uv：

```bash
uv sync
```

或者安装为普通 Python 包：

```bash
python -m pip install .
```

## 启动

Token 相当于这个服务的访问密码。由于 API 可以执行任意命令，服务拒绝在
没有 Token 的情况下启动。

```bash
export REMOTE_COMMAND_TOKEN="replace-with-a-long-random-token"

uv run remote-command-server \
  --host 127.0.0.1 \
  --port 8000 \
  --working-directory /path/to/default/workspace
```

也可以从文件读取 Token，避免它出现在 shell 环境中：

```bash
uv run remote-command-server \
  --token-file /secure/path/remote-command.token \
  --working-directory /path/to/default/workspace
```

默认状态目录是 `~/.remote-command`，其中保存命令元数据和 NDJSON
事件日志。完成命令默认保留 168 小时。

查看所有启动参数：

```bash
uv run remote-command-server --help
```

## 运行命令

`curl` 必须使用 `-N` 禁用客户端缓冲：

```bash
curl -N http://127.0.0.1:8000/commands \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "printf \"starting\\n\"; sleep 1; printf \"done\\n\""
  }'
```

单次覆盖工作目录：

```bash
curl -N http://127.0.0.1:8000/commands \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "pwd && ls",
    "working_directory": "/tmp",
    "command_id": "list-tmp"
  }'
```

相对工作目录基于服务启动时的默认工作目录解析，绝对路径则直接使用。

## NDJSON 事件

响应的每一行都是一个完整 JSON 对象：

```json
{"command_id":"example","seq":1,"type":"started","timestamp":"2026-06-13T12:00:00Z","status":"running","pid":1234}
{"command_id":"example","seq":2,"type":"stdout","timestamp":"2026-06-13T12:00:00Z","data":"hello\n"}
{"command_id":"example","seq":3,"type":"exit","timestamp":"2026-06-13T12:00:01Z","status":"exited","return_code":0}
```

普通模式使用 `stdout` 和 `stderr` 事件。事件中的 `data` 可以包含换行或
回车字符，不会因为程序没有输出换行而延迟。

长时间没有输出时，连接会收到不写盘的 `heartbeat` 事件，避免代理关闭
空闲连接。

## PTY

某些程序检测到 stdout 不是终端后会缓冲输出、关闭颜色或隐藏进度条。
为这类命令设置 `"pty": true`：

```bash
curl -N http://127.0.0.1:8000/commands \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  -H "Content-Type: application/json" \
  -d '{"command":"your-progress-command","pty":true}'
```

PTY 模式的 stdout 和 stderr 会合并为 `output` 事件。本项目目前只提供
输出流，不支持向运行中的 PTY 发送 stdin。

## 断点重连

首次响应头 `X-Command-ID` 和 `started` 事件都包含命令 ID。假设最后收到
的事件序号为 12：

```bash
curl -N \
  "http://127.0.0.1:8000/commands/example/events?after_seq=12&follow=true" \
  -H "Authorization: Bearer replace-with-a-long-random-token"
```

服务器先返回序号大于 12 的历史事件，然后继续跟随实时输出。设置
`follow=false` 只读取已有事件。

## 其他 API

```text
GET    /healthz                       健康检查，无需 Token
POST   /commands                      运行命令并返回事件流
GET    /commands                      列出命令
GET    /commands/{command_id}         查询命令状态
GET    /commands/{command_id}/events  重放并跟随事件
DELETE /commands/{command_id}         取消命令
```

交互式 API 文档位于 `/docs`。

## Python 客户端

### 简单命令行脚本

先设置连接地址和 Token：

```bash
export REMOTE_COMMAND_URL="http://127.0.0.1:8000"
export REMOTE_COMMAND_TOKEN="replace-with-a-long-random-token"
```

然后直接运行命令：

```bash
python remote_command.py --command "ls -la"
```

客户端会直接显示普通终端输出，不显示底层 JSON。它还会在 stderr 中显示
`command_id`、PID 和退出状态，并使用远端命令的退出码作为自己的退出码。

指定工作目录、命令 ID 或 PTY：

```bash
python remote_command.py \
  --command "python train.py" \
  --working-directory /path/to/project \
  --command-id training-1 \
  --pty
```

重新连接某个命令并从头显示已有输出，然后继续实时跟随：

```bash
python remote_command.py --reconnect training-1
```

如果已经处理到事件序号 120，只读取之后的输出：

```bash
python remote_command.py --reconnect training-1 --after-seq 120
```

只打印当前已有输出，不继续等待：

```bash
python remote_command.py --reconnect training-1 --no-follow
```

按 `Ctrl-C` 只会断开客户端，服务器上的命令会继续运行，客户端会打印一条
带有最后事件序号的重连命令。加 `--quiet` 可以隐藏客户端自己的状态信息。

安装项目后，也可以使用更短的等价命令：

```bash
remote-command --command "ls -la"
remote-command --reconnect training-1
```

CLI 覆盖服务端的全部 API：

```bash
# 健康检查，不需要 Token
remote-command --health

# 查看全部保留中的命令
remote-command --list

# 只查看正在运行的命令
remote-command --list --status running

# 查看单条命令的完整状态
remote-command --get training-1

# 终止命令及其整个子进程组
remote-command --cancel training-1
```

查询操作默认输出易读的摘要或表格。加 `--json` 可以获得适合脚本处理的
JSON：

```bash
remote-command --list --status running --json
remote-command --get training-1 --json
remote-command --health --json
```

运行或重连时使用 `--json`，会逐行输出原始 NDJSON 事件：

```bash
remote-command --reconnect training-1 --after-seq 120 --json
```

运行 `python remote_command.py --help` 可以查看全部参数。`--url` 和
`--token` 参数可以覆盖对应环境变量。

### Python API

```python
from remote_command_server import RemoteCommandClient

with RemoteCommandClient(
    "http://127.0.0.1:8000",
    token="replace-with-a-long-random-token",
) as client:
    last_seq = 0
    command_id = None

    for event in client.run_command("echo hello && sleep 1 && echo done"):
        if event["type"] == "started":
            command_id = event["command_id"]
        if event["type"] in {"stdout", "stderr", "output"}:
            print(event["data"], end="", flush=True)
        last_seq = max(last_seq, event["seq"])

    # 连接中断时，可以使用保存的 command_id 和 last_seq 继续读取。
    if command_id:
        for event in client.events(command_id, after_seq=last_seq):
            print(event)
```

## 反向代理

远程访问时建议让服务仅监听内网或回环地址，并在前面配置 HTTPS 反向代理。
代理必须关闭响应缓冲。例如 Nginx location：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 24h;
}
```

不要通过未加密的公网 HTTP 发送 Token。

## 开发

```bash
uv sync --extra test
uv run pytest
```

## 许可

MIT
