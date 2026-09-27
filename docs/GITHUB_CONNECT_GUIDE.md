# GitHub 接入与推送完整指南（macOS）

本文档用于让本地开发工具或代码助手（例如 CodeBuddy）完成以下工作：

1. 检查本地 Git 仓库状态；
2. 安装 GitHub CLI（`gh`）；
3. 通过浏览器安全授权 GitHub；
4. 配置 Git 凭据和远程仓库；
5. 将本地提交推送到 GitHub；
6. 排查代理、认证和分支问题。

> 安全原则：不要把 GitHub 密码、Personal Access Token、OAuth Token、一次性设备验证码写进代码、文档、提交记录或聊天消息。浏览器登录和授权应由仓库所有者确认。

## 一、前置条件

- macOS 已安装 Git；
- 本地项目已经存在，或准备将普通文件夹初始化为 Git 仓库；
- 用户拥有目标 GitHub 仓库的写入权限；
- 浏览器能够正常访问 GitHub；
- 如果电脑使用代理软件，需要确认终端是否也配置了代理。

检查基础环境：

```bash
git --version
uname -s
uname -m
```

Apple Silicon Mac 通常返回 `arm64`，Intel Mac 通常返回 `x86_64`。

## 二、检查本地仓库

进入项目目录：

```bash
cd /你的项目绝对路径
```

检查仓库和当前分支：

```bash
git status --short --branch
git branch -vv
git remote -v
```

如果当前目录还不是 Git 仓库：

```bash
git init
```

如果还没有设置远程仓库：

```bash
git remote add origin https://github.com/你的用户名/你的仓库名.git
```

如果 `origin` 已存在但地址错误：

```bash
git remote set-url origin https://github.com/你的用户名/你的仓库名.git
```

再次确认：

```bash
git remote -v
```

## 三、安装 GitHub CLI

GitHub CLI 是 GitHub 官方命令行工具，命令名为 `gh`。优先使用 Homebrew 安装。

### 方案 A：使用 Homebrew

检查 Homebrew：

```bash
command -v brew
```

如果有输出，执行：

```bash
brew install gh
```

检查安装结果：

```bash
gh --version
```

### 方案 B：没有 Homebrew时手动安装

1. 打开 GitHub CLI 发布页面：
   <https://github.com/cli/cli/releases/latest>
2. 根据 Mac 芯片下载压缩包：
   - Apple Silicon：`gh_<版本号>_macOS_arm64.zip`
   - Intel：`gh_<版本号>_macOS_amd64.zip`
3. 解压，并把其中的 `bin/gh` 安装到用户目录：

```bash
mkdir -p "$HOME/.local/bin"
install -m 755 /解压目录/bin/gh "$HOME/.local/bin/gh"
```

4. 确认 `~/.local/bin` 已在 `PATH` 中：

```bash
printf '%s\n' "$PATH"
```

如果没有，将下面一行加入 `~/.zshrc`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

然后重新加载：

```bash
source "$HOME/.zshrc"
gh --version
```

## 四、处理“浏览器能访问 GitHub，但终端超时”

macOS 浏览器可能自动使用系统代理，但终端命令不一定自动继承代理设置。

查看系统代理：

```bash
scutil --proxy
```

如果结果中包含类似以下内容：

```text
HTTPEnable : 1
HTTPProxy : 127.0.0.1
HTTPPort : 7890
HTTPSEnable : 1
HTTPSProxy : 127.0.0.1
HTTPSPort : 7890
```

可以只为当前命令临时添加代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 \
HTTP_PROXY=http://127.0.0.1:7890 \
gh auth status
```

端口必须以 `scutil --proxy` 的实际结果为准，不要盲目照抄 `7890`。

建议优先使用“单条命令临时代理”，避免代理软件关闭后，Git 因永久代理配置失效而无法联网。

如果确实要给当前仓库设置代理：

```bash
git config --local http.proxy http://127.0.0.1:7890
git config --local https.proxy http://127.0.0.1:7890
```

取消当前仓库代理：

```bash
git config --local --unset http.proxy
git config --local --unset https.proxy
```

## 五、通过浏览器授权 GitHub

无代理时：

```bash
gh auth login --hostname github.com --git-protocol https --web
```

需要代理时：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 \
HTTP_PROXY=http://127.0.0.1:7890 \
gh auth login --hostname github.com --git-protocol https --web
```

交互过程：

1. 出现 `Authenticate Git with your GitHub credentials?` 时选择 `Yes`；
2. 终端生成一次性设备码；
3. 按回车打开 <https://github.com/login/device>；
4. 在 GitHub 页面选择正确账号；
5. 输入一次性设备码；
6. 检查页面显示的应用名称和授权范围；
7. 由用户确认授权；
8. 页面显示设备已连接后，回到终端等待完成。

注意事项：

- 一次性设备码只应填写在 GitHub 官方页面；
- 不要把设备码复制到聊天、日志或项目文件；
- 不要让代码助手读取、输出或提交系统钥匙串里的 Token；
- 如果浏览器已经登录多个 GitHub 账号，必须确认选中的是目标仓库所有者。

## 六、验证授权并配置 Git 凭据

检查登录状态：

```bash
gh auth status
```

正常情况下会显示：

- 已登录的 GitHub 账号；
- Git 协议为 HTTPS；
- Token 保存在系统钥匙串中；
- Token 权限范围。

让 Git 使用 `gh` 管理的凭据：

