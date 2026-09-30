# 本地科研文献管理器

> 一个完全跑在本机的 PDF 文献管理器：导入 PDF → AI 抽取标题 / 作者 / DOI → 主题标签、年份标签、期刊分区 → 一键用本机 PDF 阅读器打开。

![platform](https://img.shields.io/badge/platform-Windows%20%E4%B8%BB%E5%8A%9B-0078D4)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![node](https://img.shields.io/badge/node-20.19%2B%20%2F%2022.12%2B-339933)
![local only](https://img.shields.io/badge/network-127.0.0.1%20only-2E7D6E)

**三个设计前提**

- **纯本地**：后端只监听 `127.0.0.1:8765`，没有账号体系，没有云端数据库。
- **不上传 PDF 文件**：AI 请求会把文件名、首页文本、题名及必要的主题标签信息发送给当前模型服务商，不发送 PDF 文件、截图或全文。
- **可整体搬迁**：数据库保存相对路径，项目目录换位置、换盘符后文献记录依然可用，无需重新链接。

---

## 目录

- [功能](#功能)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [首次使用](#首次使用)
- [服务与端口](#服务与端口)
- [目录与数据](#目录与数据)
- [配置说明](#配置说明)
- [迁移与备份](#迁移与备份)
- [常见问题](#常见问题)
- [API 一览](#api-一览)
- [开发](#开发)
- [隐私与安全](#隐私与安全)
- [已知限制](#已知限制)
- [许可](#许可)

---

## 功能

**导入与识别**

- 手动选择一个或多个 PDF 入库（网页按钮调用本机文件选择框，不递归扫描文件夹）
- 读取 PDF 首页文本，调用 OpenAI 兼容 API 抽取标题、作者、单位、出版时间、DOI
- 导入时自动生成中文题名和中文摘要，也可手动填写或单独调用 AI 重新生成
- AI 识别出标题后，自动把源文件移动进 `library_files` 并按论文标题重命名
- 可为文献关联本地补充文件，按 `论文标题_sp.ext` 重命名

**标签体系**

- 把本地已有标签传给 AI，优先匹配已有研究主题标签，而不是无脑造新标签
- 新标签只进入「待确认区」，确认后才加入正式标签库
- 标签支持名称、别名、说明；别名用于同义词归并和 AI 匹配
- 根据出版时间自动生成年份标签，支持按年份排序
- 拦截 XPS、SEM、XRD、Raman、FTIR、CV、EIS 等常规方法词作为主标签
- 点击标签筛选文献，另有「无主题标签」和「提取异常」视图

**期刊分区**

- 自动提取期刊名，查询 2025 中科院分区与 2026 新锐分区
- 唯一结果直接显示在文献列表；同名多结果进入待确认区
- 支持详情页单篇重查与设置页批量补查（可只补查未查询或上次失败的文献）
- 分区标签不加入全局主题标签库

**文件操作**

- 用系统默认或自选的 PDF 软件打开主文献和 PDF 补充文件
- 自动重命名遇到同名文件时弹出冲突确认：打开文件夹 / 自动编号 / 使用已有文件 / 取消
- 右键文献可新建并关联标签，或移除文献记录；移除时可选把 `library_files` 内的主 PDF 和补充文件一并移入回收站

---

## 环境要求

| 组件 | 版本 | 实测 | 说明 |
|---|---|---|---|
| Python | 3.10 及以上 | 3.13.5 | `requirements.txt` 已锁定全部依赖版本，**唯一必需项** |
| Node.js / npm | Vite 构建需 `^20.19` 或 `>=22.12`；订阅接入需 `>=22.19.0` | 24.15.0 / npm 11.12.1 | **可选**：前端产物有效时，API 与本地文献功能无需 Node；订阅登录和调用需要 Node 22.19.0+ |
| 操作系统 | Windows 10 / 11 | — | 托盘启动器、快捷方式、PowerShell 脚本、原生文件选择器均为 Windows 专用 |
| 网络 | 仅调用 AI 与分区查询时需要 | — | 其余功能完全离线 |

> **Python 是文献管理和 API 接入的必需项。** 仓库里已经带了构建好的前端 `frontend/dist`，以及一份记录"这份 dist 由哪一版源码构建"的戳记 `frontend/.build-stamp`。每次启动时程序会拿当前构建输入（`src/`、`index.html`、`package.json`、`package-lock.json`、`vite.config.ts`、`tsconfig.json`）的内容哈希去比对戳记：
>
> - **对得上** → 直接用现成的 dist，不为前端构建运行 npm。机器上没装 Node.js 也能使用本地文献和 API 接入。
> - **对不上**（你改了前端源码）→ 这时才需要 Node.js：自动 `npm install`、`npm run build`，构建完更新戳记。
>
> 判断依据是**文件内容**而不是修改时间，所以 `git clone` / 换机器 / 重新检出都不会误判。行尾被 `core.autocrlf` 转成 CRLF 也不影响（哈希前会先统一成 LF）。
>
> ⚠️ **`frontend/.build-stamp` 必须和 `frontend/dist` 一起提交。** 少了它，别人克隆后会被判定为"来源不明、需要重建"，从而又回过头去要 Node.js。
>
> **订阅接入另需 Node.js 22.19.0 或更新版本。** 环境准备会在 Node 可用时单独安装 `model_bridge/` 的锁定依赖，即使前端无需重建也会检查；Node 缺失、版本过低或安装失败时，API 接入和本地文献操作仍可使用，订阅配置页会显示原因。
>
> **macOS / Linux** 可以运行后端（`python -m backend.app`），但 `start.ps1` / `stop.ps1` / `create_shortcut.ps1` / `launch_tray.vbs` / 托盘启动器都不可用，需要手动建虚拟环境并自行构建前端，做法见下方「方式三：完全手动」。

---

## 快速开始

### 1. 获取代码

```powershell
git clone https://github.com/zhangsanbase/paper-management.git
cd paper-management
```

也可以直接下载 ZIP 解压。建议放在**纯英文、无空格**的路径下；`library_files` 会存放大体积 PDF，注意留足磁盘空间。

### 2. 启动

#### 方式一：桌面快捷方式 + 托盘（Windows，推荐）

```powershell
.\create_shortcut.ps1
```

之后双击桌面的「科研文献管理器」即可。它会隐藏命令行窗口，启动系统托盘程序，在后台拉起本地服务，并自动用 Edge（找不到则用默认浏览器）打开 `http://127.0.0.1:8765`。

脚本同时在项目目录内生成同名快捷方式，并使用 `assets/paper-manager.ico` 作为图标。**迁移项目目录后**，重跑一次 `create_shortcut.ps1`（或直接运行 `launch_tray.vbs`）刷新快捷方式路径。

托盘菜单：`当前状态` / `打开界面` / `重启服务` / `停止服务` / `退出托盘`。
托盘启动器会自动处理 `.venv`、Python 依赖、`node_modules` 和前端构建，日志在 `data/logs/launcher.log` 与 `data/logs/server.log`。

#### 方式二：命令行（Windows）

```powershell
.\start.ps1
```

`start.ps1` 会复用已装好的环境：venv 不存在时自动创建，`requirements.txt` 有变动时自动补装依赖，**前端源码内容有变动**时自动 `npm run build`（没变动就跳过，不需要 Node.js）。**服务已在运行时再次执行它只会打开浏览器，不会重复启动。**

停止服务：

```powershell
.\stop.ps1
```

`stop.ps1` 会按进程树结束 `backend.app`（兼容 Anaconda 创建的 venv 会多拉一层解释器的情况），并等待端口释放。

#### 方式三：完全手动（Windows / macOS / Linux）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # macOS / Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
npm install
npm run build
python -m backend.app
```

命令需在**项目根目录**执行（后端通过 `backend.app:app` 导入，依赖当前工作目录）。启动后会自动打开浏览器；如果只想起服务：

```powershell
$env:PAPER_MANAGER_SKIP_BROWSER='1'   # macOS / Linux: export PAPER_MANAGER_SKIP_BROWSER=1
python -m backend.app
```

也可以先只准备环境、不启动服务：

```powershell
python .\prepare_environment.py            # 建 venv、装依赖、按需构建前端
python .\prepare_environment.py --quiet    # 静默模式，托盘启动器用的就是它
```

#### 方式四：开发模式（前端热更新）

后端与 Vite 开发服务器需要同时运行，共两个终端：

```powershell
# 终端 1：后端
$env:PAPER_MANAGER_SKIP_BROWSER='1'; python -m backend.app

# 终端 2：前端，访问 http://127.0.0.1:5173
npm run dev
```

Vite 已把 `/api` 代理到 `127.0.0.1:8765`，后端 CORS 也放行了 `5173`，改前端代码即时生效。注意开发服务器用的是 `src/`，与 `frontend/dist` 无关，所以**不要**在开发模式下排查构建产物问题。

## 首次使用

1. 浏览器打开 <http://127.0.0.1:8765>（首次会新建 `data/library.sqlite3`、创建 `library_files/`，并从 `assets/partition_tables/*.xlsx` 导入分区表）。
2. 右上角 **设置 → 模型配置**，新增一组 API 配置或订阅配置。先保存，再测试连接并设为当前；订阅接入会先保存基本信息，再按页面提示登录并选择模型。未启用模型时 PDF 仍会入库，但 AI 处理状态显示「需要配置」。
3. 点「导入 PDF」入库。入库后程序会读首页文本、抽取元数据、生成中文题名与摘要，并把源文件移入 `library_files` 按标题重命名。
4. 到文献详情页确认 AI 提出的**待确认标签**；确认后才进入正式标签库。
5. 到 **设置 → 分区补查** 批量补齐期刊分区。
6. 到 **设置 → PDF 阅读器** 选择打开 PDF 的程序（不改也能用系统默认程序）。

---

## 服务与端口

| 项 | 值 |
|---|---|
| 服务地址 | <http://127.0.0.1:8765>（默认端口） |
| 端口来源 | 环境变量 `PAPER_MANAGER_PORT`，缺省 `8765` |
| 监听范围 | 仅回环地址 `127.0.0.1`，外网与局域网均不可访问 |
| 健康检查 | `GET /api/health` → `{"status":"ok"}` |
| 前端开发服务器 | <http://127.0.0.1:5173>（仅 `npm run dev` 时存在） |

常用命令：

| 目的 | 命令 |
|---|---|
| 启动（含环境准备） | `.\start.ps1` |
| 启动（仅后台服务，不开浏览器） | `$env:PAPER_MANAGER_SKIP_BROWSER='1'; python -m backend.app` |
| 停止 | `.\stop.ps1`，或托盘菜单「停止服务」 |
| 刷新桌面快捷方式 | `.\create_shortcut.ps1` |
| 只准备环境 | `python .\prepare_environment.py` |
| 查端口占用 | `Get-NetTCPConnection -LocalPort 8765 -State Listen` |
| 查服务状态 | `Invoke-WebRequest http://127.0.0.1:8765/api/health` |

> **关闭浏览器页面不会停止后端服务**，这是有意设计。需要停止时用托盘菜单或 `.\stop.ps1`。

### 换端口

`8765` 被别的软件占用时，改环境变量即可，**不需要动任何源码**：

```powershell
# 只对当前终端有效
$env:PAPER_MANAGER_PORT = '9000'
.\start.ps1
```

```powershell
# 长期有效（对托盘和桌面快捷方式也生效）；设置后需重开终端、重启托盘
setx PAPER_MANAGER_PORT 9000
```

后端 `backend/server_config.py`、托盘启动器、`start.ps1`、`stop.ps1` 与 Vite 开发代理都从这一个变量取值。取值非法（非数字，或不在 1-65535 内）会直接报错退出，而不是静默回退到 8765——否则后端和托盘会各自监听到不同端口上，症状极难排查。

---

## 目录与数据

```text
<项目目录>/
├─ backend/                    FastAPI 后端
│  ├─ app.py                   应用入口、旧调用兼容层
│  ├─ api/                     按资源拆分的 HTTP 路由
│  ├─ services/                文献入库、AI 识别、分区查询、PDF 阅读器
│  ├─ db.py                    SQLite 连接、schema 与迁移
│  ├─ file_library.py          文件库路径与移动/重命名
│  ├─ server_config.py         监听地址与端口的唯一来源
│  └─ runtime.py               路由与服务的依赖边界
├─ model_bridge/                独立的 pi 订阅适配进程（Node.js）
├─ src/                        React + TypeScript 前端源码
├─ frontend/
│  ├─ dist/                    前端构建产物（后端直接托管它）
│  └─ .build-stamp             构建戳记：dist 由哪一版源码构建（需随 dist 提交）
├─ assets/
│  ├─ paper-manager.ico        托盘与快捷方式图标
│  └─ partition_tables/        2025 中科院分区、2026 新锐分区源表
├─ library_files/              ★ 文献文件库（PDF 实体，体积最大）
├─ data/                       ★ 运行数据（数据库、配置、日志）
│  ├─ library.sqlite3          SQLite 数据库
│  ├─ config.json              模型配置集合（API Key 明文保存）
│  ├─ model_auth.json          订阅 OAuth 凭据（本机保存）
│  ├─ pi_auth_context.json     ChatGPT 设备 UUID
│  ├─ pdf_viewer.json          PDF 阅读器偏好
│  └─ logs/                    launcher.log / server.log
├─ tests/                      pytest 用例
├─ scripts/
│  ├─ check-secrets.ps1        提交前密钥兜底检查
│  └─ resolve-port.ps1         两个启停脚本共用的端口解析
├─ pytest.ini                  测试收集配置
├─ launcher.pyw                托盘启动器
├─ prepare_environment.py      环境准备（venv / 依赖 / 前端构建）
├─ start.ps1 / stop.ps1        命令行启停
├─ create_shortcut.ps1         生成桌面快捷方式
└─ LICENSE                     MIT
```

**文献库备份**至少包括 `library_files/`（PDF 实体）与 `data/library.sqlite3`（记录、标签、分区结果）。配置文件含 API Key 或订阅授权凭据；只有在需要迁移模型配置时才复制 `data/config.json`、`data/model_auth.json` 和 `data/pi_auth_context.json`，并将它们当作敏感文件妥善保管。

`data/`、`library_files/`、`backups/`、`.venv/`、`node_modules/` 均已加入 `.gitignore`，不会进入版本库。

---

## 配置说明

### 模型配置

页面右上角 **设置 → 模型配置** 可保存多组配置，并单独选择当前配置。API 接入使用兼容 Chat Completions 的 Base URL、API Key 和手动填写的模型标识；订阅接入首版支持 ChatGPT、Claude 和 GitHub Copilot，通过本机 OAuth 登录后选择该账号可用的模型。订阅授权由锁定版本的 [`@earendil-works/pi-ai@0.99.1`](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md#oauth-providers) 适配进程处理，Copilot 模型目录遵循 pi 的账号可用性过滤。

ChatGPT、Claude 在该 pi 版本中显示的是厂商通用模型目录，并不等于当前订阅账号的实际可用模型；请用“测试连接”确认选中模型的调用权限。Copilot 在授权信息包含账号模型 ID 时按账号过滤。

保存配置不会自动启用。未登录或字段不完整的配置可以先保存，只有准备就绪的配置才能设为当前。测试只发送一个最小 JSON 请求，不会保存正在编辑的模型参数或切换当前项；编辑字段后需要重新测试。所有文献识别、中文题名、摘要生成和期刊提取共用当前配置。

配置集合按 `version: 2`、`active_profile_id`、`profiles` 写入 `data/config.json`。首次打开旧版三字段配置时，程序先生成 `config.json.before-model-config-<时间戳>.bak`，完整 API 配置会自动启用，部分填写的配置会保留但不启用，空配置会迁移为空列表。API Key 只在本地配置文件中保存，`GET /api/config` 和 `GET /api/state` 都会按配置 ID 掩码；保存或测试时只会用同一 ID 的已保存密钥补回掩码值。

OAuth 凭据单独保存在 `data/model_auth.json`，ChatGPT 所需的设备 UUID 保存在 `data/pi_auth_context.json`，两者都不会从前端接口返回。订阅登录中断或模型不可用时，不会切换到其他配置或读取环境变量中的 API Key。没有模型配置时，导入、标签、打开文件等本地功能仍然可用。

订阅适配进程使用锁定版本的 `undici@8.11.2`，使登录、令牌刷新和模型调用共同遵循代理配置：优先读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 中的非空值（同名小写变量优先），未设置代理变量时读取 Windows 已启用的手动系统代理。支持 HTTP、HTTPS 和 SOCKS5 代理，以及 `NO_PROXY`；未指定 `NO_PROXY` 时默认绕过本机回环地址。系统的 PAC 自动配置脚本不在此支持范围内。修改代理后需重启应用，确保新进程读取最新设置。

订阅 OAuth 的交互与模型目录能力按 pi 的[官方接入文档](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md#oauth-providers)实现；实际模型请求仍需在设置页用「测试连接」验证。

### PDF 阅读器

**设置 → PDF 阅读器** 可选系统默认程序，或在 Windows 上指定一个能接收 PDF 文件路径作为启动参数的 `.exe`。选择后立即生效；主文献和 PDF 补充文件共用此设置，其他补充文件仍由系统决定打开软件。macOS 和 Linux 继续使用系统默认程序。

偏好单独保存在 `data/pdf_viewer.json`，不会进入 Git，也不会改动模型配置。迁移工程后若原程序路径不存在，打开 PDF 时会回退到系统默认程序，设置页会提示重新选择。

### 期刊分区表

分区数据来自 `assets/partition_tables/cas_partition_2025.xlsx`（2025 中科院分区）与 `newcomer_partition_2026.xlsx`（2026 新锐分区），启动时导入数据库。换新表时替换同名文件后重启即可，或在设置页用「分区补查」重跑。

---

## 迁移与备份

数据库保存的是**相对于程序目录的路径**（例如 `library_files\论文标题.pdf`），启动时再按程序所在目录解析成绝对路径。这样把整个项目目录连同 `library_files` 和 `data` 一起移动到别的位置（包括换盘符）后，文献记录依然可用，不需要重新链接。

- `library_files` 内的文件写成相对路径；程序目录之外的文件（多为历史遗留记录）保持绝对路径，这类文件在项目目录移动后需要点「重新链接」重新指向。
- 首次启动新版本时会把库内的绝对路径一次性改写成相对路径，改写前自动备份为 `data/library.sqlite3.before-relative-paths-<时间戳>.bak`。改写只改数据库记录，不移动任何文件。
- 数据库只记录路径，不做内容校验：在程序外改名、移动或删除文件后，界面会显示该文件「缺失」，用详情页的「重新链接」重新指向即可。

**搬迁步骤**：停止服务 → 整体拷贝项目目录 → 在新位置重跑 `.\create_shortcut.ps1` → 启动。

**备份建议**：定期复制 `library_files/` 与 `data/library.sqlite3`。`data/library.sqlite3-wal` / `-shm` 是 SQLite 的运行时副产物，复制数据库前先停止服务，或连同 `-wal` 一起复制。

> ⚠️ **不要把项目目录放进 OneDrive / 坚果云 / 百度网盘等云同步目录**，除非你接受 API Key 和订阅凭据随目录外传。`data/config.json` 与 `data/model_auth.json` 都含有本机认证材料；云同步前先退出订阅账号并吊销或轮换相关凭据。

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 托盘提示「启动失败：端口 8765 已被其他程序占用」 | 别的进程占了端口 | 先 `.\stop.ps1`；不想查占用方就直接[换端口](#换端口)：`setx PAPER_MANAGER_PORT 9000` |
| 报错「未找到 npm，请安装 Node.js 并确认 npm 已加入 PATH」 | 前端确实需要重建，但机器上没有 Node.js | 安装 Node.js LTS 并**重开终端**；若你并不打算改前端，说明 `frontend/.build-stamp` 与 `dist` 对不上，见下一行 |
| 明明没改前端却要求装 Node.js | 提交时漏了 `frontend/.build-stamp`，程序无法确认 dist 的来源 | 在有 Node 的机器上跑一次 `python .\prepare_environment.py`，把更新后的戳记连同 `dist` 一起提交 |
| `npm run build` 报 Node 版本不满足 | Vite 7 要求 `^20.19` 或 `>=22.12` | 升级 Node.js |
| 双击快捷方式毫无反应 | 启动器异常 | 看 `data/logs/launcher.log` 尾部 |
| 网页能打开但界面空白 | `frontend/dist` 缺失或过期 | `python .\prepare_environment.py`，或手动 `npm run build` |
| 文献显示「缺失」 | 文件在程序外被改名 / 移动 / 删除 | 详情页「重新链接」重新指向 |
| 打开的 PDF 不是自选阅读器 | `data/pdf_viewer.json` 里记录的路径已失效 | 设置 → PDF 阅读器重新选择 |
| 订阅配置提示缺少运行环境 | 未安装 Node.js、版本低于 22.19.0 或 pi 依赖未安装 | 安装 Node.js 22.19.0+ 后重新运行 `python .\prepare_environment.py` |
| 订阅登录失败或模型目录为空 | OAuth 会话失效、账号没有对应订阅权限或模型目录变化 | 在模型配置中重新登录、刷新目录并选择当前可用模型 |
| ChatGPT 登录在令牌交换时返回 `403 / unsupported_country_region_territory` | OpenAI 拒绝了应用进程的请求，浏览器与应用可能使用不同网络出口 | 检查环境代理和 Windows 系统代理，重启应用后重试；所在地受支持且网络设置正确仍报错时联系 OpenAI 支持 |
| 文献状态显示「需要配置」 | 没有启用准备就绪的模型配置 | 设置 → 模型配置，完成保存、测试和启用 |
| 分区查不到结果 | 期刊名缺失，或期刊不在两张分区表内 | 详情页手动填写 / 修正期刊名后再查 |
| 换目录后快捷方式失效 | 快捷方式记住了旧路径 | 重跑 `.\create_shortcut.ps1` |
| 关闭浏览器后服务还在跑 | 有意设计，服务独立于页面 | 托盘菜单「停止服务」或 `.\stop.ps1` |
| `PAPER_MANAGER_PORT` 设了不生效 | 环境变量只对新开的进程有效 | 重开终端；托盘/快捷方式需先在托盘菜单「退出托盘」再重新启动 |
| `stop.ps1` 提示「端口被占用但它不像是文献管理器」 | 保护逻辑：避免误杀其他程序 | 确认后手动结束该进程，或[换端口](#换端口) |

---

## API 一览

后端全部接口都在 `127.0.0.1:8765` 下，返回 JSON（前端静态资源除外）。完整定义见 `backend/api/`。

| 资源 | 接口 |
|---|---|
| 系统 | `GET /api/health`；`GET /api/state`（可选参数：`sort` 取 `imported_desc` / `year_desc` / `year_asc`，`tag_id` 支持伪标签 `unclassified` / `failed`，另有 `tag_ids`、`status`） |
| 文献 | `POST /api/papers/select`、`PATCH /api/papers/{id}`、`DELETE /api/papers/{id}`、`POST /api/papers/{id}/reprocess`、`POST /api/papers/{id}/tags`、`POST /api/papers/{id}/translate-title`、`POST /api/papers/{id}/generate-abstract`、`POST /api/papers/{id}/lookup-partition`、`POST /api/papers/{id}/relink-file`、`POST /api/papers/{id}/open`、`POST /api/papers/{id}/open-folder` |
| 补充文件 | `POST /api/papers/{id}/attachments/select`、`DELETE /api/papers/{id}/attachments/{aid}`、`POST /api/papers/{id}/attachments/{aid}/open`、`POST /api/papers/{id}/attachments/{aid}/open-folder` |
| 标签 | `POST /api/tags`、`PUT /api/tags/{id}`、`DELETE /api/tags/{id}`、`POST /api/suggestions/{id}/approve`、`POST /api/suggestions/{id}/reject` |
| 分区 | `POST /api/partitions/lookup-all`、`GET /api/partitions/lookup-all/status`、`POST /api/partitions/lookup-all/cancel`、`POST /api/partition-suggestions/{id}/approve`、`POST /api/partition-suggestions/{id}/reject` |
| 文件冲突 | `POST /api/file-conflicts/{id}/open-folder`、`POST /api/file-conflicts/{id}/resolve` |
| 文件库 | `GET /api/library`、`POST /api/library/open`、`POST /api/library/migrate` |
| 模型配置 | `GET /api/config`、`PUT /api/config`、`POST /api/config/test`、`POST /api/config/active` |
| 订阅登录 | `GET /api/model-providers`、`GET /api/model-configs/{id}/models`、`GET/POST/DELETE /api/model-configs/{id}/auth`、登录会话的 `GET/POST/DELETE /api/model-config-sessions/{id}` |
| PDF 阅读器 | `GET /api/pdf-viewer`、`PUT /api/pdf-viewer`、`POST /api/pdf-viewer/select` |

> 交互式文档：服务运行后访问 <http://127.0.0.1:8765/docs>。
> 未匹配的 `/api` 路径（包括裸 `/api`）一律返回 **404 + JSON**（`{"detail": "未知接口：..."}`），不会落到前端兜底路由，所以脚本可以放心地按状态码和 JSON 判断成败。

---

## 开发

### 代码结构

- `backend/api/` 按系统状态、文献、标签、分区、文件、设置和前端静态资源组织 HTTP 路由；`backend/app.py` 保留应用初始化和旧调用入口。
- `backend/api/frontend.py` 除托管前端外，还负责把未匹配的 `/api` 路径挡成 404 + JSON。它必须排在所有真实 API 路由之后注册，否则会盖住真正的接口。
- `backend/services/papers.py` 负责文献入库、AI 识别和期刊分区查询流程；`backend/db.py` 负责 SQLite 连接、schema 和迁移；文件路径操作在 `backend/file_library.py`。
- `backend/server_config.py` 是监听地址与端口的唯一来源；`scripts/resolve-port.ps1` 给两个 PowerShell 脚本提供同一份解析逻辑。改端口只需要动环境变量，不用改代码。
- PDF 阅读器偏好和打开策略在独立的 PDF 阅读器服务中，设置接口与文献打开接口共用它，不依赖模型配置或文献数据库结构。
- `backend/runtime.py` 为路由和服务提供应用依赖边界。当前兼容层仍从 `backend.app` 动态读取配置和可替换函数，后续可逐步把这些依赖改为显式服务接口。
- `prepare_environment.py` 负责 venv、Python 依赖和前端构建，托盘启动器与 `start.ps1` 共用它。前端是否需要重建由 `build_input_hash()` 的内容哈希 + `frontend/.build-stamp` 判定，**不要退回用文件修改时间判断**：`git clone` 会把 mtime 统一成检出时间，得出的结论与内容无关。
- `backend/models.py` 和 `src/types.ts` 分别维护前后端数据模型；**API 字段调整时需要同步检查这两处**及对应页面。前端请求封装在 `src/api/client.ts`，文献详情组件在 `src/components/PaperDetails.tsx`。
- 页面路由不是 SPA 路由：前端所有界面都在 `src/main.tsx` 中按状态切换，后端只做静态文件托管。

### 测试

```powershell
& .\.venv\Scripts\python.exe -m pytest        # 99 个用例，约 4 分钟
pytest                                        # 同上；pytest.ini 已配好收集范围
& .\.venv\Scripts\python.exe -m pytest -q -p no:warnings   # 关掉第三方告警噪声
```

用例覆盖核心入库/标签/分区逻辑（`tests/test_core.py`）与 PDF 阅读器设置（`tests/test_pdf_viewer.py`）。测试用临时目录替换数据库和文件库，不会碰你的真实数据。

收集范围由 `pytest.ini` 固定为 `tests/`：项目根目录下可能存在同名的测试快照目录（如 `backups/paper-manager-*/tests/`），不限定范围的话 pytest 会因为两个 `test_core.py` 重名而以 `import file mismatch` 直接中止。`pythonpath = .` 保证 `tests/` 无论用 `pytest` 还是 `python -m pytest` 启动都能 `import backend`。

前端类型检查：

```powershell
npm run typecheck
```

### 提交前检查（密钥兜底）

```powershell
pwsh scripts/check-secrets.ps1
```

首次运行会把自己安装为本仓库的 `pre-commit` 钩子，之后每次提交自动拦截：

- **路径规则**：`data/`（除 `.gitkeep`）、`backups/`、`.env*`、`*.log`、`*.tmp`、`*.sqlite3` 一旦入库即报错
- **内容规则**：`sk-` 开头的长串，或 `api_key` / `secret` / `token` 后跟长值的赋值形态
- **忽略规则自检**：确认 `data/config.json`、`backups/` 仍被 `.gitignore` 有效忽略

钩子装在 `.git/hooks/` 下，不入版本库：**换机器或重新克隆后需重新运行一次**。紧急情况可用 `git commit --no-verify` 跳过。

---

## 隐私与安全

- **服务仅供本机访问**：只绑定 `127.0.0.1`，外网与局域网无法直接访问；CORS 只放行 `127.0.0.1:5173` 和 `localhost:5173`（开发用）。调用 AI 和订阅授权时，程序会向服务商发出网络请求。
- **密钥不回传**：`GET /api/config` 与 `GET /api/state` 均以掩码形式返回 `********`，密钥不会经 HTTP 接口回传前端。
- **AI 请求内容**：按任务发送 PDF 文件名、首页文本、原文或中文题名，以及主题标签的 ID、名称、描述和使用次数。首页文本可能包含作者、单位或邮箱，标签描述也可能包含个人填写的信息；这些文本会交给当前模型服务商处理。PDF 文件本身、首页截图和全文不会上传。
- **订阅授权**：浏览器在服务商页面完成登录，访问令牌和刷新令牌仅保存在本机运行数据目录，不返回前端。API 和订阅错误提示会脱敏常见令牌、密钥和代理密码字段。
- **认证材料是本机文件**：`data/config.json` 明文保存 API Key，`data/model_auth.json` 保存 OAuth 令牌，迁移备份也可能包含旧 API Key。这些文件均在 Git 忽略的 `data/` 目录中，但复制到云盘、分享或不可信备份前应先评估泄漏风险。

**以下任一情况成立，请立即吊销并轮换 API Key**：

1. 项目目录开启云同步（OneDrive / 坚果云 / 百度网盘等）
2. 开启整机或目录备份，且备份会离开本机
3. 项目目录被拷贝、打包或分享给他人
4. 新增 CI 流程、自动推送脚本或其他自动化提交
5. 改为多人协作，或本机开始被他人使用、安装了来源不明的软件

轮换方式：到模型服务商控制台吊销旧密钥、生成新密钥，覆盖写入 `data/config.json` 与 `backups/` 下的历史副本，再运行 `pwsh scripts/check-secrets.ps1` 复检。

---

## 已知限制

- **不递归扫描文件夹**：每次导入都需要手动选择文件，不做全盘或目录监听。
- **不索引 PDF 全文**：检索范围是标题、译名、摘要、备注、作者、单位、DOI 和标签，不含 PDF 正文内容。
- **单机单用户**：没有登录、权限和多人协作，数据库是本地 SQLite。
- **不保存 PDF 内容**：数据库只记录路径。文件在程序外被移动或改名后需要手工「重新链接」。
- **Windows 优先**：托盘、快捷方式、原生文件选择器、Explorer 定位均为 Windows 专用；macOS / Linux 只保证后端可跑。
- **AI 抽取质量取决于模型**：首页文本不完整或模型能力不足时，标题 / DOI 可能抽错，可在详情页手动修正后「重新提取」。

---

## 许可

[MIT](LICENSE) © 2026 zhangsanbase。可自由使用、修改、分发，保留版权声明即可。
