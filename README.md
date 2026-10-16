# LDS Server

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)
[![Architecture](https://img.shields.io/badge/Architecture-AsyncIO%20Gateway-orange.svg)]()

- **原生协议与沙箱兼容**：
  - 内置 Flash CrossDomain 安全策略服务（`843` / `8990` 策略嗅探回放）。
  - 原生支持 **AMF3** 对象的二进制编解码与自定义 TCP Framing 上下行分帧解析。
- **完整的角色与战斗系统**：
  - **角色属性与成长**：严密对齐官方数值公式的六维属性、战力（pba）、等级成长与时装属性加成。
  - **战宠与战宠守护**：支持战宠伴生跟随、升级/觉醒、守护槽位（攻/防/血/蓝/暴击/抗性）加成计算。
  - **背包与商城**：支持穿脱装备、消耗品使用、装备强化、宝石镶嵌、商城购买与珍宝天箱系统。
基于 Python 原生 `asyncio` 构建的经典 Flash 页游《龙斗士》私有化/单机协议仿真服务端。项目实现了从 Flash 安全沙箱策略、TCP 自定义分帧/AMF3 编解码、场景漫游、战斗副本引擎到角色数据持久化与 GM 后台的全套闭环。

---

## 🌟 核心特性

- **高性能异步通信**：基于 Python `asyncio` 事件循环，支持跨端口策略探测、并发长连接调度与高效的异步 I/O 处理。
  - **全副本通用引擎 (`dungeon_engine`)**：支持多关卡场景切换、怪物批量生成（`2302`）、伤害判定与血量同步（`2411`/`2421`）、翻牌结算及通关奖励。
- **便捷的 GM 管理**：
  - 内置 Web HTTP 调试端点（支持通过 `static/gm.html` 可视化管理道具、货币与宠物）。
  - 游戏内聊天框快捷 GM 指令（如 `/fb 副本ID 难度`、`/vip 等级`、`/pet 宠物ID` 等）。
- **完善的自动化测试**：覆盖 AMF3 编解码、加解密、协议分帧、背包流转与主线教学副本的单元/集成测试套件。

---

## 📁 目录结构

```text
lds_server/
├── run_server.py                 # 服务端主入口（同时拉起 TCP 网关与 HTTP API）
├── server/
│   ├── gateway.py                # 核心 TCP 游戏网关服务（命令分发与长连接状态）
│   ├── dungeon_engine.py         # 副本状态机、战斗计算与实体下发引擎
│   ├── player_storage.py         # 角色存档、背包资产、属性公式与战宠守护
│   ├── policy_server.py          # 跨域安全策略服务 (Port 843)
│   ├── encyclopedia.py           # 游戏图鉴与商城元数据定义
│   ├── task_catalog.py           # 任务与主线教学指引元数据
│   ├── http_api.py               # 轻量 HTTP 辅助 API
│   │
│   ├── protocol/                 # 网络协议解析层
│   │   ├── amf3.py               # AMF3 数据包序列化与反序列化
│   │   ├── framing.py            # 上下行二进制封包解包与分帧状态机
│   │   └── cipher.py             # 协议通信加解密模块
│   │
│   ├── data/                     # 基础运行配置与本地持久化
│   │   ├── action_presets.pkl    # 动作状态机序列基线
│   │   ├── monsters_catalog.json # 副本实体与属性基础配置
│   │   ├── pet_catalog.json      # 战宠基础属性与资质配置
│   │   └── player_data.json      # 运行时角色持久化数据 (本地开发生成)
│   │
│   ├── static/                   # 静态 Web 资源（包含 GM 控制台 HTML）
│   │   └── gm.html
│   │
│   └── tests/                    # 单元测试与集成测试
│       ├── test_amf3.py
│       ├── test_fb10_tutorial.py
│       ├── test_framing.py
│       └── ...
└── LICENSE                       # AGPL-3.0 开源协议
```
## 🚀 快速启动

### 1. 环境要求
- **Python**: `>= 3.11`（推荐使用 Python 3.12 或 3.13）
- **操作系统**：Windows / Linux / macOS

### 2. 克隆仓库与启动服务
```bash
git clone [https://github.com/lvyijie/lds_server.git](https://github.com/lvyijie/lds_server.git)
cd lds_server

# 启动服务（TCP 网关监听 8990/8001，安全策略监听 843，HTTP API 监听 8080）
python run_server.py
```
## ⚖️ 免责声明 (Disclaimer)

1. **项目性质与用途**  
   本项目（`lds_server`）为开发者个人基于网络协议通信机制、分布式系统架构及 Python 异步 I/O 开发的研究与原型实现项目，仅用于计算机科学与网络编程技术的学术探讨与学习交流。

2. **数字资产与版权归属**  
   - 本项目仅包含自主编写的后端服务网络逻辑代码，**不包含、不分发、亦不提供任何原游戏官方的客户端文件、专有 Flash 资源（SWF）、贴图、音频、模型或商业受保护资产**。
   - 本项目所涉及的特定协议兼容性研究，相关游戏的一切商标、名称、世界观设定、视觉设计及著作权均归其原开发商与运营商所有。

3. **非商业化与禁止滥用原则**  
   - 本项目完全遵循非商业化原则，作者未通过本项目获取任何形式的经济收益，亦未设立任何付费赞助渠道。
   - **严格禁止任何组织或个人将本项目用于任何形式的商业运营、公开搭建收费服务器、打包倒卖、制作盗版客户端或进行非法牟利活动。**

4. **责任限制与行为自负**  
   - 本项目按“现状”（As-Is）提供，不包含任何明示或暗示的担保（详见 AGPL-3.0 协议第 15、16 条条款）。
   - 任何第三方下载、克隆、修改、运行或分发本项目的行为，均属于该第三方的独立行为。
   - 使用者因违反当地法律法规、侵犯第三方知识产权或违反相关软件服务协议所产生的一切民事、行政及刑事法律责任，**均由行为人自行独立承担，与本项目原作者及贡献者概无关联**。
