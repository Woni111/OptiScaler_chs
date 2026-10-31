# 自动汉化与发布

工作流文件：`.github/workflows/chs-nightly-release.yml`

## 工作方式

工作流每两小时轮询以下两个上游仓库：

- `optiscaler/OptiScaler`：正式版 Release。
- `optiscaler/OptiScaler-nightly`：官方每日 Nightly Release。

自动模式会选择“发布日期更新且本地尚未汉化”的版本。Nightly 会从其 Release 说明中提取构建时的
上游源码提交；如果当天没有新提交，则使用发布时上游 `master` 的最新提交。编译前会固定到该 SHA，
因此不会把检测之后才出现的新代码混入旧版本。

本地 Release tag 与上游一一对应并追加 `-chs`：

- `nightly-20261030` -> `nightly-20261030-chs`
- `nightly-20261030-2` -> `nightly-20261030-2-chs`
- `v0.9.4` -> `v0.9.4-chs`

如果强制重发，会生成 `-r2`、`-r3` 等新 tag，绝不覆盖旧 Release。

## 手动触发

在 Actions 页面运行 `CHS Localized Release`：

- `channel=auto`：按发布日期自动选择尚未汉化的正式版或 Nightly。
- `channel=nightly` 或 `channel=stable`：指定发布通道。
- `upstream_tag`：可选，指定精确的上游 tag，例如 `nightly-20261030`。
- `force_republish=true`：对当前版本创建新的重构建 Release。

## 可选翻译密钥

新增界面字符串和文档自动翻译使用 DeepSeek。仓库没有 `DEEPSEEK_API_KEY` 时仍会完成构建和发布，
已有翻译继续生效，新字符串回退英文，Release 说明会标记翻译状态。

如需完整自动翻译，在仓库的 `Settings -> Secrets and variables -> Actions` 中添加：

```text
DEEPSEEK_API_KEY
```

## 首次启用

Fork 仓库的定时工作流默认可能未启用，需要：

1. 在 `Actions` 页面启用工作流。
2. 在 `Settings -> Actions -> General -> Workflow permissions` 选择
   `Read and write permissions`。

工作流只写入本 Fork，不会修改原汉化仓库或 OptiScaler 上游。
