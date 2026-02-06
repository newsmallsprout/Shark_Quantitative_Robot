# Gate Attack Quant Bot

## English

### Introduction
Gate Attack Quant Bot is a high-frequency quantitative trading system designed for crypto futures markets. It features a unique "Attack Mode" for aggressive trend following and a strict "Risk Engine" to protect capital.

### Key Features
*   **Dual Mode Trading**: Neutral (Mean Reversion) and Attack (Trend Following).
*   **AI Integration**: Supports DeepSeek, OpenAI, and local LLMs for market analysis.
*   **Risk Engine**: Hard limits on daily drawdown and position risk.
*   **Real-time Dashboard**: React-based UI for monitoring and control.

### Installation
1.  **Backend**:
    ```bash
    pip install -r requirements.txt
    python src/api/server.py
    ```
2.  **Frontend**:
    ```bash
    cd frontend
    npm install
    npm run dev
    ```

### Configuration
Edit `config/settings.yaml` (template provided) to set your API keys and risk parameters.

---

## 中文 (Chinese)

### 简介
Gate Attack Quant Bot 是一款专为加密货币合约市场设计的高频量化交易系统。它结合了激进的趋势跟随“进攻模式”和严格的风控引擎，旨在在保护本金的同时最大化收益。

### 核心功能
*   **双模式交易**: 中性（均值回归）和进攻（趋势跟随）。
*   **AI 集成**: 支持 DeepSeek、OpenAI 和本地 LLM 进行市场分析。
*   **风控引擎**: 每日回撤和单笔风险的硬性限制。
*   **实时仪表盘**: 基于 React 的用户界面，用于监控和控制。

### 安装指南
1.  **后端**:
    ```bash
    pip install -r requirements.txt
    python src/api/server.py
    ```
2.  **前端**:
    ```bash
    cd frontend
    npm install
    npm run dev
    ```

### 配置
编辑 `config/settings.yaml`（已提供模板）以设置您的 API 密钥和风控参数。
