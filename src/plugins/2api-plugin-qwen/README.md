# qwen 2API (Experimental)

> 由 Python 极速实验探针验证并自动导出的 2API 插件规范

## 插件元数据
- **包名**: `2api-plugin-qwen`
- **版本**: `1.0.0`
- **上游 BaseURL**: `https://dashscope.aliyuncs.com`
- **鉴权模式**: `bearer`

## 聚合支持的模型
- `qwen-max` (上游映射: `qwen-max`, 倍率: `1.00`)
- `qwen-plus` (上游映射: `qwen-plus`, 倍率: `1.00`)
- `qwen-turbo` (上游映射: `qwen-turbo`, 倍率: `1.00`)
- `qwen-long` (上游映射: `qwen-long`, 倍率: `1.00`)

## 安装与生效
本插件由 **2API Agent 编译器** 自动转译产出，在 `nachuan` 宿主中配置 `cordis.patch.yml` 即可实现零重启热加载。
