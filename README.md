# 大肥鱼QQ空间（astrbot_plugin_dafeiyu_qzone）

> **Fork / 基于**：[Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)  
> **修改**：Grok Bot (Ideas Implementation) for 大肥鱼  
> **作者**：Grok Bot (Ideas Implementation)  
> **仓库**：https://github.com/JiachengWu-5/astrbot_plugin_dafeiyu_qzone

基于 Zhalslar 开源 QQ 空间插件的大肥鱼定制版，在原有发说说 / 评论 / 表白墙能力上，增加智能配图、来源链接、题材权重反馈与混合触发的评论扫描。

## 大肥鱼定制功能

- **智能配图**：优先使用内容自身封面；封面失败时按标题搜相关图；有封面地址时不再塞无关本地梗图
- **来源可点链接**：B站视频 / 小红书笔记（有笔记 id 才带 explore 链接）/ 网易云歌曲与歌单
- **题材权重反馈**：空间评论精确 `0/1/2`（任何人）→ `0` +2 / `1` +0.5 / `2` -2，下限 0.1；打分本身不耗聊天 token
- **非评分自动回复**：普通评论走 LLM 自动回复
- **混合评论扫描**：平时廉价检查（约 2 分钟、不调 LLM），发现新评论才完整扫；私聊触发 / 「扫评论」立即扫；约 45 分钟兜底
- **网易云热门 / 推荐歌单**素材源
- **本地缓存**：默认 `D:\AstrBot\qzone_cache\`（可用环境变量或配置覆盖，见下）

## 安装

1. AstrBot 插件市场搜索「大肥鱼QQ空间」安装，或
2. 克隆到插件目录：

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/JiachengWu-5/astrbot_plugin_dafeiyu_qzone
```

3. 安装依赖后重载插件：`pip install -r requirements.txt`（按你的 AstrBot Python 环境执行）
4. 需要 **aiocqhttp**（NapCat / OneBot）平台

## 缓存目录（可配置）

默认写入 `D:\AstrBot\qzone_cache`（临时图、反馈数据），避免塞满系统盘。

覆盖方式（任选）：

1. 环境变量 `DAFEIYU_QZONE_CACHE`，例如 `E:\bots\qzone_cache`
2. 插件配置项 `cache_root`
3. 直接改 `core/cache_paths.py` 中的默认路径

临时下载图用完会清理；请保证该目录可写。**其他人不必使用 D 盘**，改成自己的可写路径即可。

## 配置要点

在 AstrBot 插件配置中可调：

- **LLM**：写说说 / 评论 / 回复评论的 provider 与提示词（强烈建议按 Bot 人设改写）
- **trigger**：`publish_cron` / `comment_cron` 与 offset；自动发说说配图开关等
- **source**：忽略群 / 用户、写说说消息长度
- **cookie_ttl / timeout**：登录与请求超时
- **cache_root**：本地缓存目录

评论反馈扫描为内置混合触发，一般无需额外 cron。

## 常用命令（继承自上游）

| 命令 | 权限 | 说明 |
|------|------|------|
| 看说说 | ALL | 查看说说，支持 `@用户`、序号、范围 `2~5` |
| 评说说 | ALL | 评论说说（可自动点赞） |
| 赞说说 | ALL | 点赞 |
| 发说说 | ADMIN | 手动发一条说说 |
| 写说说 | ADMIN | AI 生成草稿后发布 |
| 删说说 | ADMIN | 删除自己的说说 |
| 投墙 / 匿名投墙 | ALL | 表白墙投稿 |
| 扫评论 | ADMIN/主人 | 立即跑一轮评论反馈扫描（若已注册） |

## 上游能力摘要

仍保留原版：定时自动发说说与评论、表白墙审核、仪表盘页面、卡片渲染等。详见原仓库 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)。

## 平台与依赖

- 平台：`aiocqhttp`
- Python：建议 3.10+
- 依赖见 `requirements.txt`（Pillow、json5、apilmoji 等）

## 致谢

- 原作者与上游：[Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)
- [CampuxBot](https://github.com/idoknow/CampuxBot) 及相关 QQ 空间协议资料
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)

## 许可证

与上游一致，见 `LICENSE`（GPL-3.0）。
