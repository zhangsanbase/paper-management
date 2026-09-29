# 本地科研文献管理器 V1

这是一个本地 PDF 文献标签管理原型。它不会递归扫描文件夹，也不会把 PDF 内容写进数据库；网页按钮会调用本地后端弹出文件选择框，保存文件路径，并用本机默认或自选的 PDF 软件打开文献。导入、重链接或添加补充文件后，程序会把源文件移动到项目内 `library_files` 文件库，并按论文标题尝试重命名。

## 功能

- 手动选择一个或多个 PDF 入库
- 读取 PDF 首页文本，调用 OpenAI 兼容 API 抽取标题、作者、单位、出版时间、DOI
- 将本地已有标签传给 AI，优先匹配已有研究主题标签
- 新标签只进入待确认区，确认后才加入正式标签库
- 导入时自动生成中文题名和中文摘要，也可手动填写或单独调用 AI 重新生成
- 根据出版时间自动生成年份标签，并支持按年份排序
- 自动提取期刊名并查询 2025 中科院分区、2026 新锐分区；支持在详情页单篇重查和设置页批量补查。唯一结果直接显示在文献列表中，同名多结果进入待确认区；分区标签不加入全局主题标签库
- 编辑主题标签的名称、别名和说明
- 拦截 XPS、SEM、XRD、Raman、FTIR、CV、EIS 等常规方法词作为主标签
- 点击标签筛选文献，支持无主题标签和提取异常视图
- 调用系统默认或自选的 PDF 软件打开主文献和 PDF 补充文件
- AI 成功识别新导入文献标题后，自动把本地 PDF 源文件移动到 `library_files` 并重命名为论文标题
- 为文献关联本地补充文件，移动到 `library_files` 并按 `论文标题_sp.ext` 尝试重命名，保存路径并可直接打开
- 自动重命名遇到同名文件时弹出冲突确认，可选择打开文件夹、自动编号、使用已有文件或取消
- 右键文献可新建并关联标签，或移除文献记录；移除时可选择将文件库内的主 PDF 和补充文件一并移入回收站

 AI 默认导入流程只发送 PDF 文件名、PDF 首页文字和本地已有主题标签库；不会上传 PDF 文件、首页截图或全文。导入时会基于同一份首页文字自动生成中文题名和中文摘要，单独的翻译/摘要按钮仍可用于结果不满意或为空时重新生成。文件库只集中管理本地文件路径和源文件位置，数据库不保存 PDF 文件内容。

## 代码结构

- `backend/api/` 按系统状态、文献、标签、分区、文件、设置和前端静态资源组织 HTTP 路由；`backend/app.py` 保留应用初始化和旧调用入口。
- `backend/services/papers.py` 负责文献入库、AI 识别和期刊分区查询流程；`backend/db.py` 负责 SQLite 连接、schema 和迁移；文件路径操作在 `backend/file_library.py`。
- PDF 阅读器偏好和打开策略在独立的 PDF 阅读器服务中，设置接口与文献打开接口共用它，不依赖 AI 配置或文献数据库结构。
- `backend/runtime.py` 为路由和服务提供应用依赖边界。当前兼容层仍从 `backend.app` 动态读取配置和可替换函数，后续可逐步把这些依赖改为显式服务接口。
- `backend/models.py` 和 `src/types.ts` 分别维护前后端数据模型；API 字段调整时需要检查两处及对应页面。前端请求封装在 `src/api/client.ts`，文献详情组件在 `src/components/PaperDetails.tsx`。
- `prepare_environment.py` 统一处理 Python 虚拟环境、依赖安装和前端构建；托盘启动器与 `start.ps1` 共用这套准备流程。

## 路径策略

数据库保存的是**相对于程序目录的路径**（例如 `library_files\论文标题.pdf`），启动时再按程序所在目录解析成绝对路径去访问文件。这样把整个项目目录连同 `library_files` 和 `data` 一起移动到别的位置（包括换盘符）后，文献记录依然可用，不需要重新链接。

- 文件库 `library_files` 内的文件都会写成相对路径；程序目录之外的文件（多为历史遗留记录）保持绝对路径，这类文件在项目目录移动后需要点「重新链接」重新指向。
- 首次启动新版本时会把库内的绝对路径一次性改写成相对路径，改写前自动备份为 `data/library.sqlite3.before-relative-paths-<时间戳>.bak`，改写只改数据库记录，不移动任何文件。
- 数据库只记录路径，不做内容校验：在程序外改名、移动或删除文件后，界面会显示该文件「缺失」，用详情页的「重新链接」重新指向即可。

## 快速启动

推荐使用桌面快捷方式启动。它会隐藏命令行窗口，启动系统托盘程序，并在后台启动本地服务后自动打开 `http://127.0.0.1:8765`。

```powershell
.\create_shortcut.ps1
```

之后双击桌面的“科研文献管理器”即可。脚本也会在项目目录内生成同名快捷方式，并使用 `assets/paper-manager.ico` 作为图标。迁移项目目录后，可运行 `launch_tray.vbs` 或重新运行 `create_shortcut.ps1` 刷新快捷方式路径。托盘菜单提供“打开界面”“重启服务”“停止服务”“退出托盘”和当前状态。关闭浏览器页面不会停止后端服务；需要停止时使用托盘菜单。

托盘启动器会自动处理 `.venv`、Python 依赖、`node_modules` 和前端构建。日志保存在 `data/logs/launcher.log` 和 `data/logs/server.log`。

命令行备用启动方式如下：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
npm install
npm run build
python -m backend.app
```

服务启动后会优先自动打开 Edge 访问 `http://127.0.0.1:8765`。也可以运行：

```powershell
.\start.ps1
```

停止服务：

```powershell
.\stop.ps1
```

`start.ps1` 会复用已装好的环境：venv 不存在时自动创建，`requirements.txt` 有变动时自动补装依赖，前端源码比构建产物新时自动 `npm run build`。服务已在运行时再次执行它只会打开浏览器，不会重复启动。

如果只想启动服务、不自动打开浏览器，可以先设置环境变量：

```powershell
$env:PAPER_MANAGER_SKIP_BROWSER='1'
python -m backend.app
```

## AI 配置

在页面右上角配置 OpenAI 兼容 API：

- `base_url`，例如 `https://api.openai.com/v1`
- `api_key`
- `model`

配置保存在 `data/config.json`。如果没有配置 API，PDF 仍会入库，但状态会显示需要配置。

## PDF 阅读器

在「设置 → PDF 阅读器」中可选择系统默认程序，或在 Windows 上指定一个能接收 PDF 文件路径作为启动参数的 `.exe` 程序。选择后立即生效，主文献和 PDF 补充文件共用此设置；其他补充文件仍由系统决定打开软件。macOS 和 Linux 目前继续使用系统默认程序。

阅读器偏好单独保存在本机的 `data/pdf_viewer.json`，不会进入 Git，也不会改动 AI 配置。迁移工程后若原程序路径不再存在，打开 PDF 时会回退到系统默认程序，设置页会提示重新选择。
