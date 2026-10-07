# Project conventions / 项目约定

适用于本仓库及后续开发。These conventions apply to this repository and future development.

## Repository identity / 仓库标识

- GitHub 仓库固定为 `Halo021501/agent-failure-lab`，仓库名称不带项目序号。The GitHub repository is `Halo021501/agent-failure-lab`, without a numeric project prefix.
- 本机目录 `03-agent-failure-lab` 是工作区编号，不必随仓库改名。The local `03-agent-failure-lab` directory is a workspace identifier and does not need renaming.

## Bilingual documentation / 双语文档

- `README.md` 为默认中文版，`README.en.md` 为完整英文版；两页标题下均保留互相切换的相对链接。Use `README.md` as the default Chinese version and `README.en.md` as the complete English version, with reciprocal relative language links below each title.
- 功能、安装、命令、配置、默认行为、指标、限制及验收状态更新时，在同一提交同步修改两版，确保含义和数字一致。Update both versions in the same commit whenever features, setup, commands, configuration, defaults, metrics, limitations, or verification status change; keep meaning and numbers aligned.
- GitHub description 与 `pyproject.toml` description 均采用“中文 / English”中英对照，描述实际已实现的范围。Keep both GitHub and `pyproject.toml` descriptions bilingual, in “中文 / English” format, and reflect the implemented scope.
- 更新仓库标识时同步维护 origin、README、交付文档和工作区索引链接。Keep origin, READMEs, delivery notes, and workspace index links aligned with repository identity.

## Execution preferences / 执行偏好

- 用户当前要求不做占用 GitHub 额度的测试：保持 GitHub Actions 关闭，不新增或触发云端测试/构建，提交使用 `[skip ci]`；只有用户明确更改要求后才启用。Keep GitHub Actions disabled, do not add or trigger cloud tests/builds, and use `[skip ci]` in commits until the user explicitly changes this preference.
- 真实模型测试按用户要求暂停；不得擅自发起付费模型请求。Real-model testing is deferred at the user's request; do not initiate paid model requests without updated authorization.
- 文档区分历史验证、当前检查和待完成验收；不能把脚本或模拟结果声明为真实模型能力。Distinguish historical verification, current checks, and pending acceptance; do not present scripted or simulated results as real-model capability.
