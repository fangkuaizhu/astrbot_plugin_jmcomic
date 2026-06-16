# JMComic AstrBot Plugin

<!-- markdownlint-disable MD033 -->

<p align="center">
  <b>🚀 禁漫天堂本子PDF下载插件</b><br>
  发送车号自动下载、合并为PDF、直接发送到QQ/Telegram/微信
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D4.17.0-blue" alt="AstrBot">
  <img src="https://img.shields.io/badge/Python-3.10+-green" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

## ✨ 特性

- 📥 **一键下载**：发送车号自动下载本子
- 🔍 **关键词搜索**：按关键词搜索本子车号
- 📄 **PDF生成**：自动合并图片为PDF，支持大本子（页数限制可配）
- 💾 **临时存储**：PDF发送后自动清理，不占磁盘空间
- 🔗 **跨容器兼容**：支持 NapCat + AstrBot 分离部署，挂载共享目录即可

## 📦 安装

### 方式一：手动安装（推荐）

1. 克隆仓库到 AstrBot 的 `data/plugins/` 目录：

```bash
cd /path/to/astrbot/data/plugins/
git clone https://github.com/fangkuaizhu/astrbot_plugin_jmcomic.git
```

2. 安装依赖：

```bash
pip install -r astrbot_plugin_jmcomic/requirements.txt
```

3. 重启 AstrBot 或在 WebUI 中重载插件。

### 方式二：直接下载

下载[最新源码](https://github.com/fangkuaizhu/astrbot_plugin_jmcomic)并解压到 `data/plugins/` 目录，然后安装依赖。

### 方式三：AstrBot WebUI（待上架）

插件已提交官方市场审核，审核通过后可直接在 WebUI 中搜索安装。

## 🎮 使用方法

### 下载本子PDF

```
/jm <车号>
```

示例：
- `/jm 350234`
- `/jm JM350234`

自动下载本子所有图片 → 合并为PDF → 发送到聊天 → 发送后自动清理。

### 搜索本子

```
/jm搜索 <关键词>
```

示例：
- `/jm搜索 原神`

返回搜索结果列表（包含车号），可直接用 `/jm <车号>` 下载。

## ⚙️ 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `client_impl` | string | `api` | 客户端类型：`api` (移动端) 或 `html` (网页端) |
| `max_pages` | int | `300` | PDF最大页数限制，超出部分截断 |
| `jm_temp_root` | string | `/AstrBot/data/jmcomic_temp` | 临时文件目录（高级，一般不动，可通过 AstrBot WebUI 修改） |

## 🔧 NapCat + AstrBot 分离部署配置

如果你使用 NapCat 作为 QQ 协议端，且 NapCat 和 AstrBot 分别运行在不同容器中，需要共享临时文件目录才能正常发送文件。

### 方案：共享目录挂载（推荐）

在 `docker-compose.yml` 中为 NapCat 容器添加 volume 挂载：

```yaml
services:
  napcat:
    volumes:
      # 已有的配置
      - ./napcat_config:/app/napcat/config
      # 新增：共享JMComic临时目录，路径与AstrBot一致
      - ./astrbot_data/jmcomic_temp:/AstrBot/data/jmcomic_temp:ro
```

这样 NapCat 就能访问到 AstrBot 生成的 PDF 文件了（`:ro` 只读挂载，安全无副作用）。

## 📁 目录结构

```
astrbot_plugin_jmcomic/
├── __init__.py         # 插件入口
├── _conf_schema.json   # AstrBot V4 配置模式
├── main.py             # 核心逻辑 / 命令处理
├── jm_client.py        # JMComic API 封装
├── pdf_maker.py        # 图片转PDF模块
├── metadata.yaml       # 插件元数据
├── requirements.txt    # Python 依赖
├── README.md           # 本文件
├── LICENSE             # MIT 许可证
└── .gitignore
```

## 🧪 依赖

- `jmcomic>=2.7.0` — 禁漫天堂API客户端
- `Pillow>=10.0.0` — 图片处理
- `img2pdf>=0.5.0` — 图片转PDF

## 📝 注意事项

- 请勿频繁请求，爱护禁漫服务器 🙏
- PDF发送后自动清理临时文件
- 超过 `max_pages` 限制的本子会截断处理
- 如需修改临时目录路径，可在 AstrBot WebUI → 插件配置中设置

## 🤝 贡献

欢迎提 Issue 和 PR！

## 📄 许可证

[MIT](LICENSE)
