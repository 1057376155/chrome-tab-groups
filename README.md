# Chrome Tab Group Manager

一个用 **Python + PyQt6 + SQLite + Chrome 扩展** 实现的 Chrome 标签组管理工具。

## 功能

- **离线读取标签组**：直接解析 Chrome 的 SNSS 二进制 session 文件，即使 Chrome 被关闭或卸载，也能恢复分组数据。
- **持久化保存**：所有 profile、快照、窗口、标签组、标签页都保存在本地 SQLite 数据库。
- **窗口维度**：树形结构展示 profile → snapshot → window → group → tab，按 Chrome 原样保留窗口归属、组名、颜色、顺序；跨窗口的同名组也能区分。
- **散落标签保留**：未分组的标签按所属窗口归入虚拟「(未分组)」组，不再丢失。
- **点击打开**：单击/双击标签页或标签组可打开 Chrome。
- **Chrome 扩展桥接**：内置一个小型 MV3 扩展，可"实时捕获"当前 Chrome 标签组（含窗口归属），并支持按单组或按整窗口恢复为带颜色的原生标签组。

## 安装

```bash
cd chrome-tab-groups
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 安装 Chrome 扩展

1. 打开 Chrome，进入 `chrome://extensions/`。
2. 开启右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”，选择本项目的 `chrome_extension` 文件夹。
4. 扩展图标会出现在工具栏，点击可查看连接状态并手动触发“保存当前标签组”。

## 运行

```bash
./run.py
```

## 使用

- **从文件扫描**：点击工具栏"从文件扫描"，自动读取所有 Chrome profile 的 session 文件并保存（含窗口归属和散落标签）。
- **从 Chrome 捕获**：先打开 Chrome 并安装扩展，再点工具栏"从 Chrome 捕获"。
- **打开标签页/组**：选中标签或组，点击"打开"或双击。
- **恢复为 Chrome 标签组**：选中一个组，点击"恢复为 Chrome 标签组"，扩展会自动创建窗口、打开标签并重建同名的彩色标签组。
- **恢复窗口**：选中一个窗口节点，点击"恢复窗口"，扩展会在一个新 Chrome 窗口里重建该窗口的全部标签组（保留颜色和分组）。

## 项目结构

```
chrome-tab-groups/
├── chrome_extension/        # Chrome MV3 扩展
│   ├── manifest.json
│   ├── background.js
│   ├── popup.html
│   └── popup.js
├── tabgroup_manager/        # Python 主程序
│   ├── __main__.py
│   ├── bridge.py            # 本地 HTTP bridge
│   ├── chrome_opener.py     # 用 AppleScript 打开 Chrome
│   ├── config.py            # 路径/常量
│   ├── db.py                # SQLite 数据层
│   ├── gui.py               # PyQt6 主界面
│   └── snss_parser.py       # Chrome SNSS 解析器
├── data/
│   └── tab_groups.db
├── run.py                   # 启动脚本
└── requirements.txt
```

## 致谢

SNSS 文件解析逻辑参考了 [holzerjm/ChromeGroupTabRecovery](https://github.com/holzerjm/ChromeGroupTabRecovery)，MIT 许可证。
