# Gabriel

**面向 Slack Web 客户端协议的强类型、AI 友好的多语言实时 SDK。**

Gabriel 使用与 Slack 网页端相同的会话凭证（`d` 会话 Cookie，由它自动派生工作区令牌）建立连接。不需要安装 OAuth App，也不需要暴露公网 Webhook，就能为个人或团队内部的自动化提供实时事件流（消息、线程、表情、正在输入、上下线、自定义状态、勿扰、未读角标），以及对应的响应动作。SDK 带有安全默认值，方便把 Slack 动态接入 LLM / AI Agent 工作流。

[English](./README.md)

> [!IMPORTANT]
> Gabriel 是 [EzEasy](https://github.com/Easy-Ez) 的个人学习项目（非商业用途），也是**独立的非官方**项目，与 Slack 无隶属或背书关系，并以当前登录人的身份运行。请只在你有权自动化的账号与工作区中使用，遵守所在组织的政策和 Slack 的服务条款；能使用官方 Slack App（如 [Bolt](https://slack.dev/bolt-js)）时优先使用官方方案。会话 Cookie 的敏感程度等同于密码。

## SDK 列表

| 语言 | 包 | 状态 |
| :--- | :--- | :--- |
| TypeScript / Node.js | [`gabriel-slack`](./sdks/node) | **预览版 (0.1)**，参考实现 |
| Go | [`sdks/go`](./sdks/go) | 规划中 |
| Python | [`sdks/python`](./sdks/python) | 规划中 |

所有语言的 SDK 都实现 [`spec/`](./spec) 中同一份语言无关的契约：行为规范、全量事件 JSON Schema 和共享的一致性测试夹具。

## 快速上手（Node.js ≥ 20.10）

```typescript
import { GabrielClient } from "gabriel-slack";

const client = GabrielClient.fromCookie({
  cookieD: process.env.GABRIEL_COOKIE_D!, // 从密钥管理系统注入，切勿硬编码
  workspace: "your-workspace",
});

client.onMessage({ mentionsMe: true }, async (evt, ctx) => {
  const stream = await ctx.replyStream({ initialMarkdown: "_思考中…_" });
  for await (const chunk of yourLlm.stream(evt.toLLMContext(), { signal: stream.signal })) {
    stream.append(chunk);
  }
  await stream.finish();
});

await client.start();
```

完整说明见 [Node.js SDK 文档](./sdks/node/README.md)。本地开发：`pnpm install && pnpm test`；贡献规范见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

[MIT](./LICENSE) © 2026 EzEasy
