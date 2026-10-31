# Tencent WorkBuddy 2API

> 腾讯 WorkBuddy / CodeBuddy 官方逆向 Cordis 插件，支持 OAuth 设备流授权、三段式 UA 指纹伪装与积分自动维护

## 插件元数据
- **包名**: `2api-plugin-workbuddy`
- **版本**: `1.0.0`
- **上游 BaseURL**: `https://copilot.tencent.com`
- **鉴权模式**: `oauth_device`

## 聚合支持的模型
- `glm-5.2` (上游映射: `glm-5.2`, 倍率: `1.00`)
- `glm-5.3` (上游映射: `glm-5.3`, 倍率: `1.50`)
- `deepseek-v3` (上游映射: `deepseek-v3`, 倍率: `1.00`)
- `deepseek-r1` (上游映射: `deepseek-r1`, 倍率: `2.00`)
- `deepseek-v4.1-flash` (上游映射: `deepseek-v4.1-flash`, 倍率: `0.50`)

## 安装与生效
本插件由 **2API Agent 编译器** 自动转译产出，在 `nachuan` 宿主中配置 `cordis.patch.yml` 即可实现零重启热加载。
