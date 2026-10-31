# Zhipu Qingyan (智谱清言) 2API

> 基于 HelloGML 逆向转译的智谱清言 Cordis 插件，支持 GLM-4/GLM-5 系列模型会话与多协议兼容转换

## 插件元数据
- **包名**: `2api-plugin-hellogml`
- **版本**: `1.0.0`
- **上游 BaseURL**: `https://chatglm.cn`
- **鉴权模式**: `bearer`

## 聚合支持的模型
- `glm-4` (上游映射: `glm-4`, 倍率: `1.00`)
- `glm-4.7` (上游映射: `glm-4.7`, 倍率: `1.20`)
- `glm-5` (上游映射: `glm-5`, 倍率: `1.50`)

## 安装与生效
本插件由 **2API Agent 编译器** 自动转译产出，在 `nachuan` 宿主中配置 `cordis.patch.yml` 即可实现零重启热加载。
