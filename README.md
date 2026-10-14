# 听与读 EchoRead

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows)](https://www.microsoft.com/windows)
[![Version: 1.0.0-beta](https://img.shields.io/badge/Version-1.0.0--beta-orange)](https://github.com/)

## 什么是听与读？

把 TED、B站、YouTube 上的精彩视频变成你的私人英语练习场。**一键下载**音视频和字幕，**逐句跟读**找出发音漏洞，**听写练习**强化耳朵，**AI 纠音**给你专业反馈。所有数据都在本地，完全私密，完全免费（只需自备大模型 API Key，我们有送的免费额度）。

## 核心特性

✨ **智能下载** — 粘贴链接，自动获取音频、字幕和文稿。三层兜底字幕策略（CC 优先 → 语音转写 → LLM 翻译），下载即成品

🎤 **逐句跟读** — 录音后 AI 逐词识别你的发音，标出错漏多读，给出舌头位置提示，点词收藏常错单词

👂 **听力练习** — 全文听写、填空、首字母提示三档难度自选，AI 批改并解释你为什么听错

📊 **学习中心** — 打卡热力图、14 天进度趋势、生词数量、AI 总结你的学习效果，9 套主题皮肤随意切换

🎯 **智能任务** — AI 每天根据你的练习情况生成 4-6 个任务，帮你高效复习生词、难句和新语料

📚 **生词本** — 点词查看音标、释义、例句和记忆技巧，遗忘曲线复习提醒，一键导出 Markdown

🔍 **句法解析** — AI 拆解复杂句子的语法结构、短语搭配和词汇用法，划线分析任意片段

🛠️ **语料管理** — AI 自动生成场景标签和难度评级，支持话题快筛、句子合并拆分、时间轴校准、诊断修正

## 三步上手

### 1️⃣ 安装

**最简单的方式**：下载 `setup.exe`，双击安装，完成

**开发者方式**：
```bash
git clone https://github.com/你的用户名/EchoRead.git
cd EchoRead
pip install -r requirements.txt
python app.py
```

### 2️⃣ 配置 API Key

获取免费 API Key（每个月送你免费额度）：
- 访问 [阿里云百炼官网](https://bailian.console.aliyun.com/?apiKey=1#/api-key)
- 注册账号，开通百炼服务
- 复制你的 API Key
- 在 EchoRead 设置页粘贴，保存

### 3️⃣ 开始练习

1. 粘贴 YouTube / TED / B站视频链接
2. 点「下载」，等待音频和字幕准备好
3. 选择练习模式（跟读 / 听写 / 学习）
4. 开始练习，获得 AI 反馈

## 模型分工（小白版）

| 干什么 | 用哪个模型 | 需要你管吗 |
|------|---------|--------|
| 语音识别（识别你的发音） | Paraformer（本地） | ❌ 无需操心 |
| 发音纠正、语法讲解 | 阿里通义千问 | ✅ 需要 API Key |
| 字幕翻译、AI 摘要 | 阿里通义千问 | ✅ 需要 API Key |
| 语音合成（播放英文） | 阿里云 TTS / Edge TTS | ✅ 需要 API Key（或免费 Edge） |

**关键点**：Paraformer 和本地处理都在你电脑里跑，不上传任何数据，100% 隐私

## 内置示例语料

开箱即用的 5 条精选语料：

1. **TED: "How to Speak So That People Want to Listen" (Julian Treasure)** — 3min，标准美音，适合入门
2. **B站双语字幕：英文脱口秀精选** — 日常口语，带中文对照
3. **YouTube: "English Pronunciation Guide"** — 44 音标完整讲解
4. **新概念英语 Lesson 1-10** — 经典教材，难度 L1-L2
5. **BBC Learning English: Business English** — 职场用语，难度 L3-L4

> ⚠️ **仅供个人学习使用** — EchoRead 提供的语料和生成内容仅用于学习者的自我提升。商业转载、二次发行、商业培训使用需获得原始内容方授权

## 常见功能详表

| 功能 | 说明 | 操作难度 |
|------|------|--------|
| 粘链接下载 | 支持 YouTube / TED / B站 / 网易公开课 | ⭐ 极简 |
| 逐句跟读 | 录音、对比、收藏错音 | ⭐⭐ 简单 |
| 听写练习（3 档难度） | 全文 / 填空 / 首字母 | ⭐⭐ 简单 |
| 生词本复习 | SRS 遗忘曲线算法 | ⭐⭐ 简单 |
| 44 音标训练 | 本地导入 / TTS 合成 | ⭐⭐ 简单 |
| 句法解析 | 拆解语法、短语 | ⭐⭐⭐ 中等 |
| 语料管理与诊断 | 合并、拆分、时间轴校准 | ⭐⭐⭐ 中等 |
| 环境体检 | 检查 FFmpeg、自动安装 | ⭐ 极简 |

## 参与贡献

欢迎提交 Issue、Pull Request 或建议！

- 📝 发现 Bug？[提交 Issue](https://github.com/)
- 🚀 想添加新功能？Fork 项目，创建 Feature 分支，提交 PR
- 💬 有想法？在 Discussions 里讨论

## 许可证

MIT License — 详见 [LICENSE](LICENSE) 文件

---

**快开始你的英语进阶之旅吧！** 🎉