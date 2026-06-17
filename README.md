# SysInfo Tool - 硬件信息检测工具

一款 Windows 平台的硬件信息检测、实时监控与 AI 分析工具，使用 Python 开发。

## 功能

### 硬件信息

- **CPU**：名称、制造商、核心/线程数、最大频率
- **GPU**：名称、显存容量、驱动版本（自动过滤虚拟显卡，多显卡自动编号）
- **内存**：每条内存的品牌、型号、容量、频率，以及总容量（多条自动编号）
- **硬盘**：每块硬盘的型号、制造商、容量（多块自动编号）

### 实时监控

- **CPU**：使用率、温度、频率（含睿频，通过 ProcessorInformation WMI 获取真实频率）
- **GPU**：使用率、显存占用率、温度、频率、显存用量（智能选择主 GPU：混合模式监控独显，集显模式监控集显）
- **内存**：使用率、用量详情、实际运行频率
- **交换分区**（虚拟内存）使用率
- **磁盘**：各分区使用率、读写速度
- **网络**：收发流量

### 系统信息

- 操作系统版本、计算机名、系统架构
- Python 版本、系统启动时间
- 网络适配器及 IP 地址

### 监控记录

- 手动开始/停止记录硬件监控数据
- 可选采样间隔：1 秒 / 2 秒 / 5 秒
- 表格展示历史记录（时间、CPU%、CPU温度、CPU频率、GPU%、GPU温度、显存%、内存%、内存频率）
- 导出为 CSV 文件（UTF-8 编码，可直接用 Excel 打开）
- 一键清空记录

### AI 分析

- 支持所有 OpenAI 兼容格式的 API（OpenAI、DeepSeek、Claude 等）
- 只需填写 API 地址、API Key、模型名称
- 自动将硬件配置 + 监控记录数据发送给 AI 分析
- AI 会分析性能瓶颈（CPU/GPU 过热降频、内存不足、显存溢出等）并给出优化建议
- API 地址自动补全（输入基础地址即可，如 `https://api.deepseek.com`）

## 本地运行

### 方式一：直接运行 Python 脚本

**环境要求**：Python 3.10+

```bash
# 1. 安装依赖
pip install wmi psutil nvidia-ml-py pythonnet

# 2. 运行
python main.py
```

### 方式二：打包为 exe 后运行

```bash
# 1. 安装依赖（含打包工具）
pip install -r requirements.txt

# 2. 双击 build.bat 或手动执行
pyinstaller --onefile --windowed --name "SysInfoTool" main.py

# 3. 生成的 exe 位于 dist\SysInfoTool.exe
```

## 项目结构

```
SysInfoTool/
├── main.py           # 主程序（GUI 界面 + 监控记录 + AI 分析）
├── detector.py       # 硬件检测与监控模块
├── requirements.txt  # Python 依赖
├── build.bat         # 一键打包脚本
└── README.md         # 说明文档
```

## 技术栈

- **Python 3** + **tkinter**：GUI 界面（深色主题）
- **WMI**：CPU / GPU / 内存 / 硬盘硬件信息读取，CPU 温度与频率监测
- **psutil**：CPU 使用率、内存、磁盘、网络实时监控
- **pynvml**：NVIDIA GPU 实时监控（使用率、温度、频率、显存）
- **urllib**：AI API 请求（OpenAI 兼容格式）
- **pyinstaller**：打包为 Windows 可执行文件

## AI 分析使用示例

| API 供应商 | API 地址 | 模型名称 |
|-----------|---------|---------|
| OpenAI | `https://api.openai.com` | `gpt-4o` |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| Claude | `https://api.anthropic.com/v1/messages` | `claude-sonnet-4-20250514` |

## 界面说明

- **Tab 切换**：顶部工具栏可切换"实时监控"和"监控记录"两个页面
- 深色主题（深紫配色），支持鼠标滚轮滚动浏览
- 右上角提供"刷新"和"复制全部"按钮
- 进度条颜色：绿色（<60%）→ 黄色（60-85%）→ 红色（>85%）
