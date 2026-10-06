# 生产部署与运维

生产站点是 [https://www.pinhaoke.love](https://www.pinhaoke.love)。Nginx 负责 HTTPS 和静态图片，`pinhaoke.service` 以 `www-data` 运行 Uvicorn，应用目录固定为 `/opt/pinhaoke`。

## 组件边界

- `deploy/update.sh`：唯一应用更新入口，部署精确 `origin/main`。
- `deploy/pinhaoke.service`：systemd unit，使用 `/opt/pinhaoke/venv/bin/python -m uvicorn`。
- `deploy/nginx.conf`：与 Certbot 共存的站点模板，更新脚本不会自动安装它。
- Nginx 监听 80/443；80 和裸域名跳转到 `https://www.pinhaoke.love`。
- FastAPI 只监听 `127.0.0.1:8000`，不直接暴露公网。

## 权限模型

应用代码、`.git`、虚拟环境和六个正式数据库由 `root` 持有。`www-data` 只获得运行所需的目录遍历和文件读取权限，不拥有仓库，也不能修改这些 SQLite：

- 发布目录通常为 `root:www-data`，目录 `0750`，文件 `0640`
- `venv` 为 `root:root`，目录 `0755`，普通文件 `0644`，`bin/` 可执行文件 `0755`
- 五个课程数据库和一个树洞评测数据库均由应用以 SQLite `mode=ro` 和 `PRAGMA query_only = ON` 打开
- systemd 使用 `ProtectSystem=strict`、`ReadOnlyPaths=/opt/pinhaoke`、`NoNewPrivileges=true`、私有临时目录、空 capability 和 `UMask=0027`
- 留言板、访问统计与账户数据库是仅有的三份可写数据：unit 通过 `StateDirectory=pinhaoke` 让 systemd 自动创建 `/var/lib/pinhaoke` 并归服务用户所有，`StateDirectoryMode=0750` 限制目录仅服务用户与组可读；`PINHAOKE_MESSAGES_DB` 指向其中的 `留言板.db`，`PINHAOKE_STATS_DB` 指向 `访问统计.db`，`PINHAOKE_ACCOUNTS_DB` 指向保存账号、密保哈希、会话哈希与课程收藏的 `账户.db`。它们不在仓库和 `/opt/pinhaoke` 内，更新与回滚不触碰这些数据，本机备份由独立的 `pinhaoke-backup.timer` 负责（见下方 `## 数据备份`），不随更新脚本执行。部署前可用 `python3 -c "import hashlib; hashlib.scrypt"` 确认服务器 Python 带 OpenSSL 的 scrypt 支持。

不要对 `/opt/pinhaoke` 执行递归 `chown www-data`。更新脚本会避开 `.git`、staging、备份/诊断 venv 和活动 venv，使用 symlink-safe 的权限处理。

## 前置依赖

服务器需要：Git 2.42 或更高版本、Git LFS、Python 3、`venv`、`curl`、`flock`、`sha256sum`、systemd，以及可访问 Git remote 和 Python 包源的网络。Git 2.42 是候选目录通过 `GIT_ATTR_SOURCE` 按目标 commit 的 `.gitattributes` 物化新增 LFS 路径所需的最低版本。

首次配置前检查：

```bash
git --version
git lfs version
python3 --version
systemctl --version
nginx -v
```

## 唯一更新命令

以 root 执行：

```bash
sudo bash /opt/pinhaoke/deploy/update.sh
```

不要用手工 `git pull`、直接 `git reset` 或单独重启服务替代该命令。脚本使用非阻塞 `flock`，同一时间只允许一个更新任务。

## 停服前预检

更新脚本在**停服前**完成可能耗时或可能失败的工作：

1. fetch 并解析精确 `origin/main` commit，同时记录旧 commit、旧 unit 和服务活动状态。
2. 如果旧提交使用 Git LFS，检查 `git-lfs`，fetch 旧对象并运行 LFS fsck；再用独立临时 Git index 展开旧提交，确认旧 LFS 文件已经物化而不是 pointer。
3. 如果目标提交使用 Git LFS，同样 fetch 目标对象并运行 LFS fsck。
4. 用另一份临时 Git index 把目标 commit 展开到 `.deploy-stage.*`；展开时通过 `GIT_ATTR_SOURCE` 强制读取目标 commit 自身的 `.gitattributes`，确保该版本新增加的 LFS 路径也能物化，再确认所有 LFS 文件均不是 pointer。
5. 从 staged target 的 `requirements.txt` 计算 SHA-256。
6. 仅在活动 venv 缺失或 requirements 哈希变化时构建候选 venv，执行 `python -m pip install`、`pip check` 和依赖导入检查。

LFS 文件清单读取失败同样属于预检失败，不能把失败产生的空输出当作“无需校验的文件”。

只有这些步骤全部成功才停止 `pinhaoke.service`。旧版本或目标版本的 LFS 对象缺失、下载失败、未正确物化，以及依赖解析或候选环境错误都不会造成服务停机。

## 激活与烟测

进入激活阶段后，脚本会：

1. 停止服务并把工作树切到目标 commit。
2. 再次物化并校验工作树 LFS 文件。
3. 安装目标 commit 的 systemd unit。
4. 仅在候选 venv 已构建时原子交换 venv。
5. 使用活动 Python 执行 `python -m uvicorn --version`，应用只读权限，reload unit 并启动服务。
6. 对五类本机 API 契约执行带重试、超时和 JSON 结构检查的烟测。

烟测目标：

```text
http://127.0.0.1:8000/api/health
http://127.0.0.1:8000/api/filters?term=fall
http://127.0.0.1:8000/api/courses?term=fall&page_size=1
http://127.0.0.1:8000/api/reviews?page_size=1
http://127.0.0.1:8000/api/reviews/{pid}
```

脚本先从 reviews 响应提取第一条正整数树洞号，再请求对应详情。健康检查必须报告五个课程库、五个 ID 前缀、树洞评测库、快照回复数、实体高亮数量及完整性；filters 必须返回六个列表字段；courses 必须返回正整数 total 和一条带 ID 的课程；reviews 必须返回正整数 total 和一条带树洞号及条目列表的主题；review-detail 必须返回对应主帖、精确回复数量，以及只含 `cid`、`floor`、`posted_at`、`content` 的回复对象。HTTP 200 但 JSON 不符合契约也视为失败。

## 自动回滚语义

从服务激活开始到成功标记前，以下情况都会触发自动回滚：

- 任意命令错误（ERR）
- INT、TERM 或 HUP 信号
- 未经过成功路径的进程 EXIT
- 服务启动、活动状态或任一烟测失败

自动回滚会停止失败服务，恢复旧 commit 和对应 LFS 文件，恢复旧 systemd unit，重新应用权限，并只在旧服务原本处于 active 时重新启动它。旧版本 LFS 对象已在停服前完成 fetch、fsck 和独立物化验证，回滚阶段只使用这些已验证的本地对象，不依赖临时网络下载。

虚拟环境回滚以 `VENV_SWAPPED=1` 为条件：

- 没有发生 venv 交换时，回滚不会移动或删除活动 venv。
- 已交换且存在旧 venv 时，失败候选移到 `.venv-failed-<sha>-<pid>`，旧 venv 恢复到 `/opt/pinhaoke/venv`。
- 已交换但发布前没有旧 venv 时，失败候选仍移到 `.venv-failed-*`，不会伪造旧环境。

`.venv-failed-*` 是诊断制品，不是活动环境。它们会保留以便排查；下一次更新开始时自动删除修改时间超过 7 天的诊断目录。成功部署后临时 stage 和不再需要的 `.venv-backup-*` 会清理。

如果自动回滚自身失败，脚本会明确输出“manual recovery is required”并返回非零；不要把失败退出解释为已经恢复。

## 运行状态与日志

```bash
systemctl status pinhaoke --no-pager
journalctl -u pinhaoke -n 100 --no-pager
journalctl -u pinhaoke --since '2026-07-11 00:00:00' --no-pager
curl --fail --silent http://127.0.0.1:8000/api/health
curl --fail --silent 'http://127.0.0.1:8000/api/filters?term=fall'
curl --fail --silent 'http://127.0.0.1:8000/api/courses?term=fall&page_size=1'
curl --fail --silent 'http://127.0.0.1:8000/api/reviews?page_size=1'
review_pid=$(curl --fail --silent 'http://127.0.0.1:8000/api/reviews?page_size=1' | python3 -c 'import json,sys; print(json.load(sys.stdin)["threads"][0]["pid"])')
curl --fail --silent "http://127.0.0.1:8000/api/reviews/$review_pid"
```

外部检查：

```bash
curl --fail --silent https://www.pinhaoke.love/api/health
curl --head https://www.pinhaoke.love/
```

## 手工恢复

只在脚本报告自动回滚不完整时执行手工恢复：

1. 记录日志、`git rev-parse HEAD`、服务状态和 `/opt/pinhaoke/.venv-*` 目录清单。
2. 选择已经验证的完整 commit SHA；不要用模糊的 `HEAD~N` 或未确认的分支名。
3. 停止服务，恢复该 commit，执行对应 LFS checkout/fsck。
4. 恢复该 commit 的 `deploy/pinhaoke.service` 并运行 `systemctl daemon-reload`。
5. 检查 `/opt/pinhaoke/venv/bin/python -m uvicorn --version`。只有日志证明 venv 已发生交换时，才依据 `.venv-backup-*` / `.venv-failed-*` 现场状态恢复；禁止无条件替换活动 venv。
6. 恢复 root 所有权与只读权限，启动服务，依次通过 health、filters、courses、reviews、review-detail 五类烟测。
7. 保存失败诊断目录，完成原因分析后再清理。

手工恢复不是常规发布方式。故障处理后应先修复仓库脚本与测试，再重新使用唯一更新命令。

## Nginx 与证书

`deploy/nginx.conf` 引用 Certbot 文件：

```text
/etc/letsencrypt/live/pinhaoke.love/fullchain.pem
/etc/letsencrypt/live/pinhaoke.love/privkey.pem
/etc/letsencrypt/options-ssl-nginx.conf
/etc/letsencrypt/ssl-dhparams.pem
```

模板包含 HTTPS 重定向、HSTS、安全响应头、gzip、`/Images/` 30 天缓存和到 `127.0.0.1:8000` 的反向代理。它不是完整 `nginx.conf`，也不由更新脚本复制。

手工安装或修改时：

```bash
sudo cp /etc/nginx/sites-available/pinhaoke /etc/nginx/sites-available/pinhaoke.backup
sudo cp /opt/pinhaoke/deploy/nginx.conf /etc/nginx/sites-available/pinhaoke.candidate
sudo ln -sfn /etc/nginx/sites-available/pinhaoke.candidate /etc/nginx/sites-enabled/pinhaoke
sudo nginx -t
sudo systemctl reload nginx
```

只有 `nginx -t` 成功后才能 reload。失败时把 `sites-enabled/pinhaoke` 指回备份，重新运行 `nginx -t`，通过后再 reload。

## 数据备份

三个可写库（`账户.db`、`留言板.db`、`访问统计.db`）由 `deploy/backup_databases.py` 定时快照，脚本随 `update.sh` 部署到 `/opt/pinhaoke/deploy/`，只用标准库，不产生任何 API 费用。

机制：root 父进程为每个源库启动 `www-data` UID/GID 的读取子进程，并清空子进程附加组。读取者以 `mode=ro` 和 `PRAGMA query_only = ON` 打开源库，调用 SQLite Online Backup 把未检查点的 WAL 数据一并写入私有临时快照，仅把临时快照切换为 `DELETE` 模式后再通过 stdout 流出字节。stdout 只接到父进程已打开的私有文件描述符，不给读取者备份目录访问权；父进程检查退出码和 SQLite 文件头、同步文件并用 `PRAGMA quick_check` 校验后，原子落盘为带北京时间戳的文件名（如 `账户-YYYYMMDD-HHMMSS.db`）。每个库各自滚动保留 `14` 份，读取者失败或超时不会发布副本、不会清理已有备份。

`mode=ro` 不等于禁止创建 `-wal`/`-shm`。空闲 WAL 库的最后一个连接关闭后，辅助文件可能已删除；因此读取者必须能在 `/var/lib/pinhaoke` 中创建它们，并以 `www-data` 身份保持正确属主。源 SQL 连接始终只读；不能以 `immutable=1` 或裸复制主库代替在线备份，否则可能漏掉实时 WAL 数据。

目标目录 `/var/backups/pinhaoke` 为 `root:root`、目录权限 `0700`、副本文件 `0600`：账户库保存密码与会话哈希，锁到 root-only 满足安全要求；该目录不在仓库、不在 `/opt/pinhaoke`、不在 `/var/lib/pinhaoke` 内。

`pinhaoke-backup.service` 为 `Type=oneshot`、`User=root`、`Group=root`，保留 `NoNewPrivileges=true`、`ProtectSystem=strict`、`PrivateTmp=true` 和空 `AmbientCapabilities`。`CapabilityBoundingSet` 仅允许 `CAP_SETUID CAP_SETGID CAP_KILL`：前两项供子进程切换身份及清空附加组，`CAP_KILL` 允许父进程在超时时终止已降权的读取者；不授予绕过文件权限的 DAC capability。父进程的 `SupplementaryGroups=www-data` 允许读取/遍历现有 `www-data:www-data` 的 `0750` 数据目录和 `0640` 数据文件，不改变备份的 root 属主。可写 namespace 仅放行 `/var/backups/pinhaoke` 与 `/var/lib/pinhaoke`，源目录的普通文件权限仍生效。每个读取者超时为 120 秒，单元总超时为 7 分钟；超时后父临时副本被清理，子进程私有临时目录随单元停止回收。

`pinhaoke-backup.timer` 以 `OnCalendar=*-*-* 04,16:20:00` 每天错峰两次、`Persistent=true` 在错过后补跑，使用服务器时区。两个单元一次性手工安装，`update.sh` 只更新脚本和模板，不安装它们。先预建目录，再安装和启动：

```bash
sudo install -d -o root -g root -m 0700 /var/backups/pinhaoke
sudo install -m 0644 /opt/pinhaoke/deploy/pinhaoke-backup.service /etc/systemd/system/pinhaoke-backup.service
sudo install -m 0644 /opt/pinhaoke/deploy/pinhaoke-backup.timer /etc/systemd/system/pinhaoke-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now pinhaoke-backup.timer
sudo systemctl start pinhaoke-backup.service
```

兼容性前置：若服务器已安装旧版备份单元（空 `CapabilityBoundingSet`、只读源目录），不能只更新脚本后就认为定时备份仍可用。发布前须先只读核实现场单元、服务用户/组及目录权限，并分别获得发布授权与服务器单元、目录/权限变更的对应授权后，配套更新单元、预建上述目录和验证备份；不要为适配脚本而递归修改 `/var/lib/pinhaoke` 的权限。仓库中的模板不代表单元已安装或 timer 已启用。

验证：

```bash
systemctl list-timers | grep pinhaoke-backup
journalctl -u pinhaoke-backup --no-pager | tail
sudo ls -l /var/backups/pinhaoke
```

恢复某个库须单独获得操作授权：先停服并确认所有写进程/数据库连接已关闭，成套保留当前主库及同名 `-wal`/`-shm` 作为恢复前副本，再把仍存在的旧辅助文件移入隔离目录；不能把旧 WAL/SHM 与恢复后的主文件混用。把经 `quick_check` 验证的对应快照复制回 `/var/lib/pinhaoke/` 原文件名，恢复 `www-data:www-data` 属主和 `0640` 权限，再启动服务并验证数据。快照包含已经提交的 WAL 内容，本身不依赖源库辅助文件。

## 发布边界

- 本地提交不等于 GitHub 已 push。
- GitHub 已 push 不等于生产已部署。
- 只有更新脚本成功输出目标 commit，且本机与外部烟测通过，才能记录生产版本。
- 不在未获用户明确授权的任务中执行 SSH、push、部署或证书变更。