```bash
gh auth setup-git
```

如果终端需要代理，则为命令加上前述 `HTTPS_PROXY` 和 `HTTP_PROXY` 环境变量。

不要执行会完整打印 Token 的命令，也不要把 `gh auth token` 的输出保存到文件。

## 七、提交本地修改

提交前先检查状态：

```bash
git status --short
git diff
```

只添加需要提交的文件，避免使用不加检查的全量提交：

```bash
git add 文件1 文件2
git status --short
git commit -m "描述本次修改"
```

常见不应提交的内容：

- `.env`、API Key、Token、密码；
- 本地运行输出和临时文件；
- `.DS_Store`；
- IDE 私有配置（例如 `.idea/`，除非团队明确需要）；
- 下载的视频、音频和大型模型文件。

建议提前写入 `.gitignore`。

## 八、推送到 GitHub

查看当前分支：

```bash
git branch --show-current
```

第一次推送 `main`：

```bash
git push -u origin main
```

后续推送：

```bash
git push
```

如果需要临时代理：

```bash
HTTPS_PROXY=http://127.0.0.1:7890 \
HTTP_PROXY=http://127.0.0.1:7890 \
git push -u origin main
```

推送成功通常会看到类似：

```text
To https://github.com/用户名/仓库名.git
   旧提交..新提交  main -> main
```

## 九、注意 `main` 与 `master` 分支不一致

部分旧仓库默认分支是 `master`，本地新项目可能使用 `main`。即使 `main` 推送成功，打开仓库主页仍可能先显示旧的 `master` 内容。

检查远程分支：

```bash
git ls-remote --heads origin
```

处理方式有三种：

1. 访问 GitHub 上的 `main` 分支查看最新代码；
2. 在 GitHub 仓库设置中把默认分支改为 `main`；
3. 创建 Pull Request，将 `main` 合并到 `master`。

修改默认分支属于仓库设置变更，合并分支也会改变远程代码历史。代码助手执行前应明确征得用户同意。

## 十、推送后验证

检查本地是否仍领先远程：

```bash
git status --short --branch
```

正常同步后应类似：

```text
## main...origin/main
```

查看远程最新提交：

```bash
git log --oneline --decorate -5
git ls-remote --heads origin
```

还应在浏览器打开仓库对应分支，确认以下内容：

- 最新提交已经出现；
- 文件数量和目录结构正确；
- 没有误传密钥、运行结果或大文件；
- README 中的启动方式与当前项目一致。

## 十一、常见错误及处理

### 1. `could not read Username for 'https://github.com'`

原因：Git 没有可用的 GitHub 凭据。

处理：

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
```

### 2. `gh auth login` 长时间没有响应或 `i/o timeout`

原因：终端没有使用系统代理，或者代理端口不正确。

处理：

```bash
scutil --proxy
```

根据结果给命令临时添加 `HTTPS_PROXY` 和 `HTTP_PROXY`。

### 3. `remote origin already exists`

原因：已经存在名为 `origin` 的远程仓库。

处理：

```bash
git remote -v
git remote set-url origin https://github.com/用户名/仓库名.git
```

### 4. `src refspec main does not match any`

可能原因：

- 当前分支不叫 `main`；
- 仓库还没有任何提交。

检查：

```bash
git branch --show-current
git log --oneline -1
```

### 5. 推送成功，但仓库首页看不到新文件

原因：新代码推送到了 `main`，仓库默认分支仍是 `master`。

处理：在仓库页面切换到 `main`，或经用户确认后修改默认分支/合并分支。

### 6. `403` 或 `Permission denied`

检查：

- 登录的是不是正确账号；
- 当前账号是否拥有仓库写入权限；
- 仓库组织是否要求 SSO 授权；
- Token 权限是否满足私有仓库或工作流文件的要求。

可以重新登录：

```bash
gh auth logout --hostname github.com
gh auth login --hostname github.com --git-protocol https --web
```

退出登录会删除本机保存的 GitHub CLI 凭据，执行前应由用户确认。

## 十二、代码助手执行规范

CodeBuddy 或其他代码助手接入 GitHub 时，建议遵循以下顺序：

1. 只读检查 `git status`、当前分支和远程地址；
2. 检查工作区是否有用户未提交的改动，不覆盖、不删除；
3. 检查 `gh` 是否安装；
4. 检查 `gh auth status`，不要打印真实 Token；
5. 如果终端网络超时，读取 `scutil --proxy`，使用临时代理；
6. 启动 `gh auth login --web`；
7. 浏览器涉及账号选择和 OAuth 授权时，让用户确认账号及权限；
8. 使用 `gh auth setup-git` 配置凭据；
9. 提交前检查敏感文件和 `.gitignore`；
10. 推送前说明目标仓库和目标分支；
11. 推送后验证远程分支和最新提交；
12. 未经用户允许，不修改默认分支、不强制推送、不删除远程分支。

禁止自动执行：

```bash
git push --force
git reset --hard
git clean -fd
```

除非用户明确要求，并且已经核对影响范围。

## 十三、本项目的实际配置说明

本项目远程仓库：

```text
https://github.com/imy112358/myweb.git
```

当前新代码分支：

```text
main
```

仓库原默认分支为 `master`，因此访问仓库主页时需要切换到 `main` 才能看到最新代码。不要把本项目的讯飞密钥文件、运行输出、下载的视频或识别结果推送到 GitHub。

